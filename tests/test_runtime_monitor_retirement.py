from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from time import monotonic
from types import MethodType, SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.repository import AcceptedEntry, ConversationKernelConflict
from pulsara_agent.conversation_kernel.safe_point import ProviderSafePointCoordinator
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.terminal_observation import ExistingTurnInstallation, NewTurnInstallation
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.terminal_process.manager import ProcessRegistry
from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator, TerminalMonitorPolicy
from tests.test_round2_terminal_monitor import _spawn_sleeping_process


@pytest.fixture
def monitors(tmp_path):
    owner, session = random_id("host"), random_id("session")
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    coordinator = TerminalMonitorCoordinator(
        session_id=session, owner_epoch=owner, registry=registry,
        live_bus=LiveAgentEventBus(), wake_scheduler=lambda: None,
    )
    other_hosts = []

    def another_host():
        other = TerminalMonitorCoordinator(
            session_id=random_id("session"), owner_epoch=random_id("host"),
            registry=registry, live_bus=LiveAgentEventBus(), wake_scheduler=lambda: None,
        )
        other_hosts.append(other)
        return other

    def register(*, turn=None, committed=True, root=False, **policy):
        state = _spawn_sleeping_process(registry, owner, tmp_path)
        turn = turn or random_id("turn")
        prepared = coordinator.prepare_registration(
            process_id=state.process_id, origin_turn_id=turn,
            origin_attempt_id=random_id("attempt"),
            origin_result_entry_id=random_id("entry"), writer_generation=1,
            authorization_reference="policy:retirement",
            recipient_session_id=session,
            registering_agent_id=None if root else session,
            work_turn_id=None if root else turn,
            policy=TerminalMonitorPolicy(
                min_new_output_chars=1, quiet_period_ms=0,
                minimum_progress_interval_seconds=1800, **policy,
            ),
        )
        if committed:
            coordinator.settle_registration(prepared, committed=True)
        return state, prepared, turn

    def freeze(state, prepared, turn):
        state.output.append_raw(b"retained original observation")
        coordinator._evaluate(prepared.monitor_id, monotonic() + 2000)
        attempt = coordinator.freeze(
            monitor_id=prepared.monitor_id,
            target=ExistingTurnInstallation(turn, random_id("entry")),
            workspace_id=random_id("workspace"), writer_generation=1, actor_id=owner,
        )
        assert attempt is not None
        return attempt

    try:
        yield SimpleNamespace(
            coordinator=coordinator, registry=registry, register=register, freeze=freeze,
            owner=owner, session=session,
            another_host=another_host,
        )
    finally:
        for other in other_hosts:
            other.stop_admission_and_close(timeout_seconds=2)
        coordinator.stop_admission_and_close(timeout_seconds=2)
        registry.release_owner(owner, timeout_seconds=5)


@pytest.mark.parametrize("committed", [True, False])
def test_closed_registration_keeps_token_until_exact_settlement(monitors, committed):
    c = monitors.coordinator
    state, prepared, _ = monitors.register(committed=False)
    assert c.cancel(prepared.monitor_id) == "cancelled"
    assert prepared.monitor_id in c._registrations
    assert c._tokens[prepared.token_id] is prepared
    assert c.list_current() == c.freeze_compaction_handoff() == ()
    with pytest.raises(RuntimeError, match="token conflicts"):
        c.settle_registration(replace(prepared), committed=committed)
    assert c._tokens[prepared.token_id] is prepared
    c.settle_registration(prepared, committed=committed)
    assert prepared.token_id not in c._tokens
    assert prepared.monitor_id not in c._registrations
    assert c.cancel(prepared.monitor_id) == "not_found"
    c._output_changed(prepared.monitor_id, b"copied callback arrived late")
    c._evaluate(prepared.monitor_id, monotonic() + 100000)
    c.process_completed(state.process_id, status="success", exit_code=0)
    assert c._registrations == {}
    assert state.process.poll() is None


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("close", ["cancel", "work", "host"])
def test_closed_frozen_attempt_keeps_owner_until_settlement(monitors, accepted, close):
    c = monitors.coordinator
    state, prepared, turn = monitors.register()
    attempt = monitors.freeze(state, prepared, turn)
    if close == "cancel":
        assert c.cancel(prepared.monitor_id) == "cancelled"
    elif close == "work":
        c.close_work(turn)
    else:
        c.stop_admission_and_close(timeout_seconds=2)
    assert c.current_installation_attempt(prepared.monitor_id) is attempt
    assert c.list_current() == c.freeze_compaction_handoff() == ()
    assert c.cancel(prepared.monitor_id) == "already_terminal"
    c.settle_installation(attempt, accepted=accepted)
    assert prepared.monitor_id not in c._registrations
    assert c.pending_monitor_ids() == ()
    with pytest.raises(RuntimeError, match="stale"):
        c.settle_installation(attempt, accepted=accepted)
    c._output_changed(prepared.monitor_id, b"late")
    assert c._registrations == {}


