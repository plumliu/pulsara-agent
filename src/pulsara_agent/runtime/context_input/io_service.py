"""Session-owned bounded blocking I/O for context-input preparation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from time import monotonic
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pulsara_agent.runtime.blocking_executor import auxiliary_io_executor


T = TypeVar("T")


class ContextInputIoDeadlineExceeded(TimeoutError):
    pass


class PendingContextInputIoError(RuntimeError):
    pass


@dataclass(slots=True)
class _OwnedContextInputIoOperation:
    operation_id: str
    operation_name: str
    task: asyncio.Task[Any]
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class ContextInputIoOperationHandle(Generic[T]):
    """A service-owned blocking operation whose physical task outlives waiters."""

    operation_id: str
    operation_name: str
    deadline_monotonic: float
    _task: asyncio.Task[T]

    async def wait_physical_completion(self) -> T:
        """Wait for the worker without transferring cancellation to it."""

        return await asyncio.shield(self._task)

    @property
    def physically_complete(self) -> bool:
        return self._task.done()

    @property
    def physical_task_cancelled(self) -> bool:
        return self._task.cancelled()


class ContextInputIoService:
    """Own physical blocking operations independently from cancelled waiters."""

    def __init__(
        self,
        *,
        max_pending: int = 8,
        executor: Executor | None = None,
        max_workers: int | None = None,
    ) -> None:
        if max_pending < 1 or (max_workers is not None and max_workers < 1):
            raise ValueError("context-input I/O bounds must be positive")
        self._max_pending = max_pending
        self._executor = executor or auxiliary_io_executor()
        self._lock = asyncio.Lock()
        self._operations: dict[str, _OwnedContextInputIoOperation] = {}
        self._closed = False

    async def execute(
        self,
        *,
        operation_name: str,
        operation: Callable[[], T],
        deadline_monotonic: float,
    ) -> T:
        handle = await self.start_owned(
            operation_name=operation_name,
            operation=operation,
            deadline_monotonic=deadline_monotonic,
        )
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise ContextInputIoDeadlineExceeded(
                f"{operation_name} deadline exceeded before wait"
            )
        try:
            return await asyncio.wait_for(asyncio.shield(handle._task), remaining)
        except TimeoutError as exc:
            raise ContextInputIoDeadlineExceeded(
                f"{operation_name} deadline exceeded"
            ) from exc

    async def start_owned(
        self,
        *,
        operation_name: str,
        operation: Callable[[], T],
        deadline_monotonic: float,
    ) -> ContextInputIoOperationHandle[T]:
        """Start one physical operation and return its exact service-owned handle."""

        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._closed:
                raise RuntimeError("context-input I/O service is closed")
            if len(self._operations) >= self._max_pending:
                raise PendingContextInputIoError(
                    "max pending context-input I/O operations reached"
                )
            operation_id = f"context-input-io:{uuid4().hex}"

            async def run_owned() -> T:
                if monotonic() >= deadline_monotonic:
                    raise ContextInputIoDeadlineExceeded(
                        f"{operation_name} deadline exceeded before physical start"
                    )
                return await loop.run_in_executor(self._executor, operation)

            task = asyncio.create_task(
                run_owned(),
                name=f"{operation_name}:{operation_id}",
            )
            self._operations[operation_id] = _OwnedContextInputIoOperation(
                operation_id=operation_id,
                operation_name=operation_name,
                task=task,
                deadline_monotonic=deadline_monotonic,
            )
            task.add_done_callback(
                lambda completed, owned_id=operation_id: self._operation_done(
                    owned_id, completed
                )
            )
        return ContextInputIoOperationHandle(
            operation_id=operation_id,
            operation_name=operation_name,
            deadline_monotonic=deadline_monotonic,
            _task=task,
        )

    def _operation_done(
        self,
        operation_id: str,
        completed: asyncio.Task[Any],
    ) -> None:
        # Retrieve unobserved exceptions when the caller was cancelled.  The
        # operation remains physically owned until this callback runs.
        if not completed.cancelled():
            completed.exception()

        async def remove_exact() -> None:
            async with self._lock:
                current = self._operations.get(operation_id)
                if current is not None and current.task is completed:
                    self._operations.pop(operation_id, None)

        try:
            asyncio.get_running_loop().create_task(remove_exact())
        except RuntimeError:
            # Event-loop teardown is only legal after the Host drained owners.
            pass

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        while True:
            async with self._lock:
                tasks = tuple(item.task for item in self._operations.values())
            if not tasks:
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise PendingContextInputIoError(
                    "context-input I/O drain deadline exceeded"
                )
            done, pending = await asyncio.wait(
                tuple(asyncio.shield(task) for task in tasks),
                timeout=remaining,
            )
            if pending:
                raise PendingContextInputIoError(
                    "context-input I/O drain deadline exceeded"
                )
            for completed in done:
                try:
                    completed.result()
                except BaseException:
                    # Failure is already delivered to the original caller; the
                    # close barrier only owns physical completion.
                    pass
            await asyncio.sleep(0)

    def pending_count(self) -> int:
        return len(self._operations)

    def close_if_idle(self) -> None:
        if self._operations:
            raise PendingContextInputIoError(
                "cannot close context-input I/O service with pending operations"
            )
        self._closed = True


__all__ = [
    "ContextInputIoDeadlineExceeded",
    "ContextInputIoOperationHandle",
    "ContextInputIoService",
    "PendingContextInputIoError",
]
