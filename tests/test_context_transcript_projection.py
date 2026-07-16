from __future__ import annotations

from types import SimpleNamespace

import pytest

from pulsara_agent.event import (
    ContextWindowOpenedEvent,
    EventContext,
    ModelCallControlDispositionResolvedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.message import (
    ToolResultArtifactRef,
    ToolResultPreviewMetadata,
    ToolResultState,
)
from pulsara_agent.primitives.context import (
    TranscriptCompileInput,
    TranscriptMessageFact,
    TranscriptToolResultRefFact,
    ToolArgumentsParseErrorCode,
    context_fingerprint,
)
from pulsara_agent.primitives.model_call import (
    ModelCallControlDisposition,
    sha256_fingerprint,
)
from pulsara_agent.primitives.tool_result import (
    ToolResultBodyPolicy,
    ToolResultEnvelopePolicy,
    ToolResultPayloadFormat,
    ToolResultRenderCacheHint,
    ToolResultRenderReasonCode,
    ToolResultStateFact,
)
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.runtime.context_input.compiler import lower_transcript_for_context
from pulsara_agent.runtime.context_input.event_slice import ContextEventSlice
from pulsara_agent.runtime.context_input.policy import resolve_context_compile_policy
from pulsara_agent.runtime.context_input.render import (
    InMemoryToolResultRenderCache,
    prepare_tool_result_render_input,
    render_prepared_tool_result_units,
)
from pulsara_agent.runtime.context_input.snapshot import (
    finalize_context_authority_slice_plan,
)
from pulsara_agent.runtime.context_input.transcript import (
    project_context_transcript,
)
from pulsara_agent.runtime.state import LoopBudget
from tests.conftest import (
    run_start_permission_fields,
    tool_result_end_contract_fields,
)
from tests.support.model_call import (
    model_call_end_fields,
    model_call_start_fields,
    model_terminal_projection_end_reference_fixture,
)


def _projection_fixture(
    *,
    raw_arguments_json: str,
    result_text: str = "result body",
    tool_name: str = "lookup",
    result_state: ToolResultState = ToolResultState.ERROR,
    artifacts: tuple[ToolResultArtifactRef, ...] = (),
    model_outcome: str = "completed",
    control_disposition: ModelCallControlDisposition | None = (
        ModelCallControlDisposition.ACCEPTED
    ),
    include_tool_result: bool = True,
):
    ctx = EventContext(
        run_id="run:transcript-projection",
        turn_id="turn:transcript-projection",
        reply_id="reply:transcript-projection",
    )
    run_start = RunStartEvent(
        id="run_start:test:run:transcript-projection",
        **ctx.event_fields(),
        **run_start_permission_fields(
            ctx.run_id,
            user_input="inspect the result",
            turn_id=ctx.turn_id,
            reply_id=ctx.reply_id,
        ),
        user_input_chars=len("inspect the result"),
    )
    from pulsara_agent.runtime.long_horizon.run_contract import (
        empty_projection_state_fingerprint,
        prepare_root_long_horizon_run,
    )

    prepared_long_horizon = prepare_root_long_horizon_run(
        runtime_session_id="runtime:test",
        run_id=ctx.run_id,
        run_start_event_id=run_start.id,
        primary_target=run_start.model_target,
        summarizer_target=run_start.model_target,
        graph_reducer_contract=run_start.subagent_graph_reducer_contract,
        source_through_sequence_at_open=0,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    run_start = run_start.model_copy(
        update={"long_horizon": prepared_long_horizon.contract},
        deep=True,
    )
    window_open = ContextWindowOpenedEvent(
        id=prepared_long_horizon.contract.initial_window_open_event_id,
        **ctx.event_fields(),
        window=prepared_long_horizon.initial_window,
        opening_batch_id=prepared_long_horizon.opening_batch_id,
    )
    model_start = ModelCallStartEvent(
        **ctx.event_fields(),
        **model_call_start_fields(),
    )
    model_end_payload = model_call_end_fields(resolved_call=model_start.resolved_call)
    model_end_payload["outcome"] = model_outcome
    model_end_payload["terminal_projection"] = (
        model_terminal_projection_end_reference_fixture(
            model_start.resolved_call.resolved_model_call_id,
            outcome=model_outcome,
        )
    )
    model_end = ModelCallEndEvent(
        id=model_start.recovery_plan.stable_model_call_end_event_id,
        **ctx.event_fields(),
        **model_end_payload,
    )
    disposition_fields = {
        "id": (
            "model_call_control_disposition:run:transcript-projection:"
            f"{model_start.resolved_call.resolved_model_call_id}:1"
        ),
        **ctx.event_fields(),
        "resolved_model_call_id": (
            model_start.resolved_call.resolved_model_call_id
        ),
        "model_call_start_event_id": model_start.id,
        "model_call_end_event_id": model_end.id,
        "model_call_index": 1,
        "source_result_fingerprint": "sha256:" + "e" * 64,
        "run_execution_activation": (
            model_start.recovery_plan.run_execution_activation
        ),
        "disposition": control_disposition,
        "termination_intent": None,
        "recovery_reason_code": (
            "process_restarted_before_control_resolution"
            if control_disposition
            is ModelCallControlDisposition.SUPPRESSED_BY_RECOVERY
            else None
        ),
    }
    disposition = None
    if control_disposition is not None:
        disposition_candidate = (
            ModelCallControlDispositionResolvedEvent.model_construct(
                **disposition_fields,
                event_fingerprint="pending",
            )
        )
        disposition_payload = disposition_candidate.model_dump(
            mode="json", exclude={"event_fingerprint", "sequence"}
        )
        disposition = ModelCallControlDispositionResolvedEvent(
            **disposition_payload,
            event_fingerprint=sha256_fingerprint(
                "model-call-control-disposition-event:v1", disposition_payload
            ),
        )
    log = InMemoryEventLog()
    events = [
        run_start,
        window_open,
        ReplyStartEvent(
                id=model_start.recovery_plan.reply_start_event_id,
                **ctx.event_fields(),
                name="assistant",
            ),
        model_start,
        ToolCallStartEvent(
                **ctx.event_fields(),
                tool_call_id="call:projection",
                tool_call_name=tool_name,
            ),
        ToolCallDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:projection",
                delta=raw_arguments_json,
            ),
        ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:projection"),
        model_end,
        ReplyEndEvent(
                id=model_start.recovery_plan.stable_reply_end_event_id,
                **ctx.event_fields(),
                model_terminal_outcome=model_outcome,
            ),
    ]
    if disposition is not None:
        events.append(disposition)
    if include_tool_result:
        events.extend(
            (
                ToolResultStartEvent(
                **ctx.event_fields(),
                tool_call_id="call:projection",
                tool_call_name=tool_name,
            ),
                ToolResultTextDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:projection",
                delta=result_text,
            ),
                ToolResultEndEvent(
                **ctx.event_fields(),
                **tool_result_end_contract_fields(
                    "call:projection",
                    tool_name=tool_name,
                    state=result_state,
                ),
                tool_call_id="call:projection",
                state=result_state,
                artifacts=list(artifacts),
            ),
            )
        )
    stored = log.extend(tuple(events))
    read = log.read_raw_range_snapshot(minimum_sequence=1)
    event_slice = ContextEventSlice.from_read_snapshot(
        runtime_session_id="runtime:test",
        minimum_sequence=1,
        snapshot=read,
    )
    start_ref = event_slice.events[0].to_reference("runtime:test")
    authority = finalize_context_authority_slice_plan(
        event_slice=event_slice,
        required_local_event_refs=(start_ref,),
        run_start_ref=start_ref,
        latest_compaction_terminal_ref=None,
    )
    snapshot = SimpleNamespace(
        identity=SimpleNamespace(runtime_session_id="runtime:test"),
        run_entry=SimpleNamespace(run_start=start_ref),
        current_user_message=stored[0].current_user_message,
        authority_slice_plan=authority,
        primary_event_range=event_slice.to_range_fact(),
    )
    return snapshot, event_slice


