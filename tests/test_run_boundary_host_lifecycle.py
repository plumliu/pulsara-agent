from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from functools import wraps
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pulsara_agent.runtime.execution_handles import (
    RunExecutionHandleSet,
    CapabilityExecutionBorrowUnavailable,
    CapabilityExecutionBorrowTracker,
)
from pulsara_agent.runtime.run_execution.owner import (
    ActiveRunSuspension,
    BoundRunResources,
    NoActiveActivation,
    NoActiveSuspension,
    RunFinalizationOwner,
    RunFinalizationSlot,
    RunObserverRegistry,
    RunOwner,
    RunProgressState,
    RunRetiringResourceSet,
    RunActivationCoordinatorResult,
)
from pulsara_agent.ports.run_execution import (
    PendingInteractionIdentity,
    RunTerminalizationPending,
    RunSegmentInstallBlocked,
    RunTerminationIntent,
)
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.ports.run_authority import InstalledRunAuthorityRevision
from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
from pulsara_agent.runtime.run_execution.prepared import RunActivationStateCarrier
from pulsara_agent.runtime.state import RunActivationWorkingState
from pulsara_agent.ports.run_execution import (
    RunSuspendedOutcome,
    build_prepared_run_owner_reservation_key,
    build_run_owner_identity,
)
from pulsara_agent.runtime.agent import _await_sync_tool_thread


def _async_test(
    function: Callable[..., Coroutine[object, object, None]],
) -> Callable[..., None]:
    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> None:
        asyncio.run(function(*args, **kwargs))

    return wrapped


def _handles(
    handle_id: str = "handles:1", *, state: str = "run_owned"
) -> RunExecutionHandleSet:
    reservation = build_prepared_run_owner_reservation_key(
        runtime_session_id="runtime:1",
        run_id="run:1",
        run_start_event_id="run_start:1",
    )
    handles = RunExecutionHandleSet(
        handle_id=handle_id,
        handle_generation=1,
        owner=reservation,
        state="boundary_owned",
        mcp_installation=object(),
        capability_runtime=object(),
        tool_registry=object(),
        frozen_execution_surface=cast(Any, object()),
    )
    if state == "run_owned":
        handles.transfer_to_run(
            build_run_owner_identity(
                reservation_key=reservation,
                run_start_sequence=1,
            )
        )
    return handles


def _registry() -> tuple[RunExecutionRegistry, RunOwner]:
    registry = RunExecutionRegistry()
    handles = _handles()
    identity = cast(Any, handles.owner)
    owner = RunOwner(
        identity=identity,
        genesis=cast(Any, object()),
        authority_head=cast(Any, object()),
        progress=RunProgressState(owner_identity=identity),
        lifecycle="open",
        resource_slot=BoundRunResources(handle_set=handles),
        retiring_resources=RunRetiringResourceSet(owner_identity=identity),
        activation_slot=NoActiveActivation(),
        suspension_slot=NoActiveSuspension(),
        finalization_slot=RunFinalizationSlot(
            owner=RunFinalizationOwner(
                owner_identity=identity,
                terminal_event_id="event:run-end",
            )
        ),
        observer_registry=RunObserverRegistry(),
        activation_completion_history={},
        entry=cast(Any, object()),
        termination_intent=None,
        run_completion=asyncio.get_running_loop().create_future(),
        next_segment_generation=0,
        latest_activation_owner_kind="host_run_boundary",
        latest_activation_owner_id="boundary:initial",
    )
    _install_pending_state(owner, token="run-pending:test:initial")
    registry.register_recovered(owner)
    return registry, owner


def _install_pending_state(owner: RunOwner, *, token: str) -> None:
    state = RunActivationWorkingState(
        session_id="runtime:1",
        run_id="run:1",
        turn_id="turn:1",
        reply_id="reply:1",
    )
    owner.pending_activation_state = RunActivationStateCarrier(
        run_id="run:1",
        generation=1,
        owner_token=token,
        _working_state=state,
    )
    owner.pending_activation_owner_token = token


