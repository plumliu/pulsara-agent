"""OpenAI Chat Completions translation to adapter-private raw items."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
import json
from typing import Any, AsyncIterator

from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_CHAT_COMPLETIONS_API,
    OpenAITransportTimeoutPolicy,
    build_async_openai_client,
)
from pulsara_agent.llm.adapters.openai.errors import classify_llm_error
from pulsara_agent.llm.adapters.openai.events import (
    ProviderLiveItemBuilder,
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
    ProviderChatFieldAccumulationMode,
    ProviderProfile,
    ProviderReasoningReplayScope,
    ThinkingReplayPolicy,
    mutable_provider_value,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.ports.provider_stream import (
    ProviderAdapterStreamItem,
    ProviderAdapterTerminal,
    ProviderAdapterTerminalKind,
    ProviderOutputIncompleteReason,
    ProviderStreamFailure,
    freeze_provider_adapter_completed_replay_payload,
)
from pulsara_agent.primitives.context import (
    FrozenJsonArrayFact,
    FrozenJsonObjectFact,
    FrozenJsonValue,
    freeze_json,
    thaw_json,
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
    timeout_policy: OpenAITransportTimeoutPolicy
    api: str = OPENAI_CHAT_COMPLETIONS_API
    binding_id: str = "pulsara.openai.chat_completions"
    contract_version: str = "v3-explicit-terminal-echo-normalization"
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
    ) -> AsyncIterator[ProviderAdapterStreamItem]:
        model = call.target.model_profile
        if self._mock_chunks:
            model_identity = ReportedModelIdentityObserver(
                requested_model_id=model.id,
                policy=model.provider_profile.model_identity_policy,
            )
            accumulator = ChatCompletionAccumulator(
                builder=ProviderLiveItemBuilder(),
                provider_profile=model.provider_profile,
            )
            for raw_chunk in self._mock_chunks:
                model_identity.observe(chat_completion_reported_model(raw_chunk))
                for event in accumulator.apply(raw_chunk):
                    yield event
            report = accumulator.usage_report
            if report is not None or model_identity.reported_model_id is not None:
                yield replace(
                    report or TransportUsageReport(usage_status="missing", usage=None),
                    reported_model_id=model_identity.reported_model_id,
                )
            yield accumulator.finish()
            return

        payload = build_chat_completions_payload(call=call, context=context)
        should_close_client = self._client is None
        client = self._client or build_async_openai_client(
            api_key=self.api_key,
            base_url=model.base_url,
            timeout_policy=self.timeout_policy,
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
                accumulator = ChatCompletionAccumulator(
                    builder=ProviderLiveItemBuilder(),
                    provider_profile=model.provider_profile,
                )
                try:
                    stream = await client.chat.completions.create(
                        **payload, stream=True
                    )
                    async for raw_chunk in stream:
                        model_identity.observe(
                            chat_completion_reported_model(raw_chunk)
                        )
                        for event in accumulator.apply(raw_chunk):
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
                        and not accumulator.builder.has_semantic_output
                        and accumulator.terminal is None
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
                            has_semantic_output=accumulator.builder.has_semantic_output,
                        )
                        await self.retry_sleep(delay)
                        attempt += 1
                        continue

                    skipped_reason = _retry_skipped_reason(
                        retry_config=self.retry_config,
                        decision=decision,
                        has_semantic_output=accumulator.builder.has_semantic_output,
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
                    yield ProviderStreamFailure(
                        message=str(exc),
                        code_hint=provider_failure_code_hint(decision),
                        retry_summary=build_provider_retry_summary(
                            config=self.retry_config,
                            traces=retry_traces,
                            final_decision=decision,
                            final_attempt=attempt,
                            has_semantic_output=accumulator.builder.has_semantic_output,
                            exhausted=(
                                self.retry_config.enabled
                                and decision.kind is RetryDecisionKind.RETRY
                                and not accumulator.builder.has_semantic_output
                                and attempt >= max_attempts
                            ),
                            skipped_reason=skipped_reason,
                        ),
                    )
                    return
        finally:
            if should_close_client:
                await client.close()

        report = accumulator.usage_report
        if report is not None or completed_model_identity is not None:
            yield replace(
                report or TransportUsageReport(usage_status="missing", usage=None),
                reported_model_id=completed_model_identity,
            )
        yield accumulator.finish()


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
    plan = context.provider_wire_input_plan
    if plan is not None:
        if plan.wire_api != OPENAI_CHAT_COMPLETIONS_API:
            raise ValueError("provider wire plan API does not match Chat")
        root = thaw_json(plan.materialization.root_policy_value)
        if root is not None and not isinstance(root, str):
            raise TypeError("Chat root policy must be text or null")
        messages = [] if not root else [{"role": "system", "content": root}]
        messages.extend(
            _thaw_wire_objects(plan.materialization.ordered_input_items)
        )
        planned_tools = _thaw_wire_objects(plan.materialization.tool_items)
    else:
        messages = []
        if context.system_prompt:
            messages.append({"role": "system", "content": context.system_prompt})
        messages.extend(
            _messages_to_chat_messages(
                context.messages,
                provider_profile=provider_profile,
            )
        )
        planned_tools = [_tool_to_chat_tool(tool) for tool in context.tools]

    payload: dict[str, Any] = {
        "model": model.id,
        "messages": messages,
        "n": 1,
        "stream_options": {"include_usage": True},
    }
    for key, value in provider_profile.request_defaults.items():
        payload.setdefault(key, mutable_provider_value(value))
    if planned_tools and provider_profile.supports_tools:
        payload["tools"] = planned_tools
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


def chat_semantic_wire_group(
    message: LLMMessage,
    *,
    provider_profile: ProviderProfile,
) -> tuple[dict[str, Any], ...]:
    """Return the exact generic wire group for one compiled message."""

    return tuple(
        _messages_to_chat_messages((message,), provider_profile=provider_profile)
    )


def chat_tool_wire_items(tools: tuple[ToolSpec, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(_tool_to_chat_tool(tool) for tool in tools)


def _thaw_wire_objects(
    values: tuple[FrozenJsonObjectFact, ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        thawed = thaw_json(value)
        if not isinstance(thawed, dict):
            raise TypeError("provider wire item did not thaw to an object")
        result.append(thawed)
    return result


def _messages_to_chat_messages(
    messages: tuple[LLMMessage, ...],
    *,
    provider_profile: ProviderProfile | None = None,
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


@dataclass(slots=True)
class ChatCompletionAccumulator:
    """Closed one-choice Chat response state; EOF is never acceptance."""

    builder: ProviderLiveItemBuilder
    provider_profile: ProviderProfile
    tool_calls: "ChatToolCallAccumulator" = field(init=False)
    usage_report: TransportUsageReport | None = None
    terminal: ProviderAdapterTerminal | None = None
    _terminal_finish_reason: str | None = None
    _text_parts: list[str] = field(default_factory=list)
    _content_observed: bool = False
    _field_values: dict[str, FrozenJsonValue | list[FrozenJsonValue]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self.tool_calls = ChatToolCallAccumulator(builder=self.builder)

    def apply(self, raw_chunk: Any) -> list[ProviderStreamPayload]:
        chunk = sdk_event_to_dict(raw_chunk)
        report = transport_usage_report_from_mapping(chunk.get("usage"))
        if report.usage_status == "reported":
            if self.usage_report is not None:
                if self.terminal is None or self.usage_report != report:
                    raise LLMTransportContractError(
                        "transport emitted more than one usage report",
                        reason_code="transport_usage_report_duplicate",
                    )
            else:
                self.usage_report = report
        choices = chunk.get("choices")
        if choices is None:
            return []
        if not isinstance(choices, list):
            raise LLMTransportContractError(
                "chat choices are not an array",
                reason_code="transport_chat_choice_contract_invalid",
            )
        if not choices:
            return []
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise LLMTransportContractError(
                "chat transport requires exactly one choice",
                reason_code="transport_chat_choice_contract_invalid",
            )
        choice = choices[0]
        if choice.get("index", 0) != 0:
            raise LLMTransportContractError(
                "chat transport received an unexpected choice index",
                reason_code="transport_chat_choice_contract_invalid",
            )
        if self.terminal is not None:
            if self._is_exact_empty_terminal_echo(choice):
                return []
            raise LLMTransportContractError(
                "chat transport emitted semantic data after its terminal",
                reason_code="transport_terminal_followed_by_event",
            )
        events: list[ProviderStreamPayload] = []
        delta = choice.get("delta")
        if delta is not None and not isinstance(delta, dict):
            raise LLMTransportContractError(
                "chat delta is not an object",
                reason_code="transport_chat_delta_contract_invalid",
            )
        if isinstance(delta, dict):
            replay_contracts = {
                item.field_name: item
                for item in self.provider_profile.chat_replay_fields
            }
            live_thinking_fields = frozenset(
                self.provider_profile.thinking.delta_fields
            )
            allowed = {
                "role",
                "content",
                "tool_calls",
                *replay_contracts,
                *live_thinking_fields,
            }
            unknown = set(delta).difference(allowed)
            if unknown:
                raise LLMTransportContractError(
                    "chat delta contains an unsupported field",
                    reason_code="transport_chat_delta_contract_invalid",
                )
            role = delta.get("role")
            if role is not None and role != "assistant":
                raise LLMTransportContractError(
                    "chat delta changed the assistant role",
                    reason_code="transport_chat_delta_contract_invalid",
                )
            if "content" in delta:
                content = delta["content"]
                if content is not None and not isinstance(content, str):
                    raise LLMTransportContractError(
                        "chat content delta is not text",
                        reason_code="transport_chat_delta_contract_invalid",
                    )
                self._content_observed = True
                if isinstance(content, str):
                    self._text_parts.append(content)
                    events.extend(self.builder.text_delta(content))
            projected_fields = tuple(
                dict.fromkeys((*replay_contracts, *sorted(live_thinking_fields)))
            )
            for field_name in projected_fields:
                if field_name not in delta:
                    continue
                value = delta[field_name]
                # OpenAI-compatible Chat streams commonly keep a configured
                # reasoning field present with JSON null on the role/tool or
                # finish chunk.  Null is a closed "no fragment in this
                # delta" value: it neither becomes an empty replay carrier nor
                # erases previously accumulated bytes.  A selected completed
                # response still fails below when no non-null required carrier
                # was ever observed.
                if value is None:
                    continue
                contract = replay_contracts.get(field_name)
                if contract is not None:
                    self._accumulate_field(
                        field_name, contract.accumulation_mode, value
                    )
                if field_name in live_thinking_fields:
                    if not isinstance(value, str):
                        raise LLMTransportContractError(
                            "chat live thinking delta is not text",
                            reason_code="transport_chat_replay_field_invalid",
                        )
                    events.extend(self.builder.thinking_delta(value))
            raw_tool_calls = delta.get("tool_calls")
            if raw_tool_calls is not None:
                if not isinstance(raw_tool_calls, list):
                    raise LLMTransportContractError(
                        "chat tool-call delta is not an array",
                        reason_code="transport_chat_delta_contract_invalid",
                    )
                for raw_tool_call in raw_tool_calls:
                    if not isinstance(raw_tool_call, dict):
                        raise LLMTransportContractError(
                            "chat tool-call delta is not an object",
                            reason_code="transport_chat_delta_contract_invalid",
                        )
                    events.extend(self.tool_calls.apply_tool_call_delta(raw_tool_call))

        finish_reason = choice.get("finish_reason")
        if finish_reason is None:
            return events
        if not isinstance(finish_reason, str):
            raise LLMTransportContractError(
                "chat finish reason is not a string",
                reason_code="transport_chat_finish_reason_invalid",
            )
        incomplete = {
            "length": ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT,
            "content_filter": ProviderOutputIncompleteReason.CONTENT_FILTERED,
        }.get(finish_reason)
        if finish_reason not in {"stop", "tool_calls"}:
            self._terminal_finish_reason = finish_reason
            self.terminal = ProviderAdapterTerminal(
                ProviderAdapterTerminalKind.OUTPUT_INCOMPLETE,
                incomplete_reason=(
                    incomplete
                    or ProviderOutputIncompleteReason.UNKNOWN_PROVIDER_INCOMPLETE
                ),
            )
            self._field_values.clear()
            return events

        events.extend(self.tool_calls.close_active_tool_calls())
        self._reconcile_final_message(choice.get("message"))
        events.extend(self.builder.close_active_blocks())
        replay = self._freeze_completed_replay()
        self._terminal_finish_reason = finish_reason
        self.terminal = ProviderAdapterTerminal(
            ProviderAdapterTerminalKind.COMPLETED,
            completed_replay_payload=replay,
        )
        return events

    def _is_exact_empty_terminal_echo(self, choice: dict[str, Any]) -> bool:
        """Recognize an idempotent Chat terminal carrier for any endpoint."""

        finish_reason = choice.get("finish_reason")
        if (
            not isinstance(finish_reason, str)
            or finish_reason != self._terminal_finish_reason
            or choice.get("message") is not None
        ):
            return False
        delta = choice.get("delta")
        if delta is None:
            delta = {}
        if not isinstance(delta, dict):
            return False
        replay_fields = {
            item.field_name for item in self.provider_profile.chat_replay_fields
        }
        allowed = {
            "role",
            "content",
            "tool_calls",
            *replay_fields,
            *self.provider_profile.thinking.delta_fields,
        }
        if set(delta).difference(allowed):
            return False
        if delta.get("role") not in (None, "assistant"):
            return False
        for field_name, value in delta.items():
            if field_name == "role":
                continue
            if value not in (None, "", []):
                return False
        return True

    def _reconcile_final_message(self, raw_message: object) -> None:
        selected = (
            self.provider_profile.reasoning_replay_scope
            is ProviderReasoningReplayScope.ALL_COMPLETED_RESPONSES
            or (
                self.provider_profile.reasoning_replay_scope
                is ProviderReasoningReplayScope.TOOL_RESPONSES
                and bool(self.tool_calls.completed_calls)
            )
        )
        contracts = self.provider_profile.chat_replay_fields
        if raw_message is None:
            if selected and any(item.final_value_required for item in contracts):
                raise LLMTransportContractError(
                    "completed chat response lacks its required final message",
                    reason_code="transport_chat_replay_field_missing",
                )
            return
        if not isinstance(raw_message, dict):
            raise LLMTransportContractError(
                "chat final message is not an object",
                reason_code="transport_chat_replay_field_invalid",
            )
        allowed = {
            "role",
            "content",
            "tool_calls",
            *(item.field_name for item in contracts),
        }
        if set(raw_message).difference(allowed):
            raise LLMTransportContractError(
                "chat final message contains an unsupported field",
                reason_code="transport_chat_replay_field_invalid",
            )
        if raw_message.get("role", "assistant") != "assistant":
            raise LLMTransportContractError(
                "chat final message changed the assistant role",
                reason_code="transport_chat_replay_field_invalid",
            )
        if "content" in raw_message:
            expected_content = (
                "".join(self._text_parts) if self._content_observed else None
            )
            if raw_message["content"] != expected_content:
                raise LLMTransportContractError(
                    "chat final content differs from streamed content",
                    reason_code="transport_chat_final_message_mismatch",
                )
        if "tool_calls" in raw_message and freeze_json(
            raw_message["tool_calls"]
        ) != freeze_json(list(self.tool_calls.completed_calls)):
            raise LLMTransportContractError(
                "chat final tool calls differ from streamed tool calls",
                reason_code="transport_chat_final_message_mismatch",
            )
        for contract in contracts:
            present = contract.field_name in raw_message
            if selected and contract.final_value_required and not present:
                raise LLMTransportContractError(
                    "chat final message lacks a required replay field",
                    reason_code="transport_chat_replay_field_missing",
                )
            if not present:
                continue
            raw_value = raw_message[contract.field_name]
            if contract.accumulation_mode is (
                ProviderChatFieldAccumulationMode.TEXT_CONCAT
            ):
                if not isinstance(raw_value, str):
                    raise LLMTransportContractError(
                        "chat final replay text field is not text",
                        reason_code="transport_chat_replay_field_invalid",
                    )
                accumulated = self._field_values.get(contract.field_name)
                if accumulated is None:
                    self._field_values[contract.field_name] = raw_value
                elif accumulated != raw_value:
                    raise LLMTransportContractError(
                        "chat final replay field differs from its deltas",
                        reason_code="transport_chat_replay_field_conflict",
                    )
                continue
            frozen = freeze_json(raw_value)
            if contract.accumulation_mode is (
                ProviderChatFieldAccumulationMode.ORDERED_ARRAY_APPEND
            ):
                if not isinstance(frozen, FrozenJsonArrayFact):
                    raise LLMTransportContractError(
                        "chat final replay append field is not an array",
                        reason_code="transport_chat_replay_field_invalid",
                    )
                final_value: FrozenJsonValue | list[FrozenJsonValue] = list(
                    frozen.items
                )
            else:
                final_value = frozen
            accumulated = self._field_values.get(contract.field_name)
            if accumulated is None:
                self._field_values[contract.field_name] = final_value
            elif accumulated != final_value:
                raise LLMTransportContractError(
                    "chat final replay field differs from its deltas",
                    reason_code="transport_chat_replay_field_conflict",
                )

    def finish(self) -> ProviderAdapterTerminal | ProviderStreamFailure:
        if self.terminal is None:
            self._field_values.clear()
            return ProviderStreamFailure(
                message="Chat stream ended before a finish reason.",
                code_hint="transport_protocol_error",
            )
        return self.terminal

    def _accumulate_field(
        self,
        field_name: str,
        mode: ProviderChatFieldAccumulationMode,
        raw_value: object,
    ) -> None:
        if mode is ProviderChatFieldAccumulationMode.TEXT_CONCAT:
            if not isinstance(raw_value, str):
                raise LLMTransportContractError(
                    "chat replay text field is not text",
                    reason_code="transport_chat_replay_field_invalid",
                )
            current = self._field_values.get(field_name, "")
            if not isinstance(current, str):
                raise AssertionError("chat replay accumulator shape drifted")
            self._field_values[field_name] = current + raw_value
            return
        frozen = freeze_json(raw_value)
        if mode is ProviderChatFieldAccumulationMode.SINGLE_EXACT_VALUE:
            if field_name in self._field_values and (
                self._field_values[field_name] != frozen
            ):
                raise LLMTransportContractError(
                    "chat replay field changed its exact value",
                    reason_code="transport_chat_replay_field_conflict",
                )
            self._field_values[field_name] = frozen
            return
        if not isinstance(frozen, FrozenJsonArrayFact):
            raise LLMTransportContractError(
                "chat replay append field is not an array",
                reason_code="transport_chat_replay_field_invalid",
            )
        current = self._field_values.setdefault(field_name, [])
        if not isinstance(current, list):
            raise AssertionError("chat replay accumulator shape drifted")
        current.extend(frozen.items)

    def _freeze_completed_replay(self):
        scope = self.provider_profile.reasoning_replay_scope
        selected = (
            scope is ProviderReasoningReplayScope.ALL_COMPLETED_RESPONSES
            or (
                scope is ProviderReasoningReplayScope.TOOL_RESPONSES
                and bool(self.tool_calls.completed_calls)
            )
        )
        if not selected:
            return None
        for contract in self.provider_profile.chat_replay_fields:
            if contract.required_on_selected_response and (
                contract.field_name not in self._field_values
            ):
                raise LLMTransportContractError(
                    "completed chat response lacks a required replay field",
                    reason_code="transport_chat_replay_field_missing",
                )
        message: dict[str, object] = {
            "role": "assistant",
            "content": "".join(self._text_parts) if self._content_observed else None,
        }
        if self.tool_calls.completed_calls:
            message["tool_calls"] = list(self.tool_calls.completed_calls)
        for contract in self.provider_profile.chat_replay_fields:
            if contract.field_name not in self._field_values:
                continue
            value = self._field_values[contract.field_name]
            if isinstance(value, list):
                message[contract.field_name] = [thaw_json(item) for item in value]
            elif isinstance(value, str):
                message[contract.field_name] = value
            else:
                message[contract.field_name] = thaw_json(value)
        frozen = freeze_json(message)
        if not isinstance(frozen, FrozenJsonObjectFact):
            raise AssertionError("chat replay message did not freeze as an object")
        return freeze_provider_adapter_completed_replay_payload(
            codec_kind=self.provider_profile.assistant_replay_codec_kind,
            ordered_items=(frozen,),
        )


@dataclass(slots=True)
class _ChatToolCallState:
    tool_call_id: str | None = None
    name: str = ""
    pending_arguments: list[str] = field(default_factory=list)
    started: bool = False


@dataclass(slots=True)
class ChatToolCallAccumulator:
    builder: ProviderLiveItemBuilder
    _states: dict[str, _ChatToolCallState] = field(default_factory=dict)
    completed_calls: tuple[dict[str, object], ...] = ()

    def apply_tool_call_delta(
        self, raw_tool_call: dict[str, Any]
    ) -> list[ProviderStreamPayload]:
        if set(raw_tool_call).difference({"index", "id", "type", "function"}):
            raise LLMTransportContractError(
                "chat tool-call delta contains unsupported fields",
                reason_code="transport_tool_call_contract_invalid",
            )
        raw_index = raw_tool_call.get("index", len(self._states))
        if (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or raw_index < 0
            or raw_index > 4095
        ):
            raise LLMTransportContractError(
                "chat tool-call index is invalid",
                reason_code="transport_tool_call_contract_invalid",
            )
        key = str(raw_index)
        if key not in self._states and raw_index != len(self._states):
            raise LLMTransportContractError(
                "chat tool-call indexes are not contiguous",
                reason_code="transport_tool_call_contract_invalid",
            )
        state = self._states.setdefault(key, _ChatToolCallState())
        raw_type = raw_tool_call.get("type")
        if raw_type is not None and raw_type != "function":
            raise LLMTransportContractError(
                "chat tool-call type is unsupported",
                reason_code="transport_tool_call_contract_invalid",
            )
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
        if function is not None and not isinstance(function, dict):
            raise LLMTransportContractError(
                "chat tool-call function is not an object",
                reason_code="transport_tool_call_contract_invalid",
            )
        if isinstance(function, dict):
            if set(function).difference({"name", "arguments"}):
                raise LLMTransportContractError(
                    "chat tool-call function contains unsupported fields",
                    reason_code="transport_tool_call_contract_invalid",
                )
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

        events: list[ProviderStreamPayload] = []
        if not state.started and state.tool_call_id and state.name:
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

    def close_active_tool_calls(self) -> list[ProviderStreamPayload]:
        if any(
            not state.started or not state.tool_call_id
            for state in self._states.values()
        ):
            raise LLMTransportContractError(
                "tool-call stream ended before a named tool-call start",
                reason_code="transport_tool_call_start_missing",
            )
        events: list[ProviderStreamPayload] = []
        completed: list[dict[str, object]] = []
        for state in self._states.values():
            assert state.tool_call_id is not None
            arguments = (
                "".join(
                    self.builder.tool_call_argument_parts.get(
                        state.tool_call_id, ()
                    )
                )
                or "{}"
            )
            try:
                decoded_arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise LLMTransportContractError(
                    "chat tool-call arguments are not complete JSON",
                    reason_code="transport_tool_arguments_invalid",
                ) from exc
            if not isinstance(decoded_arguments, dict):
                raise LLMTransportContractError(
                    "chat tool-call arguments are not a JSON object",
                    reason_code="transport_tool_arguments_invalid",
                )
            events.extend(self.builder.tool_call_end(tool_call_id=state.tool_call_id))
            completed.append(
                {
                    "id": state.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": state.name,
                        "arguments": arguments,
                    },
                }
            )
        self.completed_calls = tuple(completed)
        self._states.clear()
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
            payload["tool_calls"] = [
                _tool_call_to_chat_tool_call(call) for call in message.tool_calls
            ]
        return payload
    return {
        "role": _chat_role(message.role),
        "content": "\n".join(message.content),
    }


def _chat_role(role: MessageRole) -> str:
    if role in {MessageRole.USER, MessageRole.ASSISTANT}:
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