def _lowered_messages(normalized, rendered):
    return lower_transcript_for_context(
        transcript=normalized.transcript,
        rendered_tool_results=rendered,
        prepared_rollups=(),
    )


def test_transcript_projector_preserves_malformed_arguments_and_pairing() -> None:
    raw = '{"query": '
    snapshot, event_slice = _projection_fixture(raw_arguments_json=raw)

    normalized = project_context_transcript(
        snapshot=snapshot,
        event_slice=event_slice,
    )

    transcript = normalized.transcript
    call = next(
        block
        for message in transcript.messages
        for block in message.blocks
        if getattr(block, "kind", None) == "tool_call"
    )
    assert call.raw_arguments_json == raw
    assert call.arguments_status == "invalid_json"
    assert call.parse_error_code is ToolArgumentsParseErrorCode.INVALID_JSON_SYNTAX
    assert transcript.current_user_anchor == "user-message:run:transcript-projection"
    assert len(transcript.tool_pairs) == 1
    assert transcript.tool_pairs[0].pairing_status == "completed"
    assert len(normalized.tool_result_units) == 1
    unit = normalized.tool_result_units[0]
    assert unit.tool_call_id == "call:projection"
    assert unit.result_state is ToolResultStateFact.ERROR
    assert unit.content.text_blocks[0].text == "result body"
    assert unit.call_position < unit.result_position


