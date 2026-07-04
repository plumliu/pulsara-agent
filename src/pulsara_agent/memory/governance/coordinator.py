"""Debounced application-level coordinator for automatic memory governance."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

from pulsara_agent.memory.governance.engine import MemoryGovernanceEngine


@dataclass(slots=True)
class MemoryGovernanceCoordinator:
    debounce_seconds: float = 0.25
    session_min_interval_seconds: float = 5.0
    on_commit: Callable[[], None] | None = None
    _pending: dict[str, MemoryGovernanceEngine] = field(default_factory=dict, init=False, repr=False)
    _last_run: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def notify(self, engine: MemoryGovernanceEngine) -> None:
        if self._stop.is_set():
            return
        session_id = engine.executor.runtime_session_id
        if not any(
            candidate.source_session_id == session_id
            for candidate in engine.executor.candidate_pool.list_pending()
        ):
            return
        self._pending[session_id] = engine
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            await asyncio.sleep(self.debounce_seconds)
            pending = self._pending
            self._pending = {}
            deferred_delay: float | None = None
            for session_id, engine in pending.items():
                elapsed = time.monotonic() - self._last_run.get(session_id, 0.0)
                if elapsed < self.session_min_interval_seconds:
                    self._pending[session_id] = engine
                    delay = self.session_min_interval_seconds - elapsed
                    deferred_delay = delay if deferred_delay is None else min(deferred_delay, delay)
                    continue
                result = await engine.run_pending(trigger_reason="turn_safe_point")
                self._last_run[session_id] = time.monotonic()
                if result.applied and self.on_commit is not None:
                    self.on_commit()
            if self._pending:
                if deferred_delay:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=deferred_delay)
                    except TimeoutError:
                        pass
                self._wake.set()

    async def aclose(self) -> None:
        self._stop.set()
        self._wake.set()
