"""Shared OpenAI event translation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
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
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.provider import ModelIdentityPolicy
from pulsara_agent.primitives.model_call import (
    ModelCallDiagnosticFact,
    ModelTokenUsageFact,
)


@dataclass(slots=True)
class AgentEventBuilder:
    event_context: EventContext
    text_block_id: str | None = None
    thinking_block_id: str | None = None
    active_tool_call_ids: set[str] = field(default_factory=set)
    item_id_to_tool_call_id: dict[str, str] = field(default_factory=dict)
    tool_call_has_arguments: set[str] = field(default_factory=set)
    has_semantic_output: bool = False

    def event_fields(self) -> dict[str, str]:
        return self.event_context.event_fields()

    def run_error(
        self,
        *,
        message: str,
        code: str,
        provider_data: dict[str, Any] | None = None,
    ) -> RunErrorEvent:
        self.has_semantic_output = True
        return RunErrorEvent(
            **self.event_fields(),
            message=message,
            code=code,
            metadata={"provider_data": provider_data or {}},
        )

    def text_delta(self, delta: str) -> list[AgentEvent]:
        if not delta:
            return []
        self.has_semantic_output = True
        events: list[AgentEvent] = []
        if self.text_block_id is None:
            self.text_block_id = f"text:{uuid4()}"
            events.append(
                TextBlockStartEvent(**self.event_fields(), block_id=self.text_block_id)
            )
        events.append(
            TextBlockDeltaEvent(
                **self.event_fields(), block_id=self.text_block_id, delta=delta
            )
        )
        return events

    def thinking_delta(self, delta: str) -> list[AgentEvent]:
        if not delta:
            return []
        self.has_semantic_output = True
        events: list[AgentEvent] = []
        if self.thinking_block_id is None:
            self.thinking_block_id = f"thinking:{uuid4()}"
            events.append(
                ThinkingBlockStartEvent(
                    **self.event_fields(), block_id=self.thinking_block_id
                )
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
        self.has_semantic_output = True
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
        self.has_semantic_output = True
        events: list[AgentEvent] = []
        if tool_call_id not in self.active_tool_call_ids:
            events.extend(
                self.tool_call_start(tool_call_id=tool_call_id, tool_call_name="")
            )
        self.tool_call_has_arguments.add(tool_call_id)
        events.append(
            ToolCallDeltaEvent(
                **self.event_fields(), tool_call_id=tool_call_id, delta=delta
            )
        )
        return events

    def tool_call_end(self, *, tool_call_id: str) -> list[AgentEvent]:
        if not tool_call_id or tool_call_id not in self.active_tool_call_ids:
            return []
        self.has_semantic_output = True
        self.active_tool_call_ids.remove(tool_call_id)
        return [ToolCallEndEvent(**self.event_fields(), tool_call_id=tool_call_id)]

    def tool_call(
        self, *, tool_call_id: str, tool_call_name: str, arguments: str
    ) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        events.extend(
            self.tool_call_start(
                tool_call_id=tool_call_id, tool_call_name=tool_call_name
            )
        )
        if arguments:
            events.extend(
                self.tool_call_delta(tool_call_id=tool_call_id, delta=arguments)
            )
        events.extend(self.tool_call_end(tool_call_id=tool_call_id))
        return events

    def resolve_tool_call_id(self, item_id_or_call_id: str) -> str:
        return self.item_id_to_tool_call_id.get(item_id_or_call_id, item_id_or_call_id)

    def has_arguments(self, tool_call_id: str) -> bool:
        return tool_call_id in self.tool_call_has_arguments

    def close_active_blocks(self) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        if self.text_block_id is not None:
            events.append(
                TextBlockEndEvent(**self.event_fields(), block_id=self.text_block_id)
            )
            self.text_block_id = None
        if self.thinking_block_id is not None:
            events.append(
                ThinkingBlockEndEvent(
                    **self.event_fields(), block_id=self.thinking_block_id
                )
            )
            self.thinking_block_id = None
        for tool_call_id in list(self.active_tool_call_ids):
            events.append(
                ToolCallEndEvent(**self.event_fields(), tool_call_id=tool_call_id)
            )
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


@dataclass(slots=True)
class ReportedModelIdentityObserver:
    """Observe one provider attempt without confusing aliases with fallback."""

    requested_model_id: str
    policy: ModelIdentityPolicy
    reported_model_id: str | None = None

    def observe(self, value: object) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        reported = value.strip()
        if (
            self.policy is ModelIdentityPolicy.EXACT
            and reported != self.requested_model_id
        ):
            raise LLMTransportContractError(
                "transport_changed_model_target: provider reported "
                f"{reported!r}, expected exact identity {self.requested_model_id!r}",
                reason_code="transport_changed_model_target",
            )
        if self.reported_model_id is not None and self.reported_model_id != reported:
            raise LLMTransportContractError(
                "transport_changed_model_target: provider model identity changed within stream",
                reason_code="transport_changed_model_target",
            )
        self.reported_model_id = reported


def responses_reported_model(raw_event: Any) -> object:
    event = sdk_event_to_dict(raw_event)
    response = event.get("response")
    if isinstance(response, dict) and response.get("model") is not None:
        return response.get("model")
    return event.get("model")


def chat_completion_reported_model(raw_chunk: Any) -> object:
    return sdk_event_to_dict(raw_chunk).get("model")


def arguments_to_json_string(raw_arguments: Any) -> str:
    if isinstance(raw_arguments, str):
        return raw_arguments
    if isinstance(raw_arguments, dict):
        return json.dumps(raw_arguments)
    return "{}"


def transport_usage_report_from_mapping(raw_usage: Any) -> TransportUsageReport:
    usage = sdk_event_to_dict(raw_usage) if raw_usage is not None else {}
    if not usage:
        return TransportUsageReport(usage_status="missing", usage=None)
    input_raw = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_raw = usage.get("output_tokens", usage.get("completion_tokens"))
    if input_raw is None or output_raw is None:
        return TransportUsageReport(
            usage_status="missing",
            usage=None,
            provider_diagnostics=(
                ModelCallDiagnosticFact(code="provider_usage_incomplete"),
            ),
        )
    input_tokens = int(input_raw)
    output_tokens = int(output_raw)
    normalized_total = input_tokens + output_tokens
    diagnostics: list[ModelCallDiagnosticFact] = []
    provider_total = usage.get("total_tokens")
    if provider_total is not None and int(provider_total) != normalized_total:
        diagnostics.append(
            ModelCallDiagnosticFact(
                code="provider_usage_total_mismatch",
                attributes=(
                    ("normalized_total", normalized_total),
                    ("provider_total", int(provider_total)),
                ),
            )
        )
    input_details = usage.get(
        "input_tokens_details", usage.get("prompt_tokens_details")
    )
    output_details = usage.get(
        "output_tokens_details", usage.get("completion_tokens_details")
    )
    cached = (
        input_details.get("cached_tokens")
        if isinstance(input_details, dict)
        and input_details.get("cached_tokens") is not None
        else None
    )
    reasoning = (
        output_details.get("reasoning_tokens")
        if isinstance(output_details, dict)
        and output_details.get("reasoning_tokens") is not None
        else None
    )
    fact = ModelTokenUsageFact(
        input_tokens=input_tokens,
        cached_input_tokens=int(cached) if cached is not None else None,
        output_tokens=output_tokens,
        reasoning_output_tokens=int(reasoning) if reasoning is not None else None,
        total_tokens=normalized_total,
    )
    return TransportUsageReport(
        usage_status="reported",
        usage=fact,
        provider_diagnostics=tuple(diagnostics),
    )


def event_includes_run_error(events: list[AgentEvent]) -> bool:
    return any(isinstance(event, RunErrorEvent) for event in events)
