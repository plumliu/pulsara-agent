"""OpenAI Chat Completions translation to adapter-private raw items."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator

from pulsara_agent.event import EventContext
from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_CHAT_COMPLETIONS_API,
    build_async_openai_client,
)
from pulsara_agent.llm.adapters.openai.errors import classify_llm_error
from pulsara_agent.llm.adapters.openai.events import (
    RawProviderItemBuilder,
    ReportedModelIdentityObserver,
    chat_completion_reported_model,
    sdk_event_to_dict,
    transport_usage_report_from_mapping,
)
from pulsara_agent.llm.adapters.openai.retrying import (
    build_provider_retry_summary,
    log_retry_attempt,
    make_retry_trace,
    provider_failure_code_hint,
    sdk_max_retries_for_transport,
)
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.provider import (
    ProviderProfile,
    ThinkingReplayPolicy,
    mutable_provider_value,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.raw_provider import RawProviderStreamItem
from pulsara_agent.llm.runtime_observation import (
    resolve_runtime_observation_binding,
)
from pulsara_agent.llm.retry import (
    LLMRetryConfig,
    RetryAttemptTrace,
    RetryDecisionKind,
    apply_retry_after_cap,
    compute_retry_delay,
)


@dataclass(slots=True)
class OpenAIChatCompletionsTransport:
    """Adapter for OpenAI Chat Completions-compatible APIs."""

    api_key: str
    api: str = OPENAI_CHAT_COMPLETIONS_API
    binding_id: str = "pulsara.openai.chat_completions"
    contract_version: str = "v1"
    timeout_seconds: float = 60.0
    retry_config: LLMRetryConfig = field(default_factory=LLMRetryConfig)
    openai_sdk_max_retries: int | None = None
    retry_sleep: Callable[[float], Awaitable[None]] = field(
        default=asyncio.sleep, repr=False
    )
    _mock_chunks: list[dict[str, Any]] = field(default_factory=list)
    _client: Any | None = None

    async def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[RawProviderStreamItem | TransportUsageReport]:
        model = call.target.model_profile
        builder = RawProviderItemBuilder()
        thinking_delta_fields = model.provider_profile.thinking.delta_fields
        if self._mock_chunks:
            model_identity = ReportedModelIdentityObserver(
                requested_model_id=model.id,
                policy=model.provider_profile.model_identity_policy,
            )
            accumulator = ChatToolCallAccumulator(builder=builder)
            for raw_chunk in self._mock_chunks:
                model_identity.observe(chat_completion_reported_model(raw_chunk))
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
            report = accumulator.usage_report
            if report is not None or model_identity.reported_model_id is not None:
                yield replace(
                    report or TransportUsageReport(usage_status="missing", usage=None),
                    reported_model_id=model_identity.reported_model_id,
                )
            return

        payload = build_chat_completions_payload(call=call, context=context)
        should_close_client = self._client is None
        client = self._client or build_async_openai_client(
            api_key=self.api_key,
            base_url=model.base_url,
            timeout_seconds=self.timeout_seconds,
            max_retries=sdk_max_retries_for_transport(
                retry_config=self.retry_config,
                explicit_max_retries=self.openai_sdk_max_retries,
            ),
        )
        retry_traces: list[RetryAttemptTrace] = []
        completed_model_identity: str | None = None
        try:
            attempt = 1
            max_attempts = (
                self.retry_config.attempts if self.retry_config.enabled else 1
            )
            while True:
                model_identity = ReportedModelIdentityObserver(
                    requested_model_id=model.id,
                    policy=model.provider_profile.model_identity_policy,
                )
                accumulator = ChatToolCallAccumulator(builder=builder)
                try:
                    stream = await client.chat.completions.create(
                        **payload, stream=True
                    )
                    async for raw_chunk in stream:
                        model_identity.observe(
                            chat_completion_reported_model(raw_chunk)
                        )
                        for event in translate_chat_completion_chunk(
                            raw_chunk,
                            builder=builder,
                            accumulator=accumulator,
                            thinking_delta_fields=thinking_delta_fields,
                        ):
                            yield event
                    completed_model_identity = model_identity.reported_model_id
                    break
                except Exception as exc:
                    decision = apply_retry_after_cap(
                        classify_llm_error(exc),
                        config=self.retry_config,
                    )
                    can_retry = (
                        self.retry_config.enabled
                        and decision.kind is RetryDecisionKind.RETRY
                        and not builder.has_semantic_output
                        and attempt < max_attempts
                    )
                    if can_retry:
                        delay = compute_retry_delay(
                            attempt_index=attempt,
                            config=self.retry_config,
                            retry_after_seconds=decision.retry_after_seconds,
                        )
                        trace = make_retry_trace(
                            exc=exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            delay_seconds=delay,
                        )
                        retry_traces.append(trace)
                        log_retry_attempt(
                            api=self.api,
                            model=model,
                            trace=trace,
                            has_semantic_output=builder.has_semantic_output,
                        )
                        await self.retry_sleep(delay)
                        attempt += 1
                        continue

                    skipped_reason = _retry_skipped_reason(
                        retry_config=self.retry_config,
                        decision=decision,
                        has_semantic_output=builder.has_semantic_output,
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    failure_report = accumulator.usage_report
                    if (
                        failure_report is not None
                        or model_identity.reported_model_id is not None
                    ):
                        yield replace(
                            failure_report
                            or TransportUsageReport(usage_status="missing", usage=None),
                            reported_model_id=model_identity.reported_model_id,
                        )
                    yield builder.run_error(
                        message=str(exc),
                        code=provider_failure_code_hint(decision),
                        retry_summary=build_provider_retry_summary(
                            config=self.retry_config,
                            traces=retry_traces,
                            final_decision=decision,
                            final_attempt=attempt,
                            has_semantic_output=builder.has_semantic_output,
                            exhausted=(
                                self.retry_config.enabled
                                and decision.kind is RetryDecisionKind.RETRY
                                and not builder.has_semantic_output
                                and attempt >= max_attempts
                            ),
                            skipped_reason=skipped_reason,
                        ),
                    )
                    return
        finally:
            if should_close_client:
                await client.close()

        for event in accumulator.close_active_tool_calls():
            yield event
        for event in builder.close_active_blocks():
            yield event
        report = accumulator.usage_report
        if report is not None or completed_model_identity is not None:
            yield replace(
                report or TransportUsageReport(usage_status="missing", usage=None),
                reported_model_id=completed_model_identity,
            )


def _retry_skipped_reason(
    *,
    retry_config: LLMRetryConfig,
    decision: Any,
    has_semantic_output: bool,
    attempt: int,
    max_attempts: int,
) -> str | None:
    if not retry_config.enabled:
        return "retry_disabled"
    if has_semantic_output:
        return "semantic_output_started"
    if decision.kind is not RetryDecisionKind.RETRY:
        return decision.reason
    if attempt >= max_attempts:
        return "attempts_exhausted"
    return None


def build_chat_completions_payload(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> dict[str, Any]:
    model = call.target.model_profile
    options = call.target.effective_options
    provider_profile = model.provider_profile
    runtime_observation_role: str | None = None
    if any(
        message.role is MessageRole.RUNTIME_OBSERVATION
        for message in context.messages
    ):
        carrier = call.target.fact.runtime_observation_carrier
        if carrier is None:
            raise ValueError("resolved target does not support runtime observations")
        binding = resolve_runtime_observation_binding(carrier)
        if binding.wire_role != "system":
            raise ValueError("resolved runtime observation carrier is not chat-compatible")
        runtime_observation_role = binding.wire_role
    messages: list[dict[str, Any]] = []
    if context.system_prompt:
        messages.append({"role": "system", "content": context.system_prompt})
    messages.extend(
        _messages_to_chat_messages(
            context.messages,
            provider_profile=provider_profile,
            runtime_observation_role=runtime_observation_role,
        )
    )

    payload: dict[str, Any] = {
        "model": model.id,
        "messages": messages,
        "stream_options": {"include_usage": True},
    }
    for key, value in provider_profile.request_defaults.items():
        payload.setdefault(key, mutable_provider_value(value))
    if context.tools and provider_profile.supports_tools:
        payload["tools"] = [_tool_to_chat_tool(tool) for tool in context.tools]
    payload["max_completion_tokens"] = (
        call.target.context_budget.effective_output_tokens
    )
    if options.reasoning_effort is not None:
        payload["reasoning_effort"] = options.reasoning_effort
    if provider_profile.request_extra_body:
        payload["extra_body"] = mutable_provider_value(
            provider_profile.request_extra_body
        )
    return payload


def _messages_to_chat_messages(
    messages: tuple[LLMMessage, ...],
    *,
    provider_profile: ProviderProfile | None = None,
    runtime_observation_role: str | None = None,
) -> list[dict[str, Any]]:
    provider_profile = provider_profile or ProviderProfile(
        wire_api=OPENAI_CHAT_COMPLETIONS_API
    )
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
        chat_messages.append(
            _message_to_chat_message(
                message,
                provider_profile=provider_profile,
                runtime_observation_role=runtime_observation_role,
            )
        )
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
    builder: RawProviderItemBuilder,
    accumulator: "ChatToolCallAccumulator",
    thinking_delta_fields: tuple[str, ...] = ("reasoning_content",),
) -> list[RawProviderStreamItem]:
    chunk = sdk_event_to_dict(raw_chunk)
    accumulator.update_usage(chunk.get("usage"))
    events: list[RawProviderStreamItem] = []
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
    builder: RawProviderItemBuilder
    usage_report: TransportUsageReport | None = None
    _states: dict[str, _ChatToolCallState] = field(default_factory=dict)

    def apply_tool_call_delta(
        self, raw_tool_call: dict[str, Any]
    ) -> list[RawProviderStreamItem]:
        key = str(raw_tool_call.get("index", len(self._states)))
        state = self._states.setdefault(key, _ChatToolCallState())
        tool_call_id = raw_tool_call.get("id")
        if isinstance(tool_call_id, str) and tool_call_id:
            if state.tool_call_id is not None and state.tool_call_id != tool_call_id:
                raise LLMTransportContractError(
                    "chat tool-call stream changed its frozen call ID",
                    reason_code="transport_tool_call_identity_mismatch",
                )
            state.tool_call_id = tool_call_id

        function = raw_tool_call.get("function")
        arguments_delta = ""
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                if state.started:
                    if name != state.name:
                        raise LLMTransportContractError(
                            "chat tool-call stream changed its frozen tool name",
                            reason_code="transport_tool_call_name_mismatch",
                        )
                else:
                    state.name += name
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                arguments_delta = arguments

        events: list[RawProviderStreamItem] = []
        if (
            not state.started
            and state.tool_call_id
            and state.name
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
        report = transport_usage_report_from_mapping(raw_usage)
        if report.usage_status == "reported":
            self.usage_report = report

    def close_active_tool_calls(self) -> list[RawProviderStreamItem]:
        if any(
            not state.started or not state.tool_call_id
            for state in self._states.values()
        ):
            raise LLMTransportContractError(
                "tool-call stream ended before a named tool-call start",
                reason_code="transport_tool_call_start_missing",
            )
        events: list[RawProviderStreamItem] = []
        for state in self._states.values():
            assert state.tool_call_id is not None
            events.extend(
                self.builder.tool_call_end(tool_call_id=state.tool_call_id)
            )
        self._states.clear()
        return events


def _message_to_chat_message(
    message: LLMMessage,
    *,
    provider_profile: ProviderProfile,
    runtime_observation_role: str | None = None,
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
            payload["tool_calls"] = [
                _tool_call_to_chat_tool_call(call) for call in message.tool_calls
            ]
        return payload
    if message.role is MessageRole.RUNTIME_OBSERVATION:
        if runtime_observation_role != "system":
            raise ValueError("Chat runtime observation carrier is unavailable")
        return {
            "role": runtime_observation_role,
            "content": "\n".join(message.content),
        }
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


def _should_replay_thinking(
    message: LLMMessage, *, provider_profile: ProviderProfile
) -> bool:
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
