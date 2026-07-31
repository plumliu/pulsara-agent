from __future__ import annotations

import asyncio
import threading

import pytest

from pulsara_agent.event import EventContext, RunStartEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.ports.subagent import (
    RecoveredChildCapacityOccupancySlot,
    build_recovered_child_occupancy_proof,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.run_execution.commit_gateway import read_ledger_horizon
from pulsara_agent.runtime.session import EventCommitError
from pulsara_agent.runtime.subagent import (
    ChildActivationOperationRegistry,
    ChildAdmissionSessionRegistry,
    InMemoryEventLogLocator,
    SubagentRuntime,
    fold_subagent_graph,
)
from tests.conftest import run_start_permission_fields
from tests.support.runtime_session import in_memory_runtime_session


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


def _register_owner(
    registry: ChildAdmissionSessionRegistry,
    session,
    *,
    subagent_run_id: str,
    reservation=None,
):
    reservation = reservation or registry.reserve(parent_run_id=CTX.run_id, count=1)
    horizon = read_ledger_horizon(session.event_log)
    return registry.register_prepared(
        subagent_run_id=subagent_run_id,
        child_runtime_session_id=session.runtime_session_id,
        child_session=session,
        reservation=reservation,
        parent_runtime_session_id="runtime:parent",
        parent_run_id=CTX.run_id,
        spawn_edge_id=f"edge:{subagent_run_id}",
        parent_graph_horizon=horizon,
        parent_graph_state_fingerprint=context_fingerprint(
            "test-subagent-graph-state:v1",
            [subagent_run_id, horizon.horizon_fingerprint],
        ),
    )


def _operation_registry(admission: ChildAdmissionSessionRegistry):
    return ChildActivationOperationRegistry(
        on_started=admission.mark_activation_started,
        on_exited=admission.mark_activation_exited,
    )


def test_admission_owner_never_appears_in_graph_projection(tmp_path) -> None:
    registry = ChildAdmissionSessionRegistry()
    session = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:child")
    owner = _register_owner(
        registry,
        session,
        subagent_run_id="subagent_run:ephemeral",
    )

    assert owner.child_composition_lease.child_session is session
    assert not hasattr(owner, "coroutine")
    assert not hasattr(owner, "execution_handles")
    assert fold_subagent_graph(()).runs == {}


def test_partial_reservation_keeps_attached_capacity_until_graph_settlement(
    tmp_path,
) -> None:
    registry = ChildAdmissionSessionRegistry()
    reservation = registry.reserve(parent_run_id=CTX.run_id, count=2)
    session = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:child:partial-reservation",
    )
    owner = _register_owner(
        registry,
        session,
        subagent_run_id="subagent_run:partial-reservation",
        reservation=reservation,
    )

    registry.release_reservation(reservation)

    assert reservation.uncommitted_count == 0
    assert reservation.active_slot_count == 1
    assert registry.occupied_run_ids(parent_run_id=CTX.run_id) == {
        owner.subagent_run_id
    }

    registry.mark_parent_graph_terminal_full(owner.subagent_run_id)
    assert reservation.released is True
    assert registry.occupied_run_ids(parent_run_id=CTX.run_id) == frozenset()


def test_admission_registry_has_no_activation_or_execution_handle_attach_api() -> None:
    registry = ChildAdmissionSessionRegistry()

    assert not hasattr(registry, "attach_coroutine")
    assert not hasattr(registry, "attach_execution_handles")
    assert not hasattr(registry, "cancel")


def test_recovered_occupancy_installs_before_repair_and_exact_confirms(tmp_path) -> None:
    registry = ChildAdmissionSessionRegistry()
    session = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:child:recovered",
    )
    horizon = read_ledger_horizon(session.event_log)
    proof = build_recovered_child_occupancy_proof(
        parent_runtime_session_id="runtime:parent",
        parent_run_id=CTX.run_id,
        subagent_run_id="subagent_run:recovered",
        spawn_edge_id="edge:recovered",
        parent_graph_horizon=horizon,
        parent_graph_state_fingerprint=context_fingerprint(
            "test-recovered-graph:v1", [horizon.horizon_fingerprint]
        ),
    )

    owner = registry.register_recovered(
        proof=proof,
        child_runtime_session_id=session.runtime_session_id,
        child_session=session,
    )
    same = registry.register_recovered(
        proof=proof,
        child_runtime_session_id=session.runtime_session_id,
        child_session=session,
    )

    assert same is owner
    assert isinstance(owner.capacity_slot, RecoveredChildCapacityOccupancySlot)
    assert registry.occupied_run_ids() == {owner.subagent_run_id}


