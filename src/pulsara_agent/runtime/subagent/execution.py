"""Ephemeral child execution handles, deliberately outside durable graph facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Literal
from uuid import uuid4

from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.execution_handles import BoundaryExecutionHandles
from pulsara_agent.runtime.mcp.types import McpBindingIdentity
from pulsara_agent.runtime.subagent.facts import SubagentGraphState


@dataclass(slots=True)
class ChildCapacityReservation:
    reservation_id: str
    parent_run_id: str
    count: int
    attached_run_ids: set[str] = field(default_factory=set)
    released_run_ids: set[str] = field(default_factory=set)
    uncommitted_released: bool = False
    released: bool = False

    @property
    def uncommitted_count(self) -> int:
        if self.released or self.uncommitted_released:
            return 0
        return max(0, self.count - len(self.attached_run_ids))

    @property
    def active_slot_count(self) -> int:
        if self.released:
            return 0
        return max(0, len(self.attached_run_ids - self.released_run_ids))


@dataclass(slots=True)
class ChildExecutionHandle:
    subagent_run_id: str
    child_runtime_session_id: str
    child_session: RuntimeSession | None
    coroutine: asyncio.Task[None] | None
    capacity_reservation: ChildCapacityReservation | None
    cancellation_requested: bool
    started_in_process_at: datetime
    phase: Literal["prepared", "started", "closing", "released"] = "prepared"
    release_requested: bool = False
    mcp_binding_identities: frozenset[McpBindingIdentity] = frozenset()
    execution_handles: BoundaryExecutionHandles | None = None


@dataclass(frozen=True, slots=True)
class ChildExecutionDiagnostic:
    code: str
    subagent_run_id: str
    child_runtime_session_id: str | None
    severity: Literal["warning", "error"] = "error"


class ChildExecutionRegistry:
    """Own process-local sessions/tasks/reservations; never durable status."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._handles: dict[str, ChildExecutionHandle] = {}
        self._reservations: dict[str, ChildCapacityReservation] = {}
        self._child_ids_by_mcp_binding_identity: dict[
            McpBindingIdentity, set[str]
        ] = {}

    def reserve(self, *, parent_run_id: str, count: int) -> ChildCapacityReservation:
        if count < 1:
            raise ValueError("reservation count must be >= 1")
        reservation = ChildCapacityReservation(
            reservation_id=f"subagent_capacity:{uuid4().hex}",
            parent_run_id=parent_run_id,
            count=count,
        )
        with self._lock:
            self._reservations[reservation.reservation_id] = reservation
        return reservation

    def register_prepared(
        self,
        *,
        subagent_run_id: str,
        child_runtime_session_id: str,
        child_session: RuntimeSession | None,
        reservation: ChildCapacityReservation | None,
        mcp_binding_identities: frozenset[McpBindingIdentity] = frozenset(),
    ) -> ChildExecutionHandle:
        with self._lock:
            if subagent_run_id in self._handles:
                raise ValueError(f"Child execution handle already exists: {subagent_run_id}")
            if reservation is not None:
                if reservation.released:
                    raise ValueError("capacity reservation was already released")
                if len(reservation.attached_run_ids) >= reservation.count:
                    raise ValueError("capacity reservation has no remaining slots")
                reservation.attached_run_ids.add(subagent_run_id)
            handle = ChildExecutionHandle(
                subagent_run_id=subagent_run_id,
                child_runtime_session_id=child_runtime_session_id,
                child_session=child_session,
                coroutine=None,
                capacity_reservation=reservation,
                cancellation_requested=False,
                started_in_process_at=datetime.now(timezone.utc),
                mcp_binding_identities=mcp_binding_identities,
            )
            self._handles[subagent_run_id] = handle
            for identity in mcp_binding_identities:
                self._child_ids_by_mcp_binding_identity.setdefault(
                    identity, set()
                ).add(subagent_run_id)
            return handle

    def child_ids_for_mcp_bindings(
        self,
        identities: frozenset[McpBindingIdentity],
    ) -> frozenset[str]:
        with self._lock:
            result: set[str] = set()
            for identity in identities:
                result.update(
                    self._child_ids_by_mcp_binding_identity.get(identity, ())
                )
            return frozenset(result)

    def attach_session(self, subagent_run_id: str, session: RuntimeSession) -> None:
        with self._lock:
            handle = self._handles[subagent_run_id]
            if session.runtime_session_id != handle.child_runtime_session_id:
                raise ValueError("child runtime session identity mismatch")
            handle.child_session = session

    def attach_execution_handles(
        self,
        subagent_run_id: str,
        execution_handles: BoundaryExecutionHandles,
    ) -> None:
        with self._lock:
            handle = self._handles[subagent_run_id]
            if handle.execution_handles is not None:
                raise ValueError("child execution handles are already attached")
            if execution_handles.state != "run_owned":
                raise ValueError("child execution handles must already be run-owned")
            if execution_handles.owner_id != subagent_run_id:
                raise ValueError("child execution handle owner mismatch")
            handle.execution_handles = execution_handles
            execution_handles.borrow_tracker.on_change = (
                lambda run_id=subagent_run_id, exact=execution_handles: (
                    self._execution_borrow_changed(run_id, exact)
                )
            )

    def attach_coroutine(self, subagent_run_id: str, coroutine: asyncio.Task[None]) -> None:
        with self._lock:
            handle = self._handles[subagent_run_id]
            if handle.coroutine is not None and not handle.coroutine.done():
                raise ValueError(f"Child coroutine already attached: {subagent_run_id}")
            handle.coroutine = coroutine
            handle.phase = "started"
        coroutine.add_done_callback(
            lambda completed, run_id=subagent_run_id: self._coroutine_done(
                run_id,
                completed,
            )
        )

    def get(self, subagent_run_id: str) -> ChildExecutionHandle | None:
        with self._lock:
            return self._handles.get(subagent_run_id)

    def handles(self) -> tuple[ChildExecutionHandle, ...]:
        with self._lock:
            return tuple(self._handles.values())

    def uncommitted_reservation_count(self, *, parent_run_id: str | None = None) -> int:
        with self._lock:
            reservations = tuple(self._reservations.values())
            return sum(
                reservation.uncommitted_count
                for reservation in reservations
                if parent_run_id is None or reservation.parent_run_id == parent_run_id
            )

    def release_reservation(self, reservation: ChildCapacityReservation) -> None:
        with self._lock:
            reservation.uncommitted_released = True
            if reservation.active_slot_count == 0:
                reservation.released = True
                self._reservations.pop(reservation.reservation_id, None)

    def occupied_run_ids(self, *, parent_run_id: str | None = None) -> frozenset[str]:
        """Return attached handles that still own physical child capacity."""

        with self._lock:
            occupied: set[str] = set()
            for reservation in self._reservations.values():
                if parent_run_id is not None and reservation.parent_run_id != parent_run_id:
                    continue
                occupied.update(
                    reservation.attached_run_ids - reservation.released_run_ids
                )
            return frozenset(occupied)

    def release_handle(self, subagent_run_id: str) -> None:
        """Release only after the child coroutine has fully exited.

        Completion/failure helpers are commonly called from inside the child
        coroutine. In that case the task done callback performs the physical
        session/slot release after the coroutine's ``finally`` blocks finish.
        """

        with self._lock:
            handle = self._handles.get(subagent_run_id)
            if handle is None:
                return
            handle.release_requested = True
            task = handle.coroutine
            if task is not None and not task.done():
                handle.phase = "closing"
                return
        self._finalize_release(subagent_run_id)

    async def cancel(
        self,
        subagent_run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        task = self.request_cancel(subagent_run_id)
        if task is None:
            return
        completed = await _wait_for_task_completion(
            task,
            timeout_seconds=timeout_seconds,
        )
        if not completed:
            raise TimeoutError(
                f"Timed out draining child coroutine for {subagent_run_id}"
            )
        self._finalize_release(subagent_run_id, expected_task=task)

    def request_cancel(self, subagent_run_id: str) -> asyncio.Task[None] | None:
        """Request cancellation on the task's owning loop without releasing it."""

        with self._lock:
            handle = self._handles.get(subagent_run_id)
            if handle is None:
                return None
            handle.cancellation_requested = True
            handle.release_requested = True
            handle.phase = "closing"
            task = handle.coroutine
            finalize_now = task is None or task.done()
        if finalize_now:
            self._finalize_release(subagent_run_id, expected_task=task)
            return None
        _cancel_task_on_owner_loop(task)
        return task

    def cancel_now(self, subagent_run_id: str) -> None:
        """Compatibility sync entrypoint: request only, never release a live task."""

        self.request_cancel(subagent_run_id)

    async def drain(self, *, timeout_seconds: float | None) -> None:
        await self.drain_run_ids(
            tuple(handle.subagent_run_id for handle in self.handles()),
            timeout_seconds=timeout_seconds,
        )

    async def drain_run_ids(
        self,
        subagent_run_ids: tuple[str, ...],
        *,
        timeout_seconds: float | None,
    ) -> None:
        tasks: dict[str, asyncio.Task[None]] = {}
        for subagent_run_id in subagent_run_ids:
            task = self.request_cancel(subagent_run_id)
            if task is not None:
                tasks[subagent_run_id] = task
        if not tasks:
            return

        loop = asyncio.get_running_loop()
        deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
        timed_out: list[str] = []
        for subagent_run_id, task in tasks.items():
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            completed = await _wait_for_task_completion(
                task,
                timeout_seconds=remaining,
            )
            if not completed:
                timed_out.append(subagent_run_id)
                continue
            self._finalize_release(subagent_run_id, expected_task=task)
        if timed_out:
            raise TimeoutError(
                "Timed out draining child coroutines: " + ", ".join(sorted(timed_out))
            )

    def _coroutine_done(
        self,
        subagent_run_id: str,
        completed: asyncio.Task[None],
    ) -> None:
        self._finalize_release(subagent_run_id, expected_task=completed)

    def _finalize_release(
        self,
        subagent_run_id: str,
        *,
        expected_task: asyncio.Task[None] | None = None,
    ) -> None:
        child_session: RuntimeSession | None = None
        with self._lock:
            handle = self._handles.get(subagent_run_id)
            if handle is None:
                return
            if expected_task is not None and handle.coroutine is not expected_task:
                return
            task = handle.coroutine
            if task is not None and not task.done():
                handle.release_requested = True
                handle.phase = "closing"
                return
            execution_handles = handle.execution_handles
            if execution_handles is not None:
                if execution_handles.state == "run_owned":
                    execution_handles.mark_retiring()
                if not execution_handles.borrow_tracker.can_retire():
                    handle.release_requested = True
                    handle.phase = "closing"
                    return
                if execution_handles.state == "retiring":
                    execution_handles.mark_closed()
                execution_handles.borrow_tracker.on_change = None
            self._handles.pop(subagent_run_id, None)
            for identity in handle.mcp_binding_identities:
                run_ids = self._child_ids_by_mcp_binding_identity.get(identity)
                if run_ids is None:
                    continue
                run_ids.discard(subagent_run_id)
                if not run_ids:
                    self._child_ids_by_mcp_binding_identity.pop(identity, None)
            handle.phase = "released"
            child_session = handle.child_session
            handle.child_session = None
            reservation = handle.capacity_reservation
            if reservation is not None:
                reservation.released_run_ids.add(subagent_run_id)
                if (
                    reservation.active_slot_count == 0
                    and reservation.uncommitted_count == 0
                ):
                    reservation.released = True
                    self._reservations.pop(reservation.reservation_id, None)
        if child_session is not None:
            child_session.close()

    def _execution_borrow_changed(
        self,
        subagent_run_id: str,
        exact_handles: BoundaryExecutionHandles,
    ) -> None:
        """Retry release only for the exact closing child/handle generation."""

        with self._lock:
            handle = self._handles.get(subagent_run_id)
            if (
                handle is None
                or handle.execution_handles is not exact_handles
                or handle.phase != "closing"
                or not handle.release_requested
            ):
                return
            task = handle.coroutine
            if task is not None and not task.done():
                return
        self._finalize_release(subagent_run_id, expected_task=task)

    def reconcile(self, graph: SubagentGraphState) -> tuple[ChildExecutionDiagnostic, ...]:
        diagnostics: list[ChildExecutionDiagnostic] = []
        handles = {handle.subagent_run_id: handle for handle in self.handles()}
        for run in graph.runs.values():
            handle = handles.pop(run.subagent_run_id, None)
            active = run.status in {"running", "suspended"}
            handle_active = (
                handle is not None
                and handle.phase not in {"closing", "released"}
                and handle.child_session is not None
            )
            if active and not handle_active:
                diagnostics.append(
                    ChildExecutionDiagnostic(
                        code="subagent_active_run_handle_missing",
                        subagent_run_id=run.subagent_run_id,
                        child_runtime_session_id=run.child_runtime_session_id,
                    )
                )
            elif not active and handle is not None and handle.coroutine is not None and not handle.coroutine.done():
                diagnostics.append(
                    ChildExecutionDiagnostic(
                        code="subagent_terminal_run_handle_active",
                        subagent_run_id=run.subagent_run_id,
                        child_runtime_session_id=run.child_runtime_session_id,
                    )
                )
        for handle in handles.values():
            diagnostics.append(
                ChildExecutionDiagnostic(
                    code="subagent_registry_orphan_handle",
                    subagent_run_id=handle.subagent_run_id,
                    child_runtime_session_id=handle.child_runtime_session_id,
                )
            )
        return tuple(diagnostics)


def _cancel_task_on_owner_loop(task: asyncio.Task[None]) -> None:
    if task.done():
        return
    owner_loop = task.get_loop()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is owner_loop:
        task.cancel()
        return
    if owner_loop.is_closed():
        return
    owner_loop.call_soon_threadsafe(_cancel_task_if_pending, task)


def _cancel_task_if_pending(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()


async def _wait_for_task_completion(
    task: asyncio.Task[None],
    *,
    timeout_seconds: float | None,
) -> bool:
    if task.done():
        return True
    current_loop = asyncio.get_running_loop()
    owner_loop = task.get_loop()
    if owner_loop is current_loop:
        _, pending = await asyncio.wait({task}, timeout=timeout_seconds)
        return not pending
    if owner_loop.is_closed():
        return task.done()

    completed = asyncio.Event()

    def signal_completion(_task: asyncio.Task[None]) -> None:
        current_loop.call_soon_threadsafe(completed.set)

    def register_callback() -> None:
        if task.done():
            signal_completion(task)
        else:
            task.add_done_callback(signal_completion)

    owner_loop.call_soon_threadsafe(register_callback)
    try:
        if timeout_seconds is None:
            await completed.wait()
        else:
            await asyncio.wait_for(completed.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return task.done()
    return True