def _install_suspended_state(owner: RunOwner) -> None:
    token = "suspension:test:1"
    state = RunActivationWorkingState(
        session_id="runtime:1",
        run_id="run:1",
        turn_id="turn:1",
        reply_id="reply:1",
    )
    carrier = RunActivationStateCarrier(
        run_id="run:1",
        generation=1,
        owner_token=token,
        _working_state=state,
    )
    owner.pending_activation_state = None
    owner.pending_activation_owner_token = None
    owner.suspension_slot = ActiveRunSuspension(
        authority=SimpleNamespace(
            identity=SimpleNamespace(interaction_fingerprint="interaction:1")
        ),
        resources=SimpleNamespace(
            state_carrier=carrier,
            state_owner_token=token,
        ),
    )


def _install_fake_suspended_receipt(owner: RunOwner) -> None:
    activation = SimpleNamespace(
        durable_activation=SimpleNamespace(segment_generation=1)
    )
    pending = SimpleNamespace(
        identity=SimpleNamespace(interaction_fingerprint="interaction:1")
    )
    outcome = RunSuspendedOutcome.model_construct(
        outcome_kind="suspended",
        owner_identity=owner.identity,
        activation_identity=activation,
        authority_revision_fingerprint="authority:1",
        source_interaction_event_reference=cast(Any, object()),
        pending_interaction=pending,
        progress=cast(Any, object()),
    )
    owner.activation_completion_history[1] = RunActivationCoordinatorResult(
        segment_id="segment:1",
        segment_generation=1,
        disposition="waiting_user",
        outcome=outcome,
    )


def test_pending_interaction_identity_rejects_wrong_exact_source_type() -> None:
    async def scenario() -> None:
        _registry_value, owner = _registry()
        source = ContextEventReferenceFact(
            runtime_session_id=owner.identity.runtime_session_id,
            event_id="confirm:wrong-occurrence",
            sequence=2,
            event_type="PLAN_QUESTION_ASKED",
            payload_fingerprint=context_fingerprint(
                "test-pending-source:v1", "confirm:wrong-occurrence"
            ),
        )
        payload = {
            "schema_version": 1,
            "owner_identity": owner.identity,
            "interaction_kind": "approval",
            "interaction_id": "confirm:expected-occurrence",
            "source_interaction_event_reference": source,
        }
        with pytest.raises(ValueError, match="source event type mismatch"):
            PendingInteractionIdentity(
                **payload,
                interaction_fingerprint=context_fingerprint(
                    "pending-interaction:v1", payload
                ),
            )

    asyncio.run(scenario())


@_async_test
async def test_segment_owner_is_installed_before_eager_driver_execution() -> None:
    registry, owner = _registry()
    loop = asyncio.get_running_loop()
    prior_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    started = asyncio.Event()

    async def driver() -> None:
        installed = registry.require("run:1").active_segment
        assert installed is not None
        assert installed.segment_state == "reserved"
        started.set()

    try:
        segment = registry.install_segment(
            "run:1",
            activation_kind="initial",
            activation_owner_kind="host_run_boundary",
            activation_owner_id="boundary:initial",
            driver_factory=driver,
            observer=None,
        )
        assert not isinstance(segment, RunSegmentInstallBlocked)
        await started.wait()
        assert segment.driver_task is not None
        await segment.driver_task
        assert owner.active_segment is segment
    finally:
        loop.set_task_factory(prior_factory)


@_async_test
async def test_termination_intent_blocks_segment_without_calling_factory() -> None:
    registry, _owner = _registry()
    intent = RunTerminationIntent(
        intent_id="intent:1",
        kind="user_stop",
        requested_at_utc="2026-07-12T01:02:03Z",
        requester_id="user:1",
        target_segment_id=None,
        target_segment_generation=None,
    )
    assert registry.install_termination_intent("run:1", intent)[0] == "installed"
    called = False

    async def driver() -> None:
        return None

    def factory():
        nonlocal called
        called = True
        return driver()

    result = registry.install_segment(
        "run:1",
        activation_kind="interaction_resume",
        activation_owner_kind="host_run_boundary",
        activation_owner_id="boundary:initial",
        driver_factory=factory,
        observer=None,
    )
    assert isinstance(result, RunSegmentInstallBlocked)
    assert result.reason == "termination_intent_present"
    assert called is False


