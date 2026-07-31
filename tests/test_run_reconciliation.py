from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.event import EventContext, ReplyStartEvent, RunEndEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.ports.run_execution import (
    ReconciliationConflictConfirmation,
    build_prepared_run_owner_reservation_key,
    build_run_owner_identity,
)
from pulsara_agent.runtime.execution_handles import RunExecutionHandleSet
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
from pulsara_agent.runtime.run_execution.reconciliation import (
    RunReconciliationService,
)
from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
from pulsara_agent.runtime.run_execution.snapshot import build_owner_state_identity
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.state import LoopStatus
from tests.support import run_agent_task
from tests.support.runtime_owner import build_test_agent_runtime
from tests.support.runtime_session import in_memory_runtime_session
from tests.test_agent_runtime_loop import ScriptedTransport, make_llm_runtime


def _run(coro):
    return asyncio.run(coro)


def _owner() -> tuple[RunExecutionRegistry, RunOwner]:
    reservation = build_prepared_run_owner_reservation_key(
        runtime_session_id="runtime:reconciliation",
        run_id="run:reconciliation",
        run_start_event_id="run-start:reconciliation",
    )
    identity = build_run_owner_identity(
        reservation_key=reservation,
        run_start_sequence=1,
    )
    handles = RunExecutionHandleSet(
        handle_id="handles:reconciliation",
        handle_generation=1,
        owner=reservation,
        state="boundary_owned",
        mcp_installation=object(),
        capability_runtime=object(),
        tool_registry=object(),
        frozen_execution_surface=cast(Any, object()),
    )
    handles.transfer_to_run(identity)
    registry = RunExecutionRegistry()
    owner = RunOwner(
        identity=identity,
        genesis=cast(Any, SimpleNamespace(entry=SimpleNamespace(entry_kind="host"))),
        authority_head=cast(
            Any,
            SimpleNamespace(head_fingerprint="sha256:authority-awaiting"),
        ),
        progress=RunProgressState(owner_identity=identity),
        lifecycle="initializing",
        resource_slot=BoundRunResources(handle_set=handles),
        retiring_resources=RunRetiringResourceSet(owner_identity=identity),
        activation_slot=NoActiveActivation(),
        suspension_slot=NoActiveSuspension(),
        finalization_slot=RunFinalizationSlot(
            owner=RunFinalizationOwner(
                owner_identity=identity,
                terminal_event_id="run-end:reconciliation",
            )
        ),
        observer_registry=RunObserverRegistry(),
        activation_completion_history={},
        run_completion=asyncio.get_running_loop().create_future(),
        entry=cast(Any, object()),
        termination_intent=None,
        next_segment_generation=0,
        latest_activation_owner_kind="host_run_boundary",
        latest_activation_owner_id="boundary:reconciliation",
    )
    registry.register_recovered(owner)
    return registry, owner


def _candidate(*, name: str = "assistant") -> ReplyStartEvent:
    return ReplyStartEvent(
        id="candidate:reconciliation",
        **EventContext(
            run_id="run:reconciliation",
            turn_id="turn:reconciliation",
            reply_id="reply:reconciliation",
        ).event_fields(),
        name=name,
    )


def test_terminal_snapshot_retains_confirmed_run_end_during_output_materialization() -> (
    None
):
    async def scenario() -> None:
        _registry_value, owner = _owner()
        finalization = owner.finalization_slot.owner
        assert isinstance(finalization, RunFinalizationOwner)
        reference = ContextEventReferenceFact(
            runtime_session_id=owner.identity.runtime_session_id,
            event_id=finalization.terminal_event_id,
            sequence=3,
            event_type="RUN_END",
            payload_fingerprint=context_fingerprint(
                "test-confirmed-run-end:v1", finalization.terminal_event_id
            ),
        )
        owner.lifecycle = "terminal"
        owner.finalization_slot.state = "run_end_full_pending_output"
        finalization.state = "full_output_pending"
        finalization.commit_state = "confirmed"
        finalization.run_end_candidate = None
        finalization.confirmed_run_end_event_reference = reference

        snapshot = build_owner_state_identity(owner)

        assert snapshot.lifecycle == "terminal"
        assert snapshot.finalization_slot.slot_state == "run_end_full_pending_output"
        assert snapshot.finalization_slot.stable_candidate_id == reference.event_id
        assert (
            snapshot.finalization_slot.stable_candidate_fingerprint
            == reference.payload_fingerprint
        )

    _run(scenario())


