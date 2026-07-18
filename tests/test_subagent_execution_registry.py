from __future__ import annotations

import asyncio
import threading
from typing import Any, cast

import pytest

from pulsara_agent.event import EventContext, RunStartEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.runtime import EventCommitError
from pulsara_agent.runtime.execution_handles import (
    BoundaryExecutionHandles,
    CapabilityExecutionBorrowUnavailable,
)
from pulsara_agent.runtime.subagent import (
    ChildExecutionRegistry,
    InMemoryEventLogLocator,
    SubagentRuntime,
    fold_subagent_graph,
)
from tests.support.runtime_session import in_memory_runtime_session
from tests.conftest import run_start_permission_fields


CTX = EventContext(run_id="run:parent", turn_id="turn:parent", reply_id="reply:parent")


async def _start_parent_run(parent) -> None:
    await parent.write_event(
        RunStartEvent(
            **CTX.event_fields(),
            **run_start_permission_fields(
                CTX.run_id,
                user_input="delegate",
                turn_id=CTX.turn_id,
                reply_id=CTX.reply_id,
            ),
            user_input_chars=8,
        )
    )


def test_registry_handle_never_appears_in_graph_projection(tmp_path) -> None:
    registry = ChildExecutionRegistry()
    reservation = registry.reserve(parent_run_id=CTX.run_id, count=1)
    session = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:child",
    )
    registry.register_prepared(
        subagent_run_id="subagent_run:ephemeral",
        child_runtime_session_id=session.runtime_session_id,
        child_session=session,
        reservation=reservation,
    )
    assert registry.get("subagent_run:ephemeral").child_session is session  # type: ignore[union-attr]
    assert fold_subagent_graph(()).runs == {}


def test_partial_reservation_release_keeps_attached_closing_slot_occupied(
    tmp_path,
) -> None:
    registry = ChildExecutionRegistry()
    reservation = registry.reserve(parent_run_id=CTX.run_id, count=2)
    session = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:child:partial-reservation",
    )
    registry.register_prepared(
        subagent_run_id="subagent_run:partial-reservation",
        child_runtime_session_id=session.runtime_session_id,
        child_session=session,
        reservation=reservation,
    )

    registry.release_reservation(reservation)

    assert reservation.uncommitted_count == 0
    assert reservation.active_slot_count == 1
    assert reservation.released is False
    assert registry.occupied_run_ids(parent_run_id=CTX.run_id) == {
        "subagent_run:partial-reservation"
    }

    registry.release_handle("subagent_run:partial-reservation")
    assert reservation.released is True
    assert registry.occupied_run_ids(parent_run_id=CTX.run_id) == frozenset()


def test_child_registry_owns_and_retires_child_execution_authority(tmp_path) -> None:
    registry = ChildExecutionRegistry()
    session = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:child:authority",
    )
    registry.register_prepared(
        subagent_run_id="subagent_run:authority",
        child_runtime_session_id=session.runtime_session_id,
        child_session=session,
        reservation=None,
    )
    handles = BoundaryExecutionHandles(
        handle_id="child_execution_handles:test",
        handle_generation=1,
        owner_id="subagent_run:authority",
        state="run_owned",
        mcp_installation="mcp_installation:test",
        capability_runtime=object(),
        tool_registry=object(),
        frozen_execution_surface=cast(Any, object()),
    )
    registry.attach_execution_handles("subagent_run:authority", handles)
    authority = handles.borrow_authority

    authority.borrow_child_tool_call()
    registry.release_handle("subagent_run:authority")

    retained = registry.get("subagent_run:authority")
    assert retained is not None
    assert retained.phase == "closing"
    assert handles.state == "retiring"

    authority.release_child_tool_call()

    assert handles.state == "closed"
    assert registry.get("subagent_run:authority") is None
    with pytest.raises(CapabilityExecutionBorrowUnavailable):
        authority.borrow_child_tool_call()


