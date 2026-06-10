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


@dataclass(slots=True)
class _PublishItem:
    published: RuntimePublishedEvent
    delivered: asyncio.Future[None] | None = None
    error: Exception | None = None


class RuntimeEventPublisher:
    def __init__(self, *, runtime_session_id: str) -> None:
        self.runtime_session_id = runtime_session_id
        self._subscribers: list[RuntimeEventSubscriber] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._mailbox: asyncio.Queue[_PublishItem] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._next_sequence_to_publish = 1
        self._pending_by_sequence: dict[int, _PublishItem] = {}
        self.errors: list[Exception] = []

    def subscribe(self, subscriber: RuntimeEventSubscriber) -> None:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: RuntimeEventSubscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    async def publish(self, published: RuntimePublishedEvent) -> None:
        loop = asyncio.get_running_loop()
        self._bind_loop(loop)
        delivered = loop.create_future()
        self._enqueue(_PublishItem(published=published, delivered=delivered))
        await delivered

    def publish_from_thread(self, published: RuntimePublishedEvent) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        if self._loop_thread_id == threading.get_ident():
            self._enqueue(_PublishItem(published=published))
            return
        self._loop.call_soon_threadsafe(self._enqueue, _PublishItem(published=published))

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is None or self._loop.is_closed():
            self._loop = loop
            self._loop_thread_id = threading.get_ident()
            self._mailbox = asyncio.Queue()
            return
        if self._loop is not loop:
            raise RuntimeError("RuntimeEventPublisher is already bound to a different event loop")

    def _enqueue(self, item: _PublishItem) -> None:
        assert self._mailbox is not None
        self._mailbox.put_nowait(item)
        self._ensure_drain_task()

    def _ensure_drain_task(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            task = asyncio.create_task(self._drain_mailbox())
            task.add_done_callback(self._on_drain_task_done)
            self._drain_task = task

    def _on_drain_task_done(self, task: asyncio.Task[None]) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            self.errors.append(exc)
        if self._drain_task is task:
            self._drain_task = None
        if self._mailbox is not None and not self._mailbox.empty():
            self._ensure_drain_task()

    async def _drain_mailbox(self) -> None:
        assert self._mailbox is not None
        while True:
            try:
                item = self._mailbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                self._store_pending(item)
                await self._drain_pending()
            except Exception as exc:
                self.errors.append(exc)
                if item.delivered is not None and not item.delivered.done():
                    item.delivered.set_exception(exc)

    def _store_pending(self, item: _PublishItem) -> None:
        sequence = item.published.event.sequence
        if sequence is None:
            raise ValueError("Published events must have a canonical sequence")
        self._pending_by_sequence[sequence] = item

    async def _drain_pending(self) -> None:
        while True:
            item = self._pending_by_sequence.pop(self._next_sequence_to_publish, None)
            if item is None:
                break
            for subscriber in list(self._subscribers):
                try:
                    await subscriber.on_published_event(item.published)
                except Exception as exc:
                    self.errors.append(exc)
                    if item.error is None:
                        item.error = exc
            if item.delivered is not None and item.error is not None and not item.delivered.done():
                item.delivered.set_exception(item.error)
            if item.delivered is not None and not item.delivered.done():
                item.delivered.set_result(None)
            self._next_sequence_to_publish += 1
