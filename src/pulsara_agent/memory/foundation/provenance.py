"""Runtime provenance value objects for memory persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pulsara_agent.event import AgentEvent, ToolResultStartEvent
from pulsara_agent.jsonld import NodeRef, Term, jsonld_value
from pulsara_agent.ontology import runtime as rt


@dataclass(frozen=True, slots=True)
class RuntimeEventSpan:
    session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    start_sequence: int
    end_sequence: int
    source_event_id: str | None = None

    def to_jsonld(self) -> dict[str | Term, Any]:
        payload: dict[str | Term, Any] = {
            rt.SOURCE_SESSION: self.session_id,
            rt.SOURCE_RUN: self.run_id,
            rt.SOURCE_TURN: self.turn_id,
            rt.SOURCE_REPLY: self.reply_id,
            rt.START_SEQUENCE: self.start_sequence,
            rt.END_SEQUENCE: self.end_sequence,
        }
        if self.source_event_id is not None:
            payload[rt.SOURCE_EVENT] = NodeRef(runtime_event_iri(self.source_event_id))
        return jsonld_value(payload)


@dataclass(frozen=True, slots=True)
class RuntimeEventRef:
    session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_call_id: str
    source_event_id: str | None = None


def runtime_event_iri(event_id: str) -> str:
    if event_id.startswith("event:"):
        return event_id
    return f"event:{event_id}"


def runtime_event_span_from_events(
    events: list[AgentEvent],
    tool_call_id: str,
    *,
    session_id: str,
) -> RuntimeEventSpan:
    matching = [
        event
        for event in events
        if getattr(event, "tool_call_id", None) == tool_call_id and event.sequence is not None
    ]
    if not matching:
        raise KeyError(f"No sequenced events found for tool_call_id: {tool_call_id}")
    start = next((event for event in matching if isinstance(event, ToolResultStartEvent)), matching[0])
    return RuntimeEventSpan(
        session_id=session_id,
        run_id=start.run_id,
        turn_id=start.turn_id,
        reply_id=start.reply_id,
        start_sequence=min(event.sequence or 0 for event in matching),
        end_sequence=max(event.sequence or 0 for event in matching),
        source_event_id=start.id,
    )
