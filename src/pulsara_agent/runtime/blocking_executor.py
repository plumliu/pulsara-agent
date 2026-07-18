"""Process-owned blocking lanes with reserved durable-ledger capacity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock


_EXECUTOR_LOCK = Lock()
_CRITICAL_LEDGER_EXECUTOR: ThreadPoolExecutor | None = None
_AUXILIARY_IO_EXECUTOR: ThreadPoolExecutor | None = None
_MAX_CRITICAL_LEDGER_WORKERS = 4
_MAX_AUXILIARY_IO_WORKERS = 12


@dataclass(frozen=True, slots=True)
class BlockingExecutorCapacity:
    critical_ledger_workers: int
    auxiliary_io_workers: int


def blocking_executor_capacity() -> BlockingExecutorCapacity:
    """Return the process-wide blocking lane configuration for diagnostics."""

    return BlockingExecutorCapacity(
        critical_ledger_workers=_MAX_CRITICAL_LEDGER_WORKERS,
        auxiliary_io_workers=_MAX_AUXILIARY_IO_WORKERS,
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


__all__ = [
    "BlockingExecutorCapacity",
    "auxiliary_io_executor",
    "blocking_executor_capacity",
    "critical_ledger_executor",
]