def test_reservation_released_when_event_commit_fails(tmp_path) -> None:
    backing = InMemoryEventLog()

    class FailingCommitEventLog:
        fail_writes = False

        def extend(self, events, *, expected_last_sequence=None, deadline_monotonic=None):
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

        def __getattr__(self, name):
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
    assert runtime._admission_registry.uncommitted_reservation_count() == 0  # noqa: SLF001
    assert runtime._admission_registry.owners() == ()  # noqa: SLF001


def test_terminal_graph_reports_independent_active_operation(tmp_path) -> None:
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
        await runtime.complete_fake(child.subagent_run_id, summary="done")
        diagnostics = ChildAdmissionSessionRegistry().reconcile(
            fold_subagent_graph(parent.event_log.iter()),
            active_operation_run_ids=frozenset({child.subagent_run_id}),
        )
        assert [item.code for item in diagnostics] == [
            "subagent_terminal_activation_operation_active"
        ]

    asyncio.run(run())


def test_graph_active_admission_owner_missing_reports_dangling(tmp_path) -> None:
    parent = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
    runtime = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=lambda _runtime_session_id: InMemoryEventLog(),
    )

    async def seed() -> None:
        await _start_parent_run(parent)
        await runtime.spawn_fake(task="task", event_context=CTX)

    asyncio.run(seed())
    diagnostics = ChildAdmissionSessionRegistry().reconcile(
        fold_subagent_graph(parent.event_log.iter())
    )
    assert [item.code for item in diagnostics] == [
        "subagent_active_admission_owner_missing"
    ]


def test_cancel_waits_for_activation_finally_before_session_close(tmp_path) -> None:
    admission = ChildAdmissionSessionRegistry()
    operations = _operation_registry(admission)
    order: list[str] = []
    started = asyncio.Event()

    class RecordingChildSession:
        runtime_session_id = "runtime:child:drain-order"
        event_log = InMemoryEventLog()

        def close(self) -> None:
            order.append("session_close")

    async def child() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            order.append("activation_finally")

    async def run() -> None:
        owner = _register_owner(
            admission,
            RecordingChildSession(),
            subagent_run_id="subagent_run:drain-order",
        )
        task = asyncio.create_task(child())
        operations.install(owner.subagent_run_id, task)
        await started.wait()
        admission.mark_parent_graph_terminal_full(owner.subagent_run_id)

        await operations.cancel(owner.subagent_run_id, timeout_seconds=1)
        await asyncio.sleep(0)

        assert task.cancelled()
        assert order == ["activation_finally", "session_close"]
        assert admission.owners() == ()

    asyncio.run(run())


def test_sync_cancel_runs_on_owner_loop_and_releases_after_exit(tmp_path) -> None:
    admission = ChildAdmissionSessionRegistry()
    operations = _operation_registry(admission)
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
        owner = _register_owner(
            admission,
            session,
            subagent_run_id="subagent_run:cross-thread",
        )
        task = asyncio.create_task(child())
        operations.install(owner.subagent_run_id, task)
        admission.mark_parent_graph_terminal_full(owner.subagent_run_id)
        await started.wait()

        thread = threading.Thread(
            target=operations.cancel_now,
            args=(owner.subagent_run_id,),
        )
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()

        await asyncio.wait_for(finished.wait(), timeout=1)
        await asyncio.sleep(0)
        assert task.cancelled()
        assert finally_thread_ids == [owner_thread_id]
        assert admission.owners() == ()

    asyncio.run(run())


def test_cancel_timeout_retains_admission_until_physical_exit(tmp_path) -> None:
    admission = ChildAdmissionSessionRegistry()
    operations = _operation_registry(admission)
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    closed: list[bool] = []

    class RecordingChildSession:
        runtime_session_id = "runtime:child:slow-cleanup"
        event_log = InMemoryEventLog()

        def close(self) -> None:
            closed.append(True)

    async def child() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await allow_cleanup.wait()

    async def run() -> None:
        owner = _register_owner(
            admission,
            RecordingChildSession(),
            subagent_run_id="subagent_run:slow-cleanup",
        )
        task = asyncio.create_task(child())
        operations.install(owner.subagent_run_id, task)
        admission.mark_parent_graph_terminal_full(owner.subagent_run_id)
        await asyncio.sleep(0)

        with pytest.raises(TimeoutError, match="Timed out draining child activation"):
            await operations.cancel(owner.subagent_run_id, timeout_seconds=0.01)

        await cleanup_started.wait()
        assert admission.get(owner.subagent_run_id) is owner
        assert closed == []

        allow_cleanup.set()
        await task
        await asyncio.sleep(0)
        assert admission.owners() == ()
        assert closed == [True]

    asyncio.run(run())
