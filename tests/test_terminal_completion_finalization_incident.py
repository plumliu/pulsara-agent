from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pulsara_agent.event import (
    EventContext,
    ReplyStartEvent,
    RunErrorEvent,
    TerminalProcessObservationDeliveryDispositionEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.event_log.serialization import stable_event_identity
from pulsara_agent.ports.event_write import (
    CommittedCheckpointHandoff,
    CommittedSemanticFoldSettlement,
    EventCommitError,
    EventReconciliationRequired,
)
from pulsara_agent.ports.run_execution import (
    build_prepared_run_owner_reservation_key,
    build_run_owner_identity,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives import PhysicalOperationKind
from pulsara_agent.primitives.stored_event import (
    RawRuntimeProjectionCheckpoint,
    build_raw_runtime_projection_checkpoint,
    canonical_json_object_carrier,
)
from pulsara_agent.runtime.execution_handles import RunExecutionHandleSet
from pulsara_agent.runtime.committed_reducer_repair import (
    CommittedReducerRepairService,
    OnlineReducerRepairBoundExceeded,
    build_committed_reducer_repair_receipt,
)
from pulsara_agent.runtime.committed_reducer_post_fold import (
    CommittedReducerPostFoldService,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.projection_checkpoint_maintenance import (
    CHECKPOINT_RECOVERY_HARD_BYTES,
    CHECKPOINT_RECOVERY_HARD_EVENTS,
    CHECKPOINT_RECOVERY_SOFT_EVENTS,
    RuntimeProjectionCheckpointAdmissionBlocked,
    RuntimeProjectionCheckpointMaintenanceService,
    RuntimeProjectionRecoveryBoundExceeded,
    build_committed_reducer_fold_receipt,
    read_bounded_runtime_projection_recovery_delta,
)
from pulsara_agent.runtime import (
    projection_checkpoint_maintenance as checkpoint_maintenance_module,
)
from pulsara_agent.runtime.authority_materialization import (
    build_default_authority_materialization_contract_bundle,
)
from pulsara_agent.runtime.run_execution.finalization import RunFinalizationService
from pulsara_agent.runtime.run_execution.service import RunActivationService
from pulsara_agent.runtime.run_execution.owner import (
    BoundRunResources,
    NoActiveActivation,
    NoActiveSuspension,
    RunFinalizationOwner,
    RunFinalizationSlot,
    RunObserverRegistry,
    RunOwner,
    RunProgressState,
    RunRetiringResourceSet,
)
from pulsara_agent.runtime.run_execution.prepared import RunActivationStateCarrier
from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
from pulsara_agent.runtime.session import _repair_post_fold_events
from pulsara_agent.runtime.state import RunActivationWorkingState
from tests.support.model_stream import make_text_block_segment_event
from tests.support.event_write import committed_event_write_result_fixture
from tests.support.events import typed_non_transcript_event
from tests.support.runtime_session import in_memory_runtime_session
from tests.support.terminal_monitor import terminal_process_completed_event
from pulsara_agent.ports.event_write import classify_committed_event_settlement
from pulsara_agent.runtime.terminal.output import SanitizedOutputJournal
from pulsara_agent.runtime.terminal.monitor import _FiringOwner


CTX = EventContext(
    run_id="run:incident",
    turn_id="turn:incident",
    reply_id="reply:incident",
)


def _event(label: str):
    return make_text_block_segment_event(
        **CTX.event_fields(),
        block_id=f"text:{label}",
        delta=label,
    )


def _checkpoint(
    event_log: InMemoryEventLog,
    *,
    through_sequence: int,
    base_sequence: int,
    base_payload: dict[str, object],
    state_payload: dict[str, object],
) -> RawRuntimeProjectionCheckpoint:
    return build_raw_runtime_projection_checkpoint(
        projection_kind="incident_projection.v1",
        through_sequence=through_sequence,
        projection_schema_version="incident_projection_state.v1",
        ledger_prefix=event_log.read_raw_ledger_prefix(
            through_sequence=through_sequence
        ),
        validation_base_through_sequence=base_sequence,
        validation_base_state=canonical_json_object_carrier(base_payload),
        state=canonical_json_object_carrier(state_payload),
    )


def test_canonical_checkpoint_carrier_is_owned_and_shape_neutral() -> None:
    producer = {"items": ({"nested": (1, 2)},)}
    carrier = canonical_json_object_carrier(producer)
    equivalent = canonical_json_object_carrier({"items": [{"nested": [1, 2]}]})

    producer["items"] = ()

    assert carrier == equivalent
    assert carrier.decode_object() == {"items": [{"nested": [1, 2]}]}
    decoded = carrier.decode_object()
    decoded["items"] = []
    assert carrier.decode_object() == {"items": [{"nested": [1, 2]}]}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {1: "not-a-string-key"},
        {"nested": {1: "not-a-string-key"}},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": object()},
    ],
)
def test_canonical_checkpoint_carrier_rejects_non_json_objects(payload) -> None:
    with pytest.raises(ValueError):
        canonical_json_object_carrier(payload)


def test_in_memory_checkpoint_uses_canonical_json_compatible_winner() -> None:
    event_log = InMemoryEventLog(runtime_session_id="runtime:incident")
    genesis = _checkpoint(
        event_log,
        through_sequence=0,
        base_sequence=0,
        base_payload={"items": []},
        state_payload={"items": []},
    )
    event_log.write_runtime_projection_checkpoint(genesis)
    event_log.append(_event("one"))
    successor = _checkpoint(
        event_log,
        through_sequence=1,
        base_sequence=0,
        base_payload={"items": ()},
        state_payload={"items": ({"value": 1},)},
    )
    event_log.write_runtime_projection_checkpoint(successor)

    same_semantics = _checkpoint(
        event_log,
        through_sequence=1,
        base_sequence=0,
        base_payload={"items": []},
        state_payload={"items": [{"value": 1}]},
    )
    event_log.write_runtime_projection_checkpoint(same_semantics)

    assert (
        event_log.read_runtime_projection_checkpoint("incident_projection.v1")
        == same_semantics
    )
    incompatible = _checkpoint(
        event_log,
        through_sequence=1,
        base_sequence=0,
        base_payload={"items": []},
        state_payload={"items": [{"value": 2}]},
    )
    with pytest.raises(ValueError, match="conflicts"):
        event_log.write_runtime_projection_checkpoint(incompatible)


def test_generic_checkpoint_builder_covers_canonical_carriers() -> None:
    event_log = InMemoryEventLog(runtime_session_id="runtime:incident")
    trusted = _checkpoint(
        event_log,
        through_sequence=0,
        base_sequence=0,
        base_payload={},
        state_payload={},
    )
    rebuilt = _checkpoint(
        event_log,
        through_sequence=0,
        base_sequence=0,
        base_payload={},
        state_payload={},
    )
    assert trusted.payload_fingerprint == rebuilt.payload_fingerprint
    assert trusted.validation_base_state == rebuilt.validation_base_state
    assert trusted.state == rebuilt.state


def test_online_repair_reads_exact_contiguous_joined_pages(monkeypatch) -> None:
    event_log = InMemoryEventLog(runtime_session_id="runtime:joined-pages")
    for index in range(300):
        event_log.append(_event(f"joined-{index}"))
    calls: list[tuple[int, int, int]] = []
    event_log_type = type(event_log)
    original = event_log_type.read_joined_raw_range

    def recording_read(self, **kwargs):
        if self is event_log:
            calls.append(
                (
                    kwargs["from_sequence_exclusive"],
                    kwargs["through_sequence"],
                    kwargs["max_events"],
                )
            )
        return original(self, **kwargs)

    monkeypatch.setattr(event_log_type, "read_joined_raw_range", recording_read)
    events, event_count, payload_bytes = read_bounded_runtime_projection_recovery_delta(
        event_log,
        from_sequence_exclusive=0,
        through_sequence=300,
        deadline_monotonic=monotonic() + 2,
    )

    assert len(events) == event_count == 300
    assert payload_bytes > 0
    assert calls == [(0, 256, 256), (256, 300, 256)]


def test_online_repair_stops_before_crossing_total_event_bound(
    monkeypatch,
) -> None:
    event_log = InMemoryEventLog(runtime_session_id="runtime:joined-bound")
    for index in range(3):
        event_log.append(_event(f"bound-{index}"))
    monkeypatch.setattr(
        checkpoint_maintenance_module,
        "CHECKPOINT_RECOVERY_HARD_EVENTS",
        2,
    )

    with pytest.raises(RuntimeProjectionRecoveryBoundExceeded):
        read_bounded_runtime_projection_recovery_delta(
            event_log,
            from_sequence_exclusive=0,
            through_sequence=3,
            deadline_monotonic=monotonic() + 2,
        )


def test_accounted_completion_partition_exactly_joins_stored_receipt() -> None:
    completion = terminal_process_completed_event(
        event_context=CTX,
        process_id="process:accounted",
    ).model_copy(update={"sequence": 1})
    accounting = _event("accounting").model_copy(update={"sequence": 2})
    result = committed_event_write_result_fixture(
        (completion, accounting),
        runtime_session_id="runtime:accounted",
        accounting_events=(accounting,),
    )

    settlement = classify_committed_event_settlement(
        result,
        requested_event_ids=(completion.id,),
        required_reducer_ids=(),
        repair_owner_installed=lambda _reducer, _target: False,
        checkpoint_handoff_accepted=lambda _reducer, _target: False,
        require_publication=False,
    )

    assert result.committed_events == (completion,)
    assert result.accounting_events == (accounting,)
    assert result.stored_batch_receipt is not None
    assert tuple(
        item.event_id for item in result.stored_batch_receipt.raw_stored_envelopes
    ) == (completion.id, accounting.id)
    assert settlement.requested_event_references[0][0] == completion.id


class _FlakyCheckpointEventLog:
    def __init__(self, *, mode: str) -> None:
        self.inner = InMemoryEventLog(runtime_session_id=f"runtime:{mode}")
        self.mode = mode
        self.write_calls = 0
        self.read_calls = 0
        self.candidate_fingerprints: list[str] = []

    def append(self, event):
        return self.inner.append(event)

    def read_raw_ledger_prefix(self, **kwargs):
        return self.inner.read_raw_ledger_prefix(**kwargs)

    def write_runtime_projection_checkpoint(self, checkpoint, **kwargs) -> None:
        self.write_calls += 1
        self.candidate_fingerprints.append(checkpoint.payload_fingerprint)
        if self.mode == "none" and self.write_calls == 1:
            raise OSError("synthetic pre-commit NONE")
        if self.mode == "unknown" and self.write_calls == 1:
            self.inner.write_runtime_projection_checkpoint(checkpoint, **kwargs)
            raise TimeoutError("synthetic lost acknowledgement")
        self.inner.write_runtime_projection_checkpoint(checkpoint, **kwargs)

    def read_runtime_projection_checkpoint(self, projection_kind, **kwargs):
        self.read_calls += 1
        if self.mode == "unknown" and self.read_calls == 1:
            raise TimeoutError("synthetic confirmation timeout")
        return self.inner.read_runtime_projection_checkpoint(projection_kind, **kwargs)


class _BlockingCheckpointEventLog(_FlakyCheckpointEventLog):
    def __init__(self) -> None:
        super().__init__(mode="full")
        self.write_started = Event()
        self.release_write = Event()

    def write_runtime_projection_checkpoint(self, checkpoint, **kwargs) -> None:
        self.write_started.set()
        self.release_write.wait(timeout=5)
        super().write_runtime_projection_checkpoint(checkpoint, **kwargs)


class _BlockingCheckpointPrefixEventLog(_FlakyCheckpointEventLog):
    def __init__(self) -> None:
        super().__init__(mode="full")
        self.prefix_started = Event()
        self.release_prefix = Event()
        self._block_once = True

    def read_raw_ledger_prefix(self, **kwargs):
        if self._block_once:
            self._block_once = False
            self.prefix_started.set()
            self.release_prefix.wait(timeout=5)
        return super().read_raw_ledger_prefix(**kwargs)


class _ConflictingCheckpointEventLog(_FlakyCheckpointEventLog):
    def __init__(self) -> None:
        super().__init__(mode="conflict")
        self.observed: RawRuntimeProjectionCheckpoint | None = None

    def write_runtime_projection_checkpoint(self, checkpoint, **kwargs) -> None:
        del kwargs
        self.write_calls += 1
        self.candidate_fingerprints.append(checkpoint.payload_fingerprint)
        self.observed = replace(
            checkpoint,
            state=canonical_json_object_carrier({"conflicting": True}),
            payload_fingerprint="sha256:" + "e" * 64,
        )
        raise ValueError("synthetic incompatible checkpoint winner")

    def read_runtime_projection_checkpoint(self, projection_kind, **kwargs):
        del projection_kind, kwargs
        self.read_calls += 1
        return self.observed


async def _wait_checkpoint_clean(
    service: RuntimeProjectionCheckpointMaintenanceService,
    reducer_id: str,
) -> dict[str, object]:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        diagnostic = service.diagnostics(reducer_id)
        if diagnostic["state"] == "clean":
            return diagnostic
        await asyncio.sleep(0.01)
    raise AssertionError(service.diagnostics(reducer_id))


@pytest.mark.parametrize(
    ("mode", "expected_writes"),
    [("full", 1), ("none", 2), ("unknown", 1)],
)
def test_checkpoint_owner_preserves_stable_candidate_across_outcomes(
    mode: str,
    expected_writes: int,
) -> None:
    async def scenario() -> None:
        event_log = _FlakyCheckpointEventLog(mode=mode)
        event_log.append(_event(mode))
        base = canonical_json_object_carrier({"through": 0, "items": []})
        resulting = canonical_json_object_carrier({"through": 1, "items": [mode]})
        receipt = build_committed_reducer_fold_receipt(
            reducer_id=f"reducer:{mode}",
            base_through_sequence=0,
            resulting_through_sequence=1,
            source_kind="live_batch",
            source_ordered_join_fingerprint=context_fingerprint(
                "test-fold-source:v1", {"mode": mode}
            ),
            base_state=base,
            resulting_state=resulting,
        )
        service = RuntimeProjectionCheckpointMaintenanceService(
            runtime_session_id=f"runtime:{mode}",
            event_log=event_log,
        )
        service.register_projection(
            reducer_id=f"reducer:{mode}",
            projection_kind="incident_projection.v1",
            projection_schema_version="incident_projection_state.v1",
            confirmed_head=None,
            genesis_state=base,
            current_through_sequence=0,
            current_state=base,
        )
        service.bind_running_loop()
        service.offer(receipt)
        await _wait_checkpoint_clean(service, f"reducer:{mode}")
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

        assert event_log.write_calls == expected_writes
        assert len(set(event_log.candidate_fingerprints)) == 1
        if mode == "unknown":
            assert event_log.read_calls >= 2

    asyncio.run(scenario())


def test_checkpoint_owner_coalesces_successor_without_mutating_inflight_candidate() -> (
    None
):
    async def scenario() -> None:
        event_log = _BlockingCheckpointEventLog()
        event_log.append(_event("candidate-one"))
        event_log.append(_event("candidate-two"))
        state0 = canonical_json_object_carrier({"through": 0})
        state1 = canonical_json_object_carrier({"through": 1})
        state2 = canonical_json_object_carrier({"through": 2})
        service = RuntimeProjectionCheckpointMaintenanceService(
            runtime_session_id="runtime:coalesce",
            event_log=event_log,
        )
        service.register_projection(
            reducer_id="reducer:coalesce",
            projection_kind="incident_projection.v1",
            projection_schema_version="incident_projection_state.v1",
            confirmed_head=None,
            genesis_state=state0,
            current_through_sequence=0,
            current_state=state0,
        )
        service.bind_running_loop()
        first = build_committed_reducer_fold_receipt(
            reducer_id="reducer:coalesce",
            base_through_sequence=0,
            resulting_through_sequence=1,
            source_kind="live_batch",
            source_ordered_join_fingerprint=context_fingerprint(
                "test-fold-source:v1", {"target": 1}
            ),
            base_state=state0,
            resulting_state=state1,
        )
        second = build_committed_reducer_fold_receipt(
            reducer_id="reducer:coalesce",
            base_through_sequence=1,
            resulting_through_sequence=2,
            source_kind="live_batch",
            source_ordered_join_fingerprint=context_fingerprint(
                "test-fold-source:v1", {"target": 2}
            ),
            base_state=state1,
            resulting_state=state2,
        )
        service.offer(first)
        deadline = monotonic() + 1
        while not event_log.write_started.is_set() and monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert event_log.write_started.is_set()
        inflight = service.diagnostics("reducer:coalesce")["candidate_fingerprint"]
        service.offer(second)
        assert (
            service.diagnostics("reducer:coalesce")["candidate_fingerprint"] == inflight
        )
        event_log.release_write.set()
        diagnostic = await _wait_checkpoint_clean(service, "reducer:coalesce")
        assert diagnostic["confirmed_through_sequence"] == 2
        assert event_log.write_calls == 2
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_checkpoint_prefix_io_does_not_block_successor_handoff() -> None:
    async def scenario() -> None:
        event_log = _BlockingCheckpointPrefixEventLog()
        event_log.append(_event("prefix-one"))
        event_log.append(_event("prefix-two"))
        state0 = canonical_json_object_carrier({"through": 0})
        state1 = canonical_json_object_carrier({"through": 1})
        state2 = canonical_json_object_carrier({"through": 2})
        service = RuntimeProjectionCheckpointMaintenanceService(
            runtime_session_id="runtime:prefix-lock",
            event_log=event_log,
        )
        service.register_projection(
            reducer_id="reducer:prefix-lock",
            projection_kind="incident_projection.v1",
            projection_schema_version="incident_projection_state.v1",
            confirmed_head=None,
            genesis_state=state0,
            current_through_sequence=0,
            current_state=state0,
        )
        first = build_committed_reducer_fold_receipt(
            reducer_id="reducer:prefix-lock",
            base_through_sequence=0,
            resulting_through_sequence=1,
            source_kind="live_batch",
            source_ordered_join_fingerprint=context_fingerprint(
                "test-fold-source:v1", {"prefix": 1}
            ),
            base_state=state0,
            resulting_state=state1,
        )
        second = build_committed_reducer_fold_receipt(
            reducer_id="reducer:prefix-lock",
            base_through_sequence=1,
            resulting_through_sequence=2,
            source_kind="live_batch",
            source_ordered_join_fingerprint=context_fingerprint(
                "test-fold-source:v1", {"prefix": 2}
            ),
            base_state=state1,
            resulting_state=state2,
        )
        service.bind_running_loop()
        service.offer(first)
        deadline = monotonic() + 1
        while not event_log.prefix_started.is_set() and monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert event_log.prefix_started.is_set()

        # This is the Runtime writer-facing handoff.  It must not wait for the
        # maintenance owner's blocked storage read.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(service.offer, second), timeout=0.2
            )
        finally:
            event_log.release_prefix.set()
        diagnostic = await _wait_checkpoint_clean(service, "reducer:prefix-lock")
        assert diagnostic["confirmed_through_sequence"] == 2
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_checkpoint_owner_rebinds_after_short_lived_loop_during_write() -> None:
    event_log = _BlockingCheckpointEventLog()
    event_log.append(_event("loop-handoff"))
    state0 = canonical_json_object_carrier({"through": 0})
    state1 = canonical_json_object_carrier({"through": 1})
    service = RuntimeProjectionCheckpointMaintenanceService(
        runtime_session_id="runtime:loop-handoff",
        event_log=event_log,
    )
    service.register_projection(
        reducer_id="reducer:loop-handoff",
        projection_kind="incident_projection.v1",
        projection_schema_version="incident_projection_state.v1",
        confirmed_head=None,
        genesis_state=state0,
        current_through_sequence=0,
        current_state=state0,
    )
    receipt = build_committed_reducer_fold_receipt(
        reducer_id="reducer:loop-handoff",
        base_through_sequence=0,
        resulting_through_sequence=1,
        source_kind="live_batch",
        source_ordered_join_fingerprint=context_fingerprint(
            "test-fold-source:v1", {"loop": 1}
        ),
        base_state=state0,
        resulting_state=state1,
    )

    async def start_then_detach() -> None:
        service.bind_running_loop()
        service.offer(receipt)
        deadline = monotonic() + 1
        while not event_log.write_started.is_set() and monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert event_log.write_started.is_set()

    asyncio.run(start_then_detach())
    event_log.release_write.set()

    async def rebind_and_confirm() -> None:
        service.bind_running_loop()
        diagnostic = await _wait_checkpoint_clean(service, "reducer:loop-handoff")
        assert diagnostic["confirmed_through_sequence"] == 1
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

    asyncio.run(rebind_and_confirm())


