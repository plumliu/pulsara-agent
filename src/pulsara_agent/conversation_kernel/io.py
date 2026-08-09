"""Session-owned bounded physical I/O lane for the conversation kernel.

Repository and blob adapters intentionally remain synchronous.  This owner is
the only foreground seam allowed to execute those physical operations from an
async Host activation.  Cancellation detaches a caller but never pretends the
underlying PostgreSQL operation stopped; Host close drains every admitted task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from time import monotonic
from typing import TypeVar

from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS


T = TypeVar("T")


class KernelSessionIOClosed(RuntimeError):
    pass


class KernelSessionIO:
    def __init__(
        self,
        *,
        maximum_concurrency: int = STAGE2_LIMITS.foreground_io_hard_concurrency,
    ) -> None:
        if not 1 <= maximum_concurrency <= STAGE2_LIMITS.foreground_io_hard_concurrency:
            raise ValueError("foreground I/O concurrency is outside its hard bound")
        self._semaphore = asyncio.Semaphore(maximum_concurrency)
        self._active: set[asyncio.Task[object]] = set()
        self._closing = False
        self._lock = asyncio.Lock()

    async def run(
        self,
        operation: Callable[..., T],
        /,
        *args: object,
        deadline_monotonic: float,
        **kwargs: object,
    ) -> T:
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("foreground I/O deadline expired before admission")
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=remaining)
        except TimeoutError as exc:
            raise TimeoutError("foreground I/O admission deadline expired") from exc
        task: asyncio.Task[object] | None = None
        try:
            async with self._lock:
                if self._closing:
                    raise KernelSessionIOClosed("foreground I/O owner is closing")
                task = asyncio.create_task(
                    asyncio.to_thread(
                        partial(
                            operation,
                            *args,
                            deadline_monotonic=deadline_monotonic,
                            **kwargs,
                        )
                    ),
                    name=f"kernel-physical-io:{getattr(operation, '__name__', 'operation')}",
                )
                self._active.add(task)
                task.add_done_callback(self._retire)
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("foreground I/O deadline expired after admission")
            return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)  # type: ignore[return-value]
        except BaseException:
            if task is None:
                self._semaphore.release()
            raise

    def _retire(self, task: asyncio.Task[object]) -> None:
        self._active.discard(task)
        self._semaphore.release()
        # Retrieve errors from callers that detached on cancellation/deadline.
        if not task.cancelled():
            task.exception()

    async def aclose(self, *, deadline_monotonic: float) -> None:
        async with self._lock:
            self._closing = True
        while True:
            active = tuple(self._active)
            if not active:
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("foreground physical I/O did not exit before close")
            done, pending = await asyncio.wait(active, timeout=remaining)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                raise TimeoutError("foreground physical I/O did not exit before close")


__all__ = ["KernelSessionIO", "KernelSessionIOClosed"]
