"""Session-owned FIFO for blocking runtime event writes."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from threading import RLock, local
from time import monotonic
from typing import Any, TypeVar
from uuid import uuid4

from pulsara_agent.runtime.blocking_executor import critical_ledger_executor


T = TypeVar("T")


class PendingRuntimeEventWriteError(RuntimeError):
    """The session cannot accept or drain another physical event write."""


class RuntimeEventWriteCancelled(asyncio.CancelledError):
    """Caller cancellation observed the terminal result of its physical owner."""

    def __init__(
        self,
        *,
        operation_result: Any | None,
        operation_error: BaseException | None,
        deadline_monotonic: float,
    ) -> None:
        self.operation_result = operation_result
        self.operation_error = operation_error
        self.deadline_monotonic = deadline_monotonic
        super().__init__("runtime event write caller cancelled after physical resolution")


@dataclass(slots=True)
class _OwnedRuntimeEventWrite:
    operation_id: str
    operation: Callable[[], Any]
    completion: Future[Any]
    deadline_monotonic: float


class RuntimeEventWriteService:
    """Serialize blocking ledger writes without blocking the asyncio loop."""

    def __init__(
        self,
        *,
        max_pending: int = 64,
        operation_timeout_seconds: float = 30.0,
    ) -> None:
        if max_pending < 1:
            raise ValueError("runtime event-write bound must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("runtime event-write timeout must be positive")
        self._max_pending = max_pending
        self._operation_timeout_seconds = operation_timeout_seconds
        self._executor = critical_ledger_executor()
        self._lock = RLock()
        self._operations: dict[str, _OwnedRuntimeEventWrite] = {}
        self._queue: deque[_OwnedRuntimeEventWrite] = deque()
        self._active_operation_id: str | None = None
        self._worker_local = local()
        self._closed = False

    async def execute(
        self,
        operation: Callable[[], T],
        *,
        deadline_monotonic: float | None = None,
    ) -> T:
        owned = self._enqueue(operation, deadline_monotonic=deadline_monotonic)
        try:
            return await self._await_owned(owned)
        except asyncio.CancelledError as cancelled:
            queued_error = PendingRuntimeEventWriteError(
                "runtime event write cancelled before physical start"
            )
            if self._expire_queued(owned):
                raise RuntimeEventWriteCancelled(
                    operation_result=None,
                    operation_error=queued_error,
                    deadline_monotonic=owned.deadline_monotonic,
                ) from cancelled
            # Confirmation is only safe after this physical owner has stopped.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            try:
                result = await asyncio.shield(
                    asyncio.wrap_future(owned.completion)
                )
            except BaseException as error:
                raise RuntimeEventWriteCancelled(
                    operation_result=None,
                    operation_error=error,
                    deadline_monotonic=owned.deadline_monotonic,
                ) from cancelled
            raise RuntimeEventWriteCancelled(
                operation_result=result,
                operation_error=None,
                deadline_monotonic=owned.deadline_monotonic,
            ) from cancelled

    def execute_blocking(
        self,
        operation: Callable[[], T],
        *,
        deadline_monotonic: float | None = None,
    ) -> T:
        """Join the same FIFO from a tool worker or an owned writer callback."""

        if getattr(self._worker_local, "owner", None) is self:
            return operation()
        owned = self._enqueue(operation, deadline_monotonic=deadline_monotonic)
        remaining = max(0.0, owned.deadline_monotonic - monotonic())
        try:
            return owned.completion.result(timeout=remaining)
        except FutureTimeoutError:
            error = PendingRuntimeEventWriteError(
                "runtime event-write deadline exceeded while queued"
            )
            if self._expire_queued(owned):
                raise error
            return owned.completion.result()

    def _enqueue(
        self,
        operation: Callable[[], T],
        *,
        deadline_monotonic: float | None,
    ) -> _OwnedRuntimeEventWrite:
        deadline = (
            monotonic() + self._operation_timeout_seconds
            if deadline_monotonic is None
            else deadline_monotonic
        )
        if deadline <= monotonic():
            raise PendingRuntimeEventWriteError(
                "runtime event-write deadline exceeded before enqueue"
            )
        with self._lock:
            if self._closed:
                raise PendingRuntimeEventWriteError(
                    "runtime event-write service is closed"
                )
            if len(self._operations) >= self._max_pending:
                raise PendingRuntimeEventWriteError(
                    "max pending runtime event writes reached"
                )
            owned = _OwnedRuntimeEventWrite(
                operation_id=f"runtime-event-write:{uuid4().hex}",
                operation=operation,
                completion=Future(),
                deadline_monotonic=deadline,
            )
            self._operations[owned.operation_id] = owned
            self._queue.append(owned)
            self._start_next_locked()
            return owned

    async def _await_owned(self, owned: _OwnedRuntimeEventWrite) -> T:
        remaining = max(0.0, owned.deadline_monotonic - monotonic())
        try:
            return await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(owned.completion)),
                timeout=remaining,
            )
        except TimeoutError:
            error = PendingRuntimeEventWriteError(
                "runtime event-write deadline exceeded while queued"
            )
            if self._expire_queued(owned):
                raise error
            # The operation crossed the physical-start linearization point.
            # Its database deadline and final confirmation now own resolution.
            return await asyncio.shield(asyncio.wrap_future(owned.completion))

    def _expire_queued(
        self,
        owned: _OwnedRuntimeEventWrite,
    ) -> bool:
        """CAS-remove one operation only while it is still queue-owned."""

        with self._lock:
            if self._active_operation_id == owned.operation_id:
                return False
            current = self._operations.get(owned.operation_id)
            if current is not owned:
                return False
            try:
                self._queue.remove(owned)
            except ValueError:
                return False
            self._operations.pop(owned.operation_id, None)
            owned.completion.cancel()
            return True

    def _start_next_locked(self) -> None:
        if self._active_operation_id is not None:
            return
        while self._queue:
            owned = self._queue.popleft()
            if monotonic() < owned.deadline_monotonic:
                break
            self._operations.pop(owned.operation_id, None)
            owned.completion.cancel()
        else:
            return
        self._active_operation_id = owned.operation_id
        try:
            physical = self._executor.submit(self._run_owned, owned)
        except BaseException as exc:
            self._active_operation_id = None
            self._operations.pop(owned.operation_id, None)
            owned.completion.set_exception(exc)
            self._start_next_locked()
            return
        physical.add_done_callback(
            lambda completed, operation_id=owned.operation_id: (
                self._operation_done(operation_id, completed)
            )
        )

    def _run_owned(self, owned: _OwnedRuntimeEventWrite) -> Any:
        self._worker_local.owner = self
        self._worker_local.deadline_monotonic = owned.deadline_monotonic
        try:
            if monotonic() >= owned.deadline_monotonic:
                raise PendingRuntimeEventWriteError(
                    "runtime event-write deadline exceeded before physical start"
                )
            return owned.operation()
        finally:
            self._worker_local.owner = None
            self._worker_local.deadline_monotonic = None

    def new_deadline_monotonic(self) -> float:
        """Allocate the one absolute deadline for a new FIFO attempt."""

        return monotonic() + self._operation_timeout_seconds

    def is_current_owner(self) -> bool:
        return getattr(self._worker_local, "owner", None) is self

    def current_deadline_monotonic(self) -> float:
        """Return the current critical-owner deadline; never mint a new one."""

        current = getattr(self._worker_local, "deadline_monotonic", None)
        if isinstance(current, float):
            return current
        raise RuntimeError(
            "runtime event confirmation requires the critical writer owner"
        )

    def _operation_done(self, operation_id: str, completed: Future[Any]) -> None:
        try:
            result = completed.result()
            error: BaseException | None = None
        except BaseException as exc:
            result = None
            error = exc
        with self._lock:
            owned = self._operations.pop(operation_id, None)
            if owned is None or self._active_operation_id != operation_id:
                return
            self._active_operation_id = None
            if error is None:
                owned.completion.set_result(result)
            else:
                owned.completion.set_exception(error)
            self._start_next_locked()

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        while True:
            with self._lock:
                completions = tuple(
                    item.completion for item in self._operations.values()
                )
            if not completions:
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise PendingRuntimeEventWriteError(
                    "runtime event-write drain deadline exceeded"
                )
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(asyncio.wrap_future(item) for item in completions),
                        return_exceptions=True,
                    ),
                    timeout=remaining,
                )
            except TimeoutError as exc:
                raise PendingRuntimeEventWriteError(
                    "runtime event-write drain deadline exceeded"
                ) from exc
            await asyncio.sleep(0)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._operations)

    def close_if_idle(self) -> None:
        with self._lock:
            if self._operations:
                raise PendingRuntimeEventWriteError(
                    "cannot close runtime event writer with pending operations"
                )
            self._closed = True


__all__ = [
    "PendingRuntimeEventWriteError",
    "RuntimeEventWriteCancelled",
    "RuntimeEventWriteService",
]
