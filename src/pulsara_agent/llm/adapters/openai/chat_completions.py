"""OpenAI Chat Completions protocol translation to Pulsara AgentEvent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_CHAT_COMPLETIONS_API,
    build_async_openai_client,
    provider_error_data,
)
from pulsara_agent.llm.adapters.openai.events import (
    AgentEventBuilder,
    sdk_event_to_dict,
    usage_from_mapping,
)
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.provider import ProviderProfile, ThinkingReplayPolicy
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.transport import LLMTransport
from pulsara_agent.llm.usage import Usage


@dataclass(slots=True)
class OpenAIChatCompletionsTransport(LLMTransport):
    """Adapter for OpenAI Chat Completions-compatible APIs."""

    api_key: str
    api: str = OPENAI_CHAT_COMPLETIONS_API
    timeout_seconds: float = 60.0
    _mock_chunks: list[dict[str, Any]] = field(default_factory=list)
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
        accumulator = ChatToolCallAccumulator(builder=builder)
        thinking_delta_fields = model.provider_profile.thinking.delta_fields
        yield builder.model_start()

        if self._mock_chunks:
            for raw_chunk in self._mock_chunks:
                for event in translate_chat_completion_chunk(
                    raw_chunk,
                    builder=builder,
                    accumulator=accumulator,
                    thinking_delta_fields=thinking_delta_fields,
                ):
                    yield event
            for event in accumulator.close_active_tool_calls():
                yield event
            for event in builder.close_active_blocks():
                yield event
            yield builder.model_end(usage=accumulator.usage)
            return

        payload = build_chat_completions_payload(model=model, context=context, options=options)
        should_close_client = self._client is None
        client = self._client or build_async_openai_client(
            api_key=self.api_key,
            base_url=model.base_url,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            stream = await client.chat.completions.create(**payload, stream=True)
            async for raw_chunk in stream:
                for event in translate_chat_completion_chunk(
                    raw_chunk,
                    builder=builder,
                    accumulator=accumulator,
                    thinking_delta_fields=thinking_delta_fields,
                ):
                    yield event
        except Exception as exc:
            for event in accumulator.close_active_tool_calls():
                yield event
            for event in builder.close_active_blocks():
                yield event
            yield builder.run_error(
                message=str(exc),
                code="openai_chat_completions_error",
                provider_data=provider_error_data(exc),
            )
            return
        finally:
            if should_close_client:
                await client.close()

        for event in accumulator.close_active_tool_calls():
            yield event
        for event in builder.close_active_blocks():
            yield event
        yield builder.model_end(usage=accumulator.usage)


def build_chat_completions_payload(
    *,
    model: ModelProfile,
    context: LLMContext,
    options: LLMOptions | None = None,
) -> dict[str, Any]:
    provider_profile = model.provider_profile
    messages: list[dict[str, Any]] = []
    if context.system_prompt:
        messages.append({"role": "system", "content": context.system_prompt})
    messages.extend(_messages_to_chat_messages(context.messages, provider_profile=provider_profile))

    payload: dict[str, Any] = {
        "model": model.id,
        "messages": messages,
        "stream_options": {"include_usage": True},
    }
    for key, value in provider_profile.request_defaults.items():
        payload.setdefault(key, value)
    if context.tools and provider_profile.supports_tools:
        payload["tools"] = [_tool_to_chat_tool(tool) for tool in context.tools]
    if options is not None:
        omitted = set(provider_profile.omit_params_when_thinking)
        thinking_enabled = provider_profile.thinking.enabled
        if options.temperature is not None and not (thinking_enabled and "temperature" in omitted):
            payload["temperature"] = options.temperature
        if options.max_output_tokens is not None and not (
            thinking_enabled and "max_completion_tokens" in omitted
        ):
            payload["max_completion_tokens"] = options.max_output_tokens
        if (
            options.reasoning_effort is not None
            and provider_profile.supports_reasoning
            and not (thinking_enabled and "reasoning_effort" in omitted)
        ):
            payload["reasoning_effort"] = options.reasoning_effort
    if provider_profile.request_extra_body:
        payload["extra_body"] = dict(provider_profile.request_extra_body)
    return payload


def _messages_to_chat_messages(
    messages: tuple[LLMMessage, ...],
    *,
    provider_profile: ProviderProfile | None = None,
) -> list[dict[str, Any]]:
    provider_profile = provider_profile or ProviderProfile(wire_api=OPENAI_CHAT_COMPLETIONS_API)
    chat_messages: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.TOOL_CALL:
            pending_tool_calls.append(_legacy_message_to_chat_tool_call(message))
            continue
        if pending_tool_calls:
            chat_messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": pending_tool_calls,
                }
            )
            pending_tool_calls = []
        chat_messages.append(_message_to_chat_message(message, provider_profile=provider_profile))
    if pending_tool_calls:
        chat_messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": pending_tool_calls,
            }
        )
    return chat_messages


def translate_chat_completion_chunk(
    raw_chunk: Any,
    *,
    builder: AgentEventBuilder,
    accumulator: "ChatToolCallAccumulator",
    thinking_delta_fields: tuple[str, ...] = ("reasoning_content",),
) -> list[AgentEvent]:
    chunk = sdk_event_to_dict(raw_chunk)
    accumulator.update_usage(chunk.get("usage"))
    events: list[AgentEvent] = []
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return events

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            for field_name in thinking_delta_fields:
                thinking = delta.get(field_name)
                if isinstance(thinking, str):
                    events.extend(builder.thinking_delta(thinking))
            content = delta.get("content")
            if isinstance(content, str):
                events.extend(builder.text_delta(content))
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for raw_tool_call in tool_calls:
                    if isinstance(raw_tool_call, dict):
                        events.extend(accumulator.apply_tool_call_delta(raw_tool_call))

        if choice.get("finish_reason") == "tool_calls":
            events.extend(accumulator.close_active_tool_calls())
    return events


@dataclass(slots=True)
class _ChatToolCallState:
    tool_call_id: str | None = None
    name: str = ""
    pending_arguments: list[str] = field(default_factory=list)
    started: bool = False


@dataclass(slots=True)
class ChatToolCallAccumulator:
    builder: AgentEventBuilder
    usage: Usage = field(default_factory=Usage)
    _states: dict[str, _ChatToolCallState] = field(default_factory=dict)

    def apply_tool_call_delta(self, raw_tool_call: dict[str, Any]) -> list[AgentEvent]:
        key = str(raw_tool_call.get("index", len(self._states)))
        state = self._states.setdefault(key, _ChatToolCallState())
        tool_call_id = raw_tool_call.get("id")
        if isinstance(tool_call_id, str) and tool_call_id:
            state.tool_call_id = tool_call_id

        function = raw_tool_call.get("function")
        arguments_delta = ""
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                state.name += name
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                arguments_delta = arguments

        events: list[AgentEvent] = []
        if not state.started and state.tool_call_id and (
            state.name or arguments_delta or state.pending_arguments
        ):
            events.extend(
                self.builder.tool_call_start(
                    tool_call_id=state.tool_call_id,
                    tool_call_name=state.name,
                )
            )
            state.started = True
            if state.pending_arguments:
                events.extend(
                    self.builder.tool_call_delta(
                        tool_call_id=state.tool_call_id,
                        delta="".join(state.pending_arguments),
                    )
                )
                state.pending_arguments.clear()

        if arguments_delta:
            if state.started and state.tool_call_id:
                events.extend(
                    self.builder.tool_call_delta(
                        tool_call_id=state.tool_call_id,
                        delta=arguments_delta,
                    )
                )
            else:
                state.pending_arguments.append(arguments_delta)
        return events

    def update_usage(self, raw_usage: Any) -> None:
        usage = usage_from_mapping(raw_usage)
        if usage.total_tokens or usage.input_tokens or usage.output_tokens:
            self.usage = usage

    def close_active_tool_calls(self) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        for state in self._states.values():
            if state.started and state.tool_call_id:
                events.extend(self.builder.tool_call_end(tool_call_id=state.tool_call_id))
                state.started = False
        return events


def _message_to_chat_message(
    message: LLMMessage,
    *,
    provider_profile: ProviderProfile,
) -> dict[str, Any]:
    if message.role is MessageRole.TOOL_CALL:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [_legacy_message_to_chat_tool_call(message)],
        }
    if message.role is MessageRole.TOOL_RESULT:
        if not message.tool_call_id:
            raise ValueError("Chat tool result message requires tool_call_id")
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": "\n".join(message.content),
        }
    if message.role is MessageRole.ASSISTANT:
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(message.content),
        }
        if _should_replay_thinking(message, provider_profile=provider_profile):
            message_field = provider_profile.thinking.message_field
            if message_field:
                payload[message_field] = "\n".join(message.thinking)
        if message.tool_calls:
            payload["tool_calls"] = [_tool_call_to_chat_tool_call(call) for call in message.tool_calls]
        return payload
    return {
        "role": _chat_role(message.role),
        "content": "\n".join(message.content),
    }


def _chat_role(role: MessageRole) -> str:
    if role in {MessageRole.SYSTEM, MessageRole.USER, MessageRole.ASSISTANT}:
        return role.value
    raise ValueError(f"Unsupported chat message role: {role}")


def _legacy_message_to_chat_tool_call(message: LLMMessage) -> dict[str, Any]:
    if not message.tool_call_id:
        raise ValueError("Chat assistant tool call message requires tool_call_id")
    if not message.name:
        raise ValueError("Chat assistant tool call message requires name")
    return _tool_call_to_chat_tool_call(
        LLMToolCall(
            id=message.tool_call_id,
            name=message.name,
            arguments=message.arguments or "{}",
        )
    )


def _tool_call_to_chat_tool_call(tool_call: LLMToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": tool_call.arguments or "{}",
        },
    }


def _should_replay_thinking(message: LLMMessage, *, provider_profile: ProviderProfile) -> bool:
    if not message.thinking:
        return False
    policy = provider_profile.thinking.replay_policy
    if policy is ThinkingReplayPolicy.NEVER:
        return False
    if policy is ThinkingReplayPolicy.ALWAYS:
        return True
    if policy is ThinkingReplayPolicy.WHEN_TOOL_CALLS:
        return bool(message.tool_calls)
    return False


def _tool_to_chat_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