@pytest.mark.parametrize(
    ("model_outcome", "control_disposition"),
    (
        ("provider_error", None),
        ("completed", ModelCallControlDisposition.SUPPRESSED_BY_RECOVERY),
    ),
)
def test_non_accepted_closed_tool_call_is_audit_only(
    model_outcome: str,
    control_disposition: ModelCallControlDisposition | None,
) -> None:
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json='{"query":"never execute"}',
        model_outcome=model_outcome,
        control_disposition=control_disposition,
        include_tool_result=False,
    )

    normalized = project_context_transcript(
        snapshot=snapshot,
        event_slice=event_slice,
    )

    assert [message.role for message in normalized.transcript.messages] == ["user"]
    assert normalized.transcript.tool_pairs == ()
    assert normalized.tool_result_units == ()


def test_transcript_projector_is_deterministic_and_returns_immutable_facts() -> None:
    snapshot, event_slice = _projection_fixture(raw_arguments_json='{"query":"x"}')

    first = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    second = project_context_transcript(snapshot=snapshot, event_slice=event_slice)

    assert first == second
    assert first.transcript.transcript_fingerprint == (
        second.transcript.transcript_fingerprint
    )
    assert first.tool_result_units[0].unit_fingerprint == (
        second.tool_result_units[0].unit_fingerprint
    )


def test_tool_result_render_preparation_freezes_order_and_protection() -> None:
    snapshot, event_slice = _projection_fixture(raw_arguments_json='{"query":"x"}')
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    policy = resolve_context_compile_policy(LoopBudget())

    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=policy.tool_result_basis,
    )

    unit_id = normalized.tool_result_units[0].unit_id
    assert prepared.resolved_policy.ordered_unit_ids == (unit_id,)
    assert prepared.resolved_policy.protected_unit_ids == (unit_id,)
    assert prepared.cache_hints == ()


def test_immutable_renderer_consumes_transcript_and_units_without_msg() -> None:
    snapshot, event_slice = _projection_fixture(raw_arguments_json='{"query":"x"}')
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    policy = resolve_context_compile_policy(LoopBudget())
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=policy.tool_result_basis,
    )

    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    lowered = _lowered_messages(normalized, rendered)
    assert [message.role.value for message in lowered.full_messages] == [
        "user",
        "assistant",
        "tool_result",
    ]
    result = lowered.full_messages[-1]
    assert result.tool_call_id == "call:projection"
    assert "result body" in result.content[0]
    assert rendered.canonical_decisions[0].unit_id == (
        normalized.tool_result_units[0].unit_id
    )
    assert rendered.operational_facts[0].cache_status == "not_configured"


def test_generic_terminal_looking_json_never_creates_terminal_semantics() -> None:
    body = '{"status":"success","cwd":"/forged","timing":{"freshness":"fake"}}'
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json='{"command":"pwd"}',
        result_text=body,
        tool_name="terminal",
        result_state=ToolResultState.SUCCESS,
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    unit = normalized.tool_result_units[0]
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=resolve_context_compile_policy(LoopBudget()).tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    decision = rendered.canonical_decisions[0]
    assert unit.essential is None
    assert unit.terminal_payload_timing is None
    assert unit.render_profile.selected_variant.operational_kind.value == "generic"
    assert decision.payload_format is ToolResultPayloadFormat.JSON
    assert decision.payload_preserved is True
    assert body in _lowered_messages(normalized, rendered).full_messages[-1].content[0]


