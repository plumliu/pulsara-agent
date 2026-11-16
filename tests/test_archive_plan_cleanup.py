"""Terminal Plan and compaction owners release their live-work reservations."""

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.host_plans import _HostPlanOperations
from pulsara_agent.identity import random_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.permission import PermissionMode


class _Compaction:
    def is_fenced(self, **_kwargs):
        return False

    def scope_key(self, scope_kind):
        return scope_kind

    def has_active_work(self):
        return False


class _Host(_HostPlanOperations):
    _has_runtime_work = KernelHostSession._has_runtime_work
    _reserve_compaction_write_locked = KernelHostSession._reserve_compaction_write_locked
    _release_compaction_write_reservation = KernelHostSession._release_compaction_write_reservation
    _release_compaction_write_reservation_locked = KernelHostSession._release_compaction_write_reservation_locked

    def __init__(self):
        self._lock = asyncio.Lock()
        self._compaction = _Compaction()
        self._session_scope_kind = ModelInputScopeKind.ROOT
        self._active_task = None
        self._active_turn_id = None
        self._external_new_turn_accepting = False
        self._external_new_turn_cancellation_intent = None
        self._external_new_turn_settled = asyncio.Event()
        self._plan_exit_fence = False
        self._compaction_write_reservations = {}
        self._manual_compaction_command_attempts = {}
        self._pending_root_successor = None
        self._prompt_admission_attempts = {}
        self._user_control_attempts = {}
        self._capability_reload_settlement_lock = asyncio.Lock()
        self._code_mode = SimpleNamespace(has_live_cells=lambda: False, pending_output_cells=lambda: ())
        self._runner = SimpleNamespace(_safe_point=SimpleNamespace(is_idle=lambda: True))
        self._queue_wake = asyncio.Event()
        self._cell_output_wake = asyncio.Event()
        self.session_id = random_id("session")
        self.host_session_id = random_id("host")
        self._lease = SimpleNamespace(guard=object())
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self._io = self
        self.repository = SimpleNamespace(enter_plan_by_user=object(), exit_plan_by_user=object())

    async def _query_command_row(self, _command_id):
        return None

    def _require_open(self):
        pass

    def _retire_done_active_root_locked(self):
        pass

    def _canonical_deadline(self):
        return 0

    async def run(self, *_args, **_kwargs):
        self.started.set()
        await self.finish.wait()
        raise RuntimeError("physical operation settled")


async def _cancel_while_cleanup_waits(host, task):
    await host._lock.acquire()
    host.finish.set()
    await asyncio.sleep(0)
    assert host._has_runtime_work()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert host._has_runtime_work()
    host._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not host._has_runtime_work()


@pytest.mark.parametrize("action", ["enter", "exit"])
def test_plan_finalizers_settle_even_when_cancelled_during_lock_wait(action):
    async def scenario():
        host = _Host()
        if action == "enter":
            task = asyncio.create_task(host.enter_plan(
                command_id=random_id("command"), resume_permission_mode=PermissionMode.READ_ONLY,
            ))
        else:
            task = asyncio.create_task(host.cancel_plan(
                command_id=random_id("command"), workflow_id=random_id("plan"),
                expected_workflow_revision=1,
            ))
        await host.started.wait()
        await _cancel_while_cleanup_waits(host, task)
        if action == "enter":
            assert host._external_new_turn_settled.is_set()
        else:
            assert host._queue_wake.is_set() and host._cell_output_wake.is_set()
    asyncio.run(scenario())


def test_compaction_reservation_cleanup_survives_repeated_cancellation():
    async def scenario():
        host = _Host()
        async with host._lock:
            reservation = host._reserve_compaction_write_locked(scope_kind=ModelInputScopeKind.ROOT)
        await host._lock.acquire()
        task = asyncio.create_task(host._release_compaction_write_reservation(reservation))
        await asyncio.sleep(0)
        assert host._has_runtime_work()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and host._has_runtime_work()
        host._lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not host._has_runtime_work()
        live = asyncio.create_task(asyncio.Event().wait())
        host._active_task = live
        assert host._has_runtime_work()
        live.cancel()
        with pytest.raises(asyncio.CancelledError):
            await live
        assert not host._has_runtime_work()
    asyncio.run(scenario())


@pytest.mark.parametrize("host_bound", [False, True])
def test_compaction_fence_cleanup_survives_repeated_cancellation(host_bound):
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        CompactionScope, CompactionTrigger,
    )
    from pulsara_agent.conversation_kernel.compaction.runtime import HostCompactionRuntimeOwner

    async def scenario():
        owner = HostCompactionRuntimeOwner()
        host_lock = asyncio.Lock()
        if host_bound:
            async def install(scope, trigger, attempt, task, _reservation):
                async with host_lock:
                    owner.install_fence_under_host_lock(
                        scope=scope, trigger=trigger, attempt_id=attempt, owner_task=task,
                    )

            async def remove(scope, attempt, task):
                async with host_lock:
                    owner.remove_fence_under_host_lock(
                        scope=scope, attempt_id=attempt, owner_task=task,
                    )

            owner.bind_host_fence_callbacks(install=install, remove=remove)
        scope = CompactionScope(
            session_id=random_id("session"), workspace_id=random_id("workspace"),
            turn_id=random_id("turn"), scope_kind=ModelInputScopeKind.ROOT,
        )
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def operation():
            entered.set()
            await finish.wait()
            return "settled"

        task = asyncio.create_task(owner.run_fenced(
            scope=scope, trigger=CompactionTrigger.MANUAL,
            operation=operation, attempt_id=random_id("compact-attempt"),
        ))
        await entered.wait()
        lock = host_lock if host_bound else owner._state_lock
        await lock.acquire()
        finish.set()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert owner.has_active_work() and owner.is_fenced(scope_kind=ModelInputScopeKind.ROOT)
        assert owner._summary_lane.locked()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner.has_active_work()
        assert not owner.is_fenced(scope_kind=ModelInputScopeKind.ROOT)
        assert not owner._summary_lane.locked()

    asyncio.run(scenario())


def test_plan_exit_remains_live_until_cancelled_physical_write_settles():
    import threading
    from time import monotonic
    from pulsara_agent.conversation_kernel.io import KernelSessionIO

    async def scenario():
        host = _Host()
        started = threading.Event()
        release = threading.Event()
        def write(*_args, **_kwargs):
            started.set()
            release.wait()
        host._io = KernelSessionIO()
        host.repository.exit_plan_by_user = write
        host._canonical_deadline = lambda: monotonic() + 30
        task = asyncio.create_task(host.cancel_plan(
            command_id=random_id("command"), workflow_id=random_id("plan"),
            expected_workflow_revision=1,
        ))
        try:
            await asyncio.to_thread(started.wait)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and host._has_runtime_work()
            assert host._plan_exit_fence
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not host._has_runtime_work()
        await host._io.aclose(deadline_monotonic=monotonic() + 30)

    asyncio.run(scenario())