@_async_test
async def test_suspended_stop_intent_blocks_post_commit_resume_segment_install(
    monkeypatch,
) -> None:
    registry, owner = _registry()
    predecessor = SimpleNamespace(authority_fingerprint="authority:1")
    installed = InstalledRunAuthorityRevision.model_construct(
        head_kind="installed_revision",
        revision=predecessor,
        head_fingerprint="head:1",
    )
    owner.authority_head = installed
    _install_suspended_state(owner)
    _install_fake_suspended_receipt(owner)
    monkeypatch.setattr(
        "pulsara_agent.runtime.run_execution.registry.materialize_continuation_revision",
        lambda **_kwargs: SimpleNamespace(
            authority_fingerprint="authority:2", revision=2
        ),
    )
    monkeypatch.setattr(
        "pulsara_agent.runtime.run_execution.registry.installed_authority_head",
        lambda _revision: installed,
    )
    monkeypatch.setattr(
        "pulsara_agent.runtime.run_execution.registry.event_reference_from_stored",
        lambda *_args, **_kwargs: cast(Any, object()),
    )
    incoming = _handles("handles:incoming", state="boundary_owned")
    intent = RunTerminationIntent(
        intent_id="intent:stop",
        kind="host_teardown",
        requested_at_utc="2026-07-12T01:02:03Z",
        requester_id="host:close",
        target_segment_id=None,
        target_segment_generation=None,
    )
    registry.install_termination_intent("run:1", intent)
    committed = registry.commit_continuation_activation_full(
        run_id="run:1",
        stored_boundary=SimpleNamespace(id="boundary:resume"),
        stored_exposure=cast(Any, object()),
        effective_model_target=cast(Any, object()),
        effective_permission=cast(Any, object()),
        expected_predecessor_fingerprint="authority:1",
        expected_termination_revision=owner.termination_revision,
        expected_current_handle_id="handles:1",
        incoming=incoming,
        reuse_current_handles=False,
        expected_interaction_fingerprint="interaction:1",
    )
    assert committed.resource_disposition == "activation_blocked"
    assert owner.execution_handles.handle_id == "handles:1"
    assert incoming.state == "closed"

    async def must_not_run() -> None:
        raise AssertionError("driver must not start after termination intent")

    blocked = registry.install_segment(
        "run:1",
        activation_kind="interaction_resume",
        activation_owner_kind="host_run_boundary",
        activation_owner_id="boundary:initial",
        driver_factory=must_not_run,
        observer=None,
    )
    assert isinstance(blocked, RunSegmentInstallBlocked)


@_async_test
async def test_handle_swap_retires_old_without_releasing_live_borrow(
    monkeypatch,
) -> None:
    registry, owner = _registry()
    predecessor = SimpleNamespace(authority_fingerprint="authority:1")
    installed = InstalledRunAuthorityRevision.model_construct(
        head_kind="installed_revision",
        revision=predecessor,
        head_fingerprint="head:1",
    )
    owner.authority_head = installed
    _install_suspended_state(owner)
    _install_fake_suspended_receipt(owner)
    monkeypatch.setattr(
        "pulsara_agent.runtime.run_execution.registry.materialize_continuation_revision",
        lambda **_kwargs: SimpleNamespace(
            authority_fingerprint="authority:2", revision=2
        ),
    )
    monkeypatch.setattr(
        "pulsara_agent.runtime.run_execution.registry.installed_authority_head",
        lambda _revision: installed,
    )
    monkeypatch.setattr(
        "pulsara_agent.runtime.run_execution.registry.event_reference_from_stored",
        lambda *_args, **_kwargs: cast(Any, object()),
    )
    owner.execution_handles.borrow_tracker.borrow_child_tool_call()
    incoming = _handles("handles:incoming", state="boundary_owned")
    result = registry.commit_continuation_activation_full(
        run_id="run:1",
        stored_boundary=SimpleNamespace(id="boundary:resume"),
        stored_exposure=cast(Any, object()),
        effective_model_target=cast(Any, object()),
        effective_permission=cast(Any, object()),
        expected_predecessor_fingerprint="authority:1",
        expected_termination_revision=owner.termination_revision,
        expected_current_handle_id="handles:1",
        incoming=incoming,
        reuse_current_handles=False,
        expected_interaction_fingerprint="interaction:1",
    )
    assert result.resource_disposition == "swapped"
    old = owner.retiring_execution_handles["handles:1"]
    assert old.state == "retiring"
    assert old.borrow_tracker.can_retire() is False
    with pytest.raises(RuntimeError):
        old.mark_closed()
    old.borrow_tracker.release_child_tool_call()
    assert old.state == "closed"
    assert "handles:1" not in owner.retiring_execution_handles