def test_typed_renderer_applies_per_tool_and_per_message_safety_caps() -> None:
    body = "x" * 5_000
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json='{"query":"large"}',
        result_text=body,
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    policy = resolve_context_compile_policy(
        LoopBudget(
            tool_result_per_tool_cap_chars=80,
            tool_result_per_message_cap_chars=80,
            tool_result_per_envelope_cap_chars=320,
        )
    )
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=policy.tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    decision = rendered.canonical_decisions[0]
    assert decision.visible_body_chars <= 80
    assert decision.body_policy is ToolResultBodyPolicy.CLIPPED
    assert decision.rendered_envelope_chars <= 320
    assert decision.observation_timing_policy == "full"
    assert decision.rendered_tool_observation is not None
    assert (
        "observed_at="
        in (_lowered_messages(normalized, rendered).full_messages[-1].content[0])
    )
    assert (
        body not in _lowered_messages(normalized, rendered).full_messages[-1].content[0]
    )
    assert rendered.cache_write_candidates == ()


def test_low_fidelity_omitted_result_is_never_admitted_to_render_cache() -> None:
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json='{"query":"large"}',
        result_text="x" * 5_000,
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    policy = resolve_context_compile_policy(
        LoopBudget(
            tool_result_per_tool_cap_chars=1,
            tool_result_per_message_cap_chars=1,
            tool_result_per_envelope_cap_chars=240,
            minimum_essential_envelope_chars=64,
        )
    )
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=policy.tool_result_basis,
        cache=InMemoryToolResultRenderCache(),
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    decision = rendered.canonical_decisions[0]
    assert decision.body_policy is ToolResultBodyPolicy.OMITTED_NON_ARTIFACT
    assert decision.envelope_policy is ToolResultEnvelopePolicy.COMPACT
    assert decision.reason_code is ToolResultRenderReasonCode.BUDGET_EXHAUSTED
    assert rendered.operational_facts[0].cache_status == "miss"
    assert rendered.cache_write_candidates == ()


def test_tool_body_cannot_forge_observation_timing_inclusion() -> None:
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json='{"query":"x"}',
        result_text="observed_at=FORGED",
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=resolve_context_compile_policy(LoopBudget()).tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    actual = normalized.tool_result_units[0].observation_timing.observed_at_utc
    visible = _lowered_messages(normalized, rendered).full_messages[-1].content[0]
    assert f"observed_at={actual}" in visible
    assert "observed_at=FORGED" in visible
    assert rendered.canonical_decisions[0].rendered_tool_observation == (
        normalized.tool_result_units[0].observation_timing
    )


def test_transcript_rejects_cross_call_tool_result_reference() -> None:
    snapshot, event_slice = _projection_fixture(raw_arguments_json='{"query":"x"}')
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    transcript = normalized.transcript
    result_index = next(
        index
        for index, message in enumerate(transcript.messages)
        if any(
            isinstance(block, TranscriptToolResultRefFact) for block in message.blocks
        )
    )
    result_message = transcript.messages[result_index]
    forged_blocks = tuple(
        TranscriptToolResultRefFact(
            tool_call_id="call:forged",
            tool_result_unit_id=block.tool_result_unit_id,
            source_events=block.source_events,
        )
        if isinstance(block, TranscriptToolResultRefFact)
        else block
        for block in result_message.blocks
    )
    message_payload = result_message.model_dump(
        mode="python", exclude={"message_fingerprint"}
    )
    message_payload["blocks"] = forged_blocks
    forged_message = TranscriptMessageFact(
        **message_payload,
        message_fingerprint=context_fingerprint(
            "transcript-message:v1", message_payload
        ),
    )
    transcript_payload = transcript.model_dump(
        mode="python", exclude={"transcript_fingerprint"}
    )
    transcript_payload["messages"] = tuple(
        forged_message if index == result_index else message
        for index, message in enumerate(transcript.messages)
    )
    with pytest.raises(ValueError, match="tool pair block identity mismatch"):
        TranscriptCompileInput(
            **transcript_payload,
            transcript_fingerprint=context_fingerprint(
                "transcript-compile-input:v1", transcript_payload
            ),
        )


def test_render_cache_read_failure_is_operational_and_non_blocking() -> None:
    class FailingCache:
        def get(self, _cache_key):
            raise RuntimeError("cache unavailable")

    snapshot, event_slice = _projection_fixture(raw_arguments_json='{"query":"x"}')
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=resolve_context_compile_policy(LoopBudget()).tool_result_basis,
        cache=FailingCache(),
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    assert prepared.cache_read_failed_unit_ids == (
        normalized.tool_result_units[0].unit_id,
    )
    assert rendered.operational_facts[0].cache_status == "miss"
    assert rendered.operational_facts[0].diagnostics[0].code.value == (
        "cache_read_failed"
    )


