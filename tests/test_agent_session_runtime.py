"""Physical scheduling and retirement of canonical Agent requests."""
from datetime import datetime, timedelta, timezone
from functools import wraps
from types import SimpleNamespace
import asyncio
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.subagent import AgentSessionRuntime


def asynchronous(function):
    @wraps(function)
    def run():
        return asyncio.run(function())
    return run


class Repository:
    def __init__(self, rows=()):
        self.rows = {row['id']: dict(row) for row in rows}
        self.resolved = []
        self.marked = []

    def list_all_agent_sessions(self, **kwargs):
        return tuple(self.rows.values())

    def resolve_agent_dependencies(self, *, session_id, **kwargs):
        self.resolved.append(session_id)

    def read_agent_runtime_state(self, *, session_id, **kwargs):
        return self.rows.get(session_id)

    def mark_agent_idle(self, *, session_id, at, **kwargs):
        self.marked.append(session_id)
        self.rows[session_id]['idle_since'] = at


class Core:
    def __init__(self, rows=()):
        self.repository = Repository(rows)
        self._sessions = {'root-host': SimpleNamespace(session_id='root')}
        self._suspended_agent_roots = set()
        self.loaded = []
        self.retired = []

    async def _ensure_resources(self):
        return self.repository

    def _canonical_deadline(self):
        return monotonic() + 30

    def _live_agent_session(self, session_id):
        return next((s for s in self._sessions.values() if s.session_id == session_id), None)

    async def _load_agent_session(self, row):
        self.loaded.append(row['id'])
        self._sessions[row['id']] = SimpleNamespace(session_id=row['id'], wake_queued_inputs=lambda: None)

    async def _retire_idle_agent(self, row, *, delete):
        self.retired.append((row['id'], delete))


def row(identity, *, pending=False, running=False, ready=False, idle_hours=None):
    return dict(id=identity, root_session_id='root', has_pending_work=pending,
                has_running_turn=running, head_ready=ready,
                idle_since=None if idle_hours is None else datetime.now(timezone.utc)-timedelta(hours=idle_hours))


@asynchronous
async def test_capacity_waits_without_a_logical_inventory_cap():
    runtime = AgentSessionRuntime(Core())
    await runtime.set_capacity_target(1)
    assert await runtime.try_acquire('a')
    for i in range(100):
        assert not await runtime.try_acquire(str(i))
    await runtime.release('a')
    assert await runtime.try_acquire('99')
    assert await runtime.capacity_state() == (1, 1)


@asynchronous
async def test_waiting_agent_yields_capacity_and_reacquires_after_result():
    runtime = AgentSessionRuntime(Core())
    await runtime.set_capacity_target(1)
    assert await runtime.try_acquire('waiting')
    async with runtime.suspend_execution(SimpleNamespace(session_id='waiting')):
        assert await runtime.try_acquire('replying')
        assert await runtime.capacity_state() == (1, 1)
        await runtime.release('replying')
    assert await runtime.capacity_state() == (1, 1)
    assert not await runtime.try_acquire('third')
    await runtime.release('waiting')


@asynchronous
async def test_cancelled_wait_does_not_reclaim_physical_slot():
    runtime = AgentSessionRuntime(Core())
    await runtime.set_capacity_target(1)
    assert await runtime.try_acquire('waiting')
    entered = asyncio.Event()
    async def wait():
        async with runtime.suspend_execution(SimpleNamespace(session_id='waiting')):
            entered.set()
            await asyncio.Future()
    task = asyncio.create_task(wait())
    await entered.wait()
    assert await runtime.try_acquire('replying')
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await runtime.capacity_state() == (1, 1)


@asynchronous
async def test_dependency_wait_is_not_idle_and_does_not_load_a_host():
    core = Core([row('dependent', pending=True, idle_hours=1000)])
    runtime = AgentSessionRuntime(core)
    await runtime._advance()
    assert core.repository.resolved == ['dependent']
    assert core.loaded == []
    assert core.retired == []
    assert core.repository.marked == []


@asynchronous
async def test_ready_requests_only_load_with_free_physical_capacity():
    core = Core([row('a', pending=True, ready=True), row('b', pending=True, ready=True)])
    runtime = AgentSessionRuntime(core)
    await runtime.set_capacity_target(1)
    await runtime._advance()
    assert core.loaded == ['a']
    await runtime.release('a')
    await runtime._advance()
    assert core.loaded == ['a', 'b']


@asynchronous
async def test_takeover_loads_running_turn_for_honest_interruption():
    core = Core([row('lost-host', running=True)])
    runtime = AgentSessionRuntime(core)
    await runtime._advance()
    assert core.loaded == ['lost-host']


@asynchronous
async def test_unload_and_delete_share_same_idle_origin():
    core = Core([row('unload', idle_hours=2), row('delete', idle_hours=73)])
    core._sessions['unload'] = SimpleNamespace(session_id='unload')
    runtime = AgentSessionRuntime(core)
    await runtime._advance()
    assert core.retired == [('unload', False), ('delete', True)]
    assert core.repository.marked == []


@asynchronous
async def test_notification_revision_cannot_lose_read_subscribe_race():
    runtime = AgentSessionRuntime(Core())
    revision = runtime.agent_change_revision()
    await runtime.notify_changed()
    await asyncio.wait_for(runtime.wait_for_agent_change(revision), timeout=1)