@_async_test
async def test_deferred_borrow_release_removes_confirmed_run_owner() -> None:
    registry, owner = _registry()
    handles = owner.execution_handles
    handles.borrow_tracker.borrow_parent_tool_call()
    owner.finalization_owner.commit_state = "confirmed"
    owner.finalization_slot.state = "completed"

    assert registry.retire_confirmed("run:1") is False
    assert handles.state == "retiring"
    assert registry.owner_count == 1

    handles.borrow_tracker.release_parent_tool_call()

    assert handles.state == "closed"
    assert owner.retiring_execution_handles == {}
    assert registry.owner_count == 0


def test_retiring_or_closed_handle_rejects_late_child_borrow() -> None:
    handles = _handles()
    authority = handles.borrow_authority
    handles.mark_retiring()

    with pytest.raises(CapabilityExecutionBorrowUnavailable):
        authority.borrow_child_tool_call()
    with pytest.raises(CapabilityExecutionBorrowUnavailable):
        handles.borrow_tracker.borrow_child_tool_call()

    handles.mark_closed()
    with pytest.raises(CapabilityExecutionBorrowUnavailable):
        authority.borrow_child_tool_call()


@_async_test
async def test_cancelled_sync_tool_keeps_borrow_until_worker_thread_finishes() -> None:
    registry, owner = _registry()
    handles = owner.execution_handles
    authority = handles.borrow_authority
    started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    def blocking_operation():
        started.set()
        release_worker.wait()
        worker_finished.set()
        return cast(Any, object())

    authority.borrow_parent_tool_call()
    task = asyncio.create_task(
        _await_sync_tool_thread(
            blocking_operation,
            release_borrow=authority.release_parent_tool_call,
        )
    )
    await asyncio.to_thread(started.wait)
    task.cancel()
    await asyncio.sleep(0.01)

    assert task.done() is False
    assert handles.borrow_tracker.active_parent_tool_call_borrows == 1
    assert worker_finished.is_set() is False
    owner.finalization_owner.commit_state = "confirmed"
    owner.finalization_slot.state = "completed"
    assert registry.retire_confirmed("run:1") is False
    with pytest.raises(TimeoutError):
        await registry.wait_until_retired("run:1", timeout_seconds=0.01)

    release_worker.set()
    await asyncio.to_thread(worker_finished.wait)
    # The helper returns the worker outcome so its enclosing tool-batch driver
    # can durably settle the admitted call before propagating cancellation.
    assert await task is not None
    await registry.wait_until_retired("run:1", timeout_seconds=1.0)
    assert handles.state == "closed"
    assert registry.owner_count == 0


@_async_test
async def test_stale_segment_completion_cannot_clear_new_segment() -> None:
    registry, owner = _registry()

    async def driver() -> None:
        return None

    first = registry.install_segment(
        "run:1",
        activation_kind="initial",
        activation_owner_kind="host_run_boundary",
        activation_owner_id="boundary:initial",
        driver_factory=driver,
        observer=None,
    )
    assert not isinstance(first, RunSegmentInstallBlocked)
    await cast(asyncio.Task[object], first.driver_task)
    first_result = RunActivationCoordinatorResult(
        segment_id=first.segment_id,
        segment_generation=first.segment_generation,
        disposition="waiting_user",
        outcome=cast(Any, object()),
    )
    first_carrier = first.state_carrier
    first_token = first.state_owner_token
    assert first_carrier is not None and first_token is not None
    pending_token = "run-pending:test:second"
    first_carrier.transfer(
        expected_owner_token=first_token,
        new_owner_token=pending_token,
    )
    first.state_carrier = None
    first.state_owner_token = None
    owner.pending_activation_state = first_carrier
    owner.pending_activation_owner_token = pending_token
    assert (
        registry.complete_segment(
            "run:1",
            segment_id=first.segment_id,
            segment_generation=first.segment_generation,
            result=first_result,
        )
        == "completed"
    )

    second = registry.install_segment(
        "run:1",
        activation_kind="initial",
        activation_owner_kind="host_run_boundary",
        activation_owner_id="boundary:initial",
        driver_factory=driver,
        observer=None,
    )
    assert not isinstance(second, RunSegmentInstallBlocked)
    stale = registry.complete_segment(
        "run:1",
        segment_id=first.segment_id,
        segment_generation=first.segment_generation,
        result=first_result,
    )
    assert stale == "stale_segment"
    assert owner.active_segment is second
    await cast(asyncio.Task[object], second.driver_task)


