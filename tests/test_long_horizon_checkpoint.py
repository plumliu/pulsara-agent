from __future__ import annotations

import asyncio
import ast
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Literal, get_args
from uuid import uuid4

import psycopg
import pytest
from pydantic import BaseModel
from psycopg.rows import dict_row

import pulsara_agent.event_log.postgres as postgres_event_log_module

from pulsara_agent.event import (
    EventContext,
    EventType,
    AgentEvent,
    RunStartEvent,
    SubagentGraphCheckpointCommittedEvent,
    SubagentTaskCreatedEvent,
    TextBlockDeltaEvent,
)
from pulsara_agent.event_log import (
    EventBatchConfirmation,
    EventIdConflict,
    InMemoryEventLog,
    PostgresEventLog,
    RawCheckpointLedgerCandidate,
    dump_agent_event,
)
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventSchemaContractMismatch,
    EventSchemaDomainRegistry,
    EventSchemaRegistryConflict,
    event_schema_fingerprint,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    RunLongHorizonContractFact,
    SubagentGraphReducerContractFact,
    default_subagent_graph_checkpoint_policy,
)
from pulsara_agent.llm.materialize import (
    MAX_MODEL_CALL_MATERIALIZATION_EVENTS,
    MAX_MODEL_CALL_MATERIALIZATION_PAYLOAD_BYTES,
)
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.foundation.records import ArtifactContentConflict
from pulsara_agent.runtime.long_horizon.checkpoint import (
    SubagentGraphCheckpointContractMismatch,
    SubagentGraphCheckpointLedgerUntrusted,
    prepare_subagent_graph_checkpoint,
    restore_subagent_graph_from_checkpoint,
)
from pulsara_agent.runtime.long_horizon.checkpoint_store import (
    SubagentGraphCheckpointDeltaBoundExceeded,
    EventLogSubagentGraphCheckpointReadPort,
    SubagentGraphCheckpointRebaseUnavailable,
)
from pulsara_agent.runtime.long_horizon.checkpoint_doctor import (
    SubagentGraphCheckpointRepairOutcome,
    verify_or_rebuild_subagent_graph_checkpoint,
)
from pulsara_agent.runtime.long_horizon.checkpoint_gc import (
    garbage_collect_subagent_graph_checkpoint_artifacts,
)
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    CheckpointMaintenanceLockUnavailable,
    CheckpointMaintenanceSessionNotQuiescent,
    InMemoryCheckpointMaintenanceAuthority,
    PostgresCheckpointMaintenanceAuthority,
)
from pulsara_agent.runtime.long_horizon.reducer_contract import (
    DEFAULT_SUBAGENT_GRAPH_REDUCER_REGISTRY,
    SubagentGraphReducerBinding,
    SubagentGraphReducerRegistry,
    SubagentGraphReducerRegistryConflict,
    build_default_subagent_graph_reducer_binding,
    export_subagent_graph_state,
    graph_semantic_payload_fingerprint,
    graph_state_semantic_fingerprint,
)
from pulsara_agent.runtime.context_input.candidate import (
    build_context_candidate_source_selections,
)
from pulsara_agent.runtime.context_input.policy import resolve_context_compile_policy
from pulsara_agent.runtime.session import (
    EventBatchCommitOutcome,
    EventCommitError,
    EventReconciliationRequired,
    EventWriteCancelled,
    RuntimeSession,
)
from pulsara_agent.runtime.state import LoopBudget
from pulsara_agent.settings import StorageConfig
from tests.conftest import run_start_permission_fields
from tests.support.runtime_session import in_memory_runtime_session


RUNTIME_ID = "runtime:checkpoint:test"
CTX = EventContext(
    run_id="run:checkpoint:test",
    turn_id="turn:checkpoint:test",
    reply_id="reply:checkpoint:test",
)


def _non_graph(event_id: str, text: str = "x") -> TextBlockDeltaEvent:
    return TextBlockDeltaEvent(
        id=event_id,
        created_at="2026-07-13T00:00:00Z",
        **CTX.event_fields(),
        block_id="text:checkpoint",
        delta=text,
    )


def _task(event_id: str, task_id: str = "task:checkpoint") -> SubagentTaskCreatedEvent:
    return SubagentTaskCreatedEvent(
        id=event_id,
        created_at="2026-07-13T00:00:01Z",
        **CTX.event_fields(),
        task_id=task_id,
        task_key=task_id,
        profile_id="research_worker",
        objective_preview="inspect checkpoint behavior",
        objective_artifact_id=f"artifact:{task_id}",
    )


def _write_checkpoint_artifact(runtime, prepared) -> None:
    artifact = prepared.artifact
    runtime.archive.put_text_if_absent_or_confirm_identical(
        artifact.artifact_id,
        prepared.artifact_payload_bytes.decode("utf-8"),
        session_id=runtime.runtime_session_id,
        run_id=None,
        media_type=artifact.media_type,
        semantic_metadata={
            "artifact_kind": "subagent_graph_checkpoint",
            "checkpoint_id": prepared.checkpoint.checkpoint_id,
            "content_sha256": artifact.content_sha256,
            "semantic_metadata_fingerprint": (
                artifact.semantic_metadata_fingerprint
            ),
        },
    )


def _materialize_checkpoint(runtime, through_sequence: int):
    binding = runtime.subagent_graph_checkpoint_service.reducer_binding
    prefix = runtime.event_log.read_raw_range_snapshot(
        minimum_sequence=1,
        through_sequence=through_sequence,
    ).events
    prepared = prepare_subagent_graph_checkpoint(
        runtime_session_id=runtime.runtime_session_id,
        prefix_events=prefix,
        reducer_binding=binding,
    )
    _write_checkpoint_artifact(runtime, prepared)
    stored = runtime.event_log.append(prepared.event)
    assert stored.sequence is not None
    return prepared, stored


def _read(runtime, *, through_sequence: int, preferred: str | None = None):
    service = runtime.subagent_graph_checkpoint_service
    return EventLogSubagentGraphCheckpointReadPort(
        event_log=runtime.event_log,
        archive=runtime.archive,
        runtime_session_id=runtime.runtime_session_id,
    ).read_checkpoint_and_delta_snapshot(
        requested_through_sequence=through_sequence,
        reducer_contract=service.reducer_binding.contract,
        preferred_checkpoint_id=preferred,
        max_delta_events=service.policy.checkpoint_max_delta_events,
        max_delta_bytes=service.policy.checkpoint_max_delta_bytes,
        max_checkpoint_candidates=service.policy.rebase_max_checkpoint_candidates,
    )


def _restore(runtime, *, through_sequence: int, preferred: str | None = None):
    snapshot = _read(runtime, through_sequence=through_sequence, preferred=preferred)
    assert not hasattr(snapshot, "reason_code")
    return restore_subagent_graph_from_checkpoint(
        snapshot=snapshot,
        reducer_binding=runtime.subagent_graph_checkpoint_service.reducer_binding,
    )


def _postgres_log_or_skip(tmp_path: Path):
    dsn = StorageConfig.from_env().postgres_dsn
    try:
        psycopg.connect(dsn, connect_timeout=2).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
    suffix = uuid4().hex
    runtime_session_id = f"runtime:checkpoint:postgres:{suffix}"
    context = EventContext(
        run_id=f"run:checkpoint:postgres:{suffix}",
        turn_id=f"turn:checkpoint:postgres:{suffix}",
        reply_id=f"reply:checkpoint:postgres:{suffix}",
    )
    return (
        dsn,
        runtime_session_id,
        context,
        PostgresEventLog(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        ),
    )


def _cleanup_postgres_session(dsn: str, runtime_session_id: str) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "delete from sessions where id = %s", (runtime_session_id,)
            )