def test_post_fold_owner_rebinds_pending_handoff_after_loop_teardown() -> None:
    started = Event()
    release = Event()
    calls: list[tuple[str, ...]] = []

    def callback(events) -> None:
        calls.append(tuple(event.id for event in events))
        if len(calls) == 1:
            raise OSError("install process-owner retry")
        if len(calls) == 2:
            started.set()
            release.wait(timeout=5)

    service = CommittedReducerPostFoldService()
    service.register(reducer_id="reducer:post-fold-loop", callback=callback)
    stored = _event("post-fold-loop").model_copy(update={"sequence": 1})

    async def start_then_detach() -> None:
        service.bind_running_loop()
        service.handoff(
            reducer_id="reducer:post-fold-loop",
            events=(stored,),
        )
        deadline = monotonic() + 1
        while not started.is_set() and monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert started.is_set()

    asyncio.run(start_then_detach())
    release.set()

    async def rebind_and_drain() -> None:
        await service.drain_pending(deadline_monotonic=monotonic() + 2)
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

    asyncio.run(rebind_and_drain())
    assert len(calls) == 3
    assert calls[0] == calls[1] == calls[2] == (stored.id,)


def test_checkpoint_close_reports_blocked_until_physical_io_exits() -> None:
    async def scenario() -> None:
        event_log = _BlockingCheckpointEventLog()
        event_log.append(_event("close"))
        base = canonical_json_object_carrier({"through": 0})
        resulting = canonical_json_object_carrier({"through": 1})
        service = RuntimeProjectionCheckpointMaintenanceService(
            runtime_session_id="runtime:close",
            event_log=event_log,
        )
        service.register_projection(
            reducer_id="reducer:close",
            projection_kind="incident_projection.v1",
            projection_schema_version="incident_projection_state.v1",
            confirmed_head=None,
            genesis_state=base,
            current_through_sequence=0,
            current_state=base,
        )
        service.bind_running_loop()
        service.offer(
            build_committed_reducer_fold_receipt(
                reducer_id="reducer:close",
                base_through_sequence=0,
                resulting_through_sequence=1,
                source_kind="live_batch",
                source_ordered_join_fingerprint=context_fingerprint(
                    "test-fold-source:v1", {"target": "close"}
                ),
                base_state=base,
                resulting_state=resulting,
            )
        )
        deadline = monotonic() + 1
        while not event_log.write_started.is_set() and monotonic() < deadline:
            await asyncio.sleep(0.005)
        with pytest.raises(TimeoutError, match="close blocked"):
            await service.stop_admission_and_drain(
                deadline_monotonic=monotonic() + 0.02
            )
        assert service.diagnostics("reducer:close")["state"] == "close_blocked"
        event_log.release_write.set()
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)
        assert service.diagnostics("reducer:close")["state"] == "closed"

    asyncio.run(scenario())