def test_reservation_released_when_event_commit_fails(tmp_path) -> None:
    backing = InMemoryEventLog()

    class FailingCommitEventLog:
        fail_writes = False

        def append(
            self,
            event,
            *,
            expected_last_sequence=None,
            deadline_monotonic=None,
        ):
            return self.extend(
                (event,),
                expected_last_sequence=expected_last_sequence,
                deadline_monotonic=deadline_monotonic,
            )[0]

        def extend(
            self,
            events,
            *,
            expected_last_sequence=None,
            deadline_monotonic=None,
        ):
            if self.fail_writes:
                raise RuntimeError("synthetic event commit failure")
            return backing.extend(
                events,
                expected_last_sequence=expected_last_sequence,
                deadline_monotonic=deadline_monotonic,
            )

        def extend_with_materialization_state(self, events, **kwargs):
            if self.fail_writes:
                raise RuntimeError("synthetic event commit failure")
            return backing.extend_with_materialization_state(events, **kwargs)

        def iter(self, **kwargs):
            return backing.iter(**kwargs)

        def next_sequence(self):
            return backing.next_sequence()

        def __getattr__(self, name):
            # This double only faults writes; checkpoint/raw reads still use the
            # complete EventLog contract provided by the backing store.
            return getattr(backing, name)

    faulting_log = FailingCommitEventLog()
    parent = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:parent",
        event_log=faulting_log,
        allow_unbootstrapped_test_events=False,
    )
    asyncio.run(_start_parent_run(parent))
    faulting_log.fail_writes = True
    runtime = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=lambda _runtime_session_id: InMemoryEventLog(),
    )

    async def run() -> None:
        with pytest.raises(EventCommitError):
            await runtime.spawn_fake(task="must not reserve forever", event_context=CTX)

    asyncio.run(run())
    assert runtime._execution_registry.uncommitted_reservation_count() == 0  # noqa: SLF001
    assert runtime._execution_registry.handles() == ()  # noqa: SLF001


def test_terminal_graph_reconciles_and_cancels_live_handle(tmp_path) -> None:
    parent = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
    locator = InMemoryEventLogLocator()

    def child_factory(runtime_session_id: str):
        log = InMemoryEventLog()
        locator.register(runtime_session_id, log)
        return log

    runtime = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=child_factory,
        event_log_locator=locator,
    )

    async def run() -> None:
        await _start_parent_run(parent)
        child = await runtime.spawn_fake(task="task", event_context=CTX)
        child_session = runtime.child_runtime_session(child.subagent_run_id)
        await runtime.complete_fake(child.subagent_run_id, summary="done")
        assert runtime._execution_registry.handles() == ()  # noqa: SLF001
        registry = ChildExecutionRegistry()
        handle = registry.register_prepared(
            subagent_run_id=child.subagent_run_id,
            child_runtime_session_id=child.child_runtime_session_id,
            child_session=child_session,
            reservation=None,
        )
        handle.coroutine = asyncio.create_task(asyncio.sleep(10))  # type: ignore[assignment]
        handle.phase = "started"
        diagnostics = registry.reconcile(fold_subagent_graph(parent.event_log.iter()))
        assert [item.code for item in diagnostics] == ["subagent_terminal_run_handle_active"]
        await registry.cancel(child.subagent_run_id)
        assert handle.coroutine.done()
        assert registry.handles() == ()

    asyncio.run(run())


def test_graph_active_registry_missing_reports_dangling(tmp_path) -> None:
    parent = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
    locator = InMemoryEventLogLocator()

    def child_factory(runtime_session_id: str):
        log = InMemoryEventLog()
        locator.register(runtime_session_id, log)
        return log

    runtime = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=child_factory,
        event_log_locator=locator,
    )

    async def seed() -> None:
        await _start_parent_run(parent)
        await runtime.spawn_fake(task="task", event_context=CTX)

    asyncio.run(seed())
    diagnostics = ChildExecutionRegistry().reconcile(
        fold_subagent_graph(parent.event_log.iter())
    )
    assert [item.code for item in diagnostics] == ["subagent_active_run_handle_missing"]


