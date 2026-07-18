from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.support.model_stream import (
    make_text_block_segment_event,
)

from pulsara_agent.event import (
    ContextCompactionCompletedEvent,
    ContextCompiledEvent,
    ContextWindowOpenedEvent,
    EventContext,
    RunStartEvent,
    TextBlockSegmentEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.primitives.capability import (
    build_capability_execution_surface_identity,
)
from pulsara_agent.primitives.context import (
    ContextCompileInputManifestFact,
    context_fingerprint,
)
from pulsara_agent.runtime import AgentRuntime
from pulsara_agent.runtime.context_input.live import (
    _advance_sparse_relevant_through_sequence,
    collect_live_context_inputs,
)
from pulsara_agent.runtime.context_input.snapshot import (
    build_context_snapshot,
)
from pulsara_agent.runtime.permission_snapshot import snapshot_from_mode
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventSlice,
    ContextEventSliceError,
    EventLogContextEventSliceReader,
    FrozenStoredEvent,
)
from pulsara_agent.runtime.context_input.snapshot import (
    finalize_context_authority_slice_plan,
)
from tests.support.model_call import compaction_completed_contract_fields
from tests.support.runtime_session import in_memory_runtime_session
from tests.test_agent_runtime_loop import (
    ScriptedTransport,
    make_llm_runtime,
    run_agent_task,
)


def _event(index: int) -> TextBlockSegmentEvent:
    ctx = EventContext(
        run_id="run:context-slice",
        turn_id="turn:context-slice",
        reply_id="reply:context-slice",
    )
    return make_text_block_segment_event(
        id=f"event:{index}",
        **ctx.event_fields(),
        block_id="block:1",
        delta=f"delta:{index}",
        metadata={"nested": {"items": [index]}},
    )


def test_child_sparse_authority_cursor_accepts_an_empty_delta() -> None:
    assert _advance_sparse_relevant_through_sequence(17, ()) == 17


def test_in_memory_event_log_owns_appended_payload_and_returns_fresh_copies() -> None:
    log = InMemoryEventLog()
    candidate = _event(1)
    stored = log.append(candidate)
    candidate.metadata["nested"]["items"].append(99)
    stored.metadata["nested"]["items"].append(88)

    first = log.iter()[0]
    assert first.metadata["nested"]["items"] == [1]
    first.metadata["nested"]["items"].append(77)
    assert log.get_by_id("event:1").metadata["nested"]["items"] == [1]


def test_context_event_slice_is_canonical_and_projectors_get_owned_events() -> None:
    log = InMemoryEventLog()
    log.extend((_event(1), _event(2)))
    reader = EventLogContextEventSliceReader(
        event_log=log,
        runtime_session_id="runtime:context-slice",
    )
    event_slice = asyncio.run(
        reader.read_through_current_high_water(
            runtime_session_id="runtime:context-slice",
            minimum_sequence=1,
        )
    )
    assert event_slice.from_sequence == 1
    assert event_slice.through_sequence == 2
    assert tuple(item.sequence for item in event_slice.events) == (1, 2)

    first_projection = event_slice.events[0].decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    second_projection = event_slice.events[0].decode_owned(
        DEFAULT_EVENT_SCHEMA_REGISTRY
    )
    first_projection.metadata["nested"]["items"].append(99)
    assert second_projection.metadata["nested"]["items"] == [1]
    assert event_slice.events[0].decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY).metadata[
        "nested"
    ]["items"] == [1]


def test_context_event_slice_delta_extension_preserves_canonical_fingerprints() -> None:
    log = InMemoryEventLog()
    log.extend((_event(1), _event(2)))
    first = log.read_raw_range_snapshot(
        minimum_sequence=1,
        through_sequence=1,
        max_events=2,
        max_payload_bytes=100_000,
    )
    event_slice = ContextEventSlice.from_read_snapshot(
        runtime_session_id="runtime:context-slice",
        minimum_sequence=1,
        snapshot=first,
    )
    delta = log.read_raw_range_snapshot(
        minimum_sequence=2,
        through_sequence=2,
        max_events=2,
        max_payload_bytes=100_000,
    )
    extended = event_slice.extend_snapshot(delta)

    ids = tuple(event.event_id for event in extended.events)
    payloads = tuple(event.payload_fingerprint for event in extended.events)
    assert ids == ("event:1", "event:2")
    assert extended.event_ids_fingerprint == context_fingerprint(
        "context-event-slice-ids:v1", ids
    )
    assert extended.event_payloads_fingerprint == context_fingerprint(
        "context-event-slice-payloads:v1", payloads
    )
    assert len(extended.events.chunks) == 2
    assert extended.payload_byte_count == sum(
        len(event.canonical_payload_bytes) for event in extended.events
    )


