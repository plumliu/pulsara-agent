"""Task settlement, filesystem, and admission helpers shared by Host owners."""

from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path
from time import monotonic

from pulsara_agent.workspace_identity import (
    WorkspaceUnavailable,
    observe_workspace,
    require_workspace,
)

from .host_contracts import (
    KernelHostCoreClosing,
)


async def _shielded_plugin_filesystem_call(
    operation, /, *, close_result=None, **kwargs
):
    """Join a started Plugin filesystem worker before cancellation escapes."""

    task = asyncio.create_task(
        asyncio.to_thread(operation, **kwargs),
        name=f"plugin-filesystem:{getattr(operation, '__name__', 'operation')}",
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        # A settled physical cause intentionally wins here; otherwise close
        # the successfully returned RAII value before propagating cancellation.
        result = task.result()
        if close_result is not None:
            close_result(result)
        else:
            close = getattr(result, "close", None)
            if close is not None:
                close()
        raise


async def _join_close_task(
    task: asyncio.Task[object], *, deadline_monotonic: float
) -> bool:
    """Join the exact async owner; report if its logical close deadline elapsed."""

    deadline_expired = False
    if not task.done():
        remaining = deadline_monotonic - monotonic()
        if remaining > 0:
            done, _pending = await asyncio.wait((task,), timeout=remaining)
            deadline_expired = not done
        else:
            deadline_expired = True
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Only the waiter is cancelled.  The Host-owned close operation
            # keeps the physical owner attached until it is terminal.
            continue
        except BaseException:
            break
    if not task.cancelled():
        task.result()
    return deadline_expired


async def _join_task_beyond_logical_deadline(
    task: asyncio.Task[object],
    *,
    deadline_monotonic: float,
) -> tuple[bool, asyncio.CancelledError | None, BaseException | None]:
    """Join one physical task even after its logical close deadline expires.

    The deadline controls the caller-visible close outcome; it never transfers
    or abandons ownership of the task.  Cancellation likewise detaches only the
    current waiter and is re-raised after the exact physical owner has exited.
    """

    deadline_expired = False
    waiter_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        remaining = deadline_monotonic - monotonic()
        if remaining > 0 and not deadline_expired:
            try:
                done, _pending = await asyncio.wait((task,), timeout=remaining)
            except asyncio.CancelledError as exc:
                waiter_cancellation = waiter_cancellation or exc
                continue
            if not done:
                deadline_expired = True
            continue
        deadline_expired = True
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.done():
                break
            waiter_cancellation = waiter_cancellation or exc
            continue
        except BaseException:
            break

    task_error: BaseException | None = None
    if not task.cancelled():
        try:
            task.result()
        except BaseException as exc:
            task_error = exc
    return deadline_expired, waiter_cancellation, task_error


def _restore_workspace_directory(root: Path) -> bool:
    observation = observe_workspace(root)
    if observation.outcome == "AVAILABLE":
        return False
    if observation.outcome != "MISSING":
        raise WorkspaceUnavailable(observation)
    root.parent.mkdir(parents=True, exist_ok=True)
    observation = observe_workspace(root)
    if observation.outcome == "AVAILABLE":
        return False
    if observation.outcome != "MISSING":
        raise WorkspaceUnavailable(observation)
    try:
        root.mkdir(exist_ok=False)
    except FileExistsError:
        require_workspace(root)
        return False
    require_workspace(root)
    return True


def _core_database_operation(operation=None, *, retain_result_on_cancel=False):
    """Keep an accepted Core operation owned until its physical work settles."""
    if operation is None:
        return lambda method: _core_database_operation(
            method, retain_result_on_cancel=retain_result_on_cancel
        )

    @wraps(operation)
    async def admitted(self, *args, **kwargs):
        current = asyncio.current_task()
        if (
            current in self._resource_operations
            or self._current_session_open_admitted()
        ):
            return await operation(self, *args, **kwargs)
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Kernel Host core is closing")
            task = asyncio.create_task(
                operation(self, *args, **kwargs),
                name=f"kernel-core:{operation.__name__}",
            )
            self._resource_operations[task] = self._canonical_deadline()
            task.add_done_callback(self._retire_resource_operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _join_task_beyond_logical_deadline(
                task, deadline_monotonic=self._canonical_deadline()
            )
            if retain_result_on_cancel:
                # Retirement already owns an exact committed result; its
                # established API reports it after the physical join.
                return task.result()
            raise

    return admitted


async def _acquire_lock_before_deadline(
    lock: asyncio.Lock,
    deadline_monotonic: float,
    message: str,
) -> None:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError(message)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=remaining)
    except TimeoutError:
        raise TimeoutError(message) from None


def _raise_if_deadline_expired(deadline_monotonic: float, message: str) -> None:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError(message)