def test_borrow_tracker_contains_only_in_flight_tool_call_borrows() -> None:
    tracker = CapabilityExecutionBorrowTracker()
    assert not hasattr(tracker, "child_lifetime_borrows")
    assert not hasattr(tracker, "pending_mcp_interaction_leases")
    assert not hasattr(tracker, "promote_pending_mcp_lease")
    assert not hasattr(tracker, "complete_pending_mcp_lease")
    tracker.borrow_child_tool_call()
    assert tracker.can_retire() is False
    tracker.release_child_tool_call()
    assert tracker.can_retire() is True


def test_stream_turn_registers_owner_before_first_pull(tmp_path, monkeypatch) -> None:
    from tests.test_host_lifecycle_contract import (
        ScriptedTransport,
        _core,
        _open,
    )

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "done"}], delay=0.05))
        session = await _open(core, tmp_path, host_session_id="host:stream-ingress")
        stream = session.stream_turn("hello")
        boundary_task = session._current_boundary_task()
        assert boundary_task is not None
        assert boundary_task.done() is False
        assert session._boundary_attempt is not None
        assert session._boundary_attempt.owner_task is boundary_task
        assert session._boundary_attempt.phase.value == "ingress"
        prepared = session._boundary_attempt.prepared_activation
        assert prepared is not None
        assert session._boundary_attempt.draft_run_id == prepared.run_id
        await stream.aclose()
        # Observer close is detach-only; Host close remains the cancellation owner.
        assert session._current_boundary_task() is not None
        await core.shutdown()

    asyncio.run(scenario())


def test_run_turn_waiter_cancellation_detaches_without_stopping_run(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import (
        ScriptedTransport,
        _core,
        _open,
    )
    from pulsara_agent.event import RunEndEvent

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "done"}], delay=0.1))
        session = await _open(core, tmp_path, host_session_id="host:waiter-detach")
        service = session.wiring.run_activation_service
        assert service is not None
        waiter = asyncio.create_task(session.run_turn("hello"))
        for _ in range(100):
            if service.active_host_run_view() is not None:
                break
            await asyncio.sleep(0.005)
        view = service.active_host_run_view()
        assert view is not None and view.active_driver_running
        assert session._boundary_attempt is None
        assert session.summary()["boundary"]["state"] == "committed"
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert service.active_host_run_view() is not None
        await service.wait_active_driver(view.run_id, timeout_seconds=10.0)
        assert any(isinstance(event, RunEndEvent) for event in session.replay_events())
        assert not hasattr(session, "_active_state")
        assert service.resident_owner_count() == 0
        await core.shutdown()

    asyncio.run(scenario())


def test_preparing_boundary_is_visible_and_explicit_stop_creates_no_run_start(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from pulsara_agent.event import RunStartEvent

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "unused"}]))
        session = await _open(core, tmp_path, host_session_id="host:preparing-stop")
        entered = asyncio.Event()
        release = asyncio.Event()
        original = type(session)._prepare_and_commit_new_run_boundary

        async def blocked_prepare(self, **kwargs):
            entered.set()
            await release.wait()
            return await original(self, **kwargs)

        monkeypatch.setattr(
            type(session),
            "_prepare_and_commit_new_run_boundary",
            blocked_prepare,
        )
        stream = session.stream_turn("hello")
        await entered.wait()
        live = session.summary()["boundary"]
        assert live["state"] == "preparing"
        assert live["durable_run_existence"] == "none"
        assert live["boundary_id"] is not None
        assert live["draft_run_id"] is not None

        result = await session.stop_current_turn()
        assert result is not None
        assert result.status == "cancelled_before_run_start"
        assert result.durable_run_existence.value == "none"
        assert not any(
            isinstance(event, RunStartEvent) for event in session.replay_events()
        )
        await stream.aclose()
        await core.shutdown()

    asyncio.run(scenario())


