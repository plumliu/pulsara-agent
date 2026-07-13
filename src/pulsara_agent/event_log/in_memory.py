"""Append-only in-memory EventLog implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Iterable

from pulsara_agent.event.events import AgentEvent, ReplyStartEvent
from pulsara_agent.event_log.protocol import (
    EventBatchConfirmation,
    EventIdConflict,
    EventLogReadSnapshot,
    EventLogWriteConflict,
    same_event_payload,
)
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.message.message import AssistantMsg, Msg
from pulsara_agent.message.reducer import MessageReducer


@dataclass(slots=True)
class InMemoryEventLog:
    _events: list[AgentEvent] = field(default_factory=list)
    _next_sequence: int = 1
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def ensure_runtime_session_owner(self) -> None:
        """In-memory ledgers have no external ownership foreign key."""

    def append(
        self,
        event: AgentEvent,
        *,
        expected_last_sequence: int | None = None,
    ) -> AgentEvent:
        _validate_live_batch([event])
        with self._lock:
            existing = next(
                (stored for stored in self._events if stored.id == event.id), None
            )
            if existing is not None:
                if same_event_payload(event, existing):
                    return _owned_event(existing)
                raise EventIdConflict(event.id)
            actual_last_sequence = self._next_sequence - 1
            if (
                expected_last_sequence is not None
                and expected_last_sequence != actual_last_sequence
            ):
                raise EventLogWriteConflict(
                    expected_last_sequence=expected_last_sequence,
                    actual_last_sequence=actual_last_sequence,
                )
            stored = _owned_event(
                event.model_copy(update={"sequence": self._next_sequence})
            )
            self._events.append(stored)
            self._next_sequence += 1
            return _owned_event(stored)

    def extend(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
    ) -> list[AgentEvent]:
        event_list = list(events)
        if not event_list:
            return []
        _validate_live_batch(event_list)
        with self._lock:
            actual_last_sequence = self._next_sequence - 1
            if (
                expected_last_sequence is not None
                and expected_last_sequence != actual_last_sequence
            ):
                raise EventLogWriteConflict(
                    expected_last_sequence=expected_last_sequence,
                    actual_last_sequence=actual_last_sequence,
                )
            existing_ids = {event.id for event in self._events}
            duplicate = next(
                (event.id for event in event_list if event.id in existing_ids), None
            )
            if duplicate is not None:
                raise ValueError(
                    f"Event id already exists in this session: {duplicate}"
                )
            stored_events = [
                _owned_event(
                    event.model_copy(update={"sequence": self._next_sequence + index})
                )
                for index, event in enumerate(event_list)
            ]
            self._events.extend(stored_events)
            self._next_sequence += len(stored_events)
            return [_owned_event(event) for event in stored_events]

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
        after_sequence: int | None = None,
    ) -> list[AgentEvent]:
        with self._lock:
            events = list(self._events)
        if after_sequence is not None:
            events = [
                event for event in events if (event.sequence or 0) > after_sequence
            ]
        if run_id is not None:
            events = [event for event in events if event.run_id == run_id]
        if turn_id is not None:
            events = [event for event in events if event.turn_id == turn_id]
        if reply_id is not None:
            events = [event for event in events if event.reply_id == reply_id]
        return [_owned_event(event) for event in events]

    def get_by_id(self, event_id: str) -> AgentEvent | None:
        with self._lock:
            event = next(
                (event for event in self._events if event.id == event_id), None
            )
            return _owned_event(event) if event is not None else None

    def confirm_batch(self, candidates) -> EventBatchConfirmation:
        candidate_list = list(candidates)
        ids = [event.id for event in candidate_list]
        if len(ids) != len(set(ids)):
            raise ValueError("Confirmed event ids must be unique within one batch")
        with self._lock:
            by_id = {event.id: event for event in self._events}
            committed: list[AgentEvent] = []
            missing: list[str] = []
            for candidate in candidate_list:
                existing = by_id.get(candidate.id)
                if existing is None:
                    missing.append(candidate.id)
                    continue
                if not same_event_payload(candidate, existing):
                    raise EventIdConflict(candidate.id)
                committed.append(_owned_event(existing))
            return EventBatchConfirmation(
                committed_events=tuple(committed),
                missing_event_ids=tuple(missing),
                actual_last_sequence=self._next_sequence - 1,
            )

    def read_range_snapshot(
        self,
        *,
        minimum_sequence: int,
        through_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> EventLogReadSnapshot:
        del deadline_monotonic
        if minimum_sequence < 1:
            raise ValueError("minimum sequence must be positive")
        with self._lock:
            current_high_water = self._next_sequence - 1
            effective_through = (
                current_high_water if through_sequence is None else through_sequence
            )
            if effective_through > current_high_water:
                raise ValueError("requested event high-water has not been committed")
            if effective_through < minimum_sequence:
                raise ValueError("event read range is empty or reversed")
            events = tuple(
                _owned_event(event)
                for event in self._events
                if minimum_sequence <= int(event.sequence or 0) <= effective_through
            )
        return EventLogReadSnapshot(
            through_sequence=effective_through,
            events=events,
        )

    def replay(self, reply_id: str) -> Msg:
        events = self.iter(reply_id=reply_id)
        start = next(
            (event for event in events if isinstance(event, ReplyStartEvent)), None
        )
        message = AssistantMsg(
            id=reply_id,
            name=start.name if start else "assistant",
            content=[],
            created_at=start.created_at if start else None,
        )
        reducer = MessageReducer(message)
        for event in events:
            reducer.append(event)
        return reducer.message

    def next_sequence(self) -> int:
        with self._lock:
            return self._next_sequence


def _validate_live_batch(events: list[AgentEvent]) -> None:
    if any(event.sequence is not None for event in events):
        raise ValueError("Live EventLog append requires sequence=None")
    ids = [event.id for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError("Event ids must be unique within one batch")


def _owned_event(event: AgentEvent) -> AgentEvent:
    return load_agent_event(dump_agent_event(event))
