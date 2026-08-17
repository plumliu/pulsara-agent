"""OpenAI Responses translation to adapter-private raw items."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
import json
from typing import Any, AsyncIterator

from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_RESPONSES_API,
    OpenAITransportTimeoutPolicy,
    build_async_openai_client,
)
from pulsara_agent.llm.adapters.openai.errors import classify_llm_error
from pulsara_agent.llm.adapters.openai.events import (
    ProviderLiveItemBuilder,
    ReportedModelIdentityObserver,
    arguments_to_json_string,
    sdk_event_to_dict,
    responses_reported_model,
    transport_usage_report_from_mapping,
)
from pulsara_agent.llm.adapters.openai.retrying import (
    build_provider_retry_summary,
    log_retry_attempt,
    make_retry_trace,
    provider_failure_code_hint,
    sdk_max_retries_for_transport,
)
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.provider import (
    ProviderAssistantReplayCodecKind,
    mutable_provider_value,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.provider_stream import (
    ProviderAdapterStreamItem,
    ProviderAdapterTerminal,
    ProviderAdapterTerminalKind,
    ProviderOutputIncompleteReason,
    ProviderStreamFailure,
    freeze_provider_adapter_completed_replay_payload,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
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


RESPONSES_REPLAYABLE_OUTPUT_ITEM_TYPES = frozenset(
    {"reasoning", "message", "function_call"}
)


@dataclass(slots=True)
class OpenAIResponsesTransport:
    """Adapter for OpenAI Responses-compatible APIs."""

    api_key: str
    timeout_policy: OpenAITransportTimeoutPolicy
    api: str = OPENAI_RESPONSES_API
    binding_id: str = "pulsara.openai.responses"
    contract_version: str = "v3-explicit-terminal-canonical-order"
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
    ) -> AsyncIterator[ProviderAdapterStreamItem]:
        model = call.target.model_profile
        if self._mock_events:
            model_identity = ReportedModelIdentityObserver(
                requested_model_id=model.id,
                policy=model.provider_profile.model_identity_policy,
            )
            accumulator = ResponsesCompletionAccumulator(
                builder=ProviderLiveItemBuilder()
            )
            for raw_event in self._mock_events:
                model_identity.observe(responses_reported_model(raw_event))
                for item in accumulator.apply(raw_event):
                    yield item
            report = _report_with_model_identity(
                accumulator.usage_report, model_identity.reported_model_id
            )
            if report is not None:
                yield report
            yield accumulator.finish()
            return

        payload = build_responses_payload(call=call, context=context)
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
                accumulator = ResponsesCompletionAccumulator(
                    builder=ProviderLiveItemBuilder()
                )
                try:
                    stream = await client.responses.create(**payload, stream=True)
                    async for raw_event in stream:
                        model_identity.observe(responses_reported_model(raw_event))
                        for item in accumulator.apply(raw_event):
                            yield item
                    completed_report = _report_with_model_identity(
                        accumulator.usage_report,
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
                    report = _report_with_model_identity(
                        accumulator.usage_report,
                        model_identity.reported_model_id,
                    )
                    if report is not None:
                        yield report
                    yield ProviderStreamFailure(
                        message=str(exc),
                        code_hint=(
                            exc.reason_code
                            if isinstance(exc, LLMTransportContractError)
                            else provider_failure_code_hint(decision)
                        ),
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

        if completed_report is not None:
            yield completed_report
        yield accumulator.finish()


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
    plan = context.provider_wire_input_plan
    if plan is not None:
        if plan.wire_api != OPENAI_RESPONSES_API:
            raise ValueError("provider wire plan API does not match Responses")
        root = thaw_json(plan.materialization.root_policy_value)
        if root is not None and not isinstance(root, str):
            raise TypeError("Responses root policy must be text or null")
        wire_input = _thaw_wire_objects(
            plan.materialization.ordered_input_items
        )
        planned_tools = _thaw_wire_objects(plan.materialization.tool_items)
    else:
        root = context.system_prompt
        wire_input = _messages_to_responses_inputs(context.messages)
        planned_tools = [_tool_to_responses_tool(tool) for tool in context.tools]
    # Manual full-history replay is the only correctness authority.  Keeping
    # Responses stateless also makes encrypted reasoning carriers observable
    # on providers that support zero-retention/manual-history operation; a
    # remote response ID is never needed or accepted by the Kernel.
    payload: dict[str, Any] = {
        "model": model.id,
        "input": wire_input,
        "store": False,
    }
    provider_profile = model.provider_profile
    for key, value in provider_profile.request_defaults.items():
        payload.setdefault(key, mutable_provider_value(value))
    if root:
        payload["instructions"] = root
    if planned_tools and provider_profile.supports_tools:
        payload["tools"] = planned_tools
    payload["max_output_tokens"] = call.target.context_budget.effective_output_tokens
    if options.reasoning_effort is not None:
        payload["reasoning"] = {"effort": options.reasoning_effort}
    if provider_profile.request_extra_body:
        payload["extra_body"] = mutable_provider_value(
            provider_profile.request_extra_body
        )
    return payload


def responses_semantic_wire_group(
    message: LLMMessage,
) -> tuple[dict[str, Any], ...]:
    return tuple(_message_to_responses_inputs(message))


def responses_tool_wire_items(
    tools: tuple[ToolSpec, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(_tool_to_responses_tool(tool) for tool in tools)


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


@dataclass(slots=True)
class ResponsesCompletionAccumulator:
    """Closed Responses V1 terminal and exact-output replay accumulator."""

    builder: ProviderLiveItemBuilder
    usage_report: TransportUsageReport | None = None
    terminal: ProviderAdapterTerminal | None = None
    failure: ProviderStreamFailure | None = None
    _text_parts: dict[int, list[str]] = field(default_factory=dict)
    _reasoning_summary_parts: dict[int, list[str]] = field(default_factory=dict)
    _reasoning_content_parts: dict[int, list[str]] = field(default_factory=dict)
    _text_done: set[int] = field(default_factory=set)
    _reasoning_summary_done: set[int] = field(default_factory=set)
    _reasoning_content_done: set[int] = field(default_factory=set)

    def apply(self, raw_event: Any) -> list[ProviderAdapterStreamItem]:
        event = sdk_event_to_dict(raw_event)
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise LLMTransportContractError(
                "Responses event lacks a closed type",
                reason_code="transport_responses_event_invalid",
            )
        if self.terminal is not None or self.failure is not None:
            raise LLMTransportContractError(
                "Responses emitted an event after its terminal",
                reason_code="transport_terminal_followed_by_event",
            )
        if event_type == "response.completed":
            response = event.get("response")
            if not isinstance(response, dict):
                raise LLMTransportContractError(
                    "completed Responses event lacks a response object",
                    reason_code="transport_responses_completed_invalid",
                )
            response_status = response.get("status")
            if response_status is not None and response_status != "completed":
                raise LLMTransportContractError(
                    "response.completed carried a non-completed response",
                    reason_code="transport_responses_completed_invalid",
                )
            events, output_items = _project_completed_response(
                response,
                builder=self.builder,
                streamed_text=self._text_parts,
                text_done=self._text_done,
                streamed_reasoning_summary=self._reasoning_summary_parts,
                reasoning_summary_done=self._reasoning_summary_done,
                streamed_reasoning_content=self._reasoning_content_parts,
                reasoning_content_done=self._reasoning_content_done,
            )
            self._adopt_usage(response)
            frozen_items: list[FrozenJsonObjectFact] = []
            for item in output_items:
                frozen = freeze_json(item)
                if not isinstance(frozen, FrozenJsonObjectFact):
                    raise AssertionError("Responses output item did not freeze")
                frozen_items.append(frozen)
            replay = freeze_provider_adapter_completed_replay_payload(
                codec_kind=(
                    ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS
                ),
                ordered_items=tuple(frozen_items),
            )
            self.terminal = ProviderAdapterTerminal(
                ProviderAdapterTerminalKind.COMPLETED,
                completed_replay_payload=replay,
            )
            return events
        if event_type == "response.incomplete":
            response = event.get("response")
            provider_data = response if isinstance(response, dict) else event
            self._adopt_usage(provider_data)
            reason = _responses_incomplete_reason(provider_data)
            self.terminal = ProviderAdapterTerminal(
                ProviderAdapterTerminalKind.OUTPUT_INCOMPLETE,
                incomplete_reason=reason,
            )
            return []
        if event_type in {"response.failed", "response.error", "error"}:
            response = event.get("response")
            provider_data = response if isinstance(response, dict) else event
            self._adopt_usage(provider_data)
            self.failure = ProviderStreamFailure(
                message=_response_error_message(provider_data),
                code_hint="provider_transport_error",
            )
            return []
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            if (
                not isinstance(item, dict)
                or item.get("type") not in RESPONSES_REPLAYABLE_OUTPUT_ITEM_TYPES
            ):
                raise LLMTransportContractError(
                    "Responses emitted an unsupported output item",
                    reason_code="transport_responses_output_type_unsupported",
                )
        if event_type == "response.output_text.delta":
            output_index = _responses_output_index(event)
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise LLMTransportContractError(
                    "Responses text delta is invalid",
                    reason_code="transport_responses_event_invalid",
                )
            self._text_parts.setdefault(output_index, []).append(delta)
        elif event_type == "response.output_text.done":
            output_index = _responses_output_index(event)
            final_text = event.get("text")
            if not isinstance(final_text, str) or final_text != "".join(
                self._text_parts.get(output_index, ())
            ):
                raise LLMTransportContractError(
                    "Responses text done differs from its delta prefix",
                    reason_code="transport_text_done_content_mismatch",
                )
            self._text_done.add(output_index)
        elif event_type == "response.reasoning_summary_text.delta":
            output_index = _responses_output_index(event)
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise LLMTransportContractError(
                    "Responses reasoning delta is invalid",
                    reason_code="transport_responses_event_invalid",
                )
            self._reasoning_summary_parts.setdefault(output_index, []).append(delta)
        elif event_type == "response.reasoning_summary_text.done":
            output_index = _responses_output_index(event)
            final_text = event.get("text")
            if not isinstance(final_text, str) or final_text != "".join(
                self._reasoning_summary_parts.get(output_index, ())
            ):
                raise LLMTransportContractError(
                    "Responses reasoning done differs from its delta prefix",
                    reason_code="transport_thinking_done_content_mismatch",
                )
            self._reasoning_summary_done.add(output_index)
        elif event_type == "response.reasoning_text.delta":
            output_index = _responses_output_index(event)
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise LLMTransportContractError(
                    "Responses reasoning delta is invalid",
                    reason_code="transport_responses_event_invalid",
                )
            self._reasoning_content_parts.setdefault(output_index, []).append(delta)
        elif event_type == "response.reasoning_text.done":
            output_index = _responses_output_index(event)
            final_text = event.get("text")
            if not isinstance(final_text, str) or final_text != "".join(
                self._reasoning_content_parts.get(output_index, ())
            ):
                raise LLMTransportContractError(
                    "Responses reasoning done differs from its delta prefix",
                    reason_code="transport_thinking_done_content_mismatch",
                )
            self._reasoning_content_done.add(output_index)
        return translate_responses_event(event, builder=self.builder)

    def finish(self) -> ProviderAdapterTerminal | ProviderStreamFailure:
        if self.failure is not None:
            return self.failure
        if self.terminal is not None:
            return self.terminal
        return ProviderStreamFailure(
            message="Responses stream ended before a response terminal.",
            code_hint="transport_protocol_error",
        )

    def _adopt_usage(self, response: dict[str, Any]) -> None:
        report = transport_usage_report_from_mapping(response.get("usage"))
        if report.usage_status != "reported":
            return
        if self.usage_report is not None:
            raise LLMTransportContractError(
                "transport emitted more than one usage report",
                reason_code="transport_usage_report_duplicate",
            )
        self.usage_report = report


def _responses_incomplete_reason(
    provider_data: dict[str, Any],
) -> ProviderOutputIncompleteReason:
    details = provider_data.get("incomplete_details")
    raw = details.get("reason") if isinstance(details, dict) else None
    aliases = {
        "max_output_tokens": ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT,
        "max_tokens": ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT,
        "model_context_window_exceeded": (
            ProviderOutputIncompleteReason.CONTEXT_WINDOW_LIMIT_DURING_GENERATION
        ),
        "context_length_exceeded": (
            ProviderOutputIncompleteReason.CONTEXT_WINDOW_LIMIT_DURING_GENERATION
        ),
        "content_filter": ProviderOutputIncompleteReason.CONTENT_FILTERED,
    }
    return aliases.get(
        raw, ProviderOutputIncompleteReason.UNKNOWN_PROVIDER_INCOMPLETE
    )


def _responses_output_index(event: dict[str, Any]) -> int:
    value = event.get("output_index")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LLMTransportContractError(
            "Responses semantic event lacks a bounded output index",
            reason_code="transport_responses_event_invalid",
        )
    return value


def _project_completed_response(
    response: dict[str, Any],
    *,
    builder: ProviderLiveItemBuilder,
    streamed_text: dict[int, list[str]],
    text_done: set[int],
    streamed_reasoning_summary: dict[int, list[str]],
    reasoning_summary_done: set[int],
    streamed_reasoning_content: dict[int, list[str]],
    reasoning_content_done: set[int],
) -> tuple[list[ProviderAdapterStreamItem], tuple[dict[str, Any], ...]]:
    output = response.get("output")
    if not isinstance(output, list) or not output:
        raise LLMTransportContractError(
            "completed Responses output is absent",
            reason_code="transport_responses_output_invalid",
        )
    if len(output) > 4096:
        raise LLMTransportContractError(
            "completed Responses output exceeds its item bound",
            reason_code="transport_source_item_limit_exceeded",
        )
    # The current canonical assistant row lowers one semantic TEXT group
    # followed by ordered TOOL_CALL blocks.  Admit only the exact Responses
    # subset which can survive canonical commit/read/lowering without changing
    # its public block order.  Hidden reasoning items do not affect that order.
    message_indexes = tuple(
        index
        for index, item in enumerate(output)
        if isinstance(item, dict) and item.get("type") == "message"
    )
    function_indexes = tuple(
        index
        for index, item in enumerate(output)
        if isinstance(item, dict) and item.get("type") == "function_call"
    )
    if len(message_indexes) > 1 or (
        message_indexes
        and any(index < message_indexes[0] for index in function_indexes)
    ):
        raise LLMTransportContractError(
            "Responses output order cannot round-trip through canonical blocks",
            reason_code="transport_responses_output_order_unrepresentable",
        )
    events: list[ProviderAdapterStreamItem] = []
    frozen_source: list[dict[str, Any]] = []
    final_text_indexes: set[int] = set()
    final_reasoning_indexes: set[int] = set()
    for output_index, raw_item in enumerate(output):
        if not isinstance(raw_item, dict):
            raise LLMTransportContractError(
                "Responses output item is not an object",
                reason_code="transport_responses_output_invalid",
            )
        item = dict(raw_item)
        item_type = item.get("type")
        if item_type == "reasoning":
            final_reasoning_indexes.add(output_index)
            _validate_response_item_keys(
                item,
                {
                    "type",
                    "id",
                    "status",
                    "summary",
                    "content",
                    "encrypted_content",
                    "format",
                },
            )
            _validate_optional_output_identity(item)
            reasoning_format = item.get("format")
            if reasoning_format is not None and reasoning_format != (
                "openai-responses-v1"
            ):
                raise LLMTransportContractError(
                    "Responses reasoning format is unsupported",
                    reason_code="transport_responses_output_invalid",
                )
            summary_text = _reasoning_summary_text(item)
            content_text = _reasoning_content_text(item)
            encrypted = item.get("encrypted_content")
            if encrypted is not None and not isinstance(encrypted, str):
                raise LLMTransportContractError(
                    "Responses encrypted reasoning carrier is not text",
                    reason_code="transport_responses_output_invalid",
                )
            streamed_summary = "".join(
                streamed_reasoning_summary.get(output_index, ())
            )
            if output_index in reasoning_summary_done or streamed_summary:
                if summary_text != streamed_summary:
                    raise LLMTransportContractError(
                        "final Responses reasoning summary differs from the stream",
                        reason_code="transport_responses_output_mismatch",
                    )
            elif summary_text:
                events.extend(builder.thinking_end(final_text=summary_text))
            streamed_content = "".join(
                streamed_reasoning_content.get(output_index, ())
            )
            if output_index in reasoning_content_done or streamed_content:
                if content_text != streamed_content:
                    raise LLMTransportContractError(
                        "final Responses reasoning content differs from the stream",
                        reason_code="transport_responses_output_mismatch",
                    )
        elif item_type == "message":
            final_text_indexes.add(output_index)
            _validate_response_item_keys(
                item, {"type", "id", "status", "role", "content", "phase"}
            )
            _validate_optional_output_identity(item)
            if item.get("role") != "assistant":
                raise LLMTransportContractError(
                    "Responses message output changed the assistant role",
                    reason_code="transport_responses_output_invalid",
                )
            phase = item.get("phase")
            if phase is not None and phase != "final_answer":
                raise LLMTransportContractError(
                    "Responses message phase is unsupported",
                    reason_code="transport_responses_output_invalid",
                )
            text = _response_message_text(item)
            streamed = "".join(streamed_text.get(output_index, ()))
            if output_index in text_done or streamed:
                if text != streamed:
                    raise LLMTransportContractError(
                        "final Responses message differs from the stream",
                        reason_code="transport_responses_output_mismatch",
                    )
            else:
                events.extend(builder.text_end(final_text=text))
        elif item_type == "function_call":
            _validate_response_item_keys(
                item,
                {"type", "id", "status", "call_id", "name", "arguments"},
            )
            _validate_optional_output_identity(item)
            provider_item_id = str(item.get("id") or "")
            tool_call_id = builder.resolve_completed_tool_call_id(
                provider_item_id=provider_item_id,
                tool_call_id=str(item.get("call_id") or ""),
            )
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise LLMTransportContractError(
                    "Responses function call lacks a name",
                    reason_code="transport_tool_call_identity_missing",
                )
            raw_arguments = item.get("arguments")
            if not isinstance(raw_arguments, str):
                raise LLMTransportContractError(
                    "Responses function call arguments are not exact text",
                    reason_code="transport_responses_output_invalid",
                )
            try:
                decoded_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise LLMTransportContractError(
                    "Responses function call arguments are not complete JSON",
                    reason_code="transport_tool_arguments_invalid",
                ) from exc
            if not isinstance(decoded_arguments, dict):
                raise LLMTransportContractError(
                    "Responses function call arguments are not a JSON object",
                    reason_code="transport_tool_arguments_invalid",
                )
            # A completed Responses output item is also the exact replay
            # carrier.  Do not canonicalize a mapping into text here: the
            # provider's final string is both the projection source and the
            # byte-for-byte value sent in the next manual-history request.
            arguments = raw_arguments
            if (
                tool_call_id in builder.tool_call_names
                and tool_call_id not in builder.active_tool_call_ids
            ):
                completed_arguments = (
                    "".join(builder.tool_call_argument_parts.get(tool_call_id, ()))
                    or "{}"
                )
                if (
                    builder.tool_call_names[tool_call_id] != name
                    or completed_arguments != arguments
                ):
                    raise LLMTransportContractError(
                        "final Responses function call differs from item.done",
                        reason_code="transport_responses_output_mismatch",
                    )
            else:
                events.extend(
                    builder.tool_call_start(
                        tool_call_id=tool_call_id,
                        tool_call_name=name,
                        provider_item_id=provider_item_id,
                    )
                )
                events.extend(
                    builder.reconcile_tool_call_arguments(
                        tool_call_id=tool_call_id,
                        final_arguments=arguments,
                    )
                )
                events.extend(builder.tool_call_end(tool_call_id=tool_call_id))
        else:
            raise LLMTransportContractError(
                "Responses output contains an unsupported item type",
                reason_code="transport_responses_output_type_unsupported",
            )
        frozen_source.append(item)
    if set(streamed_text).union(text_done) != final_text_indexes.intersection(
        set(streamed_text).union(text_done)
    ):
        raise LLMTransportContractError(
            "streamed Responses text lacks an exact final output item",
            reason_code="transport_responses_output_mismatch",
        )
    if set(streamed_reasoning_summary).union(reasoning_summary_done) != (
        final_reasoning_indexes.intersection(
            set(streamed_reasoning_summary).union(reasoning_summary_done)
        )
    ):
        raise LLMTransportContractError(
            "streamed Responses reasoning lacks an exact final output item",
            reason_code="transport_responses_output_mismatch",
        )
    if set(streamed_reasoning_content).union(reasoning_content_done) != (
        final_reasoning_indexes.intersection(
            set(streamed_reasoning_content).union(reasoning_content_done)
        )
    ):
        raise LLMTransportContractError(
            "streamed Responses reasoning lacks an exact final output item",
            reason_code="transport_responses_output_mismatch",
        )
    events.extend(builder.close_active_blocks())
    return events, tuple(frozen_source)


def _validate_response_item_keys(
    item: dict[str, Any], allowed: set[str]
) -> None:
    if set(item).difference(allowed):
        raise LLMTransportContractError(
            "Responses output item contains unsupported fields",
            reason_code="transport_responses_output_invalid",
        )


def _validate_optional_output_identity(item: dict[str, Any]) -> None:
    item_id = item.get("id")
    if item_id is not None and (not isinstance(item_id, str) or not item_id):
        raise LLMTransportContractError(
            "Responses output item ID is invalid",
            reason_code="transport_responses_output_invalid",
        )
    status = item.get("status")
    if status is not None and status != "completed":
        raise LLMTransportContractError(
            "completed Responses output contains a non-completed item",
            reason_code="transport_responses_output_invalid",
        )


def _response_message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        raise LLMTransportContractError(
            "Responses message content is not an array",
            reason_code="transport_responses_output_invalid",
        )
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {
            "output_text",
            "text",
        }:
            raise LLMTransportContractError(
                "Responses message contains unsupported content",
                reason_code="transport_responses_content_unsupported",
            )
        if set(block).difference({"type", "text", "annotations", "logprobs"}):
            raise LLMTransportContractError(
                "Responses message content contains unsupported fields",
                reason_code="transport_responses_output_invalid",
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise LLMTransportContractError(
                "Responses message text is invalid",
                reason_code="transport_responses_output_invalid",
            )
        parts.append(text)
    return "".join(parts)


def _reasoning_summary_text(item: dict[str, Any]) -> str:
    summary = item.get("summary")
    if summary is None:
        return ""
    if not isinstance(summary, list):
        raise LLMTransportContractError(
            "Responses reasoning summary is invalid",
            reason_code="transport_responses_output_invalid",
        )
    parts: list[str] = []
    for block in summary:
        if not isinstance(block, dict) or block.get("type") not in {
            "summary_text",
            "output_text",
        }:
            raise LLMTransportContractError(
                "Responses reasoning summary contains unsupported content",
                reason_code="transport_responses_output_invalid",
            )
        if set(block).difference({"type", "text"}):
            raise LLMTransportContractError(
                "Responses reasoning summary contains unsupported fields",
                reason_code="transport_responses_output_invalid",
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise LLMTransportContractError(
                "Responses reasoning summary text is invalid",
                reason_code="transport_responses_output_invalid",
            )
        parts.append(text)
    return "".join(parts)


def _reasoning_content_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if content is None:
        return ""
    if not isinstance(content, list):
        raise LLMTransportContractError(
            "Responses reasoning content is invalid",
            reason_code="transport_responses_output_invalid",
        )
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "reasoning_text":
            raise LLMTransportContractError(
                "Responses reasoning content contains an unsupported block",
                reason_code="transport_responses_content_unsupported",
            )
        if set(block).difference({"type", "text"}):
            raise LLMTransportContractError(
                "Responses reasoning content contains unsupported fields",
                reason_code="transport_responses_output_invalid",
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise LLMTransportContractError(
                "Responses reasoning content text is invalid",
                reason_code="transport_responses_output_invalid",
            )
        parts.append(text)
    return "".join(parts)


def translate_responses_event(
    raw_event: Any,
    *,
    builder: ProviderLiveItemBuilder,
) -> list[ProviderAdapterStreamItem]:
    event = sdk_event_to_dict(raw_event)
    event_type = event.get("type")
    if event_type == "response.output_text.delta":
        return builder.text_delta(str(event.get("delta", "")))
    if event_type == "response.output_text.done":
        final_text = event.get("text")
        return builder.text_end(
            final_text=final_text if isinstance(final_text, str) else None
        )
    if event_type in {
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    }:
        return builder.thinking_delta(str(event.get("delta", "")))
    if event_type in {
        "response.reasoning_summary_text.done",
        "response.reasoning_text.done",
    }:
        final_text = event.get("text")
        return builder.thinking_end(
            final_text=final_text if isinstance(final_text, str) else None
        )
    if event_type == "response.output_item.added":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            provider_item_id = str(item.get("id") or "")
            tool_call_id = str(item.get("call_id") or item.get("id") or "")
            if not tool_call_id:
                raise LLMTransportContractError(
                    "provider function call is missing a stable identity",
                    reason_code="transport_tool_call_identity_missing",
                )
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
            tool_call_id = builder.resolve_completed_tool_call_id(
                provider_item_id=provider_item_id,
                tool_call_id=str(item.get("call_id") or ""),
            )
            events = builder.tool_call_start(
                tool_call_id=tool_call_id,
                tool_call_name=str(item.get("name") or ""),
                provider_item_id=provider_item_id,
            )
            arguments = item.get("arguments")
            if arguments is not None:
                events.extend(
                    builder.reconcile_tool_call_arguments(
                        tool_call_id=tool_call_id,
                        final_arguments=arguments_to_json_string(arguments),
                    )
                )
            events.extend(builder.tool_call_end(tool_call_id=tool_call_id))
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
    role = message.role.value
    return {
        "role": role,
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