def test_nonempty_finalization_snapshot_rejects_missing_terminal_authority() -> None:
    async def scenario() -> None:
        _registry_value, owner = _owner()
        finalization = owner.finalization_slot.owner
        assert isinstance(finalization, RunFinalizationOwner)
        owner.lifecycle = "terminal"
        owner.finalization_slot.state = "run_end_full_pending_output"
        finalization.state = "full_output_pending"
        finalization.commit_state = "confirmed"
        with pytest.raises(
            RuntimeError,
            match="non-empty finalization owner lacks terminal authority",
        ):
            build_owner_state_identity(owner)

    _run(scenario())


def test_none_reconciliation_never_reopens_run_without_driver() -> None:
    async def scenario() -> None:
        registry, owner = _owner()
        log = InMemoryEventLog(runtime_session_id="runtime:reconciliation")
        service = RunReconciliationService(registry=registry, event_log=log)
        candidate = _candidate()
        service.install_event_batch(
            run_id=owner.identity.run_id,
            attempt_kind="activation_installation",
            candidates=(candidate,),
            repair_mode="reopen_recovery",
            resident_owner_generation=None,
        )
        receipt = await service.confirm_stable_candidate(
            run_id=owner.identity.run_id,
            candidates=(candidate,),
            deadline_monotonic=asyncio.get_running_loop().time() + 2.0,
        )
        assert receipt.confirmation.disposition == "none"
        assert receipt.retry_owner_retained is True
        assert receipt.resulting_state.lifecycle == "reconciliation_required"
        assert owner.lifecycle == "reconciliation_required"
        assert owner.reconciliation_owner is not None

    _run(scenario())


def test_full_reopen_confirmation_returns_only_to_initializing() -> None:
    async def scenario() -> None:
        registry, owner = _owner()
        log = InMemoryEventLog(runtime_session_id="runtime:reconciliation")
        candidate = _candidate()
        log.append(candidate)
        service = RunReconciliationService(registry=registry, event_log=log)
        snapshot = service.install_event_batch(
            run_id=owner.identity.run_id,
            attempt_kind="activation_installation",
            candidates=(candidate,),
            repair_mode="reopen_recovery",
            resident_owner_generation=None,
        )
        receipt = await service.confirm_stable_candidate(
            run_id=owner.identity.run_id,
            candidates=(candidate,),
            deadline_monotonic=asyncio.get_running_loop().time() + 2.0,
        )
        assert receipt.confirmation.disposition == "full"
        assert receipt.retry_owner_retained is False
        assert receipt.resulting_state.lifecycle == "initializing"
        assert owner.lifecycle == "initializing"
        assert owner.reconciliation_owner is None
        assert (
            owner.reconciliation_resolution_history[snapshot.snapshot_fingerprint]
            == receipt
        )

    _run(scenario())


def test_conflict_confirmation_cannot_escape_reconciliation() -> None:
    async def scenario() -> None:
        registry, owner = _owner()
        log = InMemoryEventLog(runtime_session_id="runtime:reconciliation")
        log.append(_candidate(name="different"))
        service = RunReconciliationService(registry=registry, event_log=log)
        candidate = _candidate()
        service.install_event_batch(
            run_id=owner.identity.run_id,
            attempt_kind="activation_installation",
            candidates=(candidate,),
            repair_mode="reopen_recovery",
            resident_owner_generation=None,
        )
        receipt = await service.confirm_stable_candidate(
            run_id=owner.identity.run_id,
            candidates=(candidate,),
            deadline_monotonic=asyncio.get_running_loop().time() + 2.0,
        )
        assert isinstance(receipt.confirmation, ReconciliationConflictConfirmation)
        assert receipt.retry_owner_retained is True
        assert owner.lifecycle == "reconciliation_required"

    _run(scenario())


def test_driver_exit_without_candidate_uses_stable_terminalization_repair(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_before_candidate(
        _runtime,
        _draft,
        _committed,
        *,
        active_skill_names=None,
    ):
        del active_skill_names
        raise RuntimeError("synthetic activation driver failure")
        yield  # pragma: no cover - preserves the async-generator contract

    monkeypatch.setattr(
        AgentRuntime,
        "stream_committed_entry",
        fail_before_candidate,
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = build_test_agent_runtime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([])),
    )

    result = _run(run_agent_task(agent, "fail before candidate"))

    assert result.status is LoopStatus.FAILED
    terminal = [
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, RunEndEvent)
    ]
    assert len(terminal) == 1
    owner = agent.run_execution_registry.get(result.run_id)
    if owner is not None:
        assert owner.active_segment is None
        assert owner.reconciliation_owner is None
        assert owner.finalization_owner.commit_state == "confirmed"