def test_render_cache_is_only_an_immutable_validated_hint() -> None:
    snapshot, event_slice = _projection_fixture(raw_arguments_json='{"query":"x"}')
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    basis = resolve_context_compile_policy(LoopBudget()).tool_result_basis
    cache = InMemoryToolResultRenderCache()
    first_prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=basis,
        cache=cache,
    )
    first = render_prepared_tool_result_units(
        prepared=first_prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )
    first_lowered = _lowered_messages(normalized, first)
    rendered_text = first_lowered.full_messages[-1].content[0]
    assert first.operational_facts[0].cache_status == "miss"
    assert len(first.cache_write_candidates) == 1
    write = first.cache_write_candidates[0]
    cache.put(write.cache_key, write.hint)
    hit_prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=basis,
        cache=cache,
    )
    hit = render_prepared_tool_result_units(
        prepared=hit_prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    assert (
        _lowered_messages(normalized, hit).full_messages == first_lowered.full_messages
    )
    assert hit.operational_facts[0].cache_status == "hit"

    wrong_text = rendered_text + " forged"
    hint_payload = write.hint.model_dump(
        mode="python",
        exclude={"hint_fingerprint"},
    )
    wrong_payload = {
        **hint_payload,
        "rendered_text": wrong_text,
        "rendered_text_fingerprint": context_fingerprint(
            "tool-result-rendered-text:v1", wrong_text
        ),
    }
    wrong_hint = ToolResultRenderCacheHint(
        **wrong_payload,
        hint_fingerprint=context_fingerprint(
            "tool-result-render-cache-hint:v1", wrong_payload
        ),
    )
    wrong_cache = InMemoryToolResultRenderCache()
    wrong_cache.put(write.cache_key, wrong_hint)
    invalidated_prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=basis,
        cache=wrong_cache,
    )
    invalidated = render_prepared_tool_result_units(
        prepared=invalidated_prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )
    assert invalidated.operational_facts[0].cache_status == "invalidated"
    assert (
        _lowered_messages(normalized, invalidated).full_messages
        == first_lowered.full_messages
    )


def test_artifact_read_more_is_normalized_before_decision_fingerprint() -> None:
    artifact = ToolResultArtifactRef(
        artifact_id="artifact:large-output",
        role="combined_output",
        media_type="text/plain; charset=utf-8",
        size_bytes=50_000,
        preview=ToolResultPreviewMetadata(
            preview_policy="head_tail",
            preview_chars=120,
            original_chars=50_000,
            original_bytes=50_000,
            omitted_middle_chars=49_880,
            visible_head_chars=80,
            visible_tail_chars=40,
            read_more={
                "tool": "artifact_read",
                "artifact_id": "artifact:large-output",
                "offset_chars": 80,
            },
        ),
    )
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json='{"command":"large"}',
        result_text="preview body",
        artifacts=(artifact,),
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=resolve_context_compile_policy(LoopBudget()).tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    decision = rendered.canonical_decisions[0]
    assert decision.primary_artifact_id == "artifact:large-output"
    assert decision.read_more is not None
    assert decision.decision_fingerprint.startswith("sha256:")


@pytest.mark.parametrize(
    ("envelope_cap", "expected_policy"),
    (
        (800, ToolResultEnvelopePolicy.COMPACT),
        (500, ToolResultEnvelopePolicy.MINIMAL),
    ),
)
def test_compact_artifact_envelope_preserves_primary_text_artifact_identity(
    envelope_cap,
    expected_policy,
) -> None:
    image = ToolResultArtifactRef(
        artifact_id="artifact:image",
        role="image",
        media_type="image/png",
        size_bytes=1_000,
        preview=ToolResultPreviewMetadata(
            preview_policy="full",
            preview_chars=10,
            original_chars=10,
            original_bytes=10,
            omitted_middle_chars=0,
            visible_head_chars=10,
            visible_tail_chars=0,
            read_more={"tool": "artifact_read", "artifact_id": "artifact:image"},
        ),
    )
    text = ToolResultArtifactRef(
        artifact_id="artifact:text",
        role="combined_output",
        media_type="text/plain; charset=utf-8",
        size_bytes=50_000,
        preview=ToolResultPreviewMetadata(
            preview_policy="head_tail",
            preview_chars=120,
            original_chars=50_000,
            original_bytes=50_000,
            omitted_middle_chars=49_880,
            visible_head_chars=80,
            visible_tail_chars=40,
            read_more={"tool": "artifact_read", "artifact_id": "artifact:text"},
        ),
    )
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json="{}",
        result_text="preview",
        artifacts=(image, text),
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    policy = resolve_context_compile_policy(
        LoopBudget(
            tool_result_per_tool_cap_chars=1,
            tool_result_per_message_cap_chars=1,
            tool_result_per_envelope_cap_chars=envelope_cap,
            minimum_essential_envelope_chars=64,
        )
    )
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=policy.tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    decision = rendered.canonical_decisions[0]
    visible = rendered.fragments[0].text
    assert decision.envelope_policy is expected_policy
    assert decision.primary_artifact_id == "artifact:text"
    assert "artifact:text" in visible
    if expected_policy is ToolResultEnvelopePolicy.COMPACT:
        assert visible.index("artifact:text") < visible.index("artifact:image")
        assert visible.count('"read_more"') == 1
    else:
        assert "artifact:image" not in visible


