"""Deterministic guards for the workspace terminal lifecycle contract.

These tests pin the §11.2 / §12 behaviors of
contracts/WORKSPACE_TERMINAL_LIFECYCLE_CONTRACT.zh.md: fail-closed identity,
rollback-safe open, exactly-once lease release with capacity recovery, a single
drainable execution handle, auditable host-teardown finalization, lifecycle
linearization, off-lock teardown, and shared-capacity diagnostics.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import AsyncIterator

import pytest

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    RunEndEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.host import (
    DuplicateHostSessionError,
    HostCore,
    HostCoreLifecycle,
    HostSession,
    HostSessionBusyError,
    HostSessionLifecycle,
    HostSessionRegistry,
    HostWorkspaceInput,
    WorkspaceClosingError,
)
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile, ModelRole
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.runtime import AbortKind, ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    TerminalAccess,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.terminal import (
    BorrowedWorkspaceTerminalRuntime,
    TerminalOwnerContext,
    TerminalRequest,
    TerminalSessionManager,
    TerminalStatus,
)
from pulsara_agent.settings import PulsaraSettings, StorageConfig


class ScriptedTransport:
    api = "scripted"

    def __init__(self, replies: list[dict], *, delay: float = 0) -> None:
        self.replies = replies
        self.delay = delay
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self.contexts.append(context)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        if "text" in reply:
            yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:1")
            yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:1", delta=reply["text"])
            yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:1")
        for call in reply.get("tool_calls", []):
            yield ToolCallStartEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                tool_call_name=call["name"],
            )
            yield ToolCallDeltaEvent(**event_context.event_fields(), tool_call_id=call["id"], delta=call["arguments"])
            yield ToolCallEndEvent(**event_context.event_fields(), tool_call_id=call["id"])
        yield ModelCallEndEvent(**event_context.event_fields())


def _settings() -> PulsaraSettings:
    return PulsaraSettings(
        llm=LLMConfig(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="scripted",
        ),
        storage=StorageConfig(postgres_dsn="", oxigraph_url="http://127.0.0.1:1"),
    )


def _core(monkeypatch, transport: ScriptedTransport) -> HostCore:
    settings = _settings()
    registry = LLMTransportRegistry()
    registry.register(transport)
    core = HostCore(settings=settings, durable=False)

    def _patched_runtime(_config):
        return LLMRuntime(config=settings.llm, registry=registry)

    import pulsara_agent.runtime.wiring as wiring

    monkeypatch.setattr(wiring, "build_llm_runtime", _patched_runtime)
    return core


def _trusted_terminal_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.RISKY_ONLY,
        terminal=TerminalAccess.ALLOW,
    )


def _trusted_terminal_ask_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.RISKY_ONLY,
        terminal=TerminalAccess.ASK,
    )


async def _open(core, root, *, host_session_id="host:test", conversation_id=None, policy=None):
    return await core.open_session(
        HostWorkspaceInput(workspace_kind="project", workspace_root=root, memory_domain_id="u_test"),
        host_session_id=host_session_id,
        conversation_id=conversation_id or f"conversation:{host_session_id}",
        model_role=ModelRole.FLASH,
        memory_reflection=False,
        permission_policy=policy,
    )


# --- §12.1 identity + open transaction ---------------------------------------


def test_duplicate_host_session_id_fail_closed_and_does_not_disturb_live_owner(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:a", "name": "terminal", "arguments": json.dumps({"command": "sleep 10", "yield_time_ms": 0})}]},
            {"text": "a started"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        a = await _open(core, tmp_path, host_session_id="host:dup", policy=_trusted_terminal_policy())
        await a.run_turn("start a process")
        manager = a.wiring.runtime_wiring.runtime_session.terminal_sessions
        a_proc = manager.list_owned("host:dup")[0].process_id
        with pytest.raises(DuplicateHostSessionError):
            await _open(core, tmp_path, host_session_id="host:dup", conversation_id="conversation:other")
        same = await core.get_session("host:dup")
        proc_status = manager.poll_process(a_proc).status
        sessions = await core.list_sessions()
        await core.shutdown()
        return same is a, proc_status, len(sessions)

    is_same, proc_status, count = asyncio.run(run())
    assert is_same  # old session not overwritten
    assert proc_status is TerminalStatus.RUNNING  # old owner's process untouched
    assert count == 1  # the duplicate never registered


def test_duplicate_conversation_id_fail_closed(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "a"}])
    core = _core(monkeypatch, transport)

    async def run():
        a = await _open(core, tmp_path, host_session_id="host:a", conversation_id="conversation:shared")
        with pytest.raises(DuplicateHostSessionError):
            await _open(core, tmp_path, host_session_id="host:b", conversation_id="conversation:shared")
        resolved = await core.find_by_conversation("conversation:shared")
        sessions = await core.list_sessions()
        await core.shutdown()
        return resolved is a, len(sessions)

    resolves_to_a, count = asyncio.run(run())
    assert resolves_to_a  # conversation index not hijacked by the rejected open
    assert count == 1


def test_stale_reservation_cannot_release_or_publish_aba_successor() -> None:
    registry = HostSessionRegistry()

    async def run():
        stale = await registry.reserve("host:aba", "conversation:aba")
        await registry.release_reservation(stale)
        current = await registry.reserve("host:aba", "conversation:aba")
        assert current.token != stale.token

        # A delayed rollback from the first open transaction must not consume
        # the second transaction's reservation for the same identities.
        await registry.release_reservation(stale)
        with pytest.raises(DuplicateHostSessionError):
            await registry.reserve("host:aba", "conversation:other")
        with pytest.raises(RuntimeError, match="consumed or released"):
            await registry.publish(stale, None)  # type: ignore[arg-type]

        await registry.release_reservation(current)
        replacement = await registry.reserve("host:aba", "conversation:aba")
        return stale.token, current.token, replacement.token

    stale_token, current_token, replacement_token = asyncio.run(run())
    assert len({stale_token, current_token, replacement_token}) == 3


def test_registry_capacity_failure_leaks_no_supervisor_owner(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "a"}])
    core = _core(monkeypatch, transport)
    core.registry = HostSessionRegistry(max_sessions=1)

    async def run():
        await _open(core, tmp_path, host_session_id="host:a")
        with pytest.raises(RuntimeError, match="limit"):
            await _open(core, tmp_path, host_session_id="host:b")
        snapshots = await core.list_workspace_terminal_snapshots()
        await core.shutdown()
        return snapshots

    snapshots = asyncio.run(run())
    assert len(snapshots) == 1
    assert snapshots[0].owner_session_count == 1  # only host:a; the over-capacity open left no lease


def test_wiring_failure_rolls_back_lease_and_reservation(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "second open ok"}])
    core = _core(monkeypatch, transport)
    import pulsara_agent.host.core as core_mod

    original = core_mod.build_agent_runtime_wiring
    calls = {"n": 0}

    def maybe_boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("wiring boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(core_mod, "build_agent_runtime_wiring", maybe_boom)

    async def run():
        with pytest.raises(RuntimeError, match="wiring boom"):
            await _open(core, tmp_path, host_session_id="host:fail")
        # After rollback: no leaked supervisor lease, and the identity is free.
        leaked = await core.list_workspace_terminal_snapshots()
        reopened = await _open(core, tmp_path, host_session_id="host:fail")
        sessions = await core.list_sessions()
        await core.shutdown()
        return leaked, reopened.host_session_id, len(sessions)

    leaked, reopened_id, count = asyncio.run(run())
    assert leaked == []  # lease released on the failed open
    assert reopened_id == "host:fail"  # reservation released; id reusable
    assert count == 1


# --- §12.2 session close ------------------------------------------------------


def test_session_close_is_idempotent_and_releases_owner_exactly_once(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:bg", "name": "terminal", "arguments": json.dumps({"command": "sleep 30", "yield_time_ms": 0, "terminal_session_id": "work"})}]},
            {"text": "started"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        s = await _open(core, tmp_path, host_session_id="host:cap", policy=_trusted_terminal_policy())
        await s.run_turn("start bg")
        manager = s.wiring.runtime_wiring.runtime_session.terminal_sessions
        proc = manager.list_owned("host:cap")[0].process_id
        sessions_before = manager.session_count()
        await core.close_session("host:cap")
        status_after = manager.poll_process(proc).status
        sessions_after = manager.session_count()
        await core.close_session("host:cap")  # idempotent, no-op, no error
        await core.shutdown()
        return sessions_before, sessions_after, status_after

    before, after, status = asyncio.run(run())
    assert before >= 1
    assert after == 0  # owner terminal sessions pruned: capacity restored (P0-7)
    assert status is TerminalStatus.KILLED


def test_begin_close_closes_mutation_gate_and_concurrent_close_waits(tmp_path, monkeypatch) -> None:
    core = _core(monkeypatch, ScriptedTransport([{"text": "unused"}]))
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    original_close = HostSession.aclose

    async def delayed_close(self, *, reason=AbortKind.HOST_TEARDOWN):
        close_entered.set()
        await release_close.wait()
        return await original_close(self, reason=reason)

    monkeypatch.setattr(HostSession, "aclose", delayed_close)

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:closing-gate")
        first = asyncio.create_task(core.close_session(session.host_session_id))
        await close_entered.wait()
        assert session.lifecycle is HostSessionLifecycle.CLOSING
        with pytest.raises(RuntimeError, match="closed"):
            session.set_permission_mode("read-only")
        with pytest.raises(RuntimeError, match="closed"):
            await core.stop_current_turn(session.host_session_id)
        second = asyncio.create_task(core.close_session(session.host_session_id))
        await asyncio.sleep(0)
        assert not second.done()
        release_close.set()
        await asyncio.gather(first, second)
        return session.lifecycle, await core.list_sessions()

    lifecycle, sessions = asyncio.run(run())
    assert lifecycle is HostSessionLifecycle.CLOSED
    assert sessions == []


def test_session_cleanup_failure_does_not_wedge_registry_close(tmp_path, monkeypatch) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))

    async def release_boom(self, lease):
        raise RuntimeError("lease release boom")

    monkeypatch.setattr(HostCore, "_release_supervisor_lease", release_boom)

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:cleanup-error")
        with pytest.raises(RuntimeError, match="lease release boom"):
            await core.close_session(session.host_session_id)
        # The close transaction is finalized despite cleanup failure: the
        # identity is gone and a repeated close cannot wait forever.
        assert await core.list_sessions() == []
        await asyncio.wait_for(core.close_session(session.host_session_id), timeout=0.2)
        await core.shutdown()
        return session.lifecycle

    assert asyncio.run(run()) is HostSessionLifecycle.CLOSED


def test_repeated_owner_release_does_not_exhaust_shared_session_capacity(tmp_path, monkeypatch) -> None:
    # The §15.2 experiment: an anchor keeps the supervisor alive while ephemeral
    # owners reuse the default terminal and close. With kill_owned-only this hit
    # "terminal session limit reached: max 4" by the 4th ephemeral; release_owner
    # must prune stale session keys so capacity recovers.
    replies: list[dict] = []
    for i in range(7):
        replies.append({"tool_calls": [{"id": f"call:{i}", "name": "terminal", "arguments": json.dumps({"command": "printf ok"})}]})
        replies.append({"text": f"ok {i}"})
    core = _core(monkeypatch, ScriptedTransport(replies))

    async def run():
        anchor = await _open(core, tmp_path, host_session_id="host:anchor", policy=_trusted_terminal_policy())
        await anchor.run_turn("anchor uses terminal")
        manager = anchor.wiring.runtime_wiring.runtime_session.terminal_sessions
        for i in range(6):
            s = await _open(core, tmp_path, host_session_id=f"host:eph-{i}", policy=_trusted_terminal_policy())
            await s.run_turn(f"use terminal {i}")
            await core.close_session(f"host:eph-{i}")
        count = manager.session_count()
        await core.shutdown()
        return count

    count = asyncio.run(run())
    assert count == 1  # only the anchor's session remains


def test_borrowed_runtime_close_does_not_touch_shared_manager(tmp_path) -> None:
    manager = TerminalSessionManager(tmp_path)
    owner = TerminalOwnerContext(host_session_id="host:x", conversation_id="conversation:x")
    rs = RuntimeSession(tmp_path, terminal_binding=BorrowedWorkspaceTerminalRuntime(owner=owner, manager=manager))
    manager.get_or_create("work", owner_host_session_id="host:x")
    assert manager.session_count() == 1
    assert rs.terminal_owner_host_session_id == "host:x"
    assert rs.terminal_owner_conversation_id == "conversation:x"
    rs.close()
    # Borrowed close releases nothing in the shared manager; lease release is the
    # supervisor/HostCore job (contract §5).
    assert manager.session_count() == 1


def test_owned_runtime_close_is_idempotent(tmp_path) -> None:
    rs = RuntimeSession(tmp_path)
    assert rs._owns_terminal_manager is True
    rs.close()
    rs.close()  # second close is a no-op


def test_close_active_streaming_run_emits_auditable_host_teardown(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "streaming"}], delay=0.3)
    core = _core(monkeypatch, transport)

    async def run():
        s = await _open(core, tmp_path, host_session_id="host:stream")
        events: list[AgentEvent] = []

        async def consume():
            try:
                async for event in s.stream_turn("go"):
                    events.append(event)
            except Exception:
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        owned = s._active_task is not None  # the HostSession owns the streaming task
        await core.close_session("host:stream")
        await consumer
        return owned, events, s.closed

    owned, events, closed = asyncio.run(run())
    assert owned  # streaming turn is drainable via the owned handle (P0-6)
    assert closed
    run_ends = [e for e in events if isinstance(e, RunEndEvent)]
    assert run_ends and run_ends[-1].status == "aborted"
    assert run_ends[-1].abort_kind == "host_teardown"  # not masqueraded as user_stop


def test_close_suspended_run_emits_auditable_host_teardown(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [{"tool_calls": [{"id": "call:danger", "name": "terminal", "arguments": json.dumps({"command": "rm -rf build"})}]}]
    )
    core = _core(monkeypatch, transport)

    async def run():
        s = await _open(core, tmp_path, host_session_id="host:susp", policy=_trusted_terminal_policy())
        first = await s.run_turn("danger")
        assert s.get_pending_approval() is not None
        await core.close_session("host:susp")
        events = s.replay_events()
        return events, first.state.run_id, s.closed

    events, run_id, closed = asyncio.run(run())
    assert closed
    run_ends = [e for e in events if isinstance(e, RunEndEvent) and e.run_id == run_id]
    # The suspended run is not silently dropped; it gets a terminal RunEnd.
    assert any(e.status == "aborted" and e.abort_kind == "host_teardown" for e in run_ends)


def test_streaming_resume_is_owned_by_host_session(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:ask", "name": "terminal", "arguments": json.dumps({"command": "printf ok"})}]},
            {"text": "resumed"},
        ],
        delay=0.2,
    )
    core = _core(monkeypatch, transport)

    async def run():
        s = await _open(core, tmp_path, host_session_id="host:resume", policy=_trusted_terminal_ask_policy())
        await s.run_turn("do x")  # suspends on terminal-ask approval
        pending = s.get_pending_approval()
        events: list[AgentEvent] = []

        async def consume():
            async for event in s.stream_approval_resolution(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=tuple(ToolApprovalDecision(tool_call_id=c.id, confirmed=True) for c in pending.tool_calls),
                )
            ):
                events.append(event)

        resume_task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        owned = s._active_task is not None  # resume runs under the same owned handle
        await resume_task
        await core.shutdown()
        return owned

    owned = asyncio.run(run())
    assert owned


def test_stream_observer_is_bounded_and_detach_does_not_cancel_run(tmp_path, monkeypatch) -> None:
    from pulsara_agent.host.session import _STREAM_QUEUE_MAX_ITEMS

    total_deltas = _STREAM_QUEUE_MAX_ITEMS * 3

    class BurstTransport:
        api = "scripted"

        def __init__(self) -> None:
            self.produced = 0

        async def stream(self, *, model, context, event_context, options=None):
            yield ModelCallStartEvent(
                **event_context.event_fields(),
                model_name=model.id,
                model_role=model.role.value,
                provider=model.provider,
            )
            yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:burst")
            for _ in range(total_deltas):
                self.produced += 1
                yield TextBlockDeltaEvent(
                    **event_context.event_fields(),
                    block_id="text:burst",
                    delta="x",
                )
            yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:burst")
            yield ModelCallEndEvent(**event_context.event_fields())

    transport = BurstTransport()
    core = _core(monkeypatch, transport)  # type: ignore[arg-type]

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:bounded-stream")
        stream = session.stream_turn("burst")
        await anext(stream)
        await asyncio.sleep(0.05)
        produced_while_attached = transport.produced
        assert produced_while_attached < total_deltas
        assert produced_while_attached <= _STREAM_QUEUE_MAX_ITEMS + 4

        await stream.aclose()  # transport observer detaches; execution stays owned
        for _ in range(100):
            if session._active_task is None:
                break
            await asyncio.sleep(0.01)
        assert session._active_task is None
        await core.shutdown()
        return produced_while_attached, transport.produced

    produced_while_attached, produced_after_detach = asyncio.run(run())
    assert produced_while_attached < total_deltas
    assert produced_after_detach == total_deltas


def test_detached_stream_remains_active_and_blocks_second_run(tmp_path, monkeypatch) -> None:
    paused = asyncio.Event()
    release = asyncio.Event()

    class PausingTransport:
        api = "scripted"

        async def stream(self, *, model, context, event_context, options=None):
            yield ModelCallStartEvent(
                **event_context.event_fields(),
                model_name=model.id,
                model_role=model.role.value,
                provider=model.provider,
            )
            paused.set()
            await release.wait()
            yield ModelCallEndEvent(**event_context.event_fields())

    core = _core(monkeypatch, PausingTransport())  # type: ignore[arg-type]

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:detached-stream")
        stream = session.stream_turn("pause")
        await anext(stream)
        await paused.wait()
        task = session._active_task
        await stream.aclose()
        assert task is session._active_task and task is not None and not task.done()
        with pytest.raises(HostSessionBusyError):
            await session.run_turn("must not overlap")
        release.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=1)
        await core.shutdown()
        return session.active_run_id

    assert asyncio.run(run()) is None


def test_host_close_aborts_run_after_stream_observer_detaches(tmp_path, monkeypatch) -> None:
    paused = asyncio.Event()

    class PausingTransport:
        api = "scripted"

        async def stream(self, *, model, context, event_context, options=None):
            yield ModelCallStartEvent(
                **event_context.event_fields(),
                model_name=model.id,
                model_role=model.role.value,
                provider=model.provider,
            )
            paused.set()
            await asyncio.Event().wait()

    core = _core(monkeypatch, PausingTransport())  # type: ignore[arg-type]

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:detached-close")
        stream = session.stream_turn("pause")
        await anext(stream)
        await paused.wait()
        await stream.aclose()
        assert session._active_task is not None
        await core.close_session(session.host_session_id)
        return session.replay_events()

    events = asyncio.run(run())
    run_ends = [event for event in events if isinstance(event, RunEndEvent)]
    assert run_ends and run_ends[-1].abort_kind == AbortKind.HOST_TEARDOWN.value


# --- §12.4 workspace close ----------------------------------------------------


def test_workspace_close_rejects_concurrent_open_in_same_workspace(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "a"}, {"text": "b"}])
    core = _core(monkeypatch, transport)
    reached = asyncio.Event()
    release = asyncio.Event()
    original_close = HostCore.close_session

    async def barrier_close(self, host_session_id):
        reached.set()
        await release.wait()
        return await original_close(self, host_session_id)

    monkeypatch.setattr(HostCore, "close_session", barrier_close)

    async def run():
        a = await _open(core, tmp_path, host_session_id="host:a")
        close_task = asyncio.create_task(core.close_workspace(a.workspace.workspace_key))
        await reached.wait()  # supervisor is CLOSING, paused inside close_session
        with pytest.raises(WorkspaceClosingError):
            await _open(core, tmp_path, host_session_id="host:b")
        release.set()
        await close_task
        return await core.list_sessions()

    sessions = asyncio.run(run())
    assert sessions == []


def test_workspace_close_invalidates_lease_acquired_by_unpublished_open(tmp_path, monkeypatch) -> None:
    """The open-before-close interleaving must lose at its publish boundary."""
    core = _core(monkeypatch, ScriptedTransport([]))
    attached = asyncio.Event()
    release = asyncio.Event()
    original_attach = HostCore._attach_supervisor

    async def barrier_after_attach(self, workspace, host_session_id, conversation_id):
        lease = await original_attach(self, workspace, host_session_id, conversation_id)
        attached.set()
        await release.wait()
        return lease

    monkeypatch.setattr(HostCore, "_attach_supervisor", barrier_after_attach)

    async def run():
        opening = asyncio.create_task(_open(core, tmp_path, host_session_id="host:mid-open"))
        await attached.wait()
        workspace_key, supervisor = next(iter(core._supervisors.items()))
        manager = supervisor.terminal_sessions
        await core.close_workspace(workspace_key)
        with pytest.raises(RuntimeError, match="closed"):
            manager.get_or_create(owner_host_session_id="host:mid-open")
        release.set()
        with pytest.raises(WorkspaceClosingError):
            await opening
        return await core.list_sessions(), await core.list_workspace_terminal_snapshots()

    sessions, supervisors = asyncio.run(run())
    assert sessions == []
    assert supervisors == []


# --- §12.5 HostCore shutdown --------------------------------------------------


def test_shutdown_gate_rejects_start_and_continue_facades_and_is_idempotent(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "ok"}])
    core = _core(monkeypatch, transport)

    async def run():
        await _open(core, tmp_path, host_session_id="host:gate")
        await core.shutdown()
        assert core.lifecycle is HostCoreLifecycle.CLOSED
        with pytest.raises(RuntimeError, match="closing|closed"):
            await _open(core, tmp_path, host_session_id="host:after")
        with pytest.raises(RuntimeError, match="closing|closed"):
            await core.set_permission_mode("host:gate", "read-only")
        with pytest.raises(RuntimeError, match="closing|closed"):
            await core.enter_plan("host:gate")
        with pytest.raises(RuntimeError, match="closing|closed"):
            await core.resolve_approval(
                "host:gate",
                ApprovalResolution(approval_id="x", decisions=()),
            )
        await core.shutdown()  # idempotent
        return await core.list_sessions()

    sessions = asyncio.run(run())
    assert sessions == []


def test_concurrent_shutdown_waits_for_owner_even_when_cleanup_fails(monkeypatch) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    close_entered = asyncio.Event()
    release_close = asyncio.Event()

    class FailingResources:
        async def aclose(self):
            close_entered.set()
            await release_close.wait()
            raise RuntimeError("retrieval close boom")

    core._retrieval_resources = FailingResources()  # type: ignore[assignment]

    async def run():
        owner = asyncio.create_task(core.shutdown())
        await close_entered.wait()
        waiter = asyncio.create_task(core.shutdown())
        await asyncio.sleep(0)
        assert not waiter.done()
        release_close.set()
        with pytest.raises(RuntimeError, match="retrieval close boom"):
            await owner
        await asyncio.wait_for(waiter, timeout=0.2)
        return core.lifecycle

    assert asyncio.run(run()) is HostCoreLifecycle.CLOSED


def test_open_parked_at_attach_then_shutdown_leaves_no_session(tmp_path, monkeypatch) -> None:
    # Linearization (P0-2): an open that passed the entry check but is still mid
    # transaction when shutdown completes must roll back, never leak a session
    # into a closed HostCore.
    transport = ScriptedTransport([{"text": "late"}])
    core = _core(monkeypatch, transport)
    reached = asyncio.Event()
    release = asyncio.Event()
    original_attach = HostCore._attach_supervisor

    async def barrier_attach(self, workspace, host_session_id, conversation_id):
        if host_session_id == "host:late":
            reached.set()
            await release.wait()
        return await original_attach(self, workspace, host_session_id, conversation_id)

    monkeypatch.setattr(HostCore, "_attach_supervisor", barrier_attach)

    async def run():
        open_task = asyncio.create_task(_open(core, tmp_path, host_session_id="host:late"))
        await reached.wait()
        await core.shutdown()  # completes while the open is parked
        release.set()
        with pytest.raises(RuntimeError):
            await open_task  # the open aborts instead of leaking
        return await core.list_sessions()

    sessions = asyncio.run(run())
    assert sessions == []


def test_nonstream_close_waits_for_abort_finalization(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "late"}], delay=0.3)
    core = _core(monkeypatch, transport)
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:nonstream-close")
        runtime_type = type(session.wiring.agent_runtime)
        original_abort = runtime_type.abort_run

        async def delayed_abort(self, state, *, reason):
            abort_started.set()
            await release_abort.wait()
            return await original_abort(self, state, reason=reason)

        monkeypatch.setattr(runtime_type, "abort_run", delayed_abort)
        caller = asyncio.create_task(session.run_turn("hold"))
        await asyncio.sleep(0.05)
        closing = asyncio.create_task(core.close_session(session.host_session_id))
        await abort_started.wait()
        assert not closing.done()
        assert not session.closed
        release_abort.set()
        await closing
        result = await caller
        return result, session.replay_events()

    result, events = asyncio.run(run())
    assert result.status.value == "aborted"
    run_ends = [event for event in events if isinstance(event, RunEndEvent)]
    assert run_ends and run_ends[-1].abort_kind == AbortKind.HOST_TEARDOWN.value


# --- §12.6 event-loop responsiveness -----------------------------------------


def test_slow_owner_release_runs_off_lock_and_does_not_block_other_workspace(tmp_path, monkeypatch) -> None:
    ws_a = tmp_path / "A"
    ws_a.mkdir()
    ws_b = tmp_path / "B"
    ws_b.mkdir()
    transport = ScriptedTransport([{"text": "a"}, {"text": "b"}])
    core = _core(monkeypatch, transport)
    import pulsara_agent.runtime.terminal.manager as manager_mod

    original_release = manager_mod.TerminalSessionManager.release_owner

    def slow_release(self, owner_host_session_id):
        time.sleep(0.3)  # synchronous, would block the loop if run on it
        return original_release(self, owner_host_session_id)

    monkeypatch.setattr(manager_mod.TerminalSessionManager, "release_owner", slow_release)

    async def run():
        await _open(core, ws_a, host_session_id="host:a")
        close_task = asyncio.create_task(core.close_session("host:a"))
        await asyncio.sleep(0.02)  # let the close reach the off-lock kill
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await _open(core, ws_b, host_session_id="host:b")  # different workspace
        elapsed = loop.time() - t0
        await close_task
        await core.shutdown()
        return elapsed

    elapsed = asyncio.run(run())
    assert elapsed < 0.2  # not serialized behind the 0.3s synchronous teardown


def test_release_owner_is_thread_safe_with_concurrent_sibling_session_create(tmp_path) -> None:
    manager = TerminalSessionManager(tmp_path, max_sessions=8)
    released_session = manager.get_or_create("one", owner_host_session_id="host:a")
    manager.get_or_create("two", owner_host_session_id="host:a")
    iteration_started = threading.Event()

    class SlowIterDict(dict):
        def __iter__(self):
            iterator = super().__iter__()
            yield next(iterator)
            iteration_started.set()
            time.sleep(0.1)
            yield from iterator

    manager._sessions = SlowIterDict(manager._sessions)
    errors: list[BaseException] = []

    def release_owner():
        try:
            manager.release_owner("host:a")
        except BaseException as exc:
            errors.append(exc)

    def create_sibling_session():
        try:
            manager.get_or_create("work", owner_host_session_id="host:b")
        except BaseException as exc:
            errors.append(exc)

    release_thread = threading.Thread(target=release_owner)
    release_thread.start()
    assert iteration_started.wait(timeout=1)
    create_thread = threading.Thread(target=create_sibling_session)
    create_thread.start()
    release_thread.join(timeout=2)
    create_thread.join(timeout=2)

    assert not release_thread.is_alive()
    assert not create_thread.is_alive()
    assert errors == []
    assert manager.owner_session_counts() == {"host:b": 1}
    stale_result = released_session.execute(TerminalRequest(command="printf should-not-run"))
    assert stale_result.status is TerminalStatus.BLOCKED
    assert "released" in (stale_result.error or "")


# --- §12.7 shared capacity ----------------------------------------------------


def test_shared_capacity_limit_error_reports_owner_distribution(tmp_path, monkeypatch) -> None:
    # Two host sessions sharing one workspace each create named terminal sessions
    # until the workspace-wide ceiling trips; the error must name the owners.
    replies: list[dict] = []
    for owner_idx, name in [("a", "one"), ("a", "two"), ("b", "three"), ("b", "four"), ("b", "five")]:
        replies.append(
            {"tool_calls": [{"id": f"call:{name}", "name": "terminal", "arguments": json.dumps({"command": "printf ok", "terminal_session_id": name})}]}
        )
        replies.append({"text": f"ok {name}"})
    core = _core(monkeypatch, ScriptedTransport(replies))

    async def run():
        a = await _open(core, tmp_path, host_session_id="host:a", policy=_trusted_terminal_policy())
        b = await _open(core, tmp_path, host_session_id="host:b", policy=_trusted_terminal_policy())
        await a.run_turn("named one")
        await a.run_turn("named two")
        await b.run_turn("named three")
        await b.run_turn("named four")
        await b.run_turn("named five")  # 5th named session over the default max of 4
        snapshot = (await core.list_workspace_terminal_snapshots())[0]
        await core.shutdown()
        return snapshot

    snapshot = asyncio.run(run())
    # Owner distribution is visible for diagnostics even at/under the ceiling.
    assert snapshot.terminal_session_count == 4
    assert snapshot.owner_session_distribution.get("host:a", 0) >= 1
    assert snapshot.owner_session_distribution.get("host:b", 0) >= 1