def test_close_work_deletes_multiple_registrations_without_mutating_iteration(monitors):
    c = monitors.coordinator
    turn = random_id("turn")
    registrations = [monitors.register(turn=turn)[1] for _ in range(3)]
    _, dormant, _ = monitors.register(turn=turn, committed=False)
    c.close_work(turn)
    assert set(c._registrations) == {dormant.monitor_id}
    assert all(c.registration_scope(item.monitor_id) is None for item in registrations)
    c.settle_registration(dormant, committed=False)
    assert c._registrations == {}


def test_cleanup_failure_cannot_be_mistaken_for_completed_close(monitors, monkeypatch):
    c = monitors.coordinator
    _, prepared, turn = monitors.register(committed=False)
    unsubscribe = monitors.registry.unsubscribe_output

    def fail(*_args, **_kwargs):
        raise RuntimeError("unsubscribe failed")

    monkeypatch.setattr(monitors.registry, "unsubscribe_output", fail)
    with pytest.raises(RuntimeError, match="unsubscribe failed"):
        c.cancel(prepared.monitor_id)
    assert c.list_current() == ()  # Closed fact is already effective.
    c.settle_registration(prepared, committed=True)
    assert prepared.monitor_id in c._registrations
    monkeypatch.setattr(monitors.registry, "unsubscribe_output", unsubscribe)
    c.close_work(turn)
    assert prepared.monitor_id not in c._registrations


@pytest.mark.parametrize("kind", ["completion", "expiry"])
def test_terminal_observation_settlement_deletes_registration(monitors, kind):
    c = monitors.coordinator
    state, prepared, turn = monitors.register(maximum_duration_seconds=1 if kind == "expiry" else 36000)
    if kind == "completion":
        c.process_completed(state.process_id, status="success", exit_code=0)
    attempt = monitors.freeze(state, prepared, turn)
    assert attempt.content.observation_kind.value == kind.upper()
    c.settle_installation(attempt, accepted=True)
    assert prepared.monitor_id not in c._registrations