def test_host_close_drains_all_handles(tmp_path) -> None:
    registry = ChildExecutionRegistry()
    session = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:child")

    async def run() -> None:
        handle = registry.register_prepared(
            subagent_run_id="subagent_run:drain",
            child_runtime_session_id=session.runtime_session_id,
            child_session=session,
            reservation=None,
        )
        task = asyncio.create_task(asyncio.sleep(10))
        registry.attach_coroutine(handle.subagent_run_id, task)
        await registry.drain(timeout_seconds=1)
        assert task.done()
        assert handle.phase == "released"
        assert registry.handles() == ()

    asyncio.run(run())


def test_cancel_waits_for_child_finally_before_session_close(tmp_path) -> None:
    registry = ChildExecutionRegistry()
    order: list[str] = []
    started = asyncio.Event()
    release_child = asyncio.Event()

    class RecordingChildSession:
        runtime_session_id = "runtime:child:drain-order"

        def close(self) -> None:
            order.append("session_close")

    async def child() -> None:
        started.set()
        try:
            await release_child.wait()
        finally:
            await asyncio.sleep(0)
            order.append("child_finally")

    async def run() -> None:
        handle = registry.register_prepared(
            subagent_run_id="subagent_run:drain-order",
            child_runtime_session_id=RecordingChildSession.runtime_session_id,
            child_session=RecordingChildSession(),  # type: ignore[arg-type]
            reservation=None,
        )
        task = asyncio.create_task(child())
        registry.attach_coroutine(handle.subagent_run_id, task)
        await started.wait()

        await registry.cancel(handle.subagent_run_id, timeout_seconds=1)

        assert task.cancelled()
        assert order == ["child_finally", "session_close"]
        assert handle.phase == "released"
        assert registry.handles() == ()

    asyncio.run(run())


def test_sync_cancel_requests_on_owner_loop_and_releases_after_done(tmp_path) -> None:
    registry = ChildExecutionRegistry()
    started = asyncio.Event()
    finished = asyncio.Event()
    owner_thread_id = threading.get_ident()
    finally_thread_ids: list[int] = []

    async def child() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finally_thread_ids.append(threading.get_ident())
            finished.set()

    async def run() -> None:
        session = in_memory_runtime_session(
            tmp_path,
            runtime_session_id="runtime:child:cross-thread",
        )
        handle = registry.register_prepared(
            subagent_run_id="subagent_run:cross-thread",
            child_runtime_session_id=session.runtime_session_id,
            child_session=session,
            reservation=None,
        )
        task = asyncio.create_task(child())
        registry.attach_coroutine(handle.subagent_run_id, task)
        await started.wait()

        thread = threading.Thread(target=registry.cancel_now, args=(handle.subagent_run_id,))
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert handle.phase == "closing"

        await asyncio.wait_for(finished.wait(), timeout=1)
        await asyncio.sleep(0)
        assert task.cancelled()
        assert finally_thread_ids == [owner_thread_id]
        assert registry.handles() == ()

    asyncio.run(run())


def test_cancel_timeout_keeps_live_handle_and_session_until_coroutine_exits(
    tmp_path,
) -> None:
    registry = ChildExecutionRegistry()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    closed: list[bool] = []

    class RecordingChildSession:
        runtime_session_id = "runtime:child:slow-cleanup"

        def close(self) -> None:
            closed.append(True)

    async def child() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await allow_cleanup.wait()

    async def run() -> None:
        handle = registry.register_prepared(
            subagent_run_id="subagent_run:slow-cleanup",
            child_runtime_session_id=RecordingChildSession.runtime_session_id,
            child_session=RecordingChildSession(),  # type: ignore[arg-type]
            reservation=None,
        )
        task = asyncio.create_task(child())
        registry.attach_coroutine(handle.subagent_run_id, task)
        await asyncio.sleep(0)

        with pytest.raises(TimeoutError, match="Timed out draining child coroutine"):
            await registry.cancel(handle.subagent_run_id, timeout_seconds=0.01)

        await cleanup_started.wait()
        assert registry.get(handle.subagent_run_id) is handle
        assert handle.phase == "closing"
        assert closed == []

        allow_cleanup.set()
        await task
        await asyncio.sleep(0)
        assert registry.handles() == ()
        assert closed == [True]

    asyncio.run(run())
