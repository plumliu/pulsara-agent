"""Process-owned blocking lanes with reserved durable-ledger capacity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock


_EXECUTOR_LOCK = Lock()
_CRITICAL_LEDGER_EXECUTOR: ThreadPoolExecutor | None = None
_AUXILIARY_IO_EXECUTOR: ThreadPoolExecutor | None = None
_MAX_CRITICAL_LEDGER_WORKERS = 4
_MAX_AUXILIARY_IO_WORKERS = 12


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


__all__ = ["auxiliary_io_executor", "critical_ledger_executor"]