@pytest.mark.parametrize("scope", ["deleted", "foreign-work", "foreign-agent", "foreign-host"])
def test_missing_monitor_message_preserves_result_classification_and_visibility(monitors, scope):
    c = monitors.coordinator
    state, prepared, turn = monitors.register()
    target = {"kind": "MONITOR", "monitor_id": prepared.monitor_id}
    if scope == "deleted":
        assert c.cancel(prepared.monitor_id) == "cancelled"
    context = SimpleNamespace(
        conversation_scope_kind="SUBAGENT",
        session_id=random_id("session") if scope == "foreign-agent" else monitors.session,
        turn_id=random_id("turn") if scope == "foreign-work" else turn,
    )
    if scope == "foreign-host":
        # Another Host has its own owner; no inspection of the original is needed.
        tool = SimpleNamespace(coordinator=monitors.another_host())
    else:
        tool = SimpleNamespace(coordinator=c)
    results = []
    for name, arguments in (
        ("manage_monitor", {"action": "cancel", "target": target}),
        ("query_monitors", {"target": target}),
    ):
        result, token = DirectKernelToolPort._invoke_terminal_monitor(
            SimpleNamespace(), tool=tool,
            call=ToolCall(id=f"call:{name}", name=name, arguments=arguments), context=context,
        )
        assert token is None
        results.append((result, json.loads(result.output)))
    (cancel, cancel_body), (query, query_body) = results
    assert cancel.status is ToolResultState.SUCCESS
    assert cancel_body["status"] == "REJECTED"
    assert cancel_body["cancellation_outcome"] == "not_found"
    assert cancel_body["target"] == target
    assert query.status is ToolResultState.ERROR
    assert query_body["status"] == "REJECTED"
    assert query_body["reason"] == "MONITOR_UNAVAILABLE"
    expected = (
        "The monitor is unavailable in the current scope. It may have been cancelled, "
        "expired, or reclaimed. Use query_monitors without a target to list currently visible monitors."
    )
    assert cancel_body["message"] == query_body["message"] == expected
    assert state.process.poll() is None
    if scope != "deleted":
        assert c.inspect_current(prepared.monitor_id) is not None


class _InlineIO:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


def _delivery_host(monitors, repository, *, closing=False):
    safe_point = ProviderSafePointCoordinator(
        repository=repository,
        guard=HostWriterGuard(monitors.session, 1, monitors.owner),
    )

    async def release():
        pass

    return SimpleNamespace(
        session_id=monitors.session, host_session_id=monitors.owner,
        workspace=SimpleNamespace(workspace_key=random_id("workspace")),
        _tools=SimpleNamespace(terminal_monitor_coordinator=monitors.coordinator),
        _runner=SimpleNamespace(_safe_point=safe_point), _io=_InlineIO(),
        _monitor_wake=asyncio.Event(), _closing=closing,
        _canonical_deadline=lambda: monotonic() + 5,
        # The Agent already moved to another Work; old notices cannot follow it.
        active_root_turn_id=lambda: random_id("turn"),
        _release_any_terminal_new_turn_reservation=release,
    )


@pytest.mark.parametrize("outcome", ["full", "none", "conflict"])
def test_work_terminal_confirms_old_attempt_before_work_gate(monitors, outcome):
    async def scenario():
        c = monitors.coordinator
        state, prepared, turn = monitors.register()
        attempt = monitors.freeze(state, prepared, turn)
        c.close_work(turn)
        seen = asyncio.Event()

        class Repository:
            def confirm_terminal_observation_winner(self, _guard, *, candidate, **_kwargs):
                assert candidate is attempt
                assert candidate.target.turn_id == turn
                seen.set()
                if outcome == "conflict":
                    raise ConversationKernelConflict("exact winner conflicts")
                return AcceptedEntry(candidate.target.entry_id, turn, 2, 2) if outcome == "full" else None

        host = _delivery_host(monitors, Repository())
        host._monitor_wake.set()
        task = asyncio.create_task(KernelHostSession._terminal_monitor_delivery_loop(host))
        try:
            await asyncio.wait_for(seen.wait(), timeout=2)
            assert prepared.monitor_id not in c._registrations
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_exact_confirmation_conflict_settles_owner_and_preserves_diagnostic(monitors):
    c = monitors.coordinator
    state, prepared, turn = monitors.register()
    attempt = monitors.freeze(state, prepared, turn)
    c.close_work(turn)

    class Repository:
        def confirm_terminal_observation_winner(self, _guard, *, candidate, **_kwargs):
            assert candidate is attempt
            raise ConversationKernelConflict("exact winner names another entry")

    safe_point = ProviderSafePointCoordinator(
        repository=Repository(), guard=HostWriterGuard(monitors.session, 1, monitors.owner),
    )
    with pytest.raises(ConversationKernelConflict, match="exact winner names another entry"):
        safe_point.prepare_terminal_observation_installation(
            coordinator=c, monitor_id=prepared.monitor_id, target=attempt.target,
            workspace_id=attempt.workspace_id, actor_id=monitors.owner,
            deadline_monotonic=monotonic() + 5,
        )
    assert prepared.monitor_id not in c._registrations