def test_explicit_context_high_water_excludes_later_append() -> None:
    log = InMemoryEventLog()
    log.append(_event(1))
    reader = EventLogContextEventSliceReader(
        event_log=log,
        runtime_session_id="runtime:context-slice",
    )
    log.append(_event(2))
    event_slice = asyncio.run(
        reader.read_through(
            runtime_session_id="runtime:context-slice",
            through_sequence=1,
        )
    )
    assert tuple(item.event_id for item in event_slice.events) == ("event:1",)


def test_context_event_slice_rejects_sequence_gap() -> None:
    first = FrozenStoredEvent.from_stored_event(
        _event(1).model_copy(update={"sequence": 1})
    )
    third = FrozenStoredEvent.from_stored_event(
        _event(3).model_copy(update={"sequence": 3})
    )
    with pytest.raises(ContextEventSliceError, match="contiguous"):
        ContextEventSlice(
            runtime_session_id="runtime:context-slice",
            from_sequence=1,
            through_sequence=3,
            events=(first, third),
            event_ids_fingerprint="invalid",
            event_payloads_fingerprint="invalid",
        )


def test_frozen_stored_event_rejects_wrapper_payload_split_brain() -> None:
    frozen = FrozenStoredEvent.from_stored_event(
        _event(1).model_copy(update={"sequence": 1})
    )
    corrupt = object.__new__(FrozenStoredEvent)
    for field_name in (
        "event_id",
        "event_type",
        "sequence",
        "created_at_utc",
        "canonical_payload_bytes",
        "payload_fingerprint",
    ):
        object.__setattr__(corrupt, field_name, getattr(frozen, field_name))
    object.__setattr__(corrupt, "sequence", 2)
    with pytest.raises(ContextEventSliceError, match="wrapper identity"):
        corrupt.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)


def test_context_slice_read_is_blocked_by_structural_latch() -> None:
    log = InMemoryEventLog()
    log.append(_event(1))
    reader = EventLogContextEventSliceReader(
        event_log=log,
        runtime_session_id="runtime:context-slice",
        reconciliation_required=lambda: True,
    )
    with pytest.raises(ContextEventSliceError, match="reconciliation"):
        asyncio.run(
            reader.read_through_current_high_water(
                runtime_session_id="runtime:context-slice",
                minimum_sequence=1,
            )
        )


