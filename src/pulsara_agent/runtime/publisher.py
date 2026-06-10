"""Ordered in-process event publishing for one RuntimeSession."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Protocol

from pulsara_agent.event import AgentEvent
from pulsara_agent.runtime.state import LoopState


@dataclass(frozen=True, slots=True)
class RuntimePublishedEvent:
    runtime_session_id: str
    event: AgentEvent
    state: LoopState | None = None


class RuntimeEventSubscriber(Protocol):
    async def on_published_event(self, published: RuntimePublishedEvent) -> None: ...


class RuntimeEventPublisher:
    def __init__(self, *, runtime_session_id: str) -> None:
        self.runtime_session_id = runtime_session_id
        self._subscribers: list[RuntimeEventSubscriber] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._drain_lock: asyncio.Lock | None = None
        self._next_sequence_to_publish = 1
        self._pending_by_sequence: dict[int, RuntimePublishedEvent] = {}

    def subscribe(self, subscriber: RuntimeEventSubscriber) -> None:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: RuntimeEventSubscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    async def publish(self, published: RuntimePublishedEvent) -> None:
        loop = asyncio.get_running_loop()
        self._bind_loop(loop)
        assert self._drain_lock is not None
        async with self._drain_lock:
            self._store_pending(published)
            await self._drain_pending()

    def publish_from_thread(self, published: RuntimePublishedEvent) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        if self._loop_thread_id == threading.get_ident():
            self._loop.create_task(self.publish(published))
            return
        future = asyncio.run_coroutine_threadsafe(self.publish(published), self._loop)
        future.result()

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is None or self._loop.is_closed():
            self._loop = loop
            self._loop_thread_id = threading.get_ident()
            self._drain_lock = asyncio.Lock()
            return
        if self._loop is not loop:
            raise RuntimeError("RuntimeEventPublisher is already bound to a different event loop")

    def _store_pending(self, published: RuntimePublishedEvent) -> None:
        sequence = published.event.sequence
        if sequence is None:
            raise ValueError("Published events must have a canonical sequence")
        self._pending_by_sequence[sequence] = published

    async def _drain_pending(self) -> None:
        while True:
            published = self._pending_by_sequence.pop(self._next_sequence_to_publish, None)
            if published is None:
                break
            for subscriber in list(self._subscribers):
                await subscriber.on_published_event(published)
            self._next_sequence_to_publish += 1