@pytest.mark.parametrize("closing", [False, True])
@pytest.mark.parametrize("later", ["full", "none", "conflict"])
def test_unknown_ends_this_object_attempt_and_allows_other_confirmation(monitors, closing, later):
    async def scenario():
        c = monitors.coordinator
        first_state, first, first_turn = monitors.register()
        second_state, second, second_turn = monitors.register()
        old = monitors.freeze(first_state, first, first_turn)
        next_attempt = monitors.freeze(second_state, second, second_turn)
        c.close_work(first_turn)
        c.close_work(second_turn)
        seen = asyncio.Event()
        calls = []
        unknown = True

        class Repository:
            def confirm_terminal_observation_winner(self, _guard, *, candidate, **_kwargs):
                calls.append(candidate)
                if candidate is old:
                    if unknown:
                        raise TimeoutError("confirmation unavailable; UNKNOWN")
                    if later == "conflict":
                        raise ConversationKernelConflict("late exact conflict")
                    return AcceptedEntry(candidate.target.entry_id, first_turn, 2, 2) if later == "full" else None
                assert candidate is next_attempt
                seen.set()
                return AcceptedEntry(candidate.target.entry_id, second_turn, 2, 2)

        host = _delivery_host(monitors, Repository(), closing=closing)
        host._monitor_wake.set()
        task = asyncio.create_task(KernelHostSession._terminal_monitor_delivery_loop(host))
        try:
            await asyncio.wait_for(seen.wait(), timeout=2)
            assert calls == [old, next_attempt]
            assert c.current_installation_attempt(first.monitor_id) is old
            assert second.monitor_id not in c._registrations
            if closing:
                await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        # A later invocation uses the same exact confirmation path.  UNKNOWN
        # never became NONE just because the original Work/Host ended.
        unknown = False
        confirmation = dict(
            coordinator=c, monitor_id=first.monitor_id, target=old.target,
            workspace_id=old.workspace_id, actor_id=monitors.owner,
            deadline_monotonic=monotonic() + 5,
        )
        if later == "conflict":
            with pytest.raises(ConversationKernelConflict, match="late exact conflict"):
                host._runner._safe_point.prepare_terminal_observation_installation(**confirmation)
        else:
            result = host._runner._safe_point.prepare_terminal_observation_installation(**confirmation)
            if later == "none":
                assert result is old
                c.settle_installation(result, accepted=False)
            else:
                assert isinstance(result, AcceptedEntry)
        assert c._registrations == {}

    asyncio.run(scenario())


def test_terminal_work_without_frozen_attempt_cannot_publish_old_draft(monitors, monkeypatch):
    async def scenario():
        c = monitors.coordinator
        state, prepared, turn = monitors.register()
        state.output.append_raw(b"mutable old Work draft")
        c._evaluate(prepared.monitor_id, monotonic() + 2000)
        assert c.current_installation_attempt(prepared.monitor_id) is None
        assert c.pending_monitor_ids() == (prepared.monitor_id,)
        closed = asyncio.Event()
        close_work = c.close_work

        def close(value):
            assert value == turn
            close_work(value)
            closed.set()

        monkeypatch.setattr(c, "close_work", close)
        # No repository publication or confirmation API is available here.
        host = _delivery_host(monitors, SimpleNamespace())
        host._monitor_wake.set()
        task = asyncio.create_task(KernelHostSession._terminal_monitor_delivery_loop(host))
        try:
            await asyncio.wait_for(closed.wait(), timeout=2)
            assert c._registrations == {}
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


