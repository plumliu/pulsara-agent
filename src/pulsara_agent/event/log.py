"""Append-only in-memory event log."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Iterable

from pulsara_agent.event.events import AgentEvent, ReplyStartEvent
from pulsara_agent.message.message import AssistantMsg, Msg
from pulsara_agent.message.reducer import MessageReducer


@dataclass(slots=True)
class InMemoryEventLog:
    _events: list[AgentEvent] = field(default_factory=list)
    _next_sequence: int = 1
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def append(self, event: AgentEvent) -> AgentEvent:
        with self._lock:
            stored = event
            if event.sequence is None:
                stored = event.model_copy(update={"sequence": self._next_sequence})
            self._next_sequence = max(self._next_sequence, (stored.sequence or 0) + 1)
            self._events.append(stored)
            return stored

    def extend(self, events: Iterable[AgentEvent]) -> list[AgentEvent]:
        return [self.append(event) for event in events]

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
    ) -> list[AgentEvent]:
        with self._lock:
            events = list(self._events)
        if run_id is not None:
            events = [event for event in events if event.run_id == run_id]
        if turn_id is not None:
            events = [event for event in events if event.turn_id == turn_id]
        if reply_id is not None:
            events = [event for event in events if event.reply_id == reply_id]
        return list(events)

    def replay(self, reply_id: str) -> Msg:
        events = self.iter(reply_id=reply_id)
        start = next((event for event in events if isinstance(event, ReplyStartEvent)), None)
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
