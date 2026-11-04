"""Minimal process-local sharing of overlapping, already authorized ASR gaps.

Coverage remains owned by successful cache blocks. Pending work is only a live
physical observation: its bound source and first accepted configuration stay
with that task, and the last departing waiter cancels and settles it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .asr_cache import AsrMediaKey
from .source import MediaError


@dataclass(eq=False, slots=True)
class _Work:
    key: AsrMediaKey
    start: float
    end: float
    task: asyncio.Task
    cleanup: Callable[[], None] | None = None
    waiters: int = 0


@dataclass(slots=True)
class PreparedTranscriptWork:
    awaitable: Awaitable[object]
    cleanup: Callable[[], None]


class TranscriptWork:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: list[_Work] = []
        self._closing = False

    @property
    def physical_close_complete(self) -> bool:
        return self._closing and not self._active

    async def aclose(self) -> None:
        async with self._lock:
            self._closing = True
            tasks = [work.task for work in self._active]
            owned = tuple(self._active)
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for work in owned:
            self._retire(work)

    async def run(
        self,
        key: AsrMediaKey,
        start: float,
        end: float,
        prepare: Callable[[float, float], PreparedTranscriptWork],
    ) -> tuple[tuple[float, float, object], ...]:
        """Join intersecting work and allocate only its exact uncovered gaps.

        ``prepare`` runs synchronously under claim ownership, before a caller can
        close its source; it must allocate the task's independent source binding.
        Audio itself is decoded only inside the admitted asynchronous work.
        """
        created = []
        try:
            async with self._lock:
                if self._closing:
                    raise MediaError("CANCELLED", "The shared ASR owner is closing")
                overlaps = sorted(
                    (
                        work
                        for work in self._active
                        if work.key == key and work.start < end and work.end > start
                    ),
                    key=lambda work: (work.start, work.end),
                )
                gaps = []
                at = start
                for work in overlaps:
                    if work.start > at:
                        gaps.append((at, min(end, work.start)))
                    at = max(at, min(end, work.end))
                if at < end:
                    gaps.append((at, end))
                chosen = list(overlaps)
                for a, b in gaps:
                    prepared = prepare(a, b)
                    try:
                        task = asyncio.create_task(prepared.awaitable)
                    except BaseException:
                        prepared.cleanup()
                        if hasattr(prepared.awaitable, "close"):
                            prepared.awaitable.close()
                        raise
                    work = _Work(key, a, b, task, prepared.cleanup)
                    self._active.append(work)
                    created.append(work)
                    chosen.append(work)
                    task.add_done_callback(
                        lambda _task, owned=work: self._retire(owned)
                    )
                for work in chosen:
                    work.waiters += 1
        except BaseException:
            for work in created:
                work.task.cancel()
            if created:
                await asyncio.gather(
                    *(work.task for work in created), return_exceptions=True
                )
            for work in created:
                self._retire(work)
            raise
        result = []
        try:
            for work in sorted(chosen, key=lambda value: (value.start, value.end)):
                result.append((work.start, work.end, await asyncio.shield(work.task)))
            return tuple(result)
        finally:
            settle = []
            async with self._lock:
                for work in chosen:
                    work.waiters -= 1
                    if not work.waiters and not work.task.done():
                        work.task.cancel()
                        settle.append(work.task)
            if settle:
                await asyncio.gather(*settle, return_exceptions=True)

    def _retire(self, work: _Work) -> None:
        if work.cleanup is not None:
            cleanup, work.cleanup = work.cleanup, None
            cleanup()
        if work in self._active:
            self._active.remove(work)
        # Retrieve exceptions even when the last logical waiter cancelled.
        if work.task.done() and not work.task.cancelled():
            work.task.exception()
