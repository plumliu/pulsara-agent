"""OpenAI Responses protocol translation to Pulsara AgentEvent."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator
from uuid import uuid4

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_RESPONSES_API,
    build_async_openai_client,
    provider_error_data,
)
from pulsara_agent.llm.adapters.openai.errors import classify_llm_error
from pulsara_agent.llm.adapters.openai.events import (
    AgentEventBuilder,
    ReportedModelIdentityObserver,
    arguments_to_json_string,
    event_includes_run_error,
    sdk_event_to_dict,
    responses_reported_model,
    transport_usage_report_from_mapping,
)
from pulsara_agent.llm.adapters.openai.retrying import (
    log_retry_attempt,
    make_retry_trace,
    provider_data_with_retry,
    retry_event,
    sdk_max_retries_for_transport,
)
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.provider import mutable_provider_value
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.retry import (
    LLMRetryConfig,
    RetryAttemptTrace,
    RetryDecisionKind,
    apply_retry_after_cap,
    compute_retry_delay,
)
from pulsara_agent.llm.transport import LLMTransport


@dataclass(slots=True)
class OpenAIResponsesTransport(LLMTransport):
    """Adapter for OpenAI Responses-compatible APIs."""

    api_key: str
    api: str = OPENAI_RESPONSES_API
    binding_id: str = "pulsara.openai.responses"
    contract_version: str = "v1"
    timeout_seconds: float = 60.0
    retry_config: LLMRetryConfig = field(default_factory=LLMRetryConfig)
    openai_sdk_max_retries: int | None = None
    retry_sleep: Callable[[float], Awaitable[None]] = field(
        default=asyncio.sleep, repr=False
    )
    _mock_events: list[dict[str, Any]] = field(default_factory=list)
    _client: Any | None = None

    async def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent | TransportUsageReport]:
        model = call.target.model_profile
        builder = AgentEventBuilder(
            event_context=event_context,
        )
        if self._mock_events:
            model_identity = ReportedModelIdentityObserver(
                requested_model_id=model.id,
                policy=model.provider_profile.model_identity_policy,
            )
            usage_report: TransportUsageReport | None = None
            for raw_event in self._mock_events:
                model_identity.observe(responses_reported_model(raw_event))
                items = translate_responses_event(raw_event, builder=builder)
                events = [item for item in items if isinstance(item, AgentEvent)]
                run_error_emitted = event_includes_run_error(events)
                for item in items:
                    if isinstance(item, TransportUsageReport):
                        if usage_report is not None:
                            raise LLMTransportContractError(
                                "transport emitted more than one usage report",
                                reason_code="transport_usage_report_duplicate",
                            )
                        usage_report = item
                    else:
                        yield item
                if run_error_emitted:
                    report = _report_with_model_identity(
                        usage_report, model_identity.reported_model_id
                    )
                    if report is not None:
                        yield report
                    return
            for event in builder.close_active_blocks():
                yield event
            report = _report_with_model_identity(
                usage_report, model_identity.reported_model_id
            )
            if report is not None:
                yield report
            return

        payload = build_responses_payload(call=call, context=context)
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
        completed_report: TransportUsageReport | None = None
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
                attempt_usage_report: TransportUsageReport | None = None
                try:
                    stream = await client.responses.create(**payload, stream=True)
                    async for raw_event in stream:
                        model_identity.observe(responses_reported_model(raw_event))
                        items = translate_responses_event(raw_event, builder=builder)
                        events = [
                            item for item in items if isinstance(item, AgentEvent)
                        ]
                        run_error_emitted = event_includes_run_error(events)
                        for item in items:
                            if isinstance(item, TransportUsageReport):
                                if attempt_usage_report is not None:
                                    raise LLMTransportContractError(
                                        "transport emitted more than one usage report",
                                        reason_code="transport_usage_report_duplicate",
                                    )
                                attempt_usage_report = item
                            else:
                                yield item
                        if run_error_emitted:
                            report = _report_with_model_identity(
                                attempt_usage_report,
                                model_identity.reported_model_id,
                            )
                            if report is not None:
                                yield report
                            return
                    completed_report = _report_with_model_identity(
                        attempt_usage_report,
                        model_identity.reported_model_id,
                    )
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
                        yield retry_event(
                            api=self.api,
                            model=model,
                            event_context=event_context,
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
                    for event in builder.close_active_blocks():
                        yield event
                    report = _report_with_model_identity(
                        attempt_usage_report,
                        model_identity.reported_model_id,
                    )
                    if report is not None:
                        yield report
                    yield builder.run_error(
                        message=str(exc),
                        code="openai_responses_error",
                        provider_data=provider_data_with_retry(
                            provider_error_data(exc),
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

        for event in builder.close_active_blocks():
            yield event
        if completed_report is not None:
            yield completed_report


def _report_with_model_identity(
    report: TransportUsageReport | None,
    reported_model_id: str | None,
) -> TransportUsageReport | None:
    if report is None and reported_model_id is None:
        return None
    return replace(
        report or TransportUsageReport(usage_status="missing", usage=None),
        reported_model_id=reported_model_id,
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


def build_responses_payload(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> dict[str, Any]:
    model = call.target.model_profile
    options = call.target.effective_options
    payload: dict[str, Any] = {
        "model": model.id,
        "input": _messages_to_responses_inputs(context.messages),
    }
    provider_profile = model.provider_profile
    for key, value in provider_profile.request_defaults.items():
        payload.setdefault(key, mutable_provider_value(value))
    if context.system_prompt:
        payload["instructions"] = context.system_prompt
    if context.tools and provider_profile.supports_tools:
        payload["tools"] = [_tool_to_responses_tool(tool) for tool in context.tools]
    payload["max_output_tokens"] = call.target.context_budget.effective_output_tokens
    if options.reasoning_effort is not None:
        payload["reasoning"] = {"effort": options.reasoning_effort}
    if provider_profile.request_extra_body:
        payload["extra_body"] = mutable_provider_value(
            provider_profile.request_extra_body
        )
    return payload


def response_to_agent_events(
    *,
    response: dict[str, Any],
    builder: AgentEventBuilder,
) -> list[AgentEvent | TransportUsageReport]:
    events: list[AgentEvent | TransportUsageReport] = []
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
    events.append(_usage(response))
    return events


def translate_responses_event(
    raw_event: Any,
    *,
    builder: AgentEventBuilder,
) -> list[AgentEvent | TransportUsageReport]:
    event = sdk_event_to_dict(raw_event)
    event_type = event.get("type")
    if event_type == "response.output_text.delta":
        return builder.text_delta(str(event.get("delta", "")))
    if event_type in {
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    }:
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
        events.append(_usage(response if isinstance(response, dict) else {}))
        return events
    if event_type in {
        "response.failed",
        "response.error",
        "response.incomplete",
        "error",
    }:
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


def _messages_to_responses_inputs(
    messages: tuple[LLMMessage, ...],
) -> list[dict[str, Any]]:
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
            raise ValueError(
                "Responses function_call_output input requires tool_call_id/call_id"
            )
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
        inputs.extend(
            _tool_call_to_responses_input(tool_call) for tool_call in message.tool_calls
        )
        return inputs
    return [_textual_responses_input(message)]


def _textual_responses_input(message: LLMMessage) -> dict[str, Any]:
    return {
        "role": message.role.value,
        # Use Responses' EasyInputMessage string form for maximum compatibility
        # with OpenAI-compatible gateways. Some gateways parse prior assistant
        # messages incorrectly when they are sent as input_text content parts.
        "content": "\n".join(message.content),
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
                    "arguments": arguments_to_json_string(
                        item.get("arguments") or "{}"
                    ),
                }
            )
    return calls


def _iter_output_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output")
    if not isinstance(output, list):
        return []
    return [item for item in output if isinstance(item, dict)]


def _usage(response: dict[str, Any]) -> TransportUsageReport:
    return transport_usage_report_from_mapping(response.get("usage"))


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
