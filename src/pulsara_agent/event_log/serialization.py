"""AgentEvent JSON serialization helpers."""

from __future__ import annotations

from typing import Any, Mapping, cast, get_args

from pulsara_agent.event.events import AgentEvent


_EVENT_CLASS_BY_TYPE = {
    str(event_cls.model_fields["type"].default): event_cls
    for event_cls in get_args(AgentEvent)
}


def dump_agent_event(event: AgentEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


def load_agent_event(payload: Mapping[str, Any]) -> AgentEvent:
    event_type = payload.get("type")
    if event_type is None:
        raise ValueError("AgentEvent payload is missing type")

    event_cls = _EVENT_CLASS_BY_TYPE.get(str(event_type))
    if event_cls is None:
        raise ValueError(f"Unknown AgentEvent type: {event_type}")

    return cast(AgentEvent, event_cls.model_validate(payload))
