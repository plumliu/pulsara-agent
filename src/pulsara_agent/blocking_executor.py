"""Process-owned blocking lanes with reserved durable-ledger capacity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock


_EXECUTOR_LOCK = Lock()
_CRITICAL_LEDGER_EXECUTOR: ThreadPoolExecutor | None = None
_AUXILIARY_IO_EXECUTOR: ThreadPoolExecutor | None = None
_PROJECTION_MAINTENANCE_EXECUTOR: ThreadPoolExecutor | None = None
_BEST_EFFORT_AUDIT_EXECUTOR: ThreadPoolExecutor | None = None
_MAX_CRITICAL_LEDGER_WORKERS = 4
_MAX_AUXILIARY_IO_WORKERS = 12
_MAX_PROJECTION_MAINTENANCE_WORKERS = 9
_MAX_BEST_EFFORT_AUDIT_WORKERS = 2


@dataclass(frozen=True, slots=True)
class BlockingExecutorCapacity:
    critical_ledger_workers: int
    auxiliary_io_workers: int
    projection_maintenance_workers: int
    best_effort_audit_workers: int


def blocking_executor_capacity() -> BlockingExecutorCapacity:
    """Return the process-wide blocking lane configuration for diagnostics."""

    return BlockingExecutorCapacity(
        critical_ledger_workers=_MAX_CRITICAL_LEDGER_WORKERS,
        auxiliary_io_workers=_MAX_AUXILIARY_IO_WORKERS,
        projection_maintenance_workers=_MAX_PROJECTION_MAINTENANCE_WORKERS,
        best_effort_audit_workers=_MAX_BEST_EFFORT_AUDIT_WORKERS,
    )


def critical_ledger_executor() -> ThreadPoolExecutor:
    """Return the lane reserved for durable event commit and confirmation."""

    global _CRITICAL_LEDGER_EXECUTOR
    with _EXECUTOR_LOCK:
        if _CRITICAL_LEDGER_EXECUTOR is None:
            _CRITICAL_LEDGER_EXECUTOR = ThreadPoolExecutor(
                max_workers=_MAX_CRITICAL_LEDGER_WORKERS,
                thread_name_prefix="pulsara-critical-ledger",
            )
        return _CRITICAL_LEDGER_EXECUTOR


def auxiliary_io_executor() -> ThreadPoolExecutor:
    """Return the lane for context, artifact, and other non-ledger blocking I/O."""

    global _AUXILIARY_IO_EXECUTOR
    with _EXECUTOR_LOCK:
        if _AUXILIARY_IO_EXECUTOR is None:
            _AUXILIARY_IO_EXECUTOR = ThreadPoolExecutor(
                max_workers=_MAX_AUXILIARY_IO_WORKERS,
                thread_name_prefix="pulsara-auxiliary-io",
            )
        return _AUXILIARY_IO_EXECUTOR


def projection_maintenance_executor() -> ThreadPoolExecutor:
    """Return the process-owned lane for derived projection work."""

    global _PROJECTION_MAINTENANCE_EXECUTOR
    with _EXECUTOR_LOCK:
        if _PROJECTION_MAINTENANCE_EXECUTOR is None:
            _PROJECTION_MAINTENANCE_EXECUTOR = ThreadPoolExecutor(
                max_workers=_MAX_PROJECTION_MAINTENANCE_WORKERS,
                thread_name_prefix="pulsara-projection-maintenance",
            )
        return _PROJECTION_MAINTENANCE_EXECUTOR


def best_effort_audit_executor() -> ThreadPoolExecutor:
    """Return the isolated lane for optional compiler-audit materialization."""

    global _BEST_EFFORT_AUDIT_EXECUTOR
    with _EXECUTOR_LOCK:
        if _BEST_EFFORT_AUDIT_EXECUTOR is None:
            _BEST_EFFORT_AUDIT_EXECUTOR = ThreadPoolExecutor(
                max_workers=_MAX_BEST_EFFORT_AUDIT_WORKERS,
                thread_name_prefix="pulsara-best-effort-audit",
            )
        return _BEST_EFFORT_AUDIT_EXECUTOR


__all__ = [
    "BlockingExecutorCapacity",
    "auxiliary_io_executor",
    "best_effort_audit_executor",
    "blocking_executor_capacity",
    "critical_ledger_executor",
    "projection_maintenance_executor",
]