def test_checkpoint_conflict_is_terminal_for_candidate_and_blocks_close() -> None:
    async def scenario() -> None:
        event_log = _ConflictingCheckpointEventLog()
        event_log.append(_event("conflict"))
        base = canonical_json_object_carrier({"through": 0})
        resulting = canonical_json_object_carrier({"through": 1})
        service = RuntimeProjectionCheckpointMaintenanceService(
            runtime_session_id="runtime:conflict",
            event_log=event_log,
        )
        service.register_projection(
            reducer_id="reducer:conflict",
            projection_kind="incident_projection.v1",
            projection_schema_version="incident_projection_state.v1",
            confirmed_head=None,
            genesis_state=base,
            current_through_sequence=0,
            current_state=base,
        )
        service.bind_running_loop()
        service.offer(
            build_committed_reducer_fold_receipt(
                reducer_id="reducer:conflict",
                base_through_sequence=0,
                resulting_through_sequence=1,
                source_kind="live_batch",
                source_ordered_join_fingerprint=context_fingerprint(
                    "test-fold-source:v1", {"target": "conflict"}
                ),
                base_state=base,
                resulting_state=resulting,
            )
        )
        deadline = monotonic() + 1
        while monotonic() < deadline:
            if service.diagnostics("reducer:conflict")["state"] == (
                "reconciliation_required"
            ):
                break
            await asyncio.sleep(0.005)
        assert service.diagnostics("reducer:conflict")["state"] == (
            "reconciliation_required"
        )
        await asyncio.sleep(0.05)
        assert event_log.write_calls == 1
        with pytest.raises(TimeoutError, match="close blocked"):
            await service.stop_admission_and_drain(
                deadline_monotonic=monotonic() + 0.02
            )

    asyncio.run(scenario())


