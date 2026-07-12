from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, cast

import pytest

from pulsara_agent.host.run_boundary import (
    BoundaryExecutionHandles,
    CapabilityExecutionBorrowUnavailable,
    CapabilityExecutionBorrowTracker,
    CommittedRunExecutionOwner,
    RunExecutionOwnerRegistry,
    RunExecutionSegmentResult,
    RunSegmentInstallBlocked,
    RunTerminationIntent,
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
) -> BoundaryExecutionHandles:
    return BoundaryExecutionHandles(
        handle_id=handle_id,
        handle_generation=1,
        owner_id="run:1" if state == "run_owned" else "boundary:resume",
        state=cast(Any, state),
        mcp_installation=object(),
        capability_runtime=object(),
        tool_registry=object(),
        frozen_execution_surface=cast(Any, object()),
    )


def _registry() -> tuple[RunExecutionOwnerRegistry, CommittedRunExecutionOwner]:
    registry = RunExecutionOwnerRegistry()
    owner = CommittedRunExecutionOwner(
        entry=cast(Any, object()),
        execution_handles=_handles(),
        retiring_execution_handles={},
        terminal_event_id="event:run-end",
        terminal_candidate=None,
        terminal_state="open",
        terminalization_task=None,
        termination_intent=None,
        run_completion=asyncio.get_running_loop().create_future(),
        next_segment_generation=0,
        active_segment=None,
        latest_activation_owner_kind="host_run_boundary",
        latest_activation_owner_id="boundary:initial",
    )
    registry.register("run:1", owner)
    return registry, owner


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
async def test_suspended_stop_intent_blocks_post_commit_resume_segment_install() -> None:
    registry, owner = _registry()
    incoming = _handles("handles:incoming", state="attempt_owned")
    intent = RunTerminationIntent(
        intent_id="intent:stop",
        kind="host_teardown",
        requested_at_utc="2026-07-12T01:02:03Z",
        requester_id="host:close",
        target_segment_id=None,
        target_segment_generation=None,
    )
    registry.install_termination_intent("run:1", intent)
    swapped = registry.swap_execution_handles_after_continuation_commit(
        "run:1",
        expected_current_handle_id="handles:1",
        incoming=incoming,
        committed_continuation_event_id="boundary:resume",
    )
    assert swapped.status == "swap_skipped_terminating"
    assert owner.execution_handles.handle_id == "handles:1"
    assert incoming.state == "attempt_owned"

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
async def test_handle_swap_retires_old_without_releasing_live_borrow() -> None:
    registry, owner = _registry()
    owner.execution_handles.borrow_tracker.borrow_child_tool_call()
    incoming = _handles("handles:incoming", state="attempt_owned")
    result = registry.swap_execution_handles_after_continuation_commit(
        "run:1",
        expected_current_handle_id="handles:1",
        incoming=incoming,
        committed_continuation_event_id="boundary:resume",
    )
    assert result.status == "swapped"
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
    owner.terminal_state = "confirmed"

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
    owner.terminal_state = "confirmed"
    assert registry.retire_confirmed("run:1") is False
    with pytest.raises(TimeoutError):
        await registry.wait_until_retired("run:1", timeout_seconds=0.01)

    release_worker.set()
    await asyncio.to_thread(worker_finished.wait)
    with pytest.raises(asyncio.CancelledError):
        await task
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
    first_result = RunExecutionSegmentResult(
        segment_id=first.segment_id,
        segment_generation=first.segment_generation,
        disposition="run_terminal",
        run_result=cast(Any, object()),
    )
    assert registry.complete_segment(
        "run:1",
        segment_id=first.segment_id,
        segment_generation=first.segment_generation,
        result=first_result,
    ) == "completed"

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
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "done"}], delay=0.05))
        session = await _open(core, tmp_path, host_session_id="host:stream-ingress")
        stream = session.stream_turn("hello")
        assert session._boundary_task is not None
        assert session._boundary_task.done() is False
        assert session._boundary_attempt is not None
        assert session._boundary_attempt.owner_task is session._boundary_task
        assert session._boundary_attempt.phase.value == "ingress"
        assert session._boundary_attempt.draft_run_id == session._preparing_state.run_id
        await stream.aclose()
        # Observer close is detach-only; Host close remains the cancellation owner.
        assert session._boundary_task is not None
        await core.shutdown()

    asyncio.run(scenario())


