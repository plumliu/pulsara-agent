"""Session-owned bounded blocking I/O for context-input preparation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from time import monotonic
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pulsara_agent.blocking_executor import (
    auxiliary_io_executor,
    best_effort_audit_executor,
)


T = TypeVar("T")


class ContextInputIoDeadlineExceeded(TimeoutError):
    pass


class PendingContextInputIoError(RuntimeError):
    pass


class ContextInputIoLane(StrEnum):
    CRITICAL = "critical"
    BEST_EFFORT_AUDIT = "best_effort_audit"


class AuditOfferDisposition(StrEnum):
    ACCEPTED = "accepted"
    SKIPPED_SERVICE_CLOSED = "skipped_service_closed"
    SKIPPED_SESSION_CAPACITY = "skipped_session_capacity"
    SKIPPED_PROCESS_CAPACITY = "skipped_process_capacity"
    SKIPPED_PROCESS_RESIDENT_BOUND = "skipped_process_resident_bound"
    SKIPPED_PHYSICAL_BOUND = "skipped_physical_bound"


@dataclass(frozen=True, slots=True)
class AuditOfferResult:
    disposition: AuditOfferDisposition
    operation_id: str | None


@dataclass(slots=True)
class _ProcessAuditPermitState:
    operation_count: int = 0
    resident_bytes: int = 0


class _ProcessAuditPermitManager:
    def __init__(self) -> None:
        self._lock = Lock()
        self._state = _ProcessAuditPermitState()
        self._maximum_operations = 8
        self._maximum_resident_bytes = 64 * 1024 * 1024

    def try_acquire(self, resident_bytes: int) -> AuditOfferDisposition | None:
        if resident_bytes < 1 or resident_bytes > 32 * 1024 * 1024:
            return AuditOfferDisposition.SKIPPED_PHYSICAL_BOUND
        with self._lock:
            if self._state.operation_count >= self._maximum_operations:
                return AuditOfferDisposition.SKIPPED_PROCESS_CAPACITY
            if (
                self._state.resident_bytes + resident_bytes
                > self._maximum_resident_bytes
            ):
                return AuditOfferDisposition.SKIPPED_PROCESS_RESIDENT_BOUND
            self._state.operation_count += 1
            self._state.resident_bytes += resident_bytes
        return None

    def release(self, resident_bytes: int) -> None:
        with self._lock:
            if (
                self._state.operation_count < 1
                or self._state.resident_bytes < resident_bytes
            ):
                raise RuntimeError("best-effort audit process permit underflow")
            self._state.operation_count -= 1
            self._state.resident_bytes -= resident_bytes

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self._state.operation_count, self._state.resident_bytes


_PROCESS_AUDIT_PERMITS = _ProcessAuditPermitManager()


def best_effort_audit_process_usage() -> tuple[int, int]:
    return _PROCESS_AUDIT_PERMITS.snapshot()


@dataclass(slots=True)
class _OwnedContextInputIoOperation:
    operation_id: str
    operation_name: str
    task: asyncio.Task[Any]
    deadline_monotonic: float


@dataclass(slots=True)
class _OwnedBestEffortAuditOperation:
    operation_id: str
    operation_name: str
    task: asyncio.Task[Any]
    deadline_monotonic: float
    resident_charge_bytes: int


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
        self._audit_operation: _OwnedBestEffortAuditOperation | None = None
        self._closed = False

    def offer_best_effort_nowait(
        self,
        *,
        operation_name: str,
        operation: Callable[[], T],
        deadline_monotonic: float,
        resident_charge_bytes: int,
        completion_observer: Callable[[str, int, int], None] | None = None,
    ) -> AuditOfferResult:
        """Offer one optional audit operation without awaiting any resource."""

        if self._closed:
            return AuditOfferResult(
                AuditOfferDisposition.SKIPPED_SERVICE_CLOSED,
                None,
            )
        if self._audit_operation is not None:
            return AuditOfferResult(
                AuditOfferDisposition.SKIPPED_SESSION_CAPACITY,
                None,
            )
        rejected = _PROCESS_AUDIT_PERMITS.try_acquire(resident_charge_bytes)
        if rejected is not None:
            return AuditOfferResult(rejected, None)
        loop = asyncio.get_running_loop()
        operation_id = f"context-input-audit:{uuid4().hex}"

        async def run_owned() -> T:
            if monotonic() >= deadline_monotonic:
                raise ContextInputIoDeadlineExceeded(
                    f"{operation_name} deadline exceeded before physical start"
                )
            return await loop.run_in_executor(best_effort_audit_executor(), operation)

        try:
            task = asyncio.create_task(
                run_owned(),
                name=f"{operation_name}:{operation_id}",
            )
            owned = _OwnedBestEffortAuditOperation(
                operation_id=operation_id,
                operation_name=operation_name,
                task=task,
                deadline_monotonic=deadline_monotonic,
                resident_charge_bytes=resident_charge_bytes,
            )
            self._audit_operation = owned
            task.add_done_callback(
                lambda completed, expected=owned: self._audit_operation_done(
                    expected,
                    completed,
                    completion_observer,
                )
            )
        except BaseException:
            _PROCESS_AUDIT_PERMITS.release(resident_charge_bytes)
            raise
        return AuditOfferResult(AuditOfferDisposition.ACCEPTED, operation_id)

    def _audit_operation_done(
        self,
        expected: _OwnedBestEffortAuditOperation,
        completed: asyncio.Task[Any],
        observer: Callable[[str, int, int], None] | None,
    ) -> None:
        result: object | None = None
        error: BaseException | None = None
        if completed.cancelled():
            error = asyncio.CancelledError()
        else:
            try:
                result = completed.result()
            except BaseException as exc:
                error = exc
        if error is None:
            code = str(getattr(result, "disposition", "completed"))
            page_count = int(getattr(result, "page_count", 0))
            byte_count = int(getattr(result, "total_page_canonical_bytes", 0))
        else:
            code = f"failed:{type(error).__name__}"
            page_count = 0
            byte_count = 0

        def settle() -> None:
            if self._audit_operation is expected:
                self._audit_operation = None
            _PROCESS_AUDIT_PERMITS.release(expected.resident_charge_bytes)
            if observer is not None:
                try:
                    observer(code, page_count, byte_count)
                except BaseException:
                    # Optional operational observers cannot retain the owner or
                    # turn audit failure into a runtime failure.
                    pass

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Legal shutdown drains the operation before loop teardown.  This
            # branch is defensive and still releases the process permit.
            settle()
        else:
            loop.call_soon(settle)

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
            audit = self._audit_operation
            if audit is not None:
                tasks = (*tasks, audit.task)
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
        return len(self._operations) + (self._audit_operation is not None)

    def close_if_idle(self) -> None:
        if self._operations or self._audit_operation is not None:
            raise PendingContextInputIoError(
                "cannot close context-input I/O service with pending operations"
            )
        self._closed = True


__all__ = [
    "AuditOfferDisposition",
    "AuditOfferResult",
    "best_effort_audit_process_usage",
    "ContextInputIoLane",
    "ContextInputIoDeadlineExceeded",
    "ContextInputIoOperationHandle",
    "ContextInputIoService",
    "PendingContextInputIoError",
]
