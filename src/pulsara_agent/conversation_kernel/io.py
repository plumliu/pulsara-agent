"""Session-owned bounded physical I/O lane for the conversation kernel.

Repository and blob adapters intentionally remain synchronous.  This owner is
the only foreground seam allowed to execute those physical operations from an
async Host activation.  Cancellation remains attached until the admitted
physical operation exits; Host close also drains every admitted task.
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
                # Admission has already created the physical thread.  A
                # logical deadline may detach no mutation owner: wait for the
                # exact task to exit before reporting the timeout, just like
                # the timed wait branch below.
                await _drain_physical_task(task)
                raise TimeoutError(
                    "foreground physical I/O exited after its deadline"
                )
            try:
                return await asyncio.wait_for(  # type: ignore[return-value]
                    asyncio.shield(task), timeout=remaining
                )
            except TimeoutError as exc:
                # The logical deadline cannot terminate a worker thread.  The
                # caller may report a deadline only after the physical owner
                # has exited; close can meanwhile observe this task and block.
                await _drain_physical_task(task)
                raise TimeoutError(
                    "foreground physical I/O exited after its deadline"
                ) from exc
            except asyncio.CancelledError:
                # asyncio cannot cancel a worker thread.  Do not let a caller
                # observe cancellation while the exact physical operation can
                # still mutate resources behind the next settlement/close.
                await _drain_physical_task(task)
                if not task.cancelled():
                    task.result()
                raise
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


async def _drain_physical_task(task: asyncio.Task[object]) -> None:
    """Join one admitted thread even if its logical waiter is cancelled again."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated cancellation only detaches the logical waiter after the
            # physical operation has reached a terminal state.
            continue
        except BaseException:
            # The task is terminal; the caller decides whether the physical
            # exception or its logical timeout/cancellation is authoritative.
            break


__all__ = ["KernelSessionIO", "KernelSessionIOClosed"]
