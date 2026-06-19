"""OpenAI Responses protocol translation to Pulsara AgentEvent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import uuid4

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_RESPONSES_API,
    build_async_openai_client,
    provider_error_data,
)
from pulsara_agent.llm.adapters.openai.events import (
    AgentEventBuilder,
    arguments_to_json_string,
    event_includes_model_end,
    event_includes_run_error,
    sdk_event_to_dict,
    usage_from_mapping,
)
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.transport import LLMTransport
from pulsara_agent.llm.usage import Usage


@dataclass(slots=True)
class OpenAIResponsesTransport(LLMTransport):
    """Adapter for OpenAI Responses-compatible APIs."""

    api_key: str
    api: str = OPENAI_RESPONSES_API
    timeout_seconds: float = 60.0
    _mock_events: list[dict[str, Any]] = field(default_factory=list)
    _client: Any | None = None

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        builder = AgentEventBuilder(model=model, event_context=event_context)
        yield builder.model_start()

        if self._mock_events:
            model_end_emitted = False
            for raw_event in self._mock_events:
                events = translate_responses_event(raw_event, builder=builder)
                model_end_emitted = model_end_emitted or event_includes_model_end(events)
                run_error_emitted = event_includes_run_error(events)
                for event in events:
                    yield event
                if run_error_emitted:
                    return
            if not model_end_emitted:
                for event in builder.close_active_blocks():
                    yield event
                yield builder.model_end()
            return

        payload = build_responses_payload(model=model, context=context, options=options)
        should_close_client = self._client is None
        client = self._client or build_async_openai_client(
            api_key=self.api_key,
            base_url=model.base_url,
            timeout_seconds=self.timeout_seconds,
        )
        model_end_emitted = False
        try:
            stream = await client.responses.create(**payload, stream=True)
            async for raw_event in stream:
                events = translate_responses_event(raw_event, builder=builder)
                model_end_emitted = model_end_emitted or event_includes_model_end(events)
                run_error_emitted = event_includes_run_error(events)
                for event in events:
                    yield event
                if run_error_emitted:
                    return
        except Exception as exc:
            for event in builder.close_active_blocks():
                yield event
            yield builder.run_error(
                message=str(exc),
                code="openai_responses_error",
                provider_data=provider_error_data(exc),
            )
            return
        finally:
            if should_close_client:
                await client.close()

        if not model_end_emitted:
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
        "input": _messages_to_responses_inputs(context.messages),
    }
    provider_profile = model.provider_profile
    for key, value in provider_profile.request_defaults.items():
        payload.setdefault(key, value)
    if context.system_prompt:
        payload["instructions"] = context.system_prompt
    if context.tools and provider_profile.supports_tools:
        payload["tools"] = [_tool_to_responses_tool(tool) for tool in context.tools]
    if options is not None:
        omitted = set(provider_profile.omit_params_when_thinking)
        thinking_enabled = provider_profile.thinking.enabled
        if options.temperature is not None and not (thinking_enabled and "temperature" in omitted):
            payload["temperature"] = options.temperature
        if options.max_output_tokens is not None and not (
            thinking_enabled and "max_output_tokens" in omitted
        ):
            payload["max_output_tokens"] = options.max_output_tokens
        if (
            options.reasoning_effort is not None
            and provider_profile.supports_reasoning
            and not (thinking_enabled and "reasoning" in omitted)
            and not (thinking_enabled and "reasoning_effort" in omitted)
        ):
            payload["reasoning"] = {"effort": options.reasoning_effort}
        if options.reasoning_summary is not None:
            payload.setdefault("reasoning", {})["summary"] = options.reasoning_summary
    if provider_profile.request_extra_body:
        payload["extra_body"] = dict(provider_profile.request_extra_body)
    return payload


def response_to_agent_events(
    *,
    response: dict[str, Any],
    builder: AgentEventBuilder,
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
    raw_event: Any,
    *,
    builder: AgentEventBuilder,
) -> list[AgentEvent]:
    event = sdk_event_to_dict(raw_event)
    event_type = event.get("type")
    if event_type == "response.output_text.delta":
        return builder.text_delta(str(event.get("delta", "")))
    if event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
        return builder.thinking_delta(str(event.get("delta", "")))
    if event_type == "response.output_item.added":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            provider_item_id = str(item.get("id") or "")
            tool_call_id = str(item.get("call_id") or item.get("id") or uuid4())
            return builder.tool_call_start(
                tool_call_id=tool_call_id,
                tool_call_name=str(item.get("name") or ""),
                provider_item_id=provider_item_id,
            )
    if event_type == "response.function_call_arguments.delta":
        item_id = str(event.get("item_id") or event.get("call_id") or "")
        return builder.tool_call_delta(
            tool_call_id=builder.resolve_tool_call_id(item_id),
            delta=str(event.get("delta", "")),
        )
    if event_type == "response.output_item.done":
        item = event.get("item")
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
                        delta=arguments_to_json_string(arguments),
                    )
                )
            events.extend(builder.tool_call_end(tool_call_id=tool_call_id))
            return events
    if event_type == "response.completed":
        response = event.get("response")
        events = builder.close_active_blocks()
        events.append(builder.model_end(usage=_usage(response if isinstance(response, dict) else {})))
        return events
    if event_type in {"response.failed", "response.error", "response.incomplete", "error"}:
        response = event.get("response")
        provider_data = response if isinstance(response, dict) else event
        message = _response_error_message(provider_data)
        events = builder.close_active_blocks()
        events.append(
            builder.run_error(
                message=message,
                code="openai_responses_error",
                provider_data=provider_data,
            )
        )
        return events
    return []


def _messages_to_responses_inputs(messages: tuple[LLMMessage, ...]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for message in messages:
        inputs.extend(_message_to_responses_inputs(message))
    return inputs


def _message_to_responses_inputs(message: LLMMessage) -> list[dict[str, Any]]:
    if message.role is MessageRole.TOOL_CALL:
        if not message.tool_call_id:
            raise ValueError("Responses function_call input requires tool_call_id")
        if not message.name:
            raise ValueError("Responses function_call input requires name")
        return [
            _tool_call_to_responses_input(
                LLMToolCall(
                    id=message.tool_call_id,
                    name=message.name,
                    arguments=message.arguments or "{}",
                )
            )
        ]
    if message.role is MessageRole.TOOL_RESULT:
        if not message.tool_call_id:
            raise ValueError("Responses function_call_output input requires tool_call_id/call_id")
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": "\n".join(message.content),
            }
        ]
    if message.role is MessageRole.ASSISTANT and message.tool_calls:
        inputs: list[dict[str, Any]] = []
        if message.content:
            inputs.append(_textual_responses_input(message))
        inputs.extend(_tool_call_to_responses_input(tool_call) for tool_call in message.tool_calls)
        return inputs
    return [_textual_responses_input(message)]


def _textual_responses_input(message: LLMMessage) -> dict[str, Any]:
    return {
        "role": message.role.value,
        "content": [{"type": "input_text", "text": text} for text in message.content],
    }


def _tool_call_to_responses_input(tool_call: LLMToolCall) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": tool_call.id,
        "name": tool_call.name,
        "arguments": tool_call.arguments or "{}",
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
                    "arguments": arguments_to_json_string(item.get("arguments") or "{}"),
                }
            )
    return calls


def _iter_output_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output")
    if not isinstance(output, list):
        return []
    return [item for item in output if isinstance(item, dict)]


def _usage(response: dict[str, Any]) -> Usage:
    return usage_from_mapping(response.get("usage"))


def _response_error_message(provider_data: dict[str, Any]) -> str:
    message = provider_data.get("message")
    if isinstance(message, str) and message:
        return message
    error = provider_data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    status = provider_data.get("status")
    if isinstance(status, str) and status:
        return f"OpenAI Responses stream ended with status: {status}"
    return "OpenAI Responses stream failed."