def _run_start(ctx: EventContext) -> tuple[RunStartEvent, ContextWindowOpenedEvent]:
    from tests.conftest import run_start_permission_fields
    from pulsara_agent.runtime.long_horizon.run_contract import (
        empty_projection_state_fingerprint,
        prepare_root_long_horizon_run,
    )

    fields = run_start_permission_fields(
        ctx.run_id,
        user_input="current request",
        turn_id=ctx.turn_id,
        reply_id=ctx.reply_id,
        mcp_installation_owner_runtime_session_id="runtime:context-slice",
    )
    run_start_id = f"run_start:test:{ctx.run_id}"
    prepared = prepare_root_long_horizon_run(
        runtime_session_id="runtime:context-slice",
        run_id=ctx.run_id,
        run_start_event_id=run_start_id,
        primary_target=fields["model_target"],
        summarizer_target=fields["model_target"],
        graph_reducer_contract=fields["subagent_graph_reducer_contract"],
        source_through_sequence_at_open=0,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    fields["long_horizon"] = prepared.contract
    run_start = RunStartEvent(
        id=run_start_id,
        **ctx.event_fields(),
        **fields,
        user_input_chars=len("current request"),
    )
    window_open = ContextWindowOpenedEvent(
        id=prepared.contract.initial_window_open_event_id,
        **ctx.event_fields(),
        window=prepared.initial_window,
        opening_batch_id=prepared.opening_batch_id,
    )
    return run_start, window_open


def _compaction(ctx: EventContext, *, label: str) -> ContextCompactionCompletedEvent:
    return ContextCompactionCompletedEvent(
        **ctx.event_fields(),
        **compaction_completed_contract_fields(),
        compaction_id=f"compaction:{label}",
        trigger="auto",
        reason="context pressure",
        window_number=1,
        window_id=f"window:{label}",
        summary_artifact_id=f"artifact:summary:{label}",
        summary_chars=7,
        threshold_tokens=100,
        through_sequence=1,
        keep_after_sequence=0,
    )


def test_preflight_compaction_window_is_independent_from_authority_slice() -> None:
    ctx = EventContext(
        run_id="run:preflight-window",
        turn_id="turn:preflight-window",
        reply_id="reply:preflight-window",
    )
    log = InMemoryEventLog()
    history = log.append(_event(1))
    compacted = log.append(_compaction(ctx, label="preflight"))
    run_start, window_open = _run_start(ctx)
    started, _ = log.extend((run_start, window_open))
    event_slice = ContextEventSlice.from_read_snapshot(
        runtime_session_id="runtime:context-slice",
        minimum_sequence=1,
        snapshot=log.read_raw_range_snapshot(minimum_sequence=1),
    )
    plan = finalize_context_authority_slice_plan(
        event_slice=event_slice,
        required_local_event_refs=(
            FrozenStoredEvent.from_stored_event(started).to_reference(
                "runtime:context-slice"
            ),
        ),
        run_start_ref=FrozenStoredEvent.from_stored_event(started).to_reference(
            "runtime:context-slice"
        ),
        latest_compaction_terminal_ref=(
            FrozenStoredEvent.from_stored_event(compacted).to_reference(
                "runtime:context-slice"
            )
        ),
        prior_transcript_through_sequence=history.sequence,
    )
    assert plan.authority_from_sequence == history.sequence
    assert plan.transcript_window.window_kind == "preflight_compaction"
    assert plan.transcript_window.protected_run_start_sequence == started.sequence


def test_mid_turn_compaction_keeps_current_run_in_protected_window() -> None:
    ctx = EventContext(
        run_id="run:mid-window",
        turn_id="turn:mid-window",
        reply_id="reply:mid-window",
    )
    log = InMemoryEventLog()
    history = log.append(_event(1))
    run_start, window_open = _run_start(ctx)
    started, _ = log.extend((run_start, window_open))
    current = log.append(
        make_text_block_segment_event(
            **ctx.event_fields(), block_id="current", delta="current run"
        )
    )
    compacted = log.append(_compaction(ctx, label="mid"))
    event_slice = ContextEventSlice.from_read_snapshot(
        runtime_session_id="runtime:context-slice",
        minimum_sequence=1,
        snapshot=log.read_raw_range_snapshot(minimum_sequence=1),
    )
    start_ref = FrozenStoredEvent.from_stored_event(started).to_reference(
        "runtime:context-slice"
    )
    plan = finalize_context_authority_slice_plan(
        event_slice=event_slice,
        required_local_event_refs=(start_ref,),
        run_start_ref=start_ref,
        latest_compaction_terminal_ref=(
            FrozenStoredEvent.from_stored_event(compacted).to_reference(
                "runtime:context-slice"
            )
        ),
        prior_transcript_through_sequence=history.sequence,
    )
    assert plan.transcript_window.window_kind == "mid_turn_compaction"
    assert plan.transcript_window.protected_run_start_sequence == started.sequence
    assert plan.transcript_window.protected_run_through_sequence == compacted.sequence
    assert current.sequence >= plan.transcript_window.protected_run_start_sequence


async def _captured_live_collect_args(tmp_path, monkeypatch):
    import pulsara_agent.runtime.context_input.live as live_module

    captured = []
    original = live_module.collect_live_context_inputs

    def capture(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(live_module, "collect_live_context_inputs", capture)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
    )
    await run_agent_task(agent, "snapshot joins")
    return captured[0], agent.runtime_session


def test_live_snapshot_permission_join_fails_closed(tmp_path, monkeypatch) -> None:
    kwargs, _runtime_session = asyncio.run(
        _captured_live_collect_args(tmp_path, monkeypatch)
    )
    working_set = kwargs["working_set"]
    wrong_permission = snapshot_from_mode(
        runtime_session_id=working_set.permission_snapshot.runtime_session_id,
        run_id=working_set.permission_snapshot.run_id,
        permission_mode=PermissionMode.READ_ONLY,
        permission_snapshot_source="session_default",
    )
    with pytest.raises(ContextEventSliceError, match="permission contract"):
        collect_live_context_inputs(
            **{
                **kwargs,
                "working_set": replace(
                    working_set, permission_snapshot=wrong_permission
                ),
            }
        )


def test_live_snapshot_mcp_surface_join_fails_closed(tmp_path, monkeypatch) -> None:
    kwargs, _runtime_session = asyncio.run(
        _captured_live_collect_args(tmp_path, monkeypatch)
    )
    working_set = kwargs["working_set"]
    old_surface = working_set.frozen_execution_surface
    changed_identity = build_capability_execution_surface_identity(
        surface_contract_version=old_surface.identity.surface_contract_version,
        entries=old_surface.identity.entries,
        mcp_installation_id="mcp_installation:drifted",
    )
    changed_surface = replace(old_surface, identity=changed_identity)
    with pytest.raises(ContextEventSliceError, match="frozen execution surface"):
        collect_live_context_inputs(
            **{
                **kwargs,
                "working_set": replace(
                    working_set, frozen_execution_surface=changed_surface
                ),
            }
        )


def test_live_snapshot_exposure_join_fails_closed(tmp_path, monkeypatch) -> None:
    kwargs, _runtime_session = asyncio.run(
        _captured_live_collect_args(tmp_path, monkeypatch)
    )
    working_set = kwargs["working_set"]
    assert working_set.effective_exposure_fact is not None
    drifted = working_set.effective_exposure_fact.model_copy(
        update={"exposure_id": "capability-exposure:drifted"}
    )
    with pytest.raises(ContextEventSliceError, match="differs from ledger"):
        collect_live_context_inputs(
            **{
                **kwargs,
                "working_set": replace(working_set, effective_exposure_fact=drifted),
            }
        )


def test_raw_suspended_token_is_not_a_snapshot_build_field() -> None:
    from pulsara_agent.runtime.context_input.snapshot import ContextSnapshotBuildInput

    assert "raw_suspended_state_token_for_validation" not in (
        ContextSnapshotBuildInput.model_fields
    )
    assert "suspended_state_token" not in ContextSnapshotBuildInput.model_fields


def test_manifest_semantic_fingerprint_excludes_checkpoint_acceleration(
    tmp_path, monkeypatch
) -> None:
    kwargs, runtime_session = asyncio.run(
        _captured_live_collect_args(tmp_path, monkeypatch)
    )
    original_input = collect_live_context_inputs(**kwargs)
    acceleration_payload = original_input.subagent_graph_acceleration.model_dump(
        mode="python", exclude={"acceleration_fingerprint"}
    )
    acceleration_payload["checkpoint_id"] = "subagent-checkpoint:alternate"
    changed_acceleration = original_input.subagent_graph_acceleration.__class__(
        **acceleration_payload,
        acceleration_fingerprint=context_fingerprint(
            "subagent-graph-acceleration:v1", acceleration_payload
        ),
    )
    changed_input = original_input.model_copy(
        update={"subagent_graph_acceleration": changed_acceleration}
    )
    compiled = next(
        event
        for event in reversed(runtime_session.event_log.iter())
        if isinstance(event, ContextCompiledEvent) and event.status == "compiled"
    )
    assert compiled.input_audit is not None
    manifest = ContextCompileInputManifestFact.model_validate_json(
        runtime_session.archive.get_text(
            compiled.input_audit.input_manifest_artifact_id,
            session_id=runtime_session.runtime_session_id,
        )
    )
    attribution = manifest.snapshot.long_horizon_attribution

    original = build_context_snapshot(
        original_input,
        long_horizon_attribution=attribution,
    )
    changed = build_context_snapshot(
        changed_input,
        long_horizon_attribution=attribution,
    )

    assert original.snapshot_semantic_fingerprint == (
        changed.snapshot_semantic_fingerprint
    )
    assert original.snapshot_fact_fingerprint != changed.snapshot_fact_fingerprint