def test_checkpoint_export_is_canonical_and_order_independent() -> None:
    binding = build_default_subagent_graph_reducer_binding()
    log = InMemoryEventLog(runtime_session_id=RUNTIME_ID)
    log.extend(
        (
            _task("event:task:b", "task:b"),
            _task("event:task:a", "task:a"),
        )
    )
    first = binding.empty_state_factory()
    for envelope in log.read_raw_range_snapshot(minimum_sequence=1).events:
        first = binding.fold_stored_event(first, envelope)
    second = replace(
        first,
        tasks={
            "task:a": first.tasks["task:a"],
            "task:b": first.tasks["task:b"],
        },
    )

    assert export_subagent_graph_state(first) == export_subagent_graph_state(second)


def test_checkpoint_event_rejects_inconsistent_graph() -> None:
    binding = build_default_subagent_graph_reducer_binding()
    raw = InMemoryEventLog(runtime_session_id=RUNTIME_ID)
    raw.append(_non_graph("event:one"))
    envelope = raw.read_raw_range_snapshot(minimum_sequence=1).events[0]
    inconsistent = replace(binding.empty_state_factory(), consistent=False)
    broken = SubagentGraphReducerBinding(
        contract=binding.contract,
        implementation_build_fingerprint="test:inconsistent",
        empty_state_factory=lambda: inconsistent,
        fold_stored_event=lambda _state, _event: inconsistent,
        export_canonical_state=binding.export_canonical_state,
        restore_canonical_state=binding.restore_canonical_state,
    )

    with pytest.raises(SubagentGraphCheckpointLedgerUntrusted):
        prepare_subagent_graph_checkpoint(
            runtime_session_id=RUNTIME_ID,
            prefix_events=(envelope,),
            reducer_binding=broken,
        )


