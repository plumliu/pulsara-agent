"""OpenAI Responses protocol translation to Pulsara AgentEvent."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
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
from pulsara_agent.llm.input import LLMMessage, MessageRole, ToolSpec
from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.usage import Usage


OPENAI_RESPONSES_API = "openai_responses"


@dataclass(slots=True)
class OpenAIResponsesTransport:
    """Adapter for OpenAI Responses-compatible APIs."""

    api_key: str
    api: str = OPENAI_RESPONSES_API
    timeout_seconds: float = 60.0
    _mock_events: list[dict[str, Any]] = field(default_factory=list)

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        builder = _AgentEventBuilder(model=model, event_context=event_context)
        yield builder.model_start()

        if not self._mock_events:
            payload = build_responses_payload(model=model, context=context, options=options)
            try:
                response = _post_responses(
                    base_url=model.base_url,
                    api_key=self.api_key,
                    payload=payload,
                    timeout_seconds=self.timeout_seconds,
                )
            except OpenAIResponsesError as exc:
                yield RunErrorEvent(
                    **event_context.event_fields(),
                    message=str(exc),
                    code="openai_responses_error",
                    metadata={"provider_data": exc.provider_data},
                )
                return

            for event in response_to_agent_events(
                response=response,
                builder=builder,
            ):
                yield event
            return

        for raw_event in self._mock_events:
            for event in translate_responses_event(raw_event, builder=builder):
                yield event

        for event in builder.close_active_blocks():
            yield event
        yield builder.model_end()


def build_responses_payload(
    *,
    model: ModelProfile,
    context: LLMContext,
    options: LLMOptions | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model.id,
        "input": [_message_to_responses_input(message) for message in context.messages],
    }
    if context.system_prompt:
        payload["instructions"] = context.system_prompt
    if context.tools:
        payload["tools"] = [_tool_to_responses_tool(tool) for tool in context.tools]
    if options is not None:
        if options.temperature is not None:
            payload["temperature"] = options.temperature
        if options.max_output_tokens is not None:
            payload["max_output_tokens"] = options.max_output_tokens
        if options.reasoning_effort is not None:
            payload["reasoning"] = {"effort": options.reasoning_effort}
        if options.reasoning_summary is not None:
            payload.setdefault("reasoning", {})["summary"] = options.reasoning_summary
    return payload


def response_to_agent_events(
    *,
    response: dict[str, Any],
    builder: "_AgentEventBuilder",
) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    thinking = _extract_reasoning_summary(response)
    if thinking:
        events.extend(builder.thinking_delta(thinking))
    text = _extract_text(response)
    if text:
        events.extend(builder.text_delta(text))
    for tool_call in _extract_tool_calls(response):
        events.extend(
            builder.tool_call(
                tool_call_id=tool_call["id"],
                tool_call_name=tool_call["name"],
                arguments=tool_call["arguments"],
            )
        )
    events.extend(builder.close_active_blocks())
    events.append(builder.model_end(usage=_usage(response)))
    return events


def translate_responses_event(
    raw_event: dict[str, Any],
    *,
    builder: "_AgentEventBuilder",
) -> list[AgentEvent]:
    event_type = raw_event.get("type")
    if event_type == "response.output_text.delta":
        return builder.text_delta(str(raw_event.get("delta", "")))
    if event_type == "response.reasoning_summary_text.delta":
        return builder.thinking_delta(str(raw_event.get("delta", "")))
    if event_type == "response.output_item.added":
        item = raw_event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            provider_item_id = str(item.get("id") or "")
            tool_call_id = str(item.get("call_id") or item.get("id") or uuid4())
            return builder.tool_call_start(
                tool_call_id=tool_call_id,
                tool_call_name=str(item.get("name") or ""),
                provider_item_id=provider_item_id,
            )
    if event_type == "response.function_call_arguments.delta":
        item_id = str(raw_event.get("item_id") or raw_event.get("call_id") or "")
        return builder.tool_call_delta(
            tool_call_id=builder.resolve_tool_call_id(item_id),
            delta=str(raw_event.get("delta", "")),
        )
    if event_type == "response.output_item.done":
        item = raw_event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            provider_item_id = str(item.get("id") or "")
            tool_call_id = str(item.get("call_id") or item.get("id") or "")
            events = builder.tool_call_start(
                tool_call_id=tool_call_id,
                tool_call_name=str(item.get("name") or ""),
                provider_item_id=provider_item_id,
            )
            arguments = item.get("arguments")
            if arguments and not builder.has_arguments(tool_call_id):
                events.extend(
                    builder.tool_call_delta(
                        tool_call_id=tool_call_id,
                        delta=_arguments_to_json_string(arguments),
                    )
                )
            events.extend(builder.tool_call_end(tool_call_id=tool_call_id))
            return events
    if event_type == "response.completed":
        response = raw_event.get("response")
        events = builder.close_active_blocks()
        events.append(builder.model_end(usage=_usage(response if isinstance(response, dict) else {})))
        return events
    return []


@dataclass(slots=True)
class _AgentEventBuilder:
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

    def text_delta(self, delta: str) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        if self.text_block_id is None:
            self.text_block_id = f"text:{uuid4()}"
            events.append(TextBlockStartEvent(**self.event_fields(), block_id=self.text_block_id))
        events.append(TextBlockDeltaEvent(**self.event_fields(), block_id=self.text_block_id, delta=delta))
        return events

    def thinking_delta(self, delta: str) -> list[AgentEvent]:
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
        events: list[AgentEvent] = []
        if tool_call_id and tool_call_id not in self.active_tool_call_ids:
            events.extend(self.tool_call_start(tool_call_id=tool_call_id, tool_call_name=""))
        if tool_call_id and delta:
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


@dataclass(frozen=True, slots=True)
class OpenAIResponsesError(Exception):
    message: str
    provider_data: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def _post_responses(
    *,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        _responses_url(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OpenAIResponsesError(
            message=f"OpenAI Responses request failed with HTTP {exc.code}: {body}",
            provider_data={"status": exc.code, "body": body},
        ) from exc
    except urllib.error.URLError as exc:
        raise OpenAIResponsesError(
            message=f"OpenAI Responses request failed: {exc.reason}",
            provider_data={"reason": str(exc.reason)},
        ) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OpenAIResponsesError(
            message="OpenAI Responses request returned invalid JSON.",
            provider_data={"body": body},
        ) from exc
    if not isinstance(parsed, dict):
        raise OpenAIResponsesError(
            message="OpenAI Responses request returned a non-object JSON payload.",
            provider_data={"body": parsed},
        )
    return parsed


def _responses_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"


def _message_to_responses_input(message: LLMMessage) -> dict[str, Any]:
    if message.role is MessageRole.TOOL_CALL:
        if not message.tool_call_id:
            raise ValueError("Responses function_call input requires tool_call_id")
        if not message.name:
            raise ValueError("Responses function_call input requires name")
        return {
            "type": "function_call",
            "call_id": message.tool_call_id,
            "name": message.name,
            "arguments": message.arguments or "{}",
        }
    if message.role is MessageRole.TOOL_RESULT:
        if not message.tool_call_id:
            raise ValueError("Responses function_call_output input requires tool_call_id/call_id")
        return {
            "type": "function_call_output",
            "call_id": message.tool_call_id,
            "output": "\n".join(message.content),
        }
    return {
        "role": message.role.value,
        "content": [{"type": "input_text", "text": text} for text in message.content],
    }


def _tool_to_responses_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _extract_text(response: dict[str, Any]) -> str:
    top_level_text = response.get("output_text")
    if isinstance(top_level_text, str) and top_level_text:
        return top_level_text

    parts: list[str] = []
    for item in _iter_output_items(response):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _extract_reasoning_summary(response: dict[str, Any]) -> str:
    reasoning = response.get("reasoning")
    if not isinstance(reasoning, dict):
        return ""
    summary = reasoning.get("summary")
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return ""
    parts: list[str] = []
    for item in summary:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("summary")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _extract_tool_calls(response: dict[str, Any]) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    for item in _iter_output_items(response):
        if item.get("type") == "function_call":
            calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "arguments": _arguments_to_json_string(item.get("arguments") or "{}"),
                }
            )
    return calls


def _arguments_to_json_string(raw_arguments: Any) -> str:
    if isinstance(raw_arguments, str):
        return raw_arguments
    if isinstance(raw_arguments, dict):
        return json.dumps(raw_arguments)
    return "{}"


def _iter_output_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output")
    if not isinstance(output, list):
        return []
    return [item for item in output if isinstance(item, dict)]


def _usage(response: dict[str, Any]) -> Usage:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return Usage()
    return Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
    )
