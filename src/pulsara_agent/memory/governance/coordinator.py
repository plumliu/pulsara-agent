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
    _retry_attempts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _next_retry_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def notify(self, engine: MemoryGovernanceEngine) -> None:
        if self._stop.is_set():
            return
        session_id = engine.executor.runtime_session_id
        # A safe point must also replay durable governance-event outbox rows.
        # Candidate absence therefore cannot suppress the session-owned worker.
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
                now = time.monotonic()
                retry_at = self._next_retry_at.get(session_id)
                if retry_at is not None and now < retry_at:
                    self._pending[session_id] = engine
                    delay = retry_at - now
                    deferred_delay = delay if deferred_delay is None else min(deferred_delay, delay)
                    continue
                elapsed = now - self._last_run.get(session_id, 0.0)
                if retry_at is None and elapsed < self.session_min_interval_seconds:
                    self._pending[session_id] = engine
                    delay = self.session_min_interval_seconds - elapsed
                    deferred_delay = delay if deferred_delay is None else min(deferred_delay, delay)
                    continue
                try:
                    result = await engine.run_pending(trigger_reason="turn_safe_point")
                except Exception:
                    retry_required = True
                    result = None
                else:
                    retry_required = (
                        engine.executor.event_dispatch_retry_required
                        or engine.retry_required
                    )
                self._last_run[session_id] = time.monotonic()
                if result is not None and result.applied and self.on_commit is not None:
                    self.on_commit()
                if retry_required:
                    attempt = min(self._retry_attempts.get(session_id, 0) + 1, 7)
                    self._retry_attempts[session_id] = attempt
                    retry_delay = min(0.5 * (2 ** (attempt - 1)), 30.0)
                    self._next_retry_at[session_id] = time.monotonic() + retry_delay
                    self._pending[session_id] = engine
                    deferred_delay = (
                        retry_delay
                        if deferred_delay is None
                        else min(deferred_delay, retry_delay)
                    )
                else:
                    self._retry_attempts.pop(session_id, None)
                    self._next_retry_at.pop(session_id, None)
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