def test_committed_run_start_owner_install_failure_writes_run_end(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from pulsara_agent.event import RunEndEvent, RunStartEvent

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "unused"}]))
        session = await _open(core, tmp_path, host_session_id="host:owner-fail")

        def fail_owner(*_args, **_kwargs) -> None:
            raise RuntimeError("synthetic owner install failure")

        monkeypatch.setattr(
            type(session),
            "_register_committed_host_run_owner",
            fail_owner,
        )
        with pytest.raises(RuntimeError, match="owner install failure"):
            await session.run_turn("hello")
        events = session.replay_events()
        assert len([event for event in events if isinstance(event, RunStartEvent)]) == 1
        assert len([event for event in events if isinstance(event, RunEndEvent)]) == 1
        service = session.wiring.run_activation_service
        assert service is not None and service.resident_owner_count() == 0
        await core.shutdown()

    asyncio.run(scenario())


def test_activation_settlement_failure_cannot_leave_an_active_run_owner(
    tmp_path,
    monkeypatch,
) -> None:
    import json

    from tests.test_host_lifecycle_contract import (
        ScriptedTransport,
        _core,
        _open,
        _trusted_terminal_ask_policy,
    )
    from pulsara_agent.event import RunEndEvent

    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:activation-settlement-failure",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "pwd"}),
                    }
                ]
            }
        ]
    )

    async def scenario() -> None:
        core = _core(monkeypatch, transport)
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:activation-settlement-failure",
            policy=_trusted_terminal_ask_policy(),
        )

        def fail_suspension_install(*_args, **_kwargs) -> None:
            raise RuntimeError("synthetic suspension owner install failure")

        monkeypatch.setattr(
            type(session._interaction_transition_port),
            "install_suspension",
            fail_suspension_install,
        )
        await session.run_turn("run pwd after approval")

        [run_end] = [
            event for event in session.replay_events() if isinstance(event, RunEndEvent)
        ]
        service = session.wiring.run_activation_service
        assert service is not None
        view = service.run_view(run_end.run_id)
        if view is not None:
            assert view.active_segment_id is None
            assert view.active_driver_running is False
            assert view.lifecycle in {
                "terminalizing",
                "terminal",
                "reconciliation_required",
            }
        await core.shutdown()

    asyncio.run(scenario())


def test_run_end_persistent_failure_keeps_owner_until_retry(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from pulsara_agent.event import RunEndEvent
    from pulsara_agent.runtime.session import EventCommitError, RuntimeSession

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "done"}]))
        session = await _open(core, tmp_path, host_session_id="host:run-end-retry")
        original_write_events = RuntimeSession.write_events
        failures = 0

        async def fail_run_end(self, events, **kwargs):
            nonlocal failures
            if any(isinstance(event, RunEndEvent) for event in events):
                failures += 1
                raise EventCommitError("synthetic RunEnd store outage")
            return await original_write_events(self, events, **kwargs)

        monkeypatch.setattr(RuntimeSession, "write_events", fail_run_end)
        pending = await session.run_turn("hello")
        assert isinstance(pending, RunTerminalizationPending)
        deadline = asyncio.get_running_loop().time() + 2.0
        while failures < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert failures >= 2
        assert not any(
            isinstance(event, RunEndEvent) for event in session.replay_events()
        )
        assert session.stopping_run_id is not None
        service = session.wiring.run_activation_service
        assert service is not None
        view = service.run_view(session.stopping_run_id)
        assert view is not None and view.terminal_state != "confirmed"
        assert view.run_completion_done is False

        completion = asyncio.create_task(
            service.wait_run_completion(pending.owner_identity.run_id)
        )
        await asyncio.sleep(0)
        monkeypatch.setattr(RuntimeSession, "write_events", original_write_events)
        terminal = await asyncio.wait_for(completion, timeout=10)
        assert terminal.output.status == "finished"
        assert any(isinstance(event, RunEndEvent) for event in session.replay_events())
        assert service.resident_owner_count() == 0
        await core.shutdown()

    asyncio.run(scenario())