def test_checkpoint_lag_soft_pressure_and_hard_admission_cover_physical_suffix() -> (
    None
):
    event_log = InMemoryEventLog(runtime_session_id="runtime:lag")
    relevant_event = _event("lag-relevant")
    irrelevant_event = ReplyStartEvent(
        id="reply-start:lag-irrelevant",
        **CTX.event_fields(),
        name="assistant",
    )
    base = canonical_json_object_carrier({"through": 0})
    resulting = canonical_json_object_carrier({"through": 1})
    service = RuntimeProjectionCheckpointMaintenanceService(
        runtime_session_id="runtime:lag",
        event_log=event_log,
    )
    service.register_projection(
        reducer_id="reducer:lag",
        projection_kind="incident_projection.v1",
        projection_schema_version="incident_projection_state.v1",
        confirmed_head=None,
        genesis_state=base,
        current_through_sequence=0,
        current_state=base,
        relevant_event_types=(str(relevant_event.type),),
    )
    service.offer(
        build_committed_reducer_fold_receipt(
            reducer_id="reducer:lag",
            base_through_sequence=0,
            resulting_through_sequence=1,
            source_kind="live_batch",
            source_ordered_join_fingerprint=context_fingerprint(
                "test-fold-source:v1", {"target": "lag"}
            ),
            base_state=base,
            resulting_state=resulting,
            checkpoint_requested=False,
            checkpoint_delta_event_count=CHECKPOINT_RECOVERY_SOFT_EVENTS,
            checkpoint_delta_payload_bytes=1,
        )
    )
    assert service.diagnostics("reducer:lag")["soft_pressure"] is True

    owner = service._owners["reducer:lag"]  # noqa: SLF001 - hard-bound fixture
    owner.pending_recovery_event_count = CHECKPOINT_RECOVERY_HARD_EVENTS
    owner.pending_recovery_payload_bytes = CHECKPOINT_RECOVERY_HARD_BYTES
    with pytest.raises(RuntimeProjectionCheckpointAdmissionBlocked):
        service.assert_event_admission((relevant_event,))
    with pytest.raises(RuntimeProjectionCheckpointAdmissionBlocked):
        service.assert_event_admission((irrelevant_event,))


def test_runtime_checkpoint_hard_fence_returns_none_before_physical_commit(
    tmp_path,
) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        reducer_id = f"terminal_notification:{runtime.runtime_session_id}"
        owner = runtime.runtime_projection_checkpoint_maintenance_service._owners[
            reducer_id
        ]  # noqa: SLF001 - pre-commit hard-bound fixture
        owner.pending_recovery_event_count = CHECKPOINT_RECOVERY_HARD_EVENTS
        owner.pending_recovery_payload_bytes = CHECKPOINT_RECOVERY_HARD_BYTES
        completion = terminal_process_completed_event(
            event_context=CTX,
            process_id="process:hard-fence",
        )
        with pytest.raises(Exception, match="checkpoint recovery bound"):
            await runtime.write_event(completion)
        assert runtime.event_log.next_sequence() == 1
        owner.pending_recovery_event_count = 0
        owner.pending_recovery_payload_bytes = 0
        runtime.close()

    asyncio.run(scenario())


def test_accounted_checkpoint_hard_fence_sees_complete_physical_batch(
    tmp_path,
) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        contracts = build_default_authority_materialization_contract_bundle()
        runtime.materialization_coordinator.bootstrap_genesis(
            context=CTX,
            business_events=(
                typed_non_transcript_event(
                    id="event:accounted-fence-genesis",
                    **CTX.event_fields(),
                    name="accounted-fence-genesis",
                ),
            ),
            genesis_profile="host_first_run",
            genesis_burst_contract=(
                contracts.burst_registry.unique_binding_for_operation(
                    PhysicalOperationKind.LEDGER_GENESIS
                ).contract
            ),
            register_transcript_consumer=True,
        )
        reducer_id = f"terminal_notification:{runtime.runtime_session_id}"
        owner = runtime.runtime_projection_checkpoint_maintenance_service._owners[
            reducer_id
        ]  # noqa: SLF001 - exact physical-batch hard-bound fixture
        owner.pending_recovery_event_count = CHECKPOINT_RECOVERY_HARD_EVENTS - 2
        owner.pending_recovery_payload_bytes = 0
        before_sequence = runtime.event_log.next_sequence()
        business = typed_non_transcript_event(
            id="event:accounted-fence-business",
            **CTX.event_fields(),
            name="accounted-fence-business",
        )

        with pytest.raises(
            EventCommitError,
            match="checkpoint recovery bound",
        ) as caught:
            await runtime.write_event(business)
        assert caught.value.commit_outcome == "none"

        # The business event alone would fit.  Its reservation and settlement
        # companions make the exact physical batch cross the hard bound, so no
        # member may reach storage.
        assert runtime.event_log.next_sequence() == before_sequence
        owner.pending_recovery_event_count = 0
        runtime.close()

    asyncio.run(scenario())


def test_checkpoint_fold_offer_conflict_latches_owner() -> None:
    event_log = InMemoryEventLog(runtime_session_id="runtime:offer-conflict")
    base = canonical_json_object_carrier({"through": 0})
    first_state = canonical_json_object_carrier({"through": 1, "value": "first"})
    conflicting_state = canonical_json_object_carrier(
        {"through": 1, "value": "conflicting"}
    )
    service = RuntimeProjectionCheckpointMaintenanceService(
        runtime_session_id="runtime:offer-conflict",
        event_log=event_log,
    )
    service.register_projection(
        reducer_id="reducer:offer-conflict",
        projection_kind="incident_projection.v1",
        projection_schema_version="incident_projection_state.v1",
        confirmed_head=None,
        genesis_state=base,
        current_through_sequence=0,
        current_state=base,
    )
    service.offer(
        build_committed_reducer_fold_receipt(
            reducer_id="reducer:offer-conflict",
            base_through_sequence=0,
            resulting_through_sequence=1,
            source_kind="live_batch",
            source_ordered_join_fingerprint=context_fingerprint(
                "test-fold-source:v1", {"candidate": "first"}
            ),
            base_state=base,
            resulting_state=first_state,
            checkpoint_delta_event_count=1,
            checkpoint_delta_payload_bytes=1,
        )
    )

    with pytest.raises(ValueError, match="fold offer conflicts"):
        service.offer(
            build_committed_reducer_fold_receipt(
                reducer_id="reducer:offer-conflict",
                base_through_sequence=0,
                resulting_through_sequence=1,
                source_kind="live_batch",
                source_ordered_join_fingerprint=context_fingerprint(
                    "test-fold-source:v1", {"candidate": "conflicting"}
                ),
                base_state=base,
                resulting_state=conflicting_state,
                checkpoint_delta_event_count=1,
                checkpoint_delta_payload_bytes=1,
            )
        )

    diagnostic = service.diagnostics("reducer:offer-conflict")
    assert diagnostic["state"] == "reconciliation_required"
    assert diagnostic["last_error_code"] == "CHECKPOINT_FOLD_OFFER_CONFLICT"


