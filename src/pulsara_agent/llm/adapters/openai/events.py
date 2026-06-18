"""Shared OpenAI event translation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    RunErrorEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.usage import Usage


@dataclass(slots=True)
class AgentEventBuilder:
    model: ModelProfile
    event_context: EventContext
    text_block_id: str | None = None
    thinking_block_id: str | None = None
    active_tool_call_ids: set[str] = field(default_factory=set)
    item_id_to_tool_call_id: dict[str, str] = field(default_factory=dict)
    tool_call_has_arguments: set[str] = field(default_factory=set)

    def event_fields(self) -> dict[str, str]:
        return self.event_context.event_fields()

    def model_start(self) -> ModelCallStartEvent:
        return ModelCallStartEvent(
            **self.event_fields(),
            model_name=self.model.id,
            model_role=self.model.role.value,
            provider=self.model.provider,
        )

    def model_end(self, usage: Usage | None = None) -> ModelCallEndEvent:
        usage = usage or Usage()
        return ModelCallEndEvent(
            **self.event_fields(),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        )

    def run_error(
        self,
        *,
        message: str,
        code: str,
        provider_data: dict[str, Any] | None = None,
    ) -> RunErrorEvent:
        return RunErrorEvent(
            **self.event_fields(),
            message=message,
            code=code,
            metadata={"provider_data": provider_data or {}},
        )

    def text_delta(self, delta: str) -> list[AgentEvent]:
        if not delta:
            return []
        events: list[AgentEvent] = []
        if self.text_block_id is None:
            self.text_block_id = f"text:{uuid4()}"
            events.append(TextBlockStartEvent(**self.event_fields(), block_id=self.text_block_id))
        events.append(TextBlockDeltaEvent(**self.event_fields(), block_id=self.text_block_id, delta=delta))
        return events

    def thinking_delta(self, delta: str) -> list[AgentEvent]:
        if not delta:
            return []
        events: list[AgentEvent] = []
        if self.thinking_block_id is None:
            self.thinking_block_id = f"thinking:{uuid4()}"
            events.append(
                ThinkingBlockStartEvent(**self.event_fields(), block_id=self.thinking_block_id)
            )
        events.append(
            ThinkingBlockDeltaEvent(
                **self.event_fields(),
                block_id=self.thinking_block_id,
                delta=delta,
            )
        )
        return events

    def tool_call_start(
        self,
        *,
        tool_call_id: str,
        tool_call_name: str,
        provider_item_id: str | None = None,
    ) -> list[AgentEvent]:
        if provider_item_id:
            self.item_id_to_tool_call_id[provider_item_id] = tool_call_id
        if tool_call_id in self.active_tool_call_ids:
            return []
        self.active_tool_call_ids.add(tool_call_id)
        return [
            ToolCallStartEvent(
                **self.event_fields(),
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
            )
        ]

    def tool_call_delta(self, *, tool_call_id: str, delta: str) -> list[AgentEvent]:
        if not tool_call_id or not delta:
            return []
        events: list[AgentEvent] = []
        if tool_call_id not in self.active_tool_call_ids:
            events.extend(self.tool_call_start(tool_call_id=tool_call_id, tool_call_name=""))
        self.tool_call_has_arguments.add(tool_call_id)
        events.append(ToolCallDeltaEvent(**self.event_fields(), tool_call_id=tool_call_id, delta=delta))
        return events

    def tool_call_end(self, *, tool_call_id: str) -> list[AgentEvent]:
        if not tool_call_id or tool_call_id not in self.active_tool_call_ids:
            return []
        self.active_tool_call_ids.remove(tool_call_id)
        return [ToolCallEndEvent(**self.event_fields(), tool_call_id=tool_call_id)]

    def tool_call(self, *, tool_call_id: str, tool_call_name: str, arguments: str) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        events.extend(self.tool_call_start(tool_call_id=tool_call_id, tool_call_name=tool_call_name))
        if arguments:
            events.extend(self.tool_call_delta(tool_call_id=tool_call_id, delta=arguments))
        events.extend(self.tool_call_end(tool_call_id=tool_call_id))
        return events

    def resolve_tool_call_id(self, item_id_or_call_id: str) -> str:
        return self.item_id_to_tool_call_id.get(item_id_or_call_id, item_id_or_call_id)

    def has_arguments(self, tool_call_id: str) -> bool:
        return tool_call_id in self.tool_call_has_arguments

    def close_active_blocks(self) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        if self.text_block_id is not None:
            events.append(TextBlockEndEvent(**self.event_fields(), block_id=self.text_block_id))
            self.text_block_id = None
        if self.thinking_block_id is not None:
            events.append(ThinkingBlockEndEvent(**self.event_fields(), block_id=self.thinking_block_id))
            self.thinking_block_id = None
        for tool_call_id in list(self.active_tool_call_ids):
            events.append(ToolCallEndEvent(**self.event_fields(), tool_call_id=tool_call_id))
            self.active_tool_call_ids.remove(tool_call_id)
        return events


def sdk_event_to_dict(raw_event: Any) -> dict[str, Any]:
    """Normalize SDK model objects and test dictionaries into plain dicts."""

    if isinstance(raw_event, dict):
        return raw_event
    model_dump = getattr(raw_event, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    if hasattr(raw_event, "__dict__"):
        return {
            key: value
            for key, value in vars(raw_event).items()
            if not key.startswith("_")
        }
    return {"value": raw_event}


def arguments_to_json_string(raw_arguments: Any) -> str:
    if isinstance(raw_arguments, str):
        return raw_arguments
    if isinstance(raw_arguments, dict):
        return json.dumps(raw_arguments)
    return "{}"


def usage_from_mapping(raw_usage: Any) -> Usage:
    usage = sdk_event_to_dict(raw_usage) if raw_usage is not None else {}
    if not usage:
        return Usage()
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
    total_tokens = usage.get("total_tokens", 0) or 0
    return Usage(
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        total_tokens=int(total_tokens),
    )


def event_includes_model_end(events: list[AgentEvent]) -> bool:
    return any(isinstance(event, ModelCallEndEvent) for event in events)


def event_includes_run_error(events: list[AgentEvent]) -> bool:
    return any(isinstance(event, RunErrorEvent) for event in events)