def test_boundary_projection_uses_writer_owned_outcome_without_ledger_requery(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from tests.support.events import typed_non_transcript_event
    from pulsara_agent.primitives.run_boundary import BoundaryBatchCommitStatus

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "unused"}]))
        session = await _open(core, tmp_path, host_session_id="host:conflict")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_prepare(_self, **_kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("stop boundary")

        monkeypatch.setattr(
            type(session), "_prepare_and_commit_new_run_boundary", blocked_prepare
        )
        stream = session.stream_turn("hello")
        await entered.wait()
        attempt = session._boundary_attempt
        assert attempt is not None and attempt.prepared_activation is not None
        state = attempt.prepared_activation.peek_for_registry(
            boundary_id=attempt.boundary_id
        )
        candidate = typed_non_transcript_event(
            id="boundary-candidate:conflict",
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
            name="candidate",
            value={"value": 1},
        )
        session._set_boundary_candidates((candidate,))
        session._set_boundary_commit_state("commit_outcome_unknown")
        session._set_boundary_commit_confirmation(BoundaryBatchCommitStatus.UNKNOWN)
        session.wiring.runtime_wiring.event_log.append(
            typed_non_transcript_event(
                id=candidate.id,
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                name="candidate",
                value={"value": 2},
            )
        )
        confirmation = session._boundary_batch_confirmation(attempt)
        assert confirmation is not None
        assert confirmation.status is BoundaryBatchCommitStatus.UNKNOWN
        assert confirmation.committed_event_ids == ()
        release.set()
        await asyncio.gather(attempt.owner_task, return_exceptions=True)
        await stream.aclose()
        await core.shutdown()

    asyncio.run(scenario())


def test_run_start_publication_failure_is_audited_as_publication_failure(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from pulsara_agent.event import RunEndEvent, RunStartEvent
    from pulsara_agent.runtime.session import EventPublicationAfterCommitError

    class FailingObserver:
        async def on_published_event(self, _published) -> None:
            raise RuntimeError("synthetic publication failure")

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "unused"}]))
        session = await _open(core, tmp_path, host_session_id="host:publish-fail")
        session.wiring.runtime_wiring.runtime_session.publisher.subscribe(
            FailingObserver()
        )
        with pytest.raises(EventPublicationAfterCommitError):
            await session.run_turn("hello")
        events = session.replay_events()
        assert len([event for event in events if isinstance(event, RunStartEvent)]) == 1
        [ended] = [event for event in events if isinstance(event, RunEndEvent)]
        assert ended.stop_reason == "runtime_publication_failure"
        service = session.wiring.run_activation_service
        assert service is not None and service.resident_owner_count() == 0
        await core.shutdown()

    asyncio.run(scenario())


def test_committed_resume_fold_failure_terminalizes_original_run(
    tmp_path, monkeypatch
) -> None:
    import json

    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from pulsara_agent.event import RunEndEvent, RunInteractionResumeBoundaryEvent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.runtime.approval import ApprovalResolution, ToolApprovalDecision
    from pulsara_agent.runtime.permission import preset_to_policy

    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:resume-fold",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "pwd"}),
                    }
                ]
            },
            {"text": "unused"},
        ]
    )

    async def scenario() -> None:
        core = _core(monkeypatch, transport)
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:resume-fold-fail",
            policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS),
        )
        first = await session.run_turn("run terminal")
        pending = session.get_pending_approval()
        assert pending is not None

        def fail_fold(*_args, **_kwargs):
            raise RuntimeError("synthetic committed resume fold failure")

        monkeypatch.setattr(
            type(session._interaction_transition_port),
            "fold_committed_resume_boundary",
            fail_fold,
        )
        with pytest.raises(RuntimeError, match="resume fold failure"):
            await session.resolve_approval(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=(
                        ToolApprovalDecision(
                            tool_call_id="call:resume-fold",
                            confirmed=True,
                        ),
                    ),
                )
            )
        events = session.replay_events()
        assert any(
            isinstance(event, RunInteractionResumeBoundaryEvent) for event in events
        )
        [ended] = [
            event
            for event in events
            if isinstance(event, RunEndEvent)
            and event.run_id == first.owner_identity.run_id
        ]
        assert ended.stop_reason == "runtime_execution_error"
        service = session.wiring.run_activation_service
        assert service is not None and service.resident_owner_count() == 0
        await core.shutdown()

    asyncio.run(scenario())
