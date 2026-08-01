"""Session-owned bounded blocking I/O for terminal presentation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from time import monotonic
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pulsara_agent.blocking_executor import auxiliary_io_executor


T = TypeVar("T")


class PresentationIoDeadlineExceeded(TimeoutError):
    pass


class PendingPresentationIoError(RuntimeError):
    pass


@dataclass(slots=True)
class _OwnedPresentationIoOperation:
    operation_id: str
    operation_name: str
    task: asyncio.Task[Any]
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class PresentationIoOperationHandle(Generic[T]):
    """Exact physical operation retained independently from a cancelled waiter."""

    operation_id: str
    operation_name: str
    deadline_monotonic: float
    _task: asyncio.Task[T]

    async def wait_physical_completion(self) -> T:
        return await asyncio.shield(self._task)

    @property
    def physically_complete(self) -> bool:
        return self._task.done()


class TerminalPresentationIoService:
    """Own blocking presentation reads/writes until their threads really exit."""

    def __init__(
        self,
        *,
        max_pending: int = 8,
        executor: Executor | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("presentation I/O bound must be positive")
        self._max_pending = max_pending
        self._executor = executor or auxiliary_io_executor()
        self._lock = asyncio.Lock()
        self._operations: dict[str, _OwnedPresentationIoOperation] = {}
        self._admission_open = True
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
            raise PresentationIoDeadlineExceeded(
                f"{operation_name} deadline exceeded before wait"
            )
        try:
            return await asyncio.wait_for(
                asyncio.shield(handle._task), timeout=remaining
            )
        except TimeoutError as exc:
            # The worker remains registered until its actual executor future exits.
            raise PresentationIoDeadlineExceeded(
                f"{operation_name} deadline exceeded"
            ) from exc

    async def start_owned(
        self,
        *,
        operation_name: str,
        operation: Callable[[], T],
        deadline_monotonic: float,
    ) -> PresentationIoOperationHandle[T]:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._closed or not self._admission_open:
                raise PendingPresentationIoError(
                    "terminal presentation I/O admission is closed"
                )
            if len(self._operations) >= self._max_pending:
                raise PendingPresentationIoError(
                    "max pending terminal presentation I/O operations reached"
                )
            operation_id = f"terminal-presentation-io:{uuid4().hex}"

            async def run_owned() -> T:
                if monotonic() >= deadline_monotonic:
                    raise PresentationIoDeadlineExceeded(
                        f"{operation_name} deadline exceeded before physical start"
                    )
                return await loop.run_in_executor(self._executor, operation)

            task = asyncio.create_task(
                run_owned(), name=f"{operation_name}:{operation_id}"
            )
            self._operations[operation_id] = _OwnedPresentationIoOperation(
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
        return PresentationIoOperationHandle(
            operation_id=operation_id,
            operation_name=operation_name,
            deadline_monotonic=deadline_monotonic,
            _task=task,
        )

    def _operation_done(self, operation_id: str, completed: asyncio.Task[Any]) -> None:
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
            # Host close must drain before loop teardown; retain the entry otherwise.
            pass

    async def stop_admission_and_drain(self, *, deadline_monotonic: float) -> None:
        async with self._lock:
            self._admission_open = False
        while True:
            async with self._lock:
                tasks = tuple(item.task for item in self._operations.values())
            if not tasks:
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise PendingPresentationIoError(
                    "terminal presentation I/O close drain deadline exceeded"
                )
            done, pending = await asyncio.wait(
                tuple(asyncio.shield(task) for task in tasks), timeout=remaining
            )
            if pending:
                raise PendingPresentationIoError(
                    "terminal presentation I/O close drain deadline exceeded"
                )
            for completed in done:
                try:
                    completed.result()
                except BaseException:
                    pass
            await asyncio.sleep(0)

    def pending_count(self) -> int:
        return len(self._operations)

    def close_if_idle(self) -> None:
        if self._operations:
            raise PendingPresentationIoError(
                "cannot close terminal presentation I/O with physical operations"
            )
        self._admission_open = False
        self._closed = True


__all__ = [
    "PendingPresentationIoError",
    "PresentationIoDeadlineExceeded",
    "PresentationIoOperationHandle",
    "TerminalPresentationIoService",
]