def test_binary_only_artifact_is_not_promoted_to_primary_artifact() -> None:
    image = ToolResultArtifactRef(
        artifact_id="artifact:image",
        role="image",
        media_type="image/png",
        size_bytes=1_000,
    )
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json="{}",
        result_text="preview",
        artifacts=(image,),
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    policy = resolve_context_compile_policy(
        LoopBudget(
            tool_result_per_tool_cap_chars=1,
            tool_result_per_message_cap_chars=1,
            tool_result_per_envelope_cap_chars=160,
            minimum_essential_envelope_chars=64,
        )
    )
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=policy.tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    assert rendered.canonical_decisions[0].primary_artifact_id is None
    assert '"primary_artifact_id":"artifact:image"' not in rendered.fragments[0].text


def test_artifact_preview_dropped_by_envelope_budget_is_not_counted_visible() -> None:
    body = "x" * 8_000
    artifact = ToolResultArtifactRef(
        artifact_id="artifact:envelope-pressure",
        role="combined_output",
        media_type="text/plain; charset=utf-8",
        size_bytes=len(body),
        preview=ToolResultPreviewMetadata(
            preview_policy="head_tail",
            preview_chars=len(body),
            original_chars=len(body),
            original_bytes=len(body),
            omitted_middle_chars=0,
            visible_head_chars=len(body),
            visible_tail_chars=0,
            read_more={
                "tool": "artifact_read",
                "artifact_id": "artifact:envelope-pressure",
            },
        ),
    )
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json='{"command":"large"}',
        result_text=body,
        artifacts=(artifact,),
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    policy = resolve_context_compile_policy(
        LoopBudget(
            tool_result_per_tool_cap_chars=8_000,
            tool_result_per_message_cap_chars=8_000,
            tool_result_per_envelope_cap_chars=256,
            minimum_essential_envelope_chars=64,
        )
    )
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=policy.tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    decision = rendered.canonical_decisions[0]
    assert decision.visible_body_chars == 0
    assert decision.body_policy is ToolResultBodyPolicy.OMITTED_ARTIFACT
    assert decision.rendered_envelope_chars >= 0
    assert decision.rendered_total_chars >= decision.rendered_envelope_chars


def test_default_artifact_projection_preserves_head_and_tail_with_body_budget() -> None:
    body = "PULSARA_HEAD\n" + ("q" * 20_000) + "\nPULSARA_TAIL"
    artifact = ToolResultArtifactRef(
        artifact_id="artifact:head-tail",
        role="combined_output",
        media_type="text/plain; charset=utf-8",
        size_bytes=len(body),
        preview=ToolResultPreviewMetadata(
            preview_policy="head_tail",
            preview_chars=2_048,
            original_chars=len(body),
            original_bytes=len(body),
            omitted_middle_chars=len(body) - 2_048,
            visible_head_chars=1_024,
            visible_tail_chars=1_024,
            read_more={
                "tool": "artifact_read",
                "artifact_id": "artifact:head-tail",
            },
        ),
    )
    snapshot, event_slice = _projection_fixture(
        raw_arguments_json='{"artifact_id":"artifact:head-tail"}',
        result_text=body,
        artifacts=(artifact,),
    )
    normalized = project_context_transcript(snapshot=snapshot, event_slice=event_slice)
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=resolve_context_compile_policy(LoopBudget()).tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )

    text = _lowered_messages(normalized, rendered).full_messages[-1].content[0]
    decision = rendered.canonical_decisions[0]
    assert "PULSARA_HEAD" in text
    assert "PULSARA_TAIL" in text
    assert "artifact:head-tail" in text
    assert decision.visible_body_chars > 0
    assert decision.rendered_envelope_chars >= 0
