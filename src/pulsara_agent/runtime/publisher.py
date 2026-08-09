"""Ordered in-process event publishing for one RuntimeSession."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pulsara_agent.event import AgentEvent


@dataclass(frozen=True, slots=True)
class RuntimePublishedEvent:
    runtime_session_id: str
    event: AgentEvent


class RuntimeEventSubscriber(Protocol):
    async def on_published_event(self, published: RuntimePublishedEvent) -> None: ...


class RuntimeEventSubscriberSemantics(StrEnum):
    SEMANTIC_REQUIRED = "semantic_required"
    DERIVED_BEST_EFFORT = "derived_best_effort"


@dataclass(frozen=True, slots=True)
class _SubscriberRegistration:
    subscriber: RuntimeEventSubscriber
    semantics: RuntimeEventSubscriberSemantics


@dataclass(slots=True)
class _PublishItem:
    published: RuntimePublishedEvent
    delivered: Future[None] | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class PublisherEnqueueResult:
    status: str
    enqueued_through_sequence: int | None
    delivery_futures: tuple[Future[None], ...] = ()


class RuntimeEventPublisher:
    def __init__(
        self, *, runtime_session_id: str, next_sequence_to_publish: int = 1
    ) -> None:
        if next_sequence_to_publish < 1:
            raise ValueError("next_sequence_to_publish must be >= 1")
        self.runtime_session_id = runtime_session_id
        self._subscribers: list[_SubscriberRegistration] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._mailbox: asyncio.Queue[_PublishItem] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._next_sequence_to_publish = next_sequence_to_publish
        self._pending_by_sequence: dict[int, _PublishItem] = {}
        self._enqueue_lock = threading.RLock()
        self._enqueued_through_sequence = next_sequence_to_publish - 1
        self.errors: deque[Exception] = deque(maxlen=128)

    @property
    def enqueued_through_sequence(self) -> int:
        with self._enqueue_lock:
            return self._enqueued_through_sequence

    def bind_running_loop(self) -> None:
        self._bind_loop(asyncio.get_running_loop())

    def enqueue_committed_batch(
        self,
        events: tuple[RuntimePublishedEvent, ...],
        *,
        await_delivery: bool,
    ) -> PublisherEnqueueResult:
        """Non-awaiting ordered enqueue used inside SessionWriteCoordinator."""

        if not events:
            return PublisherEnqueueResult(
                status="completed" if await_delivery else "enqueued",
                enqueued_through_sequence=self.enqueued_through_sequence,
            )
        sequences = [_sequence(item) for item in events]
        if sequences != list(range(sequences[0], sequences[-1] + 1)):
            raise ValueError("Publisher batch must have contiguous sequences")
        with self._enqueue_lock:
            expected = self._enqueued_through_sequence + 1
            if sequences[0] != expected:
                raise ValueError(
                    f"Publisher enqueue gap: expected {expected}, received {sequences[0]}"
                )
            loop = self._loop
            if loop is None or loop.is_closed():
                self._enqueued_through_sequence = sequences[-1]
                self._next_sequence_to_publish = max(
                    self._next_sequence_to_publish,
                    sequences[-1] + 1,
                )
                return PublisherEnqueueResult(
                    status="unavailable",
                    enqueued_through_sequence=sequences[-1],
                )
            current_thread = threading.get_ident() == self._loop_thread_id
            futures: list[Future[None]] = []
            items: list[_PublishItem] = []
            for published in events:
                delivered = Future() if await_delivery else None
                if delivered is not None:
                    futures.append(delivered)
                items.append(_PublishItem(published=published, delivered=delivered))
            if current_thread:
                for item in items:
                    self._enqueue(item)
            else:
                for item in items:
                    loop.call_soon_threadsafe(self._enqueue, item)
            self._enqueued_through_sequence = sequences[-1]
            return PublisherEnqueueResult(
                status="enqueued",
                enqueued_through_sequence=sequences[-1],
                delivery_futures=tuple(futures),
            )

    def subscribe(
        self,
        subscriber: RuntimeEventSubscriber,
        *,
        semantics: RuntimeEventSubscriberSemantics = (
            RuntimeEventSubscriberSemantics.SEMANTIC_REQUIRED
        ),
    ) -> None:
        existing = next(
            (item for item in self._subscribers if item.subscriber is subscriber),
            None,
        )
        if existing is not None:
            if existing.semantics is not semantics:
                raise ValueError(
                    "subscriber semantics cannot change after registration"
                )
            return
        self._subscribers.append(
            _SubscriberRegistration(subscriber=subscriber, semantics=semantics)
        )

    def unsubscribe(self, subscriber: RuntimeEventSubscriber) -> None:
        self._subscribers = [
            item for item in self._subscribers if item.subscriber is not subscriber
        ]

    async def publish(self, published: RuntimePublishedEvent) -> None:
        loop = asyncio.get_running_loop()
        self._bind_loop(loop)
        result = self.enqueue_committed_batch((published,), await_delivery=True)
        if not result.delivery_futures:
            raise RuntimeError("RuntimeEventPublisher loop is unavailable")
        await asyncio.wrap_future(result.delivery_futures[0])

    def publish_from_thread(self, published: RuntimePublishedEvent) -> bool:
        result = self.enqueue_committed_batch((published,), await_delivery=False)
        return result.status != "unavailable"

    def discard_unpublished(self, published: RuntimePublishedEvent) -> None:
        sequence = published.event.sequence
        if sequence is None:
            raise ValueError("Discarded events must have a canonical sequence")
        if (
            self._loop is not None
            and not self._loop.is_closed()
            and self._loop_thread_id != threading.get_ident()
        ):
            self._loop.call_soon_threadsafe(self._discard_unpublished_in_loop, sequence)
            return
        self._discard_unpublished_in_loop(sequence)

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is None or self._loop.is_closed():
            self._loop = loop
            self._loop_thread_id = threading.get_ident()
            self._mailbox = asyncio.Queue()
            return
        if self._loop is not loop:
            raise RuntimeError(
                "RuntimeEventPublisher is already bound to a different event loop"
            )

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
            await self._drain_pending()
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

    def _discard_unpublished_in_loop(self, sequence: int) -> None:
        if sequence >= self._next_sequence_to_publish:
            self._next_sequence_to_publish = sequence + 1
        if self._pending_by_sequence:
            self._ensure_drain_task()

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
            for registration in list(self._subscribers):
                try:
                    await registration.subscriber.on_published_event(item.published)
                except Exception as exc:
                    self.errors.append(exc)
                    if (
                        registration.semantics
                        is RuntimeEventSubscriberSemantics.DERIVED_BEST_EFFORT
                    ):
                        self.unsubscribe(registration.subscriber)
                    elif item.error is None:
                        item.error = exc
            if (
                item.delivered is not None
                and item.error is not None
                and not item.delivered.done()
            ):
                item.delivered.set_exception(item.error)
            if item.delivered is not None and not item.delivered.done():
                item.delivered.set_result(None)
            self._next_sequence_to_publish += 1


def _sequence(published: RuntimePublishedEvent) -> int:
    sequence = published.event.sequence
    if sequence is None:
        raise ValueError("Published events must have a canonical sequence")
    return sequence