def test_terminal_projection_sparse_bound_is_typed_for_offline_repair(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    def exceeded(_self, *_args, **_kwargs):
        raise ValueError("sparse event selection exceeds its event bound")

    monkeypatch.setattr(InMemoryEventLog, "read_raw_events_by_types", exceeded)
    try:
        with pytest.raises(OnlineReducerRepairBoundExceeded):
            runtime._read_terminal_projection_delta(  # noqa: SLF001
                event_types=(str(_event("bounded").type),),
                minimum_sequence=1,
                through_sequence=0,
                deadline_monotonic=monotonic() + 1,
            )
    finally:
        runtime.close()


def test_terminal_projection_online_bound_includes_exact_tool_result_authority(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    try:
        completion = runtime.event_log.append(
            terminal_process_completed_event(
                event_context=CTX,
                process_id="process:exact-bound",
            )
        )
        exact_authority = runtime.event_log.append(
            make_text_block_segment_event(
                **CTX.event_fields(),
                block_id="text:exact-bound",
                delta="x" * 8_192,
            )
        )
        disposition = runtime.event_log.append(
            TerminalProcessObservationDeliveryDispositionEvent(
                id="terminal_notification_disposition:exact-bound",
                **CTX.event_fields(),
                observation_source_references=(
                    event_reference_from_stored(
                        completion,
                        runtime_session_id=runtime.runtime_session_id,
                    ),
                ),
                outcome="explicitly_observed",
                tool_result_end_event_identity=stable_event_identity(
                    exact_authority,
                    runtime_session_id=runtime.runtime_session_id,
                ),
            )
        )
        raw_disposition, raw_exact = runtime.event_log.read_raw_events_by_id(
            (disposition.id, exact_authority.id)
        )
        combined_payload_bytes = len(raw_disposition.canonical_payload_bytes) + len(
            raw_exact.canonical_payload_bytes
        )
        assert len(raw_disposition.canonical_payload_bytes) < combined_payload_bytes - 1
        monkeypatch.setattr(
            "pulsara_agent.runtime.session.CHECKPOINT_RECOVERY_HARD_BYTES",
            combined_payload_bytes - 1,
        )

        with pytest.raises(
            OnlineReducerRepairBoundExceeded,
            match="authority set exceeds its online byte bound",
        ):
            runtime._read_terminal_projection_delta(  # noqa: SLF001
                event_types=(
                    str(
                        TerminalProcessObservationDeliveryDispositionEvent.model_fields[
                            "type"
                        ].default
                    ),
                ),
                minimum_sequence=disposition.sequence or 0,
                through_sequence=disposition.sequence,
                deadline_monotonic=monotonic() + 1,
            )
    finally:
        runtime.close()


def test_post_fold_process_owner_retries_without_replaying_semantic_fold() -> None:
    async def scenario() -> None:
        attempts: list[tuple[str, ...]] = []

        def adopt(events) -> None:
            attempts.append(tuple(event.id for event in events))
            if len(attempts) == 1:
                raise OSError("synthetic process-owner handoff failure")

        event_log = InMemoryEventLog(runtime_session_id="runtime:post-fold")
        stored = event_log.append(_event("post-fold"))
        service = CommittedReducerPostFoldService()
        service.register(reducer_id="reducer:post-fold", callback=adopt)
        service.bind_running_loop()
        service.handoff(
            reducer_id="reducer:post-fold",
            events=(stored,),
        )
        deadline = monotonic() + 1
        while monotonic() < deadline:
            diagnostic = service.diagnostics()[0]
            if diagnostic["state"] == "clean":
                break
            await asyncio.sleep(0.005)
        assert attempts == [(stored.id,), (stored.id,)]
        assert service.diagnostics()[0]["physical_generation"] == 1
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 1)

    asyncio.run(scenario())


def test_repair_post_fold_does_not_republish_checkpoint_prefix() -> None:
    already_published = _event("repair-prefix").model_copy(update={"sequence": 7})
    failed_batch = _event("repair-failed-batch").model_copy(update={"sequence": 8})

    assert _repair_post_fold_events(
        (already_published, failed_batch),
        failed_registration_high_water=7,
    ) == (failed_batch,)


def test_reducer_repair_waiter_cancellation_detaches_from_physical_owner() -> None:
    async def scenario() -> None:
        started = Event()
        release = Event()

        def repair_operation(plan, _deadline):
            started.set()
            release.wait(timeout=5)
            return build_committed_reducer_repair_receipt(
                plan=plan,
                resulting_semantic_state_fingerprint="sha256:" + "a" * 64,
            )

        service = CommittedReducerRepairService(repair_operation=repair_operation)
        service.bind_running_loop()
        handle = service.install(
            reducer_id="reducer:detach",
            failed_registration_high_water=10,
            target_ledger_high_water=11,
            last_error_code="SYNTHETIC",
            recovery_base_identity="base:detach",
        )
        waiter = asyncio.create_task(
            service.wait(handle, deadline_monotonic=monotonic() + 2)
        )
        deadline = monotonic() + 1
        while not started.is_set() and monotonic() < deadline:
            await asyncio.sleep(0.005)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert service.diagnostics()[0]["state"] == "rebuilding"
        release.set()
        receipt = await service.wait(
            handle,
            deadline_monotonic=monotonic() + 2,
        )
        assert receipt.plan_fingerprint == handle.plan_fingerprint
        assert service.diagnostics()[0]["state"] == "repaired"
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_reducer_repair_rebinds_stable_plan_after_loop_teardown() -> None:
    started = Event()
    release = Event()
    plans: list[str] = []
    semantic_authority_installed = False

    def repair_operation(plan, _deadline):
        nonlocal semantic_authority_installed
        plans.append(plan.plan_fingerprint)
        if semantic_authority_installed:
            raise RuntimeError(
                "semantic authority already installed by prior physical op"
            )
        started.set()
        release.wait(timeout=5)
        semantic_authority_installed = True
        return build_committed_reducer_repair_receipt(
            plan=plan,
            resulting_semantic_state_fingerprint="sha256:" + "9" * 64,
        )

    service = CommittedReducerRepairService(repair_operation=repair_operation)
    handle = service.install(
        reducer_id="reducer:repair-loop",
        failed_registration_high_water=12,
        target_ledger_high_water=13,
        last_error_code="LOOP_TEARDOWN",
        recovery_base_identity="base:repair-loop",
    )

    async def start_then_detach() -> None:
        service.bind_running_loop()
        deadline = monotonic() + 1
        while not started.is_set() and monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert started.is_set()

    asyncio.run(start_then_detach())
    release.set()

    async def rebind_and_wait() -> None:
        receipt = await service.wait(
            handle,
            deadline_monotonic=monotonic() + 2,
        )
        assert receipt.plan_fingerprint == handle.plan_fingerprint
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

    asyncio.run(rebind_and_wait())
    assert plans == [handle.plan_fingerprint]
    assert semantic_authority_installed is True
    assert service.diagnostics()[0]["physical_generation"] == 1


def test_reducer_repair_retries_transient_io_with_same_plan() -> None:
    async def scenario() -> None:
        plans: list[str] = []

        def repair_operation(plan, _deadline):
            plans.append(plan.plan_fingerprint)
            if len(plans) == 1:
                raise OSError("synthetic transient repair read")
            return build_committed_reducer_repair_receipt(
                plan=plan,
                resulting_semantic_state_fingerprint="sha256:" + "b" * 64,
            )

        service = CommittedReducerRepairService(repair_operation=repair_operation)
        service.bind_running_loop()
        handle = service.install(
            reducer_id="reducer:retry",
            failed_registration_high_water=20,
            target_ledger_high_water=21,
            last_error_code="TRANSIENT",
            recovery_base_identity="base:retry",
        )
        receipt = await service.wait(
            handle,
            deadline_monotonic=monotonic() + 2,
        )
        assert receipt.plan_fingerprint == handle.plan_fingerprint
        assert plans == [handle.plan_fingerprint, handle.plan_fingerprint]
        diagnostic = service.diagnostics()[0]
        assert diagnostic["physical_generation"] == 2
        assert diagnostic["state"] == "repaired"
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_reducer_repair_accepts_later_independent_failure() -> None:
    async def scenario() -> None:
        plans: list[str] = []

        def repair_operation(plan, _deadline):
            plans.append(plan.plan_fingerprint)
            return build_committed_reducer_repair_receipt(
                plan=plan,
                resulting_semantic_state_fingerprint="sha256:" + "c" * 64,
            )

        service = CommittedReducerRepairService(repair_operation=repair_operation)
        service.bind_running_loop()
        first = service.install(
            reducer_id="reducer:repeat",
            failed_registration_high_water=30,
            target_ledger_high_water=31,
            last_error_code="FIRST",
            recovery_base_identity="base:first",
        )
        first_receipt = await service.wait(
            first,
            deadline_monotonic=monotonic() + 2,
        )

        second = service.install(
            reducer_id="reducer:repeat",
            failed_registration_high_water=36,
            target_ledger_high_water=37,
            last_error_code="SECOND",
            recovery_base_identity="base:second",
        )
        assert second.plan_fingerprint != first.plan_fingerprint
        second_receipt = await service.wait(
            second,
            deadline_monotonic=monotonic() + 2,
        )
        # A waiter holding the earlier exact handle can still observe its
        # bounded compatible winner after the reducer slot advances.
        assert (
            await service.wait(
                first,
                deadline_monotonic=monotonic() + 2,
            )
        ) == first_receipt
        assert second_receipt.plan_fingerprint == second.plan_fingerprint
        assert plans == [first.plan_fingerprint, second.plan_fingerprint]
        assert service.diagnostics()[0]["target_high_water"] == 37
        await service.stop_admission_and_drain(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_runtime_reducer_failure_installs_bounded_repair_and_keeps_event_full(
    tmp_path,
) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        reducer_id = f"terminal_notification:{runtime.runtime_session_id}"
        registration = runtime._committed_reducers[reducer_id]  # noqa: SLF001
        original = registration.ingress
        failed = False

        def fail_once(events):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("synthetic semantic install failure")
            return original.prepare_owned_events(events)

        registration.ingress = replace(
            cast(Any, original),
            prepare_owned_events=fail_once,
        )
        event = _event("repair")
        result = await runtime.write_event(event)
        settlement = runtime.accept_committed_event_result(
            result,
            requested_event_ids=(event.id,),
            require_publication=False,
        )

        assert result.committed_events[0].sequence == 1
        assert settlement.semantic_fold is (
            CommittedSemanticFoldSettlement.REPAIR_OWNER_INSTALLED
        )
        assert settlement.checkpoint_handoff is (
            CommittedCheckpointHandoff.NOT_APPLICABLE
        )
        handle = runtime.committed_reducer_repair_service.handle_for(reducer_id, 1)
        assert handle is not None
        repair = await runtime.wait_committed_reducer_repair(
            handle,
            deadline_monotonic=monotonic() + 3,
        )
        assert repair.reducer_id == reducer_id
        assert repair.repaired_through_sequence == 1
        assert registration.reconciliation_required is False
        assert runtime.reconciliation_required is False

        follow_up = await runtime.write_event(_event("after-repair"))
        assert follow_up.committed_events[0].sequence == 2
        await runtime.committed_reducer_repair_service.stop_admission_and_drain(
            deadline_monotonic=monotonic() + 2
        )
        await runtime.runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain(
            deadline_monotonic=monotonic() + 2
        )

    asyncio.run(scenario())


def test_open_reducer_barrier_repairs_before_next_close_producer(tmp_path) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        reducer_id = f"terminal_notification:{runtime.runtime_session_id}"
        registration = runtime._committed_reducers[reducer_id]  # noqa: SLF001
        original = registration.ingress
        failed = False

        def fail_once(events):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("synthetic completion semantic failure")
            return original.prepare_owned_events(events)

        registration.ingress = replace(
            cast(Any, original),
            prepare_owned_events=fail_once,
        )
        first = _event("close-cohort-one")
        result = await runtime.write_event(first)
        settlement = runtime.accept_committed_event_result(
            result,
            requested_event_ids=(first.id,),
            require_publication=False,
        )
        assert settlement.semantic_fold is (
            CommittedSemanticFoldSettlement.REPAIR_OWNER_INSTALLED
        )

        await runtime.drain_open_committed_reducer_barrier(
            deadline_monotonic=monotonic() + 3
        )

        assert registration.reconciliation_required is False
        assert runtime.reconciliation_required is False
        second = await runtime.write_event(_event("close-cohort-two"))
        assert second.committed_events[0].sequence == 2
        await runtime.committed_reducer_repair_service.stop_admission_and_drain(
            deadline_monotonic=monotonic() + 2
        )
        await runtime.runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain(
            deadline_monotonic=monotonic() + 2
        )
        runtime.close()

    asyncio.run(scenario())


def test_monitor_full_repair_settlement_does_not_latch_ledger_unknown(
    tmp_path,
) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        reducer_id = f"terminal_monitor:{runtime.runtime_session_id}"
        registration = runtime._committed_reducers[reducer_id]  # noqa: SLF001
        original = registration.ingress
        failed = False

        def fail_once(events):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("synthetic monitor semantic failure")
            return original.prepare_owned_events(events)

        registration.ingress = replace(
            cast(Any, original),
            prepare_owned_events=fail_once,
        )
        owner = _FiringOwner(
            monitor_id="monitor:repair-owned-full",
            stable_candidates=(_event("monitor-repair-owned-full"),),
            source_state_fingerprint="sha256:" + "c" * 64,
        )
        coordinator = runtime.terminal_monitor_coordinator
        coordinator._firing[owner.monitor_id] = owner  # noqa: SLF001

        await asyncio.to_thread(coordinator._commit_firing, owner)  # noqa: SLF001

        assert owner.monitor_id not in coordinator._firing  # noqa: SLF001
        assert runtime.ledger_reconciliation_required is False
        handle = runtime.committed_reducer_repair_service.handle_for(reducer_id, 1)
        assert handle is not None
        await runtime.wait_committed_reducer_repair(
            handle,
            deadline_monotonic=monotonic() + 3,
        )
        assert registration.reconciliation_required is False
        assert runtime.reconciliation_required is False
        await runtime.committed_reducer_post_fold_service.drain_pending(
            deadline_monotonic=monotonic() + 2
        )
        await runtime.committed_reducer_repair_service.stop_admission_and_drain(
            deadline_monotonic=monotonic() + 2
        )
        await runtime.runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain(
            deadline_monotonic=monotonic() + 2
        )
        runtime.close()

    asyncio.run(scenario())


def test_runtime_reducer_can_repair_two_independent_failures(tmp_path) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        reducer_id = f"terminal_notification:{runtime.runtime_session_id}"
        registration = runtime._committed_reducers[reducer_id]  # noqa: SLF001
        original = registration.ingress
        failures_remaining = 2

        def fail_twice(events):
            nonlocal failures_remaining
            if failures_remaining:
                failures_remaining -= 1
                raise RuntimeError("synthetic repeated semantic install failure")
            return original.prepare_owned_events(events)

        registration.ingress = replace(
            cast(Any, original),
            prepare_owned_events=fail_twice,
        )
        handles = []
        for label in ("repair-one", "repair-two"):
            event = _event(label)
            result = await runtime.write_event(event)
            settlement = runtime.accept_committed_event_result(
                result,
                requested_event_ids=(event.id,),
                require_publication=False,
            )
            assert settlement.semantic_fold is (
                CommittedSemanticFoldSettlement.REPAIR_OWNER_INSTALLED
            )
            handle = runtime.committed_reducer_repair_service.handle_for(
                reducer_id, int(result.committed_events[0].sequence or 0)
            )
            assert handle is not None
            handles.append(handle)
            await runtime.wait_committed_reducer_repair(
                handle,
                deadline_monotonic=monotonic() + 3,
            )
            assert registration.reconciliation_required is False

        assert handles[0].plan_fingerprint != handles[1].plan_fingerprint
        healthy = await runtime.write_event(_event("healthy-after-two-repairs"))
        assert healthy.committed_events[0].sequence == 3
        await runtime.committed_reducer_repair_service.stop_admission_and_drain(
            deadline_monotonic=monotonic() + 2
        )
        await runtime.runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain(
            deadline_monotonic=monotonic() + 2
        )
        runtime.close()

    asyncio.run(scenario())


def test_model_safe_point_waits_for_exact_session_owned_repair(tmp_path) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        reducer_id = f"terminal_notification:{runtime.runtime_session_id}"
        registration = runtime._committed_reducers[reducer_id]  # noqa: SLF001
        original_ingress = registration.ingress
        original_repair = (
            runtime.committed_reducer_repair_service._repair_operation  # noqa: SLF001
        )
        repair_started = Event()
        release_repair = Event()
        failed = False

        def fail_once(events):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("synthetic completion semantic failure")
            return original_ingress.prepare_owned_events(events)

        def blocked_repair(plan, deadline):
            repair_started.set()
            release_repair.wait(timeout=5)
            return original_repair(plan, deadline)

        registration.ingress = replace(
            cast(Any, original_ingress),
            prepare_owned_events=fail_once,
        )
        runtime.committed_reducer_repair_service._repair_operation = (  # noqa: SLF001
            blocked_repair
        )
        result = await runtime.write_event(_event("safe-point"))
        runtime.accept_committed_event_result(
            result,
            requested_event_ids=(result.committed_events[0].id,),
            require_publication=False,
        )
        assert await asyncio.to_thread(repair_started.wait, 1)

        waiter = asyncio.create_task(
            runtime.await_committed_reducer_repair_safe_point(
                deadline_monotonic=monotonic() + 3,
            )
        )
        await asyncio.sleep(0.03)
        assert not waiter.done()
        assert registration.reconciliation_required is True

        release_repair.set()
        receipts = await asyncio.wait_for(waiter, timeout=2)
        assert len(receipts) == 1
        assert registration.reconciliation_required is False
        assert runtime.reconciliation_required is False
        runtime.close()

    asyncio.run(scenario())


def test_notification_listener_failure_is_observational_not_semantic(tmp_path) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        reducer_id = f"terminal_notification:{runtime.runtime_session_id}"

        def failed_listener(_events) -> None:
            raise RuntimeError("synthetic listener failure")

        runtime.bind_terminal_notification_listener(failed_listener)
        result = await runtime.write_event(_event("listener"))
        settlement = runtime.accept_committed_event_result(
            result,
            requested_event_ids=(result.committed_events[0].id,),
            require_publication=False,
        )
        registration = runtime._committed_reducers[reducer_id]  # noqa: SLF001
        assert settlement.semantic_fold is CommittedSemanticFoldSettlement.HEALTHY
        assert registration.reconciliation_required is False
        assert registration.through_sequence == 1
        assert (
            runtime.committed_reducer_repair_service.handle_for(reducer_id, 1) is None
        )
        runtime.close()

    asyncio.run(scenario())


def test_runtime_checkpoint_io_failure_retries_without_partial_semantic_state(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        runtime = in_memory_runtime_session(tmp_path)
        reducer_id = f"terminal_notification:{runtime.runtime_session_id}"
        journal = SanitizedOutputJournal(process_id="process:checkpoint-retry")
        prepared = runtime.terminal_notification_account_coordinator._prepare(  # noqa: SLF001
            reservation_id="terminal_completion_head:process:checkpoint-retry",
            reservation_kind="completion_process_head",
            stream_identity=journal.stream_identity,
            monitor_id=None,
            created_by_event_id="tool_result_end:checkpoint-retry",
            process=None,
        )
        cause = RunErrorEvent(
            id="run_error:checkpoint-retry",
            **CTX.event_fields(),
            message="checkpoint retry fixture",
            code="checkpoint_retry_fixture",
        )
        candidate = (
            runtime.terminal_notification_account_coordinator.freeze_created_event(
                prepared=prepared,
                cause_events=(cause,),
            )
        )
        event_log_type = type(runtime.event_log)
        original_write = event_log_type.write_runtime_projection_checkpoint
        target_event_log = runtime.event_log
        write_count = 0

        def fail_first_write(self, checkpoint, **kwargs):
            nonlocal write_count
            if self is not target_event_log:
                return original_write(self, checkpoint, **kwargs)
            write_count += 1
            if write_count == 1:
                raise OSError("synthetic checkpoint SQL failure")
            return original_write(self, checkpoint, **kwargs)

        monkeypatch.setattr(
            event_log_type,
            "write_runtime_projection_checkpoint",
            fail_first_write,
        )
        result = await runtime.write_event(candidate)
        settlement = runtime.accept_committed_event_result(
            result,
            requested_event_ids=(candidate.id,),
            require_publication=False,
        )

        registration = runtime._committed_reducers[reducer_id]  # noqa: SLF001
        assert settlement.semantic_fold is CommittedSemanticFoldSettlement.HEALTHY
        assert settlement.checkpoint_handoff is CommittedCheckpointHandoff.ACCEPTED
        assert registration.through_sequence == 1
        assert runtime.terminal_notification_store.through_sequence == 1
        assert runtime.reconciliation_required is False

        diagnostic = await _wait_checkpoint_clean(
            runtime.runtime_projection_checkpoint_maintenance_service,
            reducer_id,
        )
        assert diagnostic["confirmed_through_sequence"] == 1
        assert diagnostic["physical_generation"] == 2
        assert diagnostic["first_failure_monotonic"] is not None
        assert write_count == 2
        await runtime.committed_reducer_post_fold_service.drain_pending(
            deadline_monotonic=monotonic() + 2
        )
        runtime.close()
        journal.close(destroy_spool=True)

    asyncio.run(scenario())


def _finalization_registry() -> tuple[
    RunExecutionRegistry,
    RunOwner,
    RunActivationWorkingState,
]:
    reservation = build_prepared_run_owner_reservation_key(
        runtime_session_id="runtime:finalization",
        run_id="run:finalization",
        run_start_event_id="run-start:finalization",
    )
    identity = build_run_owner_identity(
        reservation_key=reservation,
        run_start_sequence=1,
    )
    handles = RunExecutionHandleSet(
        handle_id="handles:finalization",
        handle_generation=1,
        owner=reservation,
        state="boundary_owned",
        mcp_installation=object(),
        capability_runtime=object(),
        tool_registry=object(),
        frozen_execution_surface=cast(Any, object()),
    )
    handles.transfer_to_run(identity)
    finalization = RunFinalizationOwner(
        owner_identity=identity,
        terminal_event_id="run-end:finalization",
        state="candidate_frozen",
    )
    state = RunActivationWorkingState(
        session_id="runtime:finalization",
        run_id="run:finalization",
        turn_id="turn:finalization",
        reply_id="reply:finalization",
    )
    token = "finalization-state:test"
    finalization.state_carrier = RunActivationStateCarrier(
        run_id="run:finalization",
        generation=1,
        owner_token=token,
        _working_state=state,
    )
    finalization.state_owner_token = token
    owner = RunOwner(
        identity=identity,
        genesis=cast(Any, SimpleNamespace()),
        authority_head=cast(Any, SimpleNamespace()),
        progress=RunProgressState(owner_identity=identity),
        lifecycle="open",
        resource_slot=BoundRunResources(handle_set=handles),
        retiring_resources=RunRetiringResourceSet(owner_identity=identity),
        activation_slot=NoActiveActivation(),
        suspension_slot=NoActiveSuspension(),
        finalization_slot=RunFinalizationSlot(owner=finalization),
        observer_registry=RunObserverRegistry(),
        activation_completion_history={},
        run_completion=asyncio.get_running_loop().create_future(),
        entry=cast(Any, object()),
        termination_intent=None,
        next_segment_generation=0,
        latest_activation_owner_kind="host_run_boundary",
        latest_activation_owner_id="boundary:finalization",
    )
    registry = RunExecutionRegistry()
    registry.register_recovered(owner)
    return registry, owner, state


def test_finalization_drain_joins_admitted_output_successor() -> None:
    async def scenario() -> None:
        registry, owner, state = _finalization_registry()
        service = RunFinalizationService(registry=registry)
        physical_started = asyncio.Event()
        release_physical = asyncio.Event()
        output_started = asyncio.Event()
        release_output = asyncio.Event()

        async def output_operation() -> None:
            output_started.set()
            await release_output.wait()
            owner.finalization_owner.state = "completed"

        async def terminalization_operation():
            physical_started.set()
            await release_physical.wait()
            finalization = owner.finalization_owner
            finalization.commit_state = "confirmed"
            finalization.state = "full_output_pending"
            service.continue_output_materialization(
                run_id="run:finalization",
                operation=output_operation,
            )
            yield ReplyStartEvent(
                id="run-end-result:drain-successor",
                **CTX.event_fields(),
                name="assistant",
            )

        terminalization = service.continue_terminalization(
            run_id="run:finalization",
            state=state,
            operation=terminalization_operation,
        )
        await asyncio.wait_for(physical_started.wait(), timeout=1)
        drain = asyncio.create_task(service.drain(deadline_monotonic=monotonic() + 2))
        await asyncio.sleep(0)
        release_physical.set()
        await asyncio.wait_for(output_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not drain.done()

        release_output.set()
        result = await asyncio.wait_for(terminalization, timeout=1)
        await asyncio.wait_for(drain, timeout=1)

        assert len(result) == 1
        assert owner.finalization_owner.state == "completed"
        assert owner.finalization_owner.output_materialization_task is None

    asyncio.run(scenario())


def test_finalization_drain_rejects_done_failed_physical_task() -> None:
    async def scenario() -> None:
        registry, owner, state = _finalization_registry()
        service = RunFinalizationService(registry=registry)

        async def failed_operation():
            raise RuntimeError("synthetic physical finalization failure")
            yield  # pragma: no cover

        waiter = service.continue_terminalization(
            run_id="run:finalization",
            state=state,
            operation=failed_operation,
        )
        with pytest.raises(RuntimeError, match="synthetic physical"):
            await waiter
        assert owner.finalization_owner.physical_task is not None
        assert owner.finalization_owner.physical_task.done()
        assert owner.finalization_owner.state == "reconciliation_required"

        with pytest.raises(RuntimeError, match="physical task failed"):
            await service.drain(deadline_monotonic=monotonic() + 1)

    asyncio.run(scenario())


def test_finalization_drain_rejects_done_failed_output_task() -> None:
    async def scenario() -> None:
        registry, owner, _state = _finalization_registry()
        service = RunFinalizationService(registry=registry)
        finalization = owner.finalization_owner
        finalization.commit_state = "confirmed"
        finalization.state = "full_output_pending"

        async def failed_output() -> None:
            raise RuntimeError("synthetic output finalization failure")

        task = service.continue_output_materialization(
            run_id="run:finalization",
            operation=failed_output,
        )
        with pytest.raises(RuntimeError, match="synthetic output"):
            await task
        assert task.done()
        assert finalization.state == "full_output_pending"

        with pytest.raises(RuntimeError, match="output task failed"):
            await service.drain(deadline_monotonic=monotonic() + 1)

    asyncio.run(scenario())


def test_operational_diagnostics_expose_blocked_run_without_private_state() -> None:
    async def scenario() -> None:
        registry, owner, _state = _finalization_registry()
        finalization = owner.finalization_owner
        finalization.state = "waiting_reducer_repair"
        finalization.physical_attempt_generation = 3
        finalization.last_failure_code = "EVENT_RECONCILIATION_REQUIRED"
        finalization.reducer_repair_handles = (
            SimpleNamespace(
                reducer_id="terminal_notification:runtime:finalization",
                target_ledger_high_water=41,
                plan_fingerprint="sha256:" + "a" * 64,
            ),
        )

        class AgentRuntimeStub:
            @staticmethod
            def bind_run_reconciliation_service(_service) -> None:
                return None

        service = RunActivationService(
            registry=registry,
            event_log=InMemoryEventLog(runtime_session_id="runtime:finalization"),
            agent_runtime=cast(Any, AgentRuntimeStub()),
            runtime_session_id="runtime:finalization",
        )
        diagnostic = service.run_finalization_diagnostics("run:finalization")

        assert diagnostic is not None
        assert diagnostic["state"] == "waiting_reducer_repair"
        assert diagnostic["stable_run_end_event_id"] == "run-end:finalization"
        assert diagnostic["physical_attempt_generation"] == 3
        assert diagnostic["last_failure_code"] == ("EVENT_RECONCILIATION_REQUIRED")
        assert diagnostic["reducer_repairs"] == (
            {
                "reducer_id": "terminal_notification:runtime:finalization",
                "target_high_water": 41,
                "plan_fingerprint": "sha256:" + "a" * 64,
            },
        )

    asyncio.run(scenario())


def test_finalization_waits_for_exact_repair_without_polling_writer() -> None:
    async def scenario() -> None:
        registry, owner, state = _finalization_registry()
        repair_started = asyncio.Event()
        release_repair = asyncio.Event()
        handle = SimpleNamespace(
            plan_fingerprint="repair:exact",
            reducer_id="reducer:exact",
            target_ledger_high_water=41,
        )

        class RepairPort:
            def pending_committed_reducer_repair_handles(self):
                return (handle,)

            async def wait_committed_reducer_repair(self, candidate, **_kwargs):
                assert candidate is handle
                repair_started.set()
                await release_repair.wait()
                return SimpleNamespace(
                    plan_fingerprint="repair:exact",
                    reducer_id="reducer:exact",
                    repaired_through_sequence=41,
                )

            @staticmethod
            def resolved_write_outcome(_error):
                return None

        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise EventReconciliationRequired("wait for exact repair")
            yield ReplyStartEvent(
                id="run-end-result:finalization",
                **EventContext(
                    run_id="run:finalization",
                    turn_id="turn:finalization",
                    reply_id="reply:finalization",
                ).event_fields(),
                name="assistant",
            )

        service = RunFinalizationService(
            registry=registry,
            repair_port=RepairPort(),
        )
        task = service.continue_terminalization(
            run_id="run:finalization",
            state=state,
            operation=operation,
        )
        await asyncio.wait_for(repair_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        finalization = owner.finalization_slot.owner
        assert isinstance(finalization, RunFinalizationOwner)
        assert finalization.state == "waiting_reducer_repair"
        assert calls == 1

        release_repair.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert len(result) == 1
        assert calls == 2
        assert finalization.reducer_repair_handles == ()

    asyncio.run(scenario())


def test_finalization_retries_same_candidate_when_repair_won_before_catch() -> None:
    async def scenario() -> None:
        registry, owner, state = _finalization_registry()

        class RepairPort:
            reconciliation_required = False

            @staticmethod
            def pending_committed_reducer_repair_handles():
                raise AssertionError("completed repair must not be resolved again")

            @staticmethod
            def resolved_write_outcome(_error):
                return None

        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise EventReconciliationRequired("repair already won")
            yield ReplyStartEvent(
                id="run-end-result:repair-won",
                **CTX.event_fields(),
                name="assistant",
            )

        service = RunFinalizationService(registry=registry, repair_port=RepairPort())
        result = await asyncio.wait_for(
            service.continue_terminalization(
                run_id="run:finalization",
                state=state,
                operation=operation,
            ),
            timeout=1,
        )
        assert len(result) == 1
        assert calls == 2
        assert owner.finalization_owner.state != "reconciliation_required"

    asyncio.run(scenario())


def test_finalization_adopts_full_winner_already_confirmed_by_operation() -> None:
    async def scenario() -> None:
        registry, owner, state = _finalization_registry()
        committed = ReplyStartEvent(
            id="run-end-result:confirmed-full",
            **CTX.event_fields(),
            name="assistant",
        ).model_copy(update={"sequence": 2})

        class RepairPort:
            @staticmethod
            def resolved_write_outcome(_error):
                return SimpleNamespace(
                    status="full",
                    committed_events=(committed,),
                )

        async def operation():
            finalization = owner.finalization_owner
            finalization.commit_state = "confirmed"
            finalization.state = "full_output_pending"
            raise OSError("caller detached after compatible FULL")
            yield  # pragma: no cover

        service = RunFinalizationService(registry=registry, repair_port=RepairPort())
        result = await asyncio.wait_for(
            service.continue_terminalization(
                run_id="run:finalization",
                state=state,
                operation=operation,
            ),
            timeout=1,
        )

        assert result == (committed,)
        assert owner.finalization_owner.commit_state == "confirmed"
        assert owner.finalization_owner.state == "full_output_pending"

    asyncio.run(scenario())


def test_finalization_waiter_cancellation_detaches_from_owned_repair_task() -> None:
    async def scenario() -> None:
        registry, owner, state = _finalization_registry()
        repair_started = asyncio.Event()
        release_repair = asyncio.Event()
        handle = SimpleNamespace(
            plan_fingerprint="repair:detach",
            reducer_id="reducer:detach",
            target_ledger_high_water=9,
        )

        class RepairPort:
            @staticmethod
            def pending_committed_reducer_repair_handles():
                return (handle,)

            @staticmethod
            async def wait_committed_reducer_repair(candidate, **_kwargs):
                assert candidate is handle
                repair_started.set()
                await release_repair.wait()
                return SimpleNamespace(
                    plan_fingerprint="repair:detach",
                    reducer_id="reducer:detach",
                    repaired_through_sequence=9,
                )

            @staticmethod
            def resolved_write_outcome(_error):
                return None

        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise EventReconciliationRequired("repair")
            yield ReplyStartEvent(
                id="run-end-result:detach",
                **CTX.event_fields(),
                name="assistant",
            )

        service = RunFinalizationService(registry=registry, repair_port=RepairPort())
        waiter = service.continue_terminalization(
            run_id="run:finalization",
            state=state,
            operation=operation,
        )
        await asyncio.wait_for(repair_started.wait(), timeout=1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        finalization = owner.finalization_slot.owner
        assert isinstance(finalization, RunFinalizationOwner)
        assert finalization.physical_task is not None
        assert not finalization.physical_task.done()
        release_repair.set()
        replacement_waiter = service.continue_terminalization(
            run_id="run:finalization",
            state=state,
            operation=operation,
        )
        result = await asyncio.wait_for(replacement_waiter, timeout=1)
        assert len(result) == 1
        assert calls == 2

    asyncio.run(scenario())


def test_finalization_rejects_stale_reducer_repair_receipt() -> None:
    async def scenario() -> None:
        registry, owner, state = _finalization_registry()
        handle = SimpleNamespace(
            plan_fingerprint="repair:expected",
            reducer_id="reducer:expected",
            target_ledger_high_water=7,
        )

        class RepairPort:
            @staticmethod
            def pending_committed_reducer_repair_handles():
                return (handle,)

            @staticmethod
            async def wait_committed_reducer_repair(_candidate, **_kwargs):
                return SimpleNamespace(
                    plan_fingerprint="repair:stale",
                    reducer_id="reducer:expected",
                    repaired_through_sequence=7,
                )

            @staticmethod
            def resolved_write_outcome(_error):
                return None

        async def operation():
            raise EventReconciliationRequired("repair")
            yield  # pragma: no cover

        service = RunFinalizationService(registry=registry, repair_port=RepairPort())
        waiter = service.continue_terminalization(
            run_id="run:finalization",
            state=state,
            operation=operation,
        )
        with pytest.raises(RuntimeError, match="receipt is stale"):
            await waiter
        finalization = owner.finalization_slot.owner
        assert isinstance(finalization, RunFinalizationOwner)
        assert finalization.state == "reconciliation_required"

    asyncio.run(scenario())
