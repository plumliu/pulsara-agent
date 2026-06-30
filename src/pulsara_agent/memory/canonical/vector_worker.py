"""HostCore-owned in-process vector outbox worker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pulsara_agent.memory.canonical.vector_index_sync import MemoryVectorIndexSync


@dataclass(slots=True)
class MemoryVectorIndexWorker:
    sync: MemoryVectorIndexSync
    poll_interval_seconds: float = 1.0
    batch_size: int = 100
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                while await self.sync.consume_outbox(limit=self.batch_size):
                    if self._stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed vector surface remains retryable in the unified outbox.
                pass
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        self._stop.set()
        self._wake.set()
