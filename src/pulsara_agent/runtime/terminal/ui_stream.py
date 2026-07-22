"""Bounded UI-only stream for terminal monitor observations.

This channel is deliberately operational. Durable monitor delivery remains
owned by the event log and Host ingress; a slow UI subscriber can only lose
its own replay window.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping

from pulsara_agent.event import (
    AgentEvent,
    TerminalProcessCompletedEvent,
    TerminalProcessMonitorObservationCommittedEvent,
    TerminalProcessMonitorTerminatedEvent,
    TerminalProcessObservationDeliveryDispositionEvent,
)
from pulsara_agent.primitives.terminal_observation import (
    TerminalOutputCursorFact,
    TerminalOutputDeltaSemanticFact,
    TerminalOutputStreamIdentityFact,
)


TerminalMonitorUIEventKind = Literal[
    "journal_delta",
    "monitor_observation_committed",
    "monitor_delivery_disposition",
    "process_completed",
    "monitor_terminated",
]


@dataclass(frozen=True, slots=True)
class TerminalMonitorUIReconnectCursor:
    stream_identity: TerminalOutputStreamIdentityFact
    terminal_cursor: TerminalOutputCursorFact
    notification_projection_revision: int


@dataclass(frozen=True, slots=True)
class TerminalMonitorUIEvent:
    channel: Literal["x.pulsara/terminal_monitor_event"]
    kind: TerminalMonitorUIEventKind
    reconnect_cursor: TerminalMonitorUIReconnectCursor
    process_id: str
    monitor_id: str | None
    durable_event_id: str | None
    replay_gap: bool
    payload: Mapping[str, Any]


_SUBSCRIPTION_END = object()


class TerminalMonitorUISubscription:
    """One bounded, detachable subscriber queue."""

    def __init__(
        self,
        *,
        channel: "TerminalMonitorEventChannel",
        subscription_id: int,
        queue: asyncio.Queue[TerminalMonitorUIEvent | object],
    ) -> None:
        self._channel = channel
        self._subscription_id = subscription_id
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> "TerminalMonitorUISubscription":
        return self

    async def __anext__(self) -> TerminalMonitorUIEvent:
        item = await self._queue.get()
        if item is _SUBSCRIPTION_END:
            raise StopAsyncIteration
        assert isinstance(item, TerminalMonitorUIEvent)
        return item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._channel.detach(self._subscription_id)


@dataclass(slots=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[TerminalMonitorUIEvent | object]


class TerminalMonitorEventChannel:
    """Fan out retained journal/durable events without owning model delivery."""

    def __init__(
        self,
        *,
        projection_revision: Callable[[], int],
        event_resolver: Callable[[str], AgentEvent | None],
        maximum_replay_events: int = 512,
        maximum_subscriber_queue: int = 64,
    ) -> None:
        if maximum_replay_events < 1 or maximum_subscriber_queue < 1:
            raise ValueError("terminal monitor UI bounds must be positive")
        self._projection_revision = projection_revision
        self._event_resolver = event_resolver
        self._maximum_subscriber_queue = maximum_subscriber_queue
        self._lock = RLock()
        self._replay: deque[TerminalMonitorUIEvent] = deque(
            maxlen=maximum_replay_events
        )
        self._subscribers: dict[int, _Subscriber] = {}
        self._next_subscription_id = 1
        self._journal_cursors: dict[str, TerminalOutputCursorFact] = {}
        self._journal_monitor_ids: dict[str, str] = {}
        self._evicted_through: dict[str, TerminalMonitorUIEvent] = {}
        self._closed = False

    def subscribe(
        self,
        *,
        reconnect_cursor: TerminalMonitorUIReconnectCursor | None = None,
        after_projection_revision: int | None = None,
    ) -> TerminalMonitorUISubscription:
        if reconnect_cursor is not None and after_projection_revision is not None:
            raise ValueError(
                "terminal monitor UI reconnect cursor and legacy revision are mutually exclusive"
            )
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[TerminalMonitorUIEvent | object] = asyncio.Queue(
            maxsize=self._maximum_subscriber_queue
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("terminal monitor UI channel is closed")
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            subscriber = _Subscriber(loop=loop, queue=queue)
            self._subscribers[subscription_id] = subscriber
            retained = tuple(self._replay)
            if reconnect_cursor is not None:
                same_stream = tuple(
                    item
                    for item in retained
                    if item.reconnect_cursor.stream_identity
                    == reconnect_cursor.stream_identity
                )
                replay = tuple(
                    item
                    for item in same_stream
                    if _is_after_reconnect(item.reconnect_cursor, reconnect_cursor)
                )
                evicted = self._evicted_through.get(
                    reconnect_cursor.stream_identity.stream_identity_fingerprint
                )
                replay_gap = (
                    _replay_gap_event(same_stream[0] if same_stream else evicted)
                    if evicted is not None
                    and _is_after_reconnect(
                        evicted.reconnect_cursor,
                        reconnect_cursor,
                    )
                    else None
                )
            else:
                replay = tuple(
                    item
                    for item in retained
                    if after_projection_revision is None
                    or item.reconnect_cursor.notification_projection_revision
                    > after_projection_revision
                )
                replay_gap = None
        available = self._maximum_subscriber_queue - (
            1 if replay_gap is not None else 0
        )
        if replay_gap is not None:
            self._deliver(subscriber, replay_gap)
        for item in replay[-available:] if available else ():
            self._deliver(subscriber, item)
        return TerminalMonitorUISubscription(
            channel=self,
            subscription_id=subscription_id,
            queue=queue,
        )

    def detach(self, subscription_id: int) -> None:
        with self._lock:
            subscriber = self._subscribers.pop(subscription_id, None)
        if subscriber is not None:
            self._schedule(subscriber, self._finish_subscriber, subscriber)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers = tuple(self._subscribers.values())
            self._subscribers.clear()
        for subscriber in subscribers:
            self._schedule(subscriber, self._finish_subscriber, subscriber)

    def bind_journal(
        self,
        *,
        monitor_id: str,
        baseline_cursor: TerminalOutputCursorFact,
    ) -> None:
        key = baseline_cursor.stream_identity.stream_identity_fingerprint
        with self._lock:
            self._journal_cursors.setdefault(key, baseline_cursor)
            self._journal_monitor_ids[key] = monitor_id

    def publish_journal(self, journal: Any, *, maximum_chars: int = 8_000) -> None:
        stream = journal.stream_identity
        key = stream.stream_identity_fingerprint
        with self._lock:
            start = self._journal_cursors.get(key)
            monitor_id = self._journal_monitor_ids.get(key)
        if start is None or monitor_id is None or start == journal.end_cursor:
            return
        delta, _attribution = journal.snapshot_since(
            start,
            max_chars=maximum_chars,
            seal=False,
        )
        end = delta.end_cursor
        with self._lock:
            current = self._journal_cursors.get(key)
            if current != start:
                return
            self._journal_cursors[key] = end
        self._publish(
            self._event(
                kind="journal_delta",
                cursor=end,
                monitor_id=monitor_id,
                durable_event_id=None,
                payload={
                    "output": delta.output_preview,
                    "truncated": delta.truncated,
                    "requested_start_cursor_fingerprint": (
                        delta.requested_start_cursor.cursor_fingerprint
                    ),
                    "available_start_cursor_fingerprint": (
                        delta.available_start_cursor.cursor_fingerprint
                    ),
                },
            )
        )

    def publish_committed(self, events: tuple[AgentEvent, ...]) -> None:
        for event in events:
            if isinstance(event, TerminalProcessCompletedEvent):
                self._publish(
                    self._event(
                        kind="process_completed",
                        cursor=event.completion_semantic.terminal_output_cursor,
                        monitor_id=None,
                        durable_event_id=event.id,
                        payload={
                            "outcome_fingerprint": (
                                event.completion_semantic.outcome.outcome_fingerprint
                            ),
                            "output_preview": event.output_preview,
                            "output_truncated": event.output_truncated,
                        },
                        projection_revision=event.sequence,
                    )
                )
            elif isinstance(event, TerminalProcessMonitorObservationCommittedEvent):
                output = event.observation.output_authority
                cursor = _output_cursor(output)
                payload = {
                    "observation_kind": event.observation.observation_kind,
                    "observation_ordinal": event.observation.observation_ordinal,
                    "observation_semantic_fingerprint": (
                        event.observation.observation_semantic_fingerprint
                    ),
                }
                if isinstance(output, TerminalOutputDeltaSemanticFact):
                    payload["output_preview"] = output.output_preview
                    payload["output_truncated"] = output.truncated
                else:
                    payload["output_preview"] = ""
                    payload["output_truncated"] = True
                    payload["recovery_reason"] = output.recovery_reason
                self._publish(
                    self._event(
                        kind="monitor_observation_committed",
                        cursor=cursor,
                        monitor_id=event.observation.monitor_id,
                        durable_event_id=event.id,
                        payload=payload,
                        projection_revision=event.sequence,
                    )
                )
            elif isinstance(event, TerminalProcessObservationDeliveryDispositionEvent):
                for reference in event.observation_source_references:
                    source = self._event_resolver(reference.event_id)
                    cursor, monitor_id = _source_cursor_and_monitor(source)
                    if cursor is None:
                        continue
                    self._publish(
                        self._event(
                            kind="monitor_delivery_disposition",
                            cursor=cursor,
                            monitor_id=monitor_id,
                            durable_event_id=event.id,
                            payload={
                                "outcome": event.outcome,
                                "source_event_id": reference.event_id,
                            },
                            projection_revision=event.sequence,
                        )
                    )
            elif isinstance(event, TerminalProcessMonitorTerminatedEvent):
                self._publish(
                    self._event(
                        kind="monitor_terminated",
                        cursor=event.termination_semantic.terminal_cursor,
                        monitor_id=event.termination_semantic.monitor_id,
                        durable_event_id=event.id,
                        payload={
                            "terminal_reason": (
                                event.termination_semantic.terminal_reason
                            )
                        },
                        projection_revision=event.sequence,
                    )
                )

    def _event(
        self,
        *,
        kind: TerminalMonitorUIEventKind,
        cursor: TerminalOutputCursorFact,
        monitor_id: str | None,
        durable_event_id: str | None,
        payload: Mapping[str, Any],
        projection_revision: int | None = None,
    ) -> TerminalMonitorUIEvent:
        return TerminalMonitorUIEvent(
            channel="x.pulsara/terminal_monitor_event",
            kind=kind,
            reconnect_cursor=TerminalMonitorUIReconnectCursor(
                stream_identity=cursor.stream_identity,
                terminal_cursor=cursor,
                notification_projection_revision=(
                    self._projection_revision()
                    if projection_revision is None
                    else projection_revision
                ),
            ),
            process_id=cursor.stream_identity.process_id,
            monitor_id=monitor_id,
            durable_event_id=durable_event_id,
            replay_gap=False,
            payload=MappingProxyType(dict(payload)),
        )

    def _publish(self, event: TerminalMonitorUIEvent) -> None:
        with self._lock:
            if self._closed:
                return
            if len(self._replay) == self._replay.maxlen:
                evicted = self._replay[0]
                key = (
                    evicted.reconnect_cursor.stream_identity.stream_identity_fingerprint
                )
                self._evicted_through[key] = evicted
            self._replay.append(event)
            subscribers = tuple(self._subscribers.values())
        stale: list[_Subscriber] = []
        for subscriber in subscribers:
            if not self._schedule(subscriber, self._deliver, subscriber, event):
                stale.append(subscriber)
        if stale:
            stale_ids = {id(item) for item in stale}
            with self._lock:
                self._subscribers = {
                    key: item
                    for key, item in self._subscribers.items()
                    if id(item) not in stale_ids
                }

    @staticmethod
    def _schedule(
        subscriber: _Subscriber,
        callback: Callable[..., None],
        *args: object,
    ) -> bool:
        try:
            subscriber.loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            return False
        return True

    @staticmethod
    def _deliver(subscriber: _Subscriber, event: TerminalMonitorUIEvent) -> None:
        if subscriber.queue.full():
            while True:
                try:
                    subscriber.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            event = TerminalMonitorUIEvent(
                channel=event.channel,
                kind=event.kind,
                reconnect_cursor=event.reconnect_cursor,
                process_id=event.process_id,
                monitor_id=event.monitor_id,
                durable_event_id=event.durable_event_id,
                replay_gap=True,
                payload=MappingProxyType(
                    {
                        "gap_reason": "subscriber_backpressure",
                        "latest_kind": event.kind,
                    }
                ),
            )
        subscriber.queue.put_nowait(event)

    @staticmethod
    def _finish_subscriber(subscriber: _Subscriber) -> None:
        while subscriber.queue.full():
            try:
                subscriber.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        subscriber.queue.put_nowait(_SUBSCRIPTION_END)


def _source_cursor_and_monitor(
    source: AgentEvent | None,
) -> tuple[TerminalOutputCursorFact | None, str | None]:
    if isinstance(source, TerminalProcessCompletedEvent):
        return source.completion_semantic.terminal_output_cursor, None
    if isinstance(source, TerminalProcessMonitorObservationCommittedEvent):
        output = source.observation.output_authority
        return (
            _output_cursor(output),
            source.observation.monitor_id,
        )
    return None, None


def _output_cursor(output: Any) -> TerminalOutputCursorFact:
    if isinstance(output, TerminalOutputDeltaSemanticFact):
        return output.end_cursor
    return output.terminal_cursor


def _is_after_reconnect(
    candidate: TerminalMonitorUIReconnectCursor,
    previous: TerminalMonitorUIReconnectCursor,
) -> bool:
    if candidate.stream_identity != previous.stream_identity:
        return False
    if (
        candidate.notification_projection_revision
        != previous.notification_projection_revision
    ):
        return (
            candidate.notification_projection_revision
            > previous.notification_projection_revision
        )
    candidate_cursor = candidate.terminal_cursor
    previous_cursor = previous.terminal_cursor
    return (
        candidate_cursor.sanitized_char_offset,
        candidate_cursor.sanitized_utf8_byte_offset,
    ) > (
        previous_cursor.sanitized_char_offset,
        previous_cursor.sanitized_utf8_byte_offset,
    )


def _replay_gap_event(first_retained: TerminalMonitorUIEvent) -> TerminalMonitorUIEvent:
    return TerminalMonitorUIEvent(
        channel=first_retained.channel,
        kind=first_retained.kind,
        reconnect_cursor=first_retained.reconnect_cursor,
        process_id=first_retained.process_id,
        monitor_id=first_retained.monitor_id,
        durable_event_id=first_retained.durable_event_id,
        replay_gap=True,
        payload=MappingProxyType(
            {
                "gap_reason": "retained_replay_window_exceeded",
                "first_retained_kind": first_retained.kind,
            }
        ),
    )


__all__ = [
    "TerminalMonitorEventChannel",
    "TerminalMonitorUIEvent",
    "TerminalMonitorUIReconnectCursor",
    "TerminalMonitorUISubscription",
]