@pytest.mark.parametrize("close_error", [False, True])
def test_new_turn_start_failure_releases_own_reservation_and_dispatch(monitors, close_error, caplog):
    async def scenario():
        c = monitors.coordinator
        state, prepared, _ = monitors.register(root=True)
        state.output.append_raw(b"new-turn observation")
        c._evaluate(prepared.monitor_id, monotonic() + 2000)
        original_error = RuntimeError("TODO continuation run is closing")
        dispatch = SimpleNamespace(closed=False)

        def close_dispatch():
            dispatch.closed = True
            if close_error:
                raise RuntimeError("dispatch cleanup failed")

        dispatch.close = close_dispatch

        def bind_closing_todo(**_kwargs):
            raise original_error

        class Repository:
            def pending_prompt_head_mode(self, **_kwargs):
                return None

        host = _delivery_host(monitors, Repository())
        host.repository = Repository()
        host._tools.todo_owner = SimpleNamespace(bind_continuation_if_present=bind_closing_todo)
        host._session_scope_kind = "ROOT"
        host._compaction = SimpleNamespace(is_fenced=lambda **_: False)
        host._lock = asyncio.Lock()
        host._active_task = host._active_turn_id = None
        host.root_session_id = None
        host._plan_exit_fence = host._external_new_turn_accepting = False
        host._terminal_new_turn_observation_id = None
        host._external_new_turn_settled = asyncio.Event()
        host._external_new_turn_settled.set()
        host._queue_wake = asyncio.Event()
        host._retire_done_active_root_locked = lambda: None
        writer = object()
        host._reserve_compaction_write_locked = lambda **_: writer

        async def release_writer(value):
            assert value is writer

        host._release_compaction_write_reservation = release_writer
        host._release_terminal_new_turn_reservation = MethodType(
            KernelHostSession._release_terminal_new_turn_reservation, host)
        host._release_any_terminal_new_turn_reservation = MethodType(
            KernelHostSession._release_any_terminal_new_turn_reservation, host)
        host._start_terminal_observation_turn = MethodType(
            KernelHostSession._start_terminal_observation_turn, host)

        async def publish(**kwargs):
            target = kwargs["target"]
            assert isinstance(target, NewTurnInstallation)
            attempt = c.freeze(
                monitor_id=prepared.monitor_id, target=target,
                workspace_id=host.workspace.workspace_key, writer_generation=1,
                actor_id=monitors.owner,
            )
            assert attempt is not None
            c.settle_installation(attempt, accepted=True)
            # Publication is accepted, then Host close wins before local start.
            host._closing = True
            return AcceptedEntry(target.initial_entry_id, target.turn_id, 2, 2), dispatch

        host._runner.install_terminal_observation = publish
        host._monitor_wake.set()
        with pytest.raises(RuntimeError) as raised:
            await KernelHostSession._terminal_monitor_delivery_loop(host)
        assert raised.value is original_error
        assert host._terminal_new_turn_observation_id is None
        assert not host._external_new_turn_accepting
        assert host._external_new_turn_settled.is_set()
        assert dispatch.closed
        assert c.current_installation_attempt(prepared.monitor_id) is None
        assert c.inspect_current(prepared.monitor_id)["observation_ordinal"] == 1
        if close_error:
            assert "Terminal observation dispatch cleanup failed" in caplog.text

    asyncio.run(scenario())


def test_failed_new_turn_start_cannot_release_another_observation_reservation():
    async def scenario():
        other = random_id("observation")
        closed = []
        host = SimpleNamespace(
            _lock=asyncio.Lock(), _retire_done_active_root_locked=lambda: None,
            _terminal_new_turn_observation_id=other,
            _external_new_turn_accepting=True,
            _external_new_turn_settled=asyncio.Event(), _queue_wake=asyncio.Event(),
            _active_task=None,
        )
        host._release_terminal_new_turn_reservation = MethodType(
            KernelHostSession._release_terminal_new_turn_reservation, host)
        with pytest.raises(RuntimeError, match="lost its local admission"):
            await KernelHostSession._start_terminal_observation_turn(
                host, random_id("turn"), random_id("observation"),
                prospective_root_dispatch=SimpleNamespace(close=lambda: closed.append(True)),
            )
        assert closed == [True]
        assert host._terminal_new_turn_observation_id == other
        assert host._external_new_turn_accepting
        assert not host._external_new_turn_settled.is_set()

    asyncio.run(scenario())