def test_checkpoint_artifact_same_id_same_bytes_is_idempotent(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:artifact-idempotent"))
    binding = runtime.subagent_graph_checkpoint_service.reducer_binding
    prepared = prepare_subagent_graph_checkpoint(
        runtime_session_id=RUNTIME_ID,
        prefix_events=runtime.event_log.read_raw_range_snapshot(
            minimum_sequence=1
        ).events,
        reducer_binding=binding,
    )

    _write_checkpoint_artifact(runtime, prepared)
    _write_checkpoint_artifact(runtime, prepared)

    assert tuple(runtime.archive.blobs) == (prepared.artifact.artifact_id,)
    assert runtime.archive.get_text(
        prepared.artifact.artifact_id,
        session_id=RUNTIME_ID,
    ).encode("utf-8") == prepared.artifact_payload_bytes


def test_checkpoint_artifact_metadata_only_conflict_fails_closed(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:artifact-metadata-conflict"))
    prepared = prepare_subagent_graph_checkpoint(
        runtime_session_id=RUNTIME_ID,
        prefix_events=runtime.event_log.read_raw_range_snapshot(
            minimum_sequence=1
        ).events,
        reducer_binding=runtime.subagent_graph_checkpoint_service.reducer_binding,
    )
    _write_checkpoint_artifact(runtime, prepared)

    with pytest.raises(ArtifactContentConflict):
        runtime.archive.put_text_if_absent_or_confirm_identical(
            prepared.artifact.artifact_id,
            prepared.artifact_payload_bytes.decode("utf-8"),
            session_id=RUNTIME_ID,
            run_id=None,
            media_type=prepared.artifact.media_type,
            semantic_metadata={
                "artifact_kind": "subagent_graph_checkpoint",
                "checkpoint_id": prepared.checkpoint.checkpoint_id,
                "content_sha256": prepared.artifact.content_sha256,
                "semantic_metadata_fingerprint": "sha256:metadata-drift",
            },
        )


def test_raw_envelope_wrapper_schema_and_payload_identity_are_consistent() -> None:
    log = InMemoryEventLog(runtime_session_id=RUNTIME_ID)
    stored = log.append(_non_graph("event:raw-wrapper"))
    raw = log.read_raw_events_by_id((stored.id,))[0]

    assert raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY) == stored
    with pytest.raises(ValueError, match="wrapper identity mismatch"):
        replace(raw, event_id="event:raw-wrapper-drift")
    with pytest.raises(ValueError, match="payload fingerprint mismatch"):
        replace(raw, canonical_payload_bytes=b"{}")


def test_checkpoint_delta_rejects_gap_duplicate_and_out_of_order(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:delta-one"))
    prepared, stored = _materialize_checkpoint(runtime, 1)
    runtime.event_log.extend(
        (
            _non_graph("event:delta-three"),
            _non_graph("event:delta-four"),
        )
    )
    raw = runtime.event_log.read_raw_range_snapshot(
        minimum_sequence=2,
        through_sequence=4,
    ).events
    checkpoint_raw = raw[0]
    assert checkpoint_raw.event_id == stored.id

    invalid_deltas = (
        (raw[0], raw[2]),
        (raw[0], raw[1], raw[1]),
        (raw[0], raw[2], raw[1]),
    )
    for delta in invalid_deltas:
        with pytest.raises(ValueError):
            RawCheckpointLedgerCandidate(
                checkpoint_id=prepared.checkpoint.checkpoint_id,
                checkpoint_through_sequence=1,
                checkpoint_event=checkpoint_raw,
                delta_events=delta,
                delta_event_count=3,
                delta_payload_bytes=sum(
                    len(item.canonical_payload_bytes) for item in delta
                ),
                event_bound_satisfied=True,
                byte_bound_satisfied=True,
            )


def test_checkpoint_restore_matches_full_fold(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.extend(
        (_non_graph("event:one"), _task("event:task"), _non_graph("event:three"))
    )
    prepared, _stored = _materialize_checkpoint(runtime, 2)
    restored, semantic, acceleration = _restore(runtime, through_sequence=3)

    binding = runtime.subagent_graph_checkpoint_service.reducer_binding
    full = binding.empty_state_factory()
    raw = runtime.event_log.read_raw_range_snapshot(
        minimum_sequence=1, through_sequence=3
    ).events
    for envelope in raw:
        full = binding.fold_stored_event(full, envelope)

    assert graph_state_semantic_fingerprint(restored) == graph_state_semantic_fingerprint(
        full
    )
    assert semantic.graph_state_semantic_fingerprint == graph_state_semantic_fingerprint(
        full
    )
    assert acceleration.checkpoint_id == prepared.checkpoint.checkpoint_id
    assert acceleration.delta_count == 1


def test_checkpoint_schema_match_reducer_contract_mismatch_fails_closed(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:one"))
    _materialize_checkpoint(runtime, 1)
    snapshot = _read(runtime, through_sequence=1)
    binding = runtime.subagent_graph_checkpoint_service.reducer_binding
    mismatched_contract = binding.contract.model_copy(
        update={"graph_reducer_contract_fingerprint": "sha256:mismatch"}
    )
    mismatched = replace(binding, contract=mismatched_contract)

    with pytest.raises(SubagentGraphCheckpointContractMismatch):
        restore_subagent_graph_from_checkpoint(
            snapshot=snapshot,
            reducer_binding=mismatched,
        )


def test_checkpoint_event_and_declared_non_graph_events_extend_ledger_continuity_only(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_task("event:task"))
    prepared, stored = _materialize_checkpoint(runtime, 1)
    assert stored.sequence == 2
    restored, semantic, acceleration = _restore(runtime, through_sequence=2)

    assert semantic.graph_event_count == prepared.checkpoint.graph_event_count
    assert semantic.graph_semantic_accumulator == (
        prepared.checkpoint.graph_semantic_accumulator
    )
    assert acceleration.delta_count == 1
    assert acceleration.ledger_continuity_accumulator != (
        prepared.checkpoint.ledger_continuity_accumulator
    )
    assert tuple(restored.tasks) == ("task:checkpoint",)


def test_checkpoint_event_does_not_change_graph_semantic_accumulator_or_source_fingerprint(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_task("event:task"))
    prepared, _stored = _materialize_checkpoint(runtime, 1)
    _state_before, semantic_before, _acceleration_before = _restore(
        runtime, through_sequence=1
    )
    _state_after, semantic_after, _acceleration_after = _restore(
        runtime, through_sequence=2
    )

    assert semantic_before == semantic_after
    assert semantic_before.graph_semantic_accumulator == (
        prepared.checkpoint.graph_semantic_accumulator
    )
    policy = resolve_context_compile_policy(LoopBudget()).candidate_collection
    selection_before = build_context_candidate_source_selections(
        subagent_graph=_state_before,
        semantic_source=semantic_before,
        policy=policy,
    )
    selection_after = build_context_candidate_source_selections(
        subagent_graph=_state_after,
        semantic_source=semantic_after,
        policy=policy,
    )
    assert selection_before == selection_after


def test_checkpoint_schedule_does_not_change_semantic_source_or_selection_fingerprint(
    tmp_path,
) -> None:
    test_checkpoint_event_does_not_change_graph_semantic_accumulator_or_source_fingerprint(
        tmp_path
    )


def test_graph_semantic_payload_fingerprint_cannot_reuse_storage_payload_with_sequence() -> None:
    binding = build_default_subagent_graph_reducer_binding()
    first_log = InMemoryEventLog(runtime_session_id="runtime:first")
    second_log = InMemoryEventLog(runtime_session_id="runtime:second")
    event = _task("event:stable")
    first_log.append(event)
    second_log.append(_non_graph("event:padding"))
    second_log.append(event)
    first = first_log.read_raw_range_snapshot(minimum_sequence=1).events[0]
    second = second_log.read_raw_range_snapshot(minimum_sequence=2).events[0]

    assert first.payload_fingerprint != second.payload_fingerprint
    assert graph_semantic_payload_fingerprint(
        envelope=first, contract=binding.contract
    ) == graph_semantic_payload_fingerprint(
        envelope=second, contract=binding.contract
    )


def test_checkpoint_does_not_include_its_own_materialization_event(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:one"))
    prepared, stored = _materialize_checkpoint(runtime, 1)

    assert stored.sequence == 2
    assert prepared.checkpoint.through_sequence == 1
    assert prepared.checkpoint.ledger_continuity_accumulator != context_fingerprint(
        "impossible:self-inclusive-checkpoint:v1", stored.id
    )


def test_checkpoint_reader_returns_deep_copies(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:one"))
    _materialize_checkpoint(runtime, 1)

    first = _read(runtime, through_sequence=1)
    second = _read(runtime, through_sequence=1)
    assert first is not second
    assert first.checkpoint_event is not second.checkpoint_event
    assert first.checkpoint_payload_bytes == second.checkpoint_payload_bytes


def test_new_session_bounded_bootstrap_confirms_checkpoint_before_compile(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:one"))

    snapshot = asyncio.run(
        runtime.subagent_graph_checkpoint_service.restore_for_selection(
            requested_through_sequence=1
        )
    )

    stored = runtime.event_log.get_by_id(snapshot.checkpoint_event.event_id)
    assert isinstance(stored, SubagentGraphCheckpointCommittedEvent)
    assert stored.sequence is not None
    assert snapshot.selected_checkpoint_id == stored.checkpoint.checkpoint_id


def test_existing_session_without_checkpoint_never_uses_production_full_fold(
    tmp_path,
) -> None:
    log = InMemoryEventLog(runtime_session_id=RUNTIME_ID)
    log.append(_non_graph("event:existing"))
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=RUNTIME_ID,
        event_log=log,
    )

    with pytest.raises(SubagentGraphCheckpointRebaseUnavailable):
        asyncio.run(
            runtime.subagent_graph_checkpoint_service.restore_for_selection(
                requested_through_sequence=1
            )
        )
    assert not any(
        event.type is EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED
        for event in log.iter()
    )


def test_checkpoint_delta_bound_blocks_unbounded_full_fold(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:delta-bound:one"))
    prepared, _stored = _materialize_checkpoint(runtime, 1)
    runtime.event_log.extend(
        tuple(
            _non_graph(f"event:delta-bound:{index}")
            for index in range(3, 8)
        )
    )
    service = runtime.subagent_graph_checkpoint_service
    service.policy = service.policy.model_copy(
        update={"checkpoint_max_delta_events": 1}
    )

    with pytest.raises(
        SubagentGraphCheckpointDeltaBoundExceeded,
        match="subagent_checkpoint_delta_bound_exceeded",
    ):
        asyncio.run(
            service.restore_for_selection(
                requested_through_sequence=7,
                preferred_checkpoint_id=prepared.checkpoint.checkpoint_id,
            )
        )

    assert service.write_states() == {}


def test_graph_checkpoint_delta_bound_uses_shared_ledger_hard_horizon() -> None:
    from pulsara_agent.runtime.authority_materialization import (
        build_default_authority_materialization_contract_bundle,
    )

    limits = build_default_authority_materialization_contract_bundle().limits
    policy = default_subagent_graph_checkpoint_policy(
        max_unreclaimable_ledger_events=limits.max_unreclaimable_ledger_events,
        max_unreclaimable_charged_payload_bytes=(
            limits.max_unreclaimable_charged_payload_bytes
        ),
    )

    assert (
        policy.checkpoint_max_delta_events
        == limits.max_unreclaimable_ledger_events
    )
    assert (
        policy.checkpoint_max_delta_bytes
        == limits.max_unreclaimable_charged_payload_bytes
    )
    assert policy.checkpoint_max_delta_events > MAX_MODEL_CALL_MATERIALIZATION_EVENTS
    assert (
        policy.checkpoint_max_delta_bytes
        > MAX_MODEL_CALL_MATERIALIZATION_PAYLOAD_BYTES
    )


def test_checkpoint_partial_commit_latches_ledger(tmp_path, monkeypatch) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:partial:one"))

    def partial_confirmation(_self, candidates, *, deadline_monotonic=None):
        del deadline_monotonic
        prepared = runtime._prepare_event_batch(candidates)  # noqa: SLF001
        committed = prepared[0].model_copy(update={"sequence": 2})
        return EventBatchConfirmation(
            committed_events=(committed,),
            missing_event_ids=("event:missing-atomic-sibling",),
            actual_last_sequence=2,
        )

    monkeypatch.setattr(InMemoryEventLog, "confirm_batch", partial_confirmation)

    with pytest.raises(EventReconciliationRequired):
        asyncio.run(
            runtime.confirm_event_batch_async(
                (_non_graph("event:partial:candidate"),),
                deadline_monotonic=monotonic() + 1.0,
            )
        )

    assert runtime.ledger_reconciliation_required is True
    with pytest.raises(EventReconciliationRequired):
        runtime.require_mutation_allowed()


def test_existing_checkpoint_catalog_with_missing_artifacts_does_not_use_bootstrap(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:one"))
    binding = runtime.subagent_graph_checkpoint_service.reducer_binding
    prefix = runtime.event_log.read_raw_range_snapshot(minimum_sequence=1).events
    prepared = prepare_subagent_graph_checkpoint(
        runtime_session_id=RUNTIME_ID,
        prefix_events=prefix,
        reducer_binding=binding,
    )
    runtime.event_log.append(prepared.event)

    with pytest.raises(SubagentGraphCheckpointRebaseUnavailable):
        asyncio.run(
            runtime.subagent_graph_checkpoint_service.restore_for_selection(
                requested_through_sequence=1
            )
        )
    assert sum(
        event.type is EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED
        for event in runtime.event_log.iter()
    ) == 1


def test_checkpoint_writer_cancel_after_commit_confirms_full(
    tmp_path, monkeypatch
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:one"))
    original = RuntimeSession.write_event

    async def commit_then_cancel(self, event, **kwargs):
        result = await original(self, event, **kwargs)
        raise EventWriteCancelled(
            EventBatchCommitOutcome(
                status="full",
                deadline_monotonic=monotonic(),
                result=result,
            )
        )

    monkeypatch.setattr(RuntimeSession, "write_event", commit_then_cancel)
    snapshot = asyncio.run(
        runtime.subagent_graph_checkpoint_service.restore_for_selection(
            requested_through_sequence=1
        )
    )

    assert runtime.subagent_graph_checkpoint_service.write_states()[1] == "committed"
    assert runtime.event_log.get_by_id(snapshot.checkpoint_event.event_id) is not None


def test_run_start_checkpoint_event_is_schema_bound() -> None:
    contract = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(
        str(EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED)
    )
    assert contract.event_domain == "non_graph"
    assert contract.event_schema_version
    assert contract.event_schema_fingerprint


def test_event_schema_fingerprint_is_canonical_under_schema_key_order() -> None:
    class FirstSchema:
        @classmethod
        def model_json_schema(cls, **_kwargs):
            return {
                "type": "object",
                "required": ["type", "value"],
                "properties": {
                    "type": {"const": "LEGACY_TEST"},
                    "value": {"type": "string"},
                },
            }

    class ReorderedSchema:
        @classmethod
        def model_json_schema(cls, **_kwargs):
            return {
                "properties": {
                    "value": {"type": "string"},
                    "type": {"const": "LEGACY_TEST"},
                },
                "required": ["type", "value"],
                "type": "object",
            }

    first = event_schema_fingerprint(
        event_type="LEGACY_TEST",
        event_schema_version="agent-event:legacy-test:v1",
        event_model=FirstSchema,  # type: ignore[arg-type]
    )
    second = event_schema_fingerprint(
        event_type="LEGACY_TEST",
        event_schema_version="agent-event:legacy-test:v1",
        event_model=ReorderedSchema,  # type: ignore[arg-type]
    )

    assert first == second


def test_same_event_type_version_with_different_schema_fingerprint_is_registry_conflict() -> (
    None
):
    class FirstCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: str

    class ChangedCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: int

    registry = EventSchemaDomainRegistry()
    registry.register(
        event_model=FirstCustomEvent,
        event_schema_version="agent-event:custom:test-v1",
    )
    with pytest.raises(EventSchemaRegistryConflict):
        registry.register(
            event_model=ChangedCustomEvent,
            event_schema_version="agent-event:custom:test-v1",
        )


def test_event_schema_semantic_change_requires_version_and_fingerprint_change() -> (
    None
):
    class LegacyCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: str

    class ChangedCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: int

    registry = EventSchemaDomainRegistry()
    first = registry.register(
        event_model=LegacyCustomEvent,
        event_schema_version="agent-event:custom:semantic-v1",
    )
    second = registry.register(
        event_model=ChangedCustomEvent,
        event_schema_version="agent-event:custom:semantic-v2",
    )

    assert first.schema_contract.event_schema_version != (
        second.schema_contract.event_schema_version
    )
    assert first.schema_contract.event_schema_fingerprint != (
        second.schema_contract.event_schema_fingerprint
    )


def test_historical_decoder_restores_old_schema_before_current_union() -> None:
    class LegacyCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        legacy_value: str

    registry = EventSchemaDomainRegistry()
    binding = registry.register(
        event_model=LegacyCustomEvent,
        event_schema_version="agent-event:custom:legacy-v0",
    )
    payload = canonical_json_bytes(
        LegacyCustomEvent(legacy_value="historical").model_dump(mode="json")
    )
    resolved = registry.resolve_historical_binding(
        event_type=str(EventType.CUSTOM),
        event_schema_version=binding.schema_contract.event_schema_version,
        event_schema_fingerprint=(
            binding.schema_contract.event_schema_fingerprint
        ),
        event_domain_contract_fingerprint=(
            binding.schema_contract.domain_contract_fingerprint
        ),
    )

    decoded = resolved.decode_owned_payload(payload)
    assert isinstance(decoded, LegacyCustomEvent)
    assert decoded.legacy_value == "historical"


def test_historical_decoder_contract_fingerprint_drift_is_contract_mismatch() -> (
    None
):
    contract = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(
        str(EventType.TEXT_BLOCK_DELTA)
    )
    with pytest.raises(EventSchemaContractMismatch):
        DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_historical_binding(
            event_type=contract.event_type,
            event_schema_version=contract.event_schema_version,
            event_schema_fingerprint=contract.event_schema_fingerprint,
            event_domain_contract_fingerprint="sha256:drift",
        )


def test_event_schema_domain_fingerprint_drift_is_contract_mismatch() -> None:
    contract = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(
        str(EventType.TEXT_BLOCK_DELTA)
    )

    with pytest.raises(EventSchemaContractMismatch):
        DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_historical_binding(
            event_type=contract.event_type,
            event_schema_version=contract.event_schema_version,
            event_schema_fingerprint=contract.event_schema_fingerprint,
            event_domain_contract_fingerprint="sha256:domain-drift",
        )


def test_historical_event_domain_binding_rebinds_after_registry_upgrade() -> None:
    class LegacyCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: str

    class CurrentCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: str
        generation: int

    registry = EventSchemaDomainRegistry()
    legacy = registry.register(
        event_model=LegacyCustomEvent,
        event_schema_version="agent-event:custom:domain-v1",
    )
    registry.register(
        event_model=CurrentCustomEvent,
        event_schema_version="agent-event:custom:domain-v2",
    )

    rebound = registry.resolve_historical_binding(
        event_type=legacy.schema_contract.event_type,
        event_schema_version=legacy.schema_contract.event_schema_version,
        event_schema_fingerprint=legacy.schema_contract.event_schema_fingerprint,
        event_domain_contract_fingerprint=(
            legacy.schema_contract.domain_contract_fingerprint
        ),
    )
    assert rebound.schema_contract == legacy.schema_contract


def test_event_domain_is_immutable_for_event_type_and_schema_version() -> None:
    class FirstCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: str

    class NextCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: str
        generation: int

    registry = EventSchemaDomainRegistry()
    first = registry.register(
        event_model=FirstCustomEvent,
        event_schema_version="agent-event:custom:domain-stable-v1",
    )
    second = registry.register(
        event_model=NextCustomEvent,
        event_schema_version="agent-event:custom:domain-stable-v2",
    )

    assert first.schema_contract.event_domain == "non_graph"
    assert second.schema_contract.event_domain == first.schema_contract.event_domain


def test_new_non_graph_event_does_not_change_existing_graph_reducer_contract() -> (
    None
):
    registry = EventSchemaDomainRegistry()
    for event_model in get_args(AgentEvent):
        event_type = str(event_model.model_fields["type"].default)
        current = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(event_type)
        registry.register(
            event_model=event_model,
            event_schema_version=current.event_schema_version,
        )
    before = build_default_subagent_graph_reducer_binding(registry).contract

    class AdditionalCustomEvent(BaseModel):
        type: Literal[EventType.CUSTOM] = EventType.CUSTOM
        value: str
        additional_non_graph_field: bool

    registry.register(
        event_model=AdditionalCustomEvent,
        event_schema_version="agent-event:custom:additional-non-graph-v2",
    )
    after = build_default_subagent_graph_reducer_binding(registry).contract

    assert after == before


def test_in_memory_event_log_stores_raw_envelope_and_returns_owned_decode_copy() -> (
    None
):
    log = InMemoryEventLog(runtime_session_id=RUNTIME_ID)
    candidate = _non_graph("event:owned", "before")
    candidate.metadata["nested"] = {"value": "original"}
    stored = log.append(candidate)
    candidate.metadata["nested"]["value"] = "mutated-before-read"
    first = log.iter()[0]
    first.metadata["nested"]["value"] = "mutated-return-copy"
    second = log.iter()[0]
    raw = log.read_raw_events_by_id((stored.id,))[0]

    assert second.metadata["nested"]["value"] == "original"
    assert raw.event_schema_version
    assert raw.event_schema_fingerprint
    assert raw.event_domain_contract_fingerprint


def test_same_reducer_id_version_with_different_contract_fingerprint_is_registry_conflict() -> (
    None
):
    binding = build_default_subagent_graph_reducer_binding()
    payload = binding.contract.model_dump(
        mode="json", exclude={"graph_reducer_contract_fingerprint"}
    )
    payload["transition_contract_fingerprint"] = "sha256:changed-transition"
    changed = SubagentGraphReducerContractFact(
        **payload,
        graph_reducer_contract_fingerprint=context_fingerprint(
            "subagent-graph-reducer-contract:v1", payload
        ),
    )
    registry = SubagentGraphReducerRegistry()
    registry.register(binding)

    with pytest.raises(SubagentGraphReducerRegistryConflict):
        registry.register(replace(binding, contract=changed))


def test_reducer_semantic_change_requires_version_and_contract_fingerprint_change() -> (
    None
):
    binding = build_default_subagent_graph_reducer_binding()
    payload = binding.contract.model_dump(
        mode="json", exclude={"graph_reducer_contract_fingerprint"}
    )
    payload["graph_reducer_version"] = "2"
    payload["transition_contract_fingerprint"] = "sha256:transition-v2"
    changed = SubagentGraphReducerContractFact(
        **payload,
        graph_reducer_contract_fingerprint=context_fingerprint(
            "subagent-graph-reducer-contract:v1", payload
        ),
    )
    registry = SubagentGraphReducerRegistry()
    registry.register(binding)
    registry.register(replace(binding, contract=changed))

    rebound = registry.resolve_binding(
        reducer_id=changed.graph_reducer_id,
        reducer_version=changed.graph_reducer_version,
        reducer_contract_fingerprint=changed.graph_reducer_contract_fingerprint,
    )
    assert rebound.contract == changed


def test_graph_state_semantic_fingerprint_excludes_event_sequence_attribution() -> (
    None
):
    binding = build_default_subagent_graph_reducer_binding()
    first_log = InMemoryEventLog(runtime_session_id="runtime:first-state")
    second_log = InMemoryEventLog(runtime_session_id="runtime:second-state")
    task = _task("event:stable-task", "task:stable")
    first_log.append(task)
    second_log.append(_non_graph("event:padding-state"))
    second_log.append(task)
    first_state = binding.empty_state_factory()
    second_state = binding.empty_state_factory()
    for event in first_log.read_raw_range_snapshot(minimum_sequence=1).events:
        first_state = binding.fold_stored_event(first_state, event)
    for event in second_log.read_raw_range_snapshot(minimum_sequence=1).events:
        second_state = binding.fold_stored_event(second_state, event)

    assert graph_state_semantic_fingerprint(
        first_state
    ) == graph_state_semantic_fingerprint(second_state)


def test_graph_semantic_accumulator_ignores_physical_sequence_and_checkpoint_schedule(
    tmp_path,
) -> None:
    first = in_memory_runtime_session(
        tmp_path / "first", runtime_session_id="runtime:schedule:first"
    )
    second = in_memory_runtime_session(
        tmp_path / "second", runtime_session_id="runtime:schedule:second"
    )
    task = _task("event:schedule:task", "task:schedule")
    first.event_log.append(task)
    second.event_log.append(_non_graph("event:schedule:padding"))
    second.event_log.append(task)
    _materialize_checkpoint(first, 1)
    second.event_log.append(_non_graph("event:schedule:padding-two"))
    _materialize_checkpoint(second, 3)

    _first_state, first_semantic, _first_acceleration = _restore(
        first, through_sequence=1
    )
    _second_state, second_semantic, _second_acceleration = _restore(
        second, through_sequence=3
    )

    assert first_semantic.graph_event_count == second_semantic.graph_event_count
    assert first_semantic.graph_semantic_accumulator == (
        second_semantic.graph_semantic_accumulator
    )
    assert first_semantic.graph_state_semantic_fingerprint == (
        second_semantic.graph_state_semantic_fingerprint
    )


def test_future_declared_graph_event_unsupported_by_run_contract_fails_before_emit(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    binding = runtime.subagent_graph_checkpoint_service.reducer_binding
    task_contract = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(
        str(EventType.SUBAGENT_TASK_CREATED)
    )
    retained = tuple(
        item
        for item in binding.contract.supported_graph_events
        if item.event_type != task_contract.event_type
    )
    payload = binding.contract.model_dump(
        mode="json", exclude={"graph_reducer_contract_fingerprint"}
    )
    payload["supported_graph_events"] = tuple(
        item.model_dump(mode="json") for item in retained
    )
    payload["event_filter_contract_fingerprint"] = context_fingerprint(
        "subagent-graph-event-filter:v1",
        tuple((item.event_type, item.event_schema_version) for item in retained),
    )
    frozen = SubagentGraphReducerContractFact(
        **payload,
        graph_reducer_contract_fingerprint=context_fingerprint(
            "subagent-graph-reducer-contract:v1", payload
        ),
    )
    permission_fields = run_start_permission_fields(
        CTX.run_id,
        mcp_installation_owner_runtime_session_id=RUNTIME_ID,
    )
    long_horizon = permission_fields["long_horizon"]
    assert isinstance(long_horizon, RunLongHorizonContractFact)
    long_horizon_payload = long_horizon.model_dump(
        mode="json", exclude={"contract_fingerprint"}
    )
    long_horizon_payload["subagent_graph_reducer_contract"] = frozen.model_dump(
        mode="json"
    )
    frozen_long_horizon = RunLongHorizonContractFact(
        **long_horizon_payload,
        contract_fingerprint=context_fingerprint(
            "run-long-horizon:v1", long_horizon_payload
        ),
    )
    start = RunStartEvent(
        **CTX.event_fields(),
        **{
            **permission_fields,
            "subagent_graph_reducer_contract": frozen,
            "long_horizon": frozen_long_horizon,
        },
        user_input_chars=0,
    )
    asyncio.run(runtime.emit(start))

    with pytest.raises(ValueError, match="unsupported by the owning RunStart"):
        asyncio.run(runtime.emit(_task("event:future-graph")))
    assert runtime.event_log.get_by_id("event:future-graph") is None


def test_graph_domain_event_without_owning_run_start_fails_before_emit(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)

    with pytest.raises(ValueError, match="owning durable RunStart"):
        asyncio.run(runtime.emit(_task("event:no-run-start")))
    assert runtime.event_log.next_sequence() == 1


def test_offline_doctor_can_full_fold_and_rebuild_checkpoint(tmp_path) -> None:
    log = InMemoryEventLog(runtime_session_id=RUNTIME_ID)
    log.extend((_non_graph("event:doctor:one"), _task("event:doctor:task")))
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=RUNTIME_ID,
        event_log=log,
    )
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda runtime_session_id: runtime_session_id == RUNTIME_ID
    )
    contract = build_default_subagent_graph_reducer_binding().contract

    report = verify_or_rebuild_subagent_graph_checkpoint(
        runtime_session_id=RUNTIME_ID,
        through_sequence=2,
        reducer_contract=contract,
        mode="rebuild",
        event_log=log,
        archive=runtime.archive,
        reducer_registry=DEFAULT_SUBAGENT_GRAPH_REDUCER_REGISTRY,
        maintenance_authority=authority,
    )

    assert report.outcome is SubagentGraphCheckpointRepairOutcome.REBUILT
    assert report.scanned_event_count == 2
    assert report.graph_event_count == 1
    assert report.checkpoint_id is not None
    assert report.checkpoint_artifact_id is not None
    assert any(
        isinstance(event, SubagentGraphCheckpointCommittedEvent)
        and event.checkpoint.checkpoint_id == report.checkpoint_id
        for event in log.iter()
    )
    restored, semantic, _acceleration = _restore(runtime, through_sequence=2)
    assert tuple(restored.tasks) == ("task:checkpoint",)
    assert semantic.graph_event_count == 1


def test_checkpoint_gc_refuses_live_open_or_resumable_session(tmp_path) -> None:
    del tmp_path
    log = InMemoryEventLog(runtime_session_id=RUNTIME_ID)
    log.append(_non_graph("event:doctor:live"))
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda _runtime_session_id: False
    )

    with pytest.raises(CheckpointMaintenanceSessionNotQuiescent):
        verify_or_rebuild_subagent_graph_checkpoint(
            runtime_session_id=RUNTIME_ID,
            through_sequence=1,
            reducer_contract=build_default_subagent_graph_reducer_binding().contract,
            mode="verify",
            event_log=log,
            archive=InMemoryArchiveStore(),
            reducer_registry=DEFAULT_SUBAGENT_GRAPH_REDUCER_REGISTRY,
            maintenance_authority=authority,
        )


def test_checkpoint_maintenance_lock_is_released_after_failure(tmp_path) -> None:
    del tmp_path
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda _runtime_session_id: True
    )
    with authority.acquire_exclusive(RUNTIME_ID):
        with pytest.raises(CheckpointMaintenanceLockUnavailable):
            with authority.acquire_exclusive(RUNTIME_ID):
                raise AssertionError("unreachable")

    with authority.acquire_exclusive(RUNTIME_ID) as permit:
        assert permit.exclusive is True


def test_checkpoint_gc_requires_exclusive_postgres_advisory_maintenance_lock(
    tmp_path,
) -> None:
    dsn, runtime_session_id, _context, event_log = _postgres_log_or_skip(tmp_path)
    event_log.ensure_runtime_session_owner()
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                update sessions
                set metadata = coalesce(metadata, '{}'::jsonb)
                    || '{"lifecycle":{"closed_at":"2026-07-14T00:00:00Z"}}'::jsonb
                where id = %s
                """,
                (runtime_session_id,),
            )
    authority = PostgresCheckpointMaintenanceAuthority(dsn)
    try:
        with authority.acquire_shared(runtime_session_id):
            with pytest.raises(CheckpointMaintenanceLockUnavailable):
                garbage_collect_subagent_graph_checkpoint_artifacts(
                    runtime_session_id=runtime_session_id,
                    event_log=event_log,
                    archive=InMemoryArchiveStore(),
                    maintenance_authority=authority,
                    retained_checkpoint_min_count=1,
                )
    finally:
        _cleanup_postgres_session(dsn, runtime_session_id)


def test_checkpoint_doctor_refuses_runtime_session_writer_and_uses_offline_writer() -> (
    None
):
    root = Path(__file__).parents[1] / "src" / "pulsara_agent"
    doctor_tree = ast.parse(
        (root / "runtime" / "long_horizon" / "checkpoint_doctor.py").read_text(
            encoding="utf-8"
        )
    )
    cli_tree = ast.parse((root / "cli.py").read_text(encoding="utf-8"))
    doctor_names = {
        node.id for node in ast.walk(doctor_tree) if isinstance(node, ast.Name)
    }
    doctor_attributes = {
        node.attr for node in ast.walk(doctor_tree) if isinstance(node, ast.Attribute)
    }
    cli_calls = {
        node.func.id
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "RuntimeSession" not in doctor_names
    assert "write_event" not in doctor_attributes
    assert {"append", "confirm_batch"} <= doctor_attributes
    assert "verify_or_rebuild_subagent_graph_checkpoint" in cli_calls


def test_checkpoint_maintenance_lock_is_released_when_process_connection_dies(
    tmp_path,
) -> None:
    dsn, runtime_session_id, _context, event_log = _postgres_log_or_skip(tmp_path)
    event_log.ensure_runtime_session_owner()
    lock_key = f"pulsara:checkpoint-maintenance:{runtime_session_id}"
    owner = psycopg.connect(dsn)
    authority = PostgresCheckpointMaintenanceAuthority(dsn)
    try:
        with owner.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
        with pytest.raises(CheckpointMaintenanceLockUnavailable):
            with authority.acquire_shared(runtime_session_id):
                raise AssertionError("unreachable")
        owner.close()
        with authority.acquire_shared(runtime_session_id) as permit:
            assert permit.exclusive is False
    finally:
        owner.close()
        _cleanup_postgres_session(dsn, runtime_session_id)


def test_checkpoint_maintenance_shared_readers_exclude_privileged_writer() -> None:
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda _runtime_session_id: True
    )

    with authority.acquire_shared(RUNTIME_ID) as first:
        with authority.acquire_shared(RUNTIME_ID) as second:
            assert first.exclusive is False
            assert second.exclusive is False
            with pytest.raises(CheckpointMaintenanceLockUnavailable):
                with authority.acquire_exclusive(RUNTIME_ID):
                    raise AssertionError("unreachable")

    with authority.acquire_exclusive(RUNTIME_ID):
        with pytest.raises(CheckpointMaintenanceLockUnavailable):
            with authority.acquire_shared(RUNTIME_ID):
                raise AssertionError("unreachable")


def test_checkpoint_reader_holds_shared_lock_across_ledger_and_artifact(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:shared-read:one"))
    prepared, _stored = _materialize_checkpoint(runtime, 1)
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda _runtime_session_id: True
    )

    class SharedGuardArchive(InMemoryArchiveStore):
        def get_text(self, *args, **kwargs):
            with pytest.raises(CheckpointMaintenanceLockUnavailable):
                with authority.acquire_exclusive(RUNTIME_ID):
                    raise AssertionError("unreachable")
            return super().get_text(*args, **kwargs)

    guarded_archive = SharedGuardArchive()
    guarded_archive.blobs = runtime.archive.blobs
    result = EventLogSubagentGraphCheckpointReadPort(
        event_log=runtime.event_log,
        archive=guarded_archive,
        runtime_session_id=RUNTIME_ID,
        maintenance_authority=authority,
    ).read_checkpoint_and_delta_snapshot(
        requested_through_sequence=1,
        reducer_contract=(
            runtime.subagent_graph_checkpoint_service.reducer_binding.contract
        ),
        preferred_checkpoint_id=prepared.checkpoint.checkpoint_id,
        max_delta_events=16,
        max_delta_bytes=1_000_000,
        max_checkpoint_candidates=4,
    )

    assert not hasattr(result, "reason_code")
    assert result.selected_checkpoint_id == prepared.checkpoint.checkpoint_id


def test_inspector_checkpoint_projection_is_bounded_and_schema_aware(
    tmp_path,
) -> None:
    from pulsara_agent.inspector.service import (
        _subagent_graph_checkpoint_projection,
    )

    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:inspect-checkpoint:one"))
    prepared, stored = _materialize_checkpoint(runtime, 1)

    projection = _subagent_graph_checkpoint_projection(runtime.event_log.iter())

    assert projection["status"] == "available"
    assert projection["confirmed_checkpoint_count"] == 1
    assert projection["truncated"] is False
    assert projection["checkpoints"] == [
        {
            "event_id": stored.id,
            "event_sequence": stored.sequence,
            "checkpoint_id": prepared.checkpoint.checkpoint_id,
            "through_sequence": 1,
            "artifact_id": prepared.artifact.artifact_id,
            "artifact_content_sha256": prepared.artifact.content_sha256,
            "graph_reducer_id": prepared.checkpoint.graph_reducer_id,
            "graph_reducer_version": prepared.checkpoint.graph_reducer_version,
            "graph_reducer_contract_fingerprint": (
                prepared.checkpoint.graph_reducer_contract_fingerprint
            ),
            "graph_event_count": 0,
            "graph_state_semantic_fingerprint": (
                prepared.checkpoint.graph_state_semantic_fingerprint
            ),
            "writer_status": "committed",
        }
    ]


def test_compiler_and_live_replay_cannot_import_or_call_offline_repair() -> None:
    root = Path(__file__).parents[1] / "src" / "pulsara_agent" / "runtime"
    production_files = (
        root / "context_input" / "live.py",
        root / "context_input" / "replay.py",
        root / "agent.py",
    )
    forbidden = {
        "checkpoint_doctor",
        "verify_or_rebuild_subagent_graph_checkpoint",
    }
    for path in production_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        }
        modules = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not (names & forbidden), path
        assert not any("checkpoint_doctor" in module for module in modules), path


def test_checkpoint_falls_back_to_earlier_contract_compatible_checkpoint(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_task("event:fallback:task"))
    earlier, _ = _materialize_checkpoint(runtime, 1)
    runtime.event_log.append(_non_graph("event:fallback:delta"))
    newer, _ = _materialize_checkpoint(runtime, 3)
    runtime.archive.blobs.pop(newer.artifact.artifact_id)

    snapshot = _read(
        runtime,
        through_sequence=3,
        preferred=newer.checkpoint.checkpoint_id,
    )

    assert snapshot.selected_checkpoint_id == earlier.checkpoint.checkpoint_id
    assert snapshot.rebased is True
    restored, semantic, _acceleration = restore_subagent_graph_from_checkpoint(
        snapshot=snapshot,
        reducer_binding=runtime.subagent_graph_checkpoint_service.reducer_binding,
    )
    assert tuple(restored.tasks) == ("task:checkpoint",)
    assert semantic.graph_event_count == 1


def test_original_checkpoint_missing_rebase_can_remain_exact_replay(
    tmp_path,
) -> None:
    test_checkpoint_falls_back_to_earlier_contract_compatible_checkpoint(tmp_path)


def test_checkpoint_materialized_after_manifest_can_accelerate_historical_exact_replay(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_task("event:historical:task"))
    prepared, stored = _materialize_checkpoint(runtime, 1)

    restored, semantic, acceleration = _restore(runtime, through_sequence=1)

    assert stored.sequence == 2
    assert acceleration.ledger_through_sequence == 1
    assert acceleration.checkpoint_materialization_event_id == stored.id
    assert acceleration.checkpoint_id == prepared.checkpoint.checkpoint_id
    assert tuple(restored.tasks) == ("task:checkpoint",)
    assert semantic.graph_event_count == 1


def test_rebase_compares_semantic_source_graph_selection_and_candidates(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_task("event:rebase:task"))
    earlier, _ = _materialize_checkpoint(runtime, 1)
    runtime.event_log.append(_non_graph("event:rebase:delta"))
    newer, _ = _materialize_checkpoint(runtime, 3)
    preferred_snapshot = _read(
        runtime,
        through_sequence=3,
        preferred=newer.checkpoint.checkpoint_id,
    )
    preferred_graph, preferred_semantic, _preferred_acceleration = (
        restore_subagent_graph_from_checkpoint(
            snapshot=preferred_snapshot,
            reducer_binding=(
                runtime.subagent_graph_checkpoint_service.reducer_binding
            ),
        )
    )
    runtime.archive.blobs.pop(newer.artifact.artifact_id)

    rebased_snapshot = _read(
        runtime,
        through_sequence=3,
        preferred=newer.checkpoint.checkpoint_id,
    )
    rebased_graph, rebased_semantic, rebased_acceleration = (
        restore_subagent_graph_from_checkpoint(
            snapshot=rebased_snapshot,
            reducer_binding=(
                runtime.subagent_graph_checkpoint_service.reducer_binding
            ),
        )
    )
    policy = resolve_context_compile_policy(LoopBudget()).candidate_collection
    preferred_selection = build_context_candidate_source_selections(
        subagent_graph=preferred_graph,
        semantic_source=preferred_semantic,
        policy=policy,
    )
    rebased_selection = build_context_candidate_source_selections(
        subagent_graph=rebased_graph,
        semantic_source=rebased_semantic,
        policy=policy,
    )

    assert rebased_snapshot.selected_checkpoint_id == earlier.checkpoint.checkpoint_id
    assert rebased_acceleration.checkpoint_id == earlier.checkpoint.checkpoint_id
    assert rebased_graph == preferred_graph
    assert rebased_semantic == preferred_semantic
    assert rebased_selection == preferred_selection


def test_readable_preferred_checkpoint_with_oversized_delta_uses_newer_compatible_checkpoint(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_task("event:preferred:task"))
    preferred, _ = _materialize_checkpoint(runtime, 1)
    runtime.event_log.extend(
        (
            _non_graph("event:preferred:three"),
            _non_graph("event:preferred:four"),
        )
    )
    newer, _ = _materialize_checkpoint(runtime, 4)
    runtime.event_log.append(_non_graph("event:preferred:six"))
    service = runtime.subagent_graph_checkpoint_service

    snapshot = EventLogSubagentGraphCheckpointReadPort(
        event_log=runtime.event_log,
        archive=runtime.archive,
        runtime_session_id=runtime.runtime_session_id,
    ).read_checkpoint_and_delta_snapshot(
        requested_through_sequence=6,
        reducer_contract=service.reducer_binding.contract,
        preferred_checkpoint_id=preferred.checkpoint.checkpoint_id,
        max_delta_events=2,
        max_delta_bytes=service.policy.checkpoint_max_delta_bytes,
        max_checkpoint_candidates=service.policy.rebase_max_checkpoint_candidates,
    )

    assert not hasattr(snapshot, "reason_code")
    assert snapshot.selected_checkpoint_id == newer.checkpoint.checkpoint_id
    assert snapshot.rebased is True
    assert len(snapshot.delta_events) == 2


def test_checkpoint_unknown_keeps_physical_owner(
    tmp_path, monkeypatch
) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:unknown:one"))

    async def uncertain_write(_self, _event, **_kwargs):
        raise EventCommitError(
            "confirmation unavailable",
            commit_outcome="unknown",
        )

    monkeypatch.setattr(RuntimeSession, "write_event", uncertain_write)

    with pytest.raises(EventCommitError, match="confirmation unavailable"):
        asyncio.run(
            runtime.subagent_graph_checkpoint_service.restore_for_selection(
                requested_through_sequence=1
            )
        )

    assert runtime.subagent_graph_checkpoint_service.write_states()[1] == "unknown"
    assert runtime.ledger_reconciliation_required is True
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        runtime.subagent_graph_checkpoint_service.close_if_idle()


def test_checkpoint_close_drains_blocking_postgres_operation(
    tmp_path,
) -> None:
    started = Event()
    release = Event()

    class BlockingArchive(InMemoryArchiveStore):
        def put_text_if_absent_or_confirm_identical(self, *args, **kwargs):
            started.set()
            release.wait(timeout=5)
            return super().put_text_if_absent_or_confirm_identical(*args, **kwargs)

    archive = BlockingArchive()
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=RUNTIME_ID,
        archive=archive,
    )
    runtime.event_log.append(_non_graph("event:blocking:one"))

    async def exercise() -> None:
        owner = asyncio.create_task(
            runtime.subagent_graph_checkpoint_service.restore_for_selection(
                requested_through_sequence=1
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        with pytest.raises(TimeoutError, match="drain timed out"):
            await runtime.subagent_graph_checkpoint_service.drain_pending(
                deadline_monotonic=monotonic() + 0.02
            )
        release.set()
        await owner
        await runtime.subagent_graph_checkpoint_service.drain_pending(
            deadline_monotonic=monotonic() + 1
        )

    asyncio.run(exercise())


def test_checkpoint_gc_does_not_pin_historical_manifest_artifact(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:gc:one"))
    oldest, _ = _materialize_checkpoint(runtime, 1)
    runtime.event_log.append(_non_graph("event:gc:three"))
    middle, _ = _materialize_checkpoint(runtime, 3)
    runtime.event_log.append(_non_graph("event:gc:five"))
    newest, _ = _materialize_checkpoint(runtime, 5)
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda runtime_session_id: runtime_session_id == RUNTIME_ID
    )

    report = garbage_collect_subagent_graph_checkpoint_artifacts(
        runtime_session_id=RUNTIME_ID,
        event_log=runtime.event_log,
        archive=runtime.archive,
        maintenance_authority=authority,
        retained_checkpoint_min_count=2,
    )

    assert report.catalog_event_count == 3
    assert report.retained_checkpoint_ids == (
        newest.checkpoint.checkpoint_id,
        middle.checkpoint.checkpoint_id,
    )
    assert report.deleted_checkpoint_ids == (oldest.checkpoint.checkpoint_id,)
    assert oldest.artifact.artifact_id not in runtime.archive.blobs
    assert sum(
        isinstance(event, SubagentGraphCheckpointCommittedEvent)
        for event in runtime.event_log.iter()
    ) == 3


def test_checkpoint_gc_identity_mismatch_fails_closed(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path, runtime_session_id=RUNTIME_ID)
    runtime.event_log.append(_non_graph("event:gc-conflict:one"))
    stale, _ = _materialize_checkpoint(runtime, 1)
    runtime.event_log.append(_non_graph("event:gc-conflict:three"))
    _latest, _ = _materialize_checkpoint(runtime, 3)
    runtime.archive.blobs[stale.artifact.artifact_id].metadata[
        "semantic_metadata_fingerprint"
    ] = "sha256:drift"
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda _runtime_session_id: True
    )

    with pytest.raises(ArtifactContentConflict, match="maintenance identity mismatch"):
        garbage_collect_subagent_graph_checkpoint_artifacts(
            runtime_session_id=RUNTIME_ID,
            event_log=runtime.event_log,
            archive=runtime.archive,
            maintenance_authority=authority,
            retained_checkpoint_min_count=1,
        )
    assert stale.artifact.artifact_id in runtime.archive.blobs


def test_postgres_raw_snapshot_returns_schema_envelope_without_current_union_decode(
    tmp_path,
) -> None:
    dsn, runtime_session_id, context, log = _postgres_log_or_skip(tmp_path)
    try:
        stored = log.append(
            TextBlockDeltaEvent(
                **context.event_fields(),
                block_id="text:legacy-row",
                delta="legacy",
            )
        )
        payload = dump_agent_event(stored)
        payload["type"] = "LEGACY_ONLY_EVENT"
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update agent_events
                    set event_type = %s,
                        event_schema_version = %s,
                        event_schema_fingerprint = %s,
                        event_domain_contract_fingerprint = %s,
                        payload = %s::jsonb
                    where id = %s
                    """,
                    (
                        "LEGACY_ONLY_EVENT",
                        "agent-event:legacy-only:v0",
                        "sha256:legacy-schema",
                        "sha256:legacy-domain",
                        json.dumps(payload),
                        stored.id,
                    ),
                )

        raw = log.read_raw_range_snapshot(minimum_sequence=1)

        assert raw.through_sequence == 1
        assert raw.events[0].event_type == "LEGACY_ONLY_EVENT"
        assert raw.events[0].canonical_payload_bytes == canonical_json_bytes(payload)
        with pytest.raises(EventSchemaContractMismatch):
            raw.events[0].decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    finally:
        _cleanup_postgres_session(dsn, runtime_session_id)


def test_row_without_explicit_per_event_schema_identity_fails_closed(
    tmp_path,
) -> None:
    dsn, runtime_session_id, context, log = _postgres_log_or_skip(tmp_path)
    try:
        stored = log.append(
            TextBlockDeltaEvent(
                **context.event_fields(),
                block_id="text:missing-schema",
                delta="schema",
            )
        )
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where id = %s
                    """,
                    (stored.id,),
                )
                row = dict(cursor.fetchone())
        row["event_schema_fingerprint"] = None

        with pytest.raises(
            EventSchemaContractMismatch,
            match="lacks explicit per-event schema identity",
        ):
            log._raw_from_row(row)
    finally:
        _cleanup_postgres_session(dsn, runtime_session_id)


def test_confirm_batch_compares_raw_per_event_schema_identity_and_payload_without_current_union(
    tmp_path,
) -> None:
    dsn, runtime_session_id, context, log = _postgres_log_or_skip(tmp_path)
    candidate = TextBlockDeltaEvent(
        **context.event_fields(),
        block_id="text:confirm-schema",
        delta="stable",
    )
    try:
        log.append(candidate)
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update agent_events
                    set event_schema_fingerprint = %s
                    where id = %s
                    """,
                    ("sha256:drift", candidate.id),
                )

        with pytest.raises(EventIdConflict):
            log.confirm_batch((candidate,))
    finally:
        _cleanup_postgres_session(dsn, runtime_session_id)


def test_checkpoint_delta_snapshot_is_one_database_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    dsn, runtime_session_id, context, log = _postgres_log_or_skip(tmp_path)
    task = SubagentTaskCreatedEvent(
        **context.event_fields(),
        task_id=f"task:{uuid4().hex}",
        task_key="checkpoint-race",
        profile_id="research_worker",
        objective_preview="checkpoint race",
        objective_artifact_id=f"artifact:{uuid4().hex}",
    )
    try:
        log.append(task)
        log.append(
            TextBlockDeltaEvent(
                **context.event_fields(),
                block_id="text:checkpoint-race",
                delta="padding",
            )
        )
        binding = build_default_subagent_graph_reducer_binding()
        prepared = prepare_subagent_graph_checkpoint(
            runtime_session_id=runtime_session_id,
            prefix_events=log.read_raw_range_snapshot(
                minimum_sequence=1,
                through_sequence=2,
            ).events,
            reducer_binding=binding,
        )

        high_water_read = Event()
        late_append_finished = Event()
        pause_claimed = Event()
        worker_errors: list[BaseException] = []
        real_connection = postgres_event_log_module.postgres_event_connection

        class CursorProxy:
            def __init__(self, cursor) -> None:
                self._cursor = cursor
                self._last_query = ""

            def __enter__(self):
                self._cursor.__enter__()
                return self

            def __exit__(self, *args):
                return self._cursor.__exit__(*args)

            def execute(self, query, params=None):
                self._last_query = str(query)
                self._cursor.execute(query, params)
                return self

            def fetchone(self):
                row = self._cursor.fetchone()
                if (
                    "as high_water" in self._last_query
                    and not pause_claimed.is_set()
                ):
                    pause_claimed.set()
                    high_water_read.set()
                    if not late_append_finished.wait(timeout=5):
                        raise TimeoutError("late checkpoint append did not finish")
                return row

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class ConnectionProxy:
            def __init__(self, connection) -> None:
                self._connection = connection

            def __enter__(self):
                self._connection.__enter__()
                return self

            def __exit__(self, *args):
                return self._connection.__exit__(*args)

            def cursor(self, *args, **kwargs):
                return CursorProxy(self._connection.cursor(*args, **kwargs))

            def __getattr__(self, name):
                return getattr(self._connection, name)

        @contextmanager
        def delayed_connection(*args, **kwargs):
            with real_connection(*args, **kwargs) as connection:
                yield ConnectionProxy(connection)

        monkeypatch.setattr(
            postgres_event_log_module,
            "postgres_event_connection",
            delayed_connection,
        )

        def append_late_checkpoint() -> None:
            if not high_water_read.wait(timeout=5):
                worker_errors.append(
                    TimeoutError("checkpoint reader did not reach high-water")
                )
                late_append_finished.set()
                return
            try:
                log.append(prepared.event)
            except BaseException as exc:  # pragma: no cover - asserted below
                worker_errors.append(exc)
            finally:
                late_append_finished.set()

        worker = Thread(target=append_late_checkpoint, daemon=True)
        worker.start()
        snapshot = log.read_raw_checkpoint_ledger_snapshot(
            checkpoint_event_type=str(
                EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED
            ),
            requested_through_sequence=2,
            graph_reducer_id=binding.contract.graph_reducer_id,
            graph_reducer_version=binding.contract.graph_reducer_version,
            graph_reducer_contract_fingerprint=(
                binding.contract.graph_reducer_contract_fingerprint
            ),
            preferred_checkpoint_id=None,
            max_delta_events=16,
            max_delta_bytes=1_000_000,
            max_checkpoint_candidates=4,
        )
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert worker_errors == []
        assert snapshot.ledger_high_water_observed == 2
        assert snapshot.confirmed_checkpoint_count == 0
        assert snapshot.candidates == ()
        assert log.get_by_id(prepared.event.id).sequence == 3
    finally:
        _cleanup_postgres_session(dsn, runtime_session_id)