def test_run_turn_waiter_cancellation_detaches_without_stopping_run(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from pulsara_agent.event import RunEndEvent

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "done"}], delay=0.1))
        session = await _open(core, tmp_path, host_session_id="host:waiter-detach")
        waiter = asyncio.create_task(session.run_turn("hello"))
        for _ in range(100):
            if session._active_task is not None:
                break
            await asyncio.sleep(0.005)
        segment_task = session._active_task
        assert segment_task is not None
        assert session._boundary_attempt is None
        assert session.summary()["boundary"]["state"] == "committed"
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert segment_task.cancelled() is False
        await segment_task
        assert any(
            isinstance(event, RunEndEvent) for event in session.replay_events()
        )
        assert session._active_state is None
        assert session._run_execution_owners.owner_count == 0
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
        assert session._run_execution_owners.owner_count == 0
        await core.shutdown()

    asyncio.run(scenario())


def test_run_end_persistent_failure_keeps_owner_until_retry(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from pulsara_agent.event import RunEndEvent
    from pulsara_agent.runtime.session import RuntimeSession

    async def scenario() -> None:
        core = _core(monkeypatch, ScriptedTransport([{"text": "done"}]))
        session = await _open(core, tmp_path, host_session_id="host:run-end-retry")
        original_emit = RuntimeSession.emit
        failures = 0

        async def fail_run_end(self, event, **kwargs):
            nonlocal failures
            if isinstance(event, RunEndEvent):
                failures += 1
                raise RuntimeError("synthetic RunEnd store outage")
            return await original_emit(self, event, **kwargs)

        monkeypatch.setattr(RuntimeSession, "emit", fail_run_end)
        with pytest.raises(RuntimeError, match="RunEnd store outage"):
            await session.run_turn("hello")
        assert failures >= 2
        assert not any(
            isinstance(event, RunEndEvent) for event in session.replay_events()
        )
        assert session.active_run_id is not None
        owner = session._run_execution_owners.require(session.active_run_id)
        assert owner.terminal_state != "confirmed"
        assert owner.run_completion.done() is False

        monkeypatch.setattr(RuntimeSession, "emit", original_emit)
        result = await session.stop_current_turn()
        assert result is not None
        assert result.state.finalized is True
        assert any(isinstance(event, RunEndEvent) for event in session.replay_events())
        assert session._run_execution_owners.owner_count == 0
        await core.shutdown()

    asyncio.run(scenario())


def test_boundary_confirmation_detects_same_id_different_payload(
    tmp_path, monkeypatch
) -> None:
    from tests.test_host_lifecycle_contract import ScriptedTransport, _core, _open
    from pulsara_agent.event import CustomEvent
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
        state = session._preparing_state
        assert attempt is not None and state is not None
        candidate = CustomEvent(
            id="boundary-candidate:conflict",
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
            name="candidate",
            value={"value": 1},
        )
        session._set_boundary_candidates((candidate,))
        session.wiring.runtime_wiring.event_log.append(
            candidate.model_copy(update={"value": {"value": 2}})
        )
        confirmation = session._boundary_batch_confirmation(attempt)
        assert confirmation is not None
        assert confirmation.status is BoundaryBatchCommitStatus.CONFLICT
        assert session.wiring.runtime_wiring.runtime_session.reconciliation_required
        release.set()
        await asyncio.gather(session._boundary_task, return_exceptions=True)
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
        assert session._run_execution_owners.owner_count == 0
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
            type(session), "_fold_committed_resume_boundary", fail_fold
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
            isinstance(event, RunInteractionResumeBoundaryEvent)
            for event in events
        )
        [ended] = [
            event
            for event in events
            if isinstance(event, RunEndEvent) and event.run_id == first.state.run_id
        ]
        assert ended.stop_reason == "runtime_execution_error"
        assert session._run_execution_owners.owner_count == 0
        await core.shutdown()

    asyncio.run(scenario())
