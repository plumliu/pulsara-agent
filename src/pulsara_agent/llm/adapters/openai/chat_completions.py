"""OpenAI Chat Completions translation to adapter-private raw items."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
import json
import random
import sys
from typing import Any, AsyncIterator

from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_CHAT_COMPLETIONS_API,
    OpenAITransportTimeoutPolicy,
    admit_provider_request,
    build_async_openai_client,
    prepare_openai_sdk,
    openai_auth_request_options,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.llm.adapters.openai.errors import classify_llm_error
from pulsara_agent.llm.adapters.openai.events import (
    ProviderLiveItemBuilder,
    ReportedModelIdentityObserver,
    chat_completion_reported_model,
    sdk_event_to_dict,
    transport_usage_report_from_mapping,
    merge_response_usage_reports,
)
from pulsara_agent.llm.adapters.openai.function_tools import (
    openai_chat_function_tool,
)
from pulsara_agent.llm.adapters.openai.retrying import (
    build_provider_retry_summary,
    make_retry_trace,
    provider_failure_code_hint,
    provider_failure_message,
    log_invalid_tool_arguments,
    sdk_max_retries_for_transport,
    provider_diagnostic_await,
    capture_retry_scrubber,
    closed_sdk_stream,
    close_sdk_resource,
    _SDKStreamClosure,
    PhysicalStreamCloseError,
    replay_is_safe,
    log_provider_decision,
    retry_reason,
    observe_retry,
)
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.provider_replay import chat_reasoning_detail_parts
from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    LLMToolCall,
    MessageRole,
    ToolSpec,
    join_text_content,
)
from pulsara_agent.llm.provider import (
    CHAT_CLOSED_REASONING_FIELD_CONTRACTS,
    ProviderChatFieldAccumulationMode,
    RouteWireProfile,
    mutable_provider_value,
)
from pulsara_agent.llm.validation import validate_model_context_shape_for_call
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall, resolve_wire_output_tokens
from pulsara_agent.llm.model_target import reasoning_wire_fields
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.stream_limits import (
    MAX_CHAT_REASONING_REPLAY_AGGREGATE_BYTES,
)
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.ports.provider_stream import (
    ProviderAdapterStreamItem,
    ProviderAdapterTerminal,
    ProviderAdapterTerminalKind,
    ProviderOutputIncompleteReason,
    ProviderStreamFailure,
    ProviderRetryUpdate,
    ProviderRetryPhase,
    ProviderGenerationRetry,
    freeze_provider_adapter_completed_replay_payload,
)
from pulsara_agent.primitives.context import (
    FrozenJsonArrayFact,
    FrozenJsonObjectFact,
    FrozenJsonValue,
    canonical_json_bytes,
    freeze_json,
    thaw_json,
)
from pulsara_agent.llm.retry import (
    LLMRetryConfig,
    LLMRetryDecision,
    RetryAttemptTrace,
    RetryDecisionKind,
    apply_retry_after_cap,
    compute_retry_delay,
    retry_config_for_purpose,
)
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.primitives.model_call import ModelCallPurpose


_CHAT_LIVE_THINKING_FIELDS = frozenset(
    item.field_name
    for item in CHAT_CLOSED_REASONING_FIELD_CONTRACTS
    if item.accumulation_mode is ProviderChatFieldAccumulationMode.TEXT_CONCAT
)


@dataclass(slots=True)
class OpenAIChatCompletionsTransport:
    """Adapter for OpenAI Chat Completions-compatible APIs."""

    settings: LocalSettingsStore = field(repr=False)
    timeout_policy: OpenAITransportTimeoutPolicy
    api: str = OPENAI_CHAT_COMPLETIONS_API
    binding_id: str = "pulsara.openai.chat_completions"
    contract_version: str = "v8-connection-reasoning-profiles-and-tool-correlation"
    retry_config: LLMRetryConfig | None = None
    retry_sleep: Callable[[float], Awaitable[None]] = field(
        default=asyncio.sleep, repr=False
    )
    _mock_chunks: list[dict[str, Any]] = field(default_factory=list)
    _client: Any | None = None
    retry_rng: random.Random | None = field(default=None, repr=False)

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
                policy=model.route_wire_profile.model_identity_policy,
            )
            accumulator = ChatCompletionAccumulator(
                builder=ProviderLiveItemBuilder(),
                route_wire_profile=model.route_wire_profile,
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
        retry_config = retry_config_for_purpose(self.retry_config, call.fact.purpose)
        agent_loop = call.fact.purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        safe_to_replay = replay_is_safe(api=self.api, payload=payload)
        # Freeze saved settings and credentials once for this admitted logical call.
        saved_settings = self.settings.read()
        should_close_client = self._client is None
        api_key = (
            saved_settings.require_model_api_key(call.binding.connection_id)
            if call.target.connection.requires_api_key
            else None
        )
        credential_boundary = ProcessCredentialBoundary(api_key or "")
        error_scrubber = capture_retry_scrubber(
            saved_settings, credential_boundary, model.base_url
        )
        if context.provider_error_scrubber_observer is not None:
            context.provider_error_scrubber_observer(error_scrubber.scrub_text)
        if self._client is None:
            await prepare_openai_sdk()
            client = build_async_openai_client(
                api_key=api_key,
                base_url=model.base_url,
                timeout_policy=self.timeout_policy,
                credential_boundary=credential_boundary,
                max_retries=sdk_max_retries_for_transport(
                    retry_config=retry_config, purpose=call.fact.purpose
                ),
            )
        else:
            client = self._client
        retry_traces: list[RetryAttemptTrace] = []
        attempt = 1
        max_attempts = retry_config.attempts if retry_config.enabled else 1
        pending_failure = None
        pending_exception = None
        final_decision = None
        terminal = None
        try:
            while True:
                model_identity = ReportedModelIdentityObserver(
                    requested_model_id=model.id,
                    policy=model.route_wire_profile.model_identity_policy,
                )
                accumulator = ChatCompletionAccumulator(
                    builder=ProviderLiveItemBuilder(),
                    route_wire_profile=model.route_wire_profile,
                )
                phase = "opening"
                stream_closed = True  # no physical stream exists until open succeeds
                closure = None
                try:
                    stream = await provider_diagnostic_await(
                        admit_provider_request(
                            credential_boundary=credential_boundary,
                            payload=payload,
                            operation=lambda: client.chat.completions.create(
                                **payload,
                                stream=True,
                                **openai_auth_request_options(
                                    requires_api_key=call.target.connection.requires_api_key
                                ),
                            ),
                        ),
                        error_scrubber,
                    )
                    stream_closed = False
                    phase = "streaming"
                    async with closed_sdk_stream(
                        stream, scrub=error_scrubber
                    ) as closure:
                        while True:
                            try:
                                raw_item = await provider_diagnostic_await(
                                    anext(stream), error_scrubber
                                )
                            except StopAsyncIteration:
                                break
                            model_identity.observe(
                                chat_completion_reported_model(raw_item)
                            )
                            for item in accumulator.apply(raw_item):
                                if accumulator.builder.has_semantic_output:
                                    observe_retry(context, None)
                                yield item
                        phase = "closing"
                    stream_closed = True
                    break
                except asyncio.CancelledError as exc:
                    stream_closed = stream_closed or (
                        closure is not None and closure.closed
                    )
                    if closure is not None and closure.closing:
                        phase = "closing"
                    log_provider_decision(
                        api=self.api,
                        model=model,
                        scrub=error_scrubber,
                        config=retry_config,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        phase=phase,
                        exc=exc,
                        decision=None,
                        skipped_reason="cancelled",
                        semantic=accumulator.builder.has_semantic_output,
                        terminal=accumulator.terminal is not None,
                        closed=stream_closed,
                        outcome="cancelled",
                    )
                    raise
                except Exception as exc:
                    close_failed = isinstance(exc, PhysicalStreamCloseError)
                    # A raised iteration exception has already joined SDK close.
                    stream_closed = not close_failed
                    regenerate = (
                        agent_loop
                        and stream_closed
                        and isinstance(exc, LLMTransportContractError)
                        and exc.reason_code == "transport_tool_arguments_invalid"
                    )
                    if regenerate:
                        log_invalid_tool_arguments(
                            scrub=error_scrubber,
                            attempt=attempt,
                            arguments={
                                key: "".join(parts)
                                for key, parts in accumulator.builder.tool_call_argument_parts.items()
                            },
                            terminal_chunk=sdk_event_to_dict(raw_item),
                            exc=exc,
                        )
                    decision = apply_retry_after_cap(
                        LLMRetryDecision(
                            kind=RetryDecisionKind.DO_NOT_RETRY,
                            reason="completed_stream_close_failed"
                            if accumulator.terminal is not None
                            else "physical_close_unconfirmed",
                        )
                        if close_failed
                        else LLMRetryDecision(
                            kind=RetryDecisionKind.RETRY,
                            reason="tool_arguments_invalid",
                        )
                        if regenerate
                        else classify_llm_error(exc),
                        config=retry_config,
                    )
                    skipped_reason = (
                        decision.reason
                        if close_failed
                        else "terminal_already_observed"
                        if accumulator.terminal is not None
                        else "retry_disabled"
                        if not retry_config.enabled
                        else decision.reason
                        if decision.kind is not RetryDecisionKind.RETRY
                        else "unsafe_to_replay"
                        if agent_loop and not safe_to_replay
                        else "semantic_output_started"
                        if not agent_loop and accumulator.builder.has_semantic_output
                        else "attempts_exhausted"
                        if attempt >= max_attempts
                        else None
                    )
                    can_retry = skipped_reason is None
                    delay = (
                        compute_retry_delay(
                            attempt_index=attempt,
                            config=retry_config,
                            retry_after_seconds=decision.retry_after_seconds,
                            rng=self.retry_rng,
                        )
                        if can_retry
                        else None
                    )
                    log_provider_decision(
                        api=self.api,
                        model=model,
                        scrub=error_scrubber,
                        config=retry_config,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        phase="closing" if close_failed else phase,
                        exc=exc,
                        decision=decision,
                        skipped_reason=skipped_reason,
                        semantic=accumulator.builder.has_semantic_output,
                        terminal=accumulator.terminal is not None,
                        closed=stream_closed,
                        delay=delay,
                        outcome="waiting" if can_retry else "failed",
                    )
                    if can_retry:
                        trace = make_retry_trace(
                            exc=exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            delay_seconds=delay,
                        )
                        reason = retry_reason(decision)
                        # The normalized boundary clears every disposable accumulator;
                        # Runner replaces the draft without accepting old tool calls.
                        yield ProviderGenerationRetry(reason=reason)
                        update = ProviderRetryUpdate(
                            attempt=attempt + 1,
                            retry_number=attempt,
                            max_retries=max_attempts - 1,
                            phase=ProviderRetryPhase.WAITING,
                            reason=reason,
                            delay_seconds=delay,
                            has_semantic_output=accumulator.builder.has_semantic_output,
                        )
                        observe_retry(context, update)
                        try:
                            await self.retry_sleep(delay)
                        except asyncio.CancelledError as cancelled:
                            log_provider_decision(
                                api=self.api,
                                model=model,
                                scrub=error_scrubber,
                                config=retry_config,
                                attempt=attempt,
                                max_attempts=max_attempts,
                                phase="waiting",
                                exc=cancelled,
                                decision=decision,
                                skipped_reason="cancelled",
                                semantic=accumulator.builder.has_semantic_output,
                                terminal=False,
                                closed=True,
                                delay=delay,
                                outcome="cancelled",
                            )
                            raise
                        observe_retry(
                            context,
                            replace(
                                update,
                                phase=ProviderRetryPhase.RECONNECTING,
                                delay_seconds=0,
                            ),
                        )
                        # Only entering the next transport attempt counts as a retry.
                        retry_traces.append(trace)
                        attempt += 1
                        continue
                    message = provider_failure_message(exc)
                    if accumulator.terminal is not None and (
                        close_failed
                        or decision.reason in {"transport_error", "transport_timeout"}
                    ):
                        message = "已收到结果，但连接收尾失败。 " + message
                    observe_retry(context, None)
                    pending_failure = ProviderStreamFailure(
                        message=error_scrubber.scrub_text(message),
                        code_hint=exc.reason_code
                        if isinstance(exc, LLMTransportContractError)
                        else provider_failure_code_hint(decision),
                        retry_summary=build_provider_retry_summary(
                            config=retry_config,
                            traces=retry_traces,
                            final_decision=decision,
                            final_attempt=attempt,
                            has_semantic_output=accumulator.builder.has_semantic_output,
                            exhausted=skipped_reason == "attempts_exhausted",
                            skipped_reason=skipped_reason,
                        ),
                    )
                    pending_exception = exc
                    final_decision = decision
                    break
            if pending_failure is None:
                terminal = accumulator.finish()
                if isinstance(terminal, ProviderStreamFailure):
                    pending_failure = terminal
                    pending_exception = LLMTransportContractError(
                        terminal.message,
                        reason_code=terminal.code_hint or "transport_protocol_error",
                    )
                    final_decision = LLMRetryDecision(
                        kind=RetryDecisionKind.DO_NOT_RETRY,
                        reason=terminal.code_hint or "transport_protocol_error",
                    )
                    log_provider_decision(
                        api=self.api,
                        model=model,
                        scrub=error_scrubber,
                        config=retry_config,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        phase="closing",
                        exc=pending_exception,
                        decision=final_decision,
                        skipped_reason="non_retryable",
                        semantic=accumulator.builder.has_semantic_output,
                        terminal=False,
                        closed=True,
                        outcome="failed",
                    )
        finally:
            active_error = sys.exception()
            observe_retry(context, None)
            api_key = None
            if should_close_client:
                client_closure = _SDKStreamClosure()
                try:
                    await close_sdk_resource(
                        client,
                        scrub=error_scrubber,
                        primary=pending_exception or active_error,
                        closure=client_closure,
                    )
                except asyncio.CancelledError as cancelled:
                    log_provider_decision(
                        api=self.api,
                        model=model,
                        scrub=error_scrubber,
                        config=retry_config,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        phase="closing",
                        exc=cancelled,
                        decision=None,
                        skipped_reason="cancelled",
                        semantic=accumulator.builder.has_semantic_output,
                        terminal=accumulator.terminal is not None,
                        closed=stream_closed,
                        outcome="cancelled",
                        close_scope="sdk_client",
                        client_close_confirmed=client_closure.closed,
                    )
                    raise
                except PhysicalStreamCloseError as close_error:
                    close_decision = replace(
                        final_decision
                        or LLMRetryDecision(
                            kind=RetryDecisionKind.DO_NOT_RETRY,
                            reason="physical_close_unconfirmed",
                        ),
                        kind=RetryDecisionKind.DO_NOT_RETRY,
                        reason="completed_stream_close_failed"
                        if accumulator.terminal is not None
                        else "physical_close_unconfirmed",
                    )
                    log_provider_decision(
                        api=self.api,
                        model=model,
                        scrub=error_scrubber,
                        config=retry_config,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        phase="closing",
                        exc=close_error,
                        decision=close_decision,
                        skipped_reason=close_decision.reason,
                        semantic=accumulator.builder.has_semantic_output,
                        terminal=accumulator.terminal is not None,
                        closed=stream_closed,
                        outcome="failed",
                        close_scope="sdk_client",
                        client_close_confirmed=False,
                        original_classification=None
                        if final_decision is None
                        else final_decision.reason,
                    )
                    if active_error is not None:
                        raise
                    message = provider_failure_message(close_error)
                    if accumulator.terminal is not None:
                        message = "已收到结果，但连接收尾失败。 " + message
                    pending_failure = ProviderStreamFailure(
                        message=error_scrubber.scrub_text(message),
                        code_hint=(
                            pending_failure.code_hint
                            if pending_failure is not None
                            else provider_failure_code_hint(close_decision)
                        ),
                        retry_summary=build_provider_retry_summary(
                            config=retry_config,
                            traces=retry_traces,
                            final_decision=close_decision,
                            final_attempt=attempt,
                            has_semantic_output=accumulator.builder.has_semantic_output,
                            exhausted=False,
                            skipped_reason=close_decision.reason,
                        ),
                    )

        report = accumulator.usage_report
        if report is not None or model_identity.reported_model_id is not None:
            yield replace(
                report or TransportUsageReport(usage_status="missing", usage=None),
                reported_model_id=model_identity.reported_model_id,
            )
        if pending_failure is not None:
            yield replace(
                pending_failure,
                message=error_scrubber.scrub_text(pending_failure.message),
            )
            return
        assert terminal is not None
        log_provider_decision(
            api=self.api,
            model=model,
            scrub=error_scrubber,
            config=retry_config,
            attempt=attempt,
            max_attempts=max_attempts,
            phase="closing",
            exc=None,
            decision=None,
            skipped_reason=None,
            semantic=accumulator.builder.has_semantic_output,
            terminal=True,
            closed=True,
            outcome="success"
            if terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
            else "output_incomplete",
        )
        yield terminal


def build_chat_completions_payload(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> dict[str, Any]:
    validate_model_context_shape_for_call(call=call, context=context)
    model = call.target.model_profile
    route_wire_profile = model.route_wire_profile
    plan = context.provider_wire_input_plan or context.ephemeral_wire_input
    if plan is not None:
        if plan.wire_api != OPENAI_CHAT_COMPLETIONS_API:
            raise ValueError("provider wire plan API does not match Chat")
        context_fields = thaw_json(plan.materialization.context_bearing_projection)
        if not isinstance(context_fields, dict):
            raise TypeError("Chat context projection must be an object")
        if context_fields.get("tool_choice") != context.tool_choice:
            raise ValueError("Chat context tool choice changed after wire planning")
        wire_input_tokens = (
            plan.budget_input_tokens
            if context.ephemeral_wire_input is not None
            else plan.quote.budget_input_tokens
        )
    else:
        items_with_sources = _chat_message_items_with_sources(context.messages)
        ordered_items = tuple(item for item, _source in items_with_sources)
        context_fields = materialize_chat_context_bearing_wire_projection(
            call=call,
            root_policy=context.system_prompt,
            ordered_input_items=ordered_items,
            tool_items=tuple(_tool_to_chat_tool(tool) for tool in context.tools),
            tool_choice=context.tool_choice,
        )
        wire_input_tokens = (
            call.target.token_estimator.estimate_final_wire_json_components(
                fixed_context=materialize_chat_context_bearing_wire_projection(
                    call=call,
                    root_policy=context.system_prompt,
                    ordered_input_items=(),
                    tool_items=tuple(
                        _tool_to_chat_tool(tool) for tool in context.tools
                    ),
                    tool_choice=context.tool_choice,
                ),
                ordered_input_items=ordered_items,
                ordered_input_sources=tuple(
                    source for _item, source in items_with_sources
                ),
            ).total_input_tokens
        )

    payload: dict[str, Any] = dict(context_fields)
    extra_body: dict[str, Any] = {}
    for key, value in route_wire_profile.request_extra_body.items():
        materialized_value = mutable_provider_value(value)
        if context_fields.get(key) != materialized_value:
            raise ValueError("Chat extra-body context changed after wire planning")
        extra_body[key] = context_fields[key]
        payload.pop(key, None)
    payload.update(
        {
            "model": model.id,
            "n": 1,
            "stream_options": {"include_usage": True},
        }
    )
    for key, value in route_wire_profile.request_defaults.items():
        payload.setdefault(key, mutable_provider_value(value))
    payload["max_completion_tokens"] = resolve_wire_output_tokens(
        target=call.target,
        final_wire_input_tokens=wire_input_tokens,
    )
    reasoning = reasoning_wire_fields(call.target.contract, call.selected_reasoning)
    for key, value in reasoning.root.items():
        if key in payload:
            raise ValueError("Chat reasoning root field has another owner")
        payload[key] = mutable_provider_value(value)
    for key, value in reasoning.extra_body.items():
        if key in extra_body or key in payload:
            raise ValueError("Chat reasoning extra-body field has another owner")
        extra_body[key] = mutable_provider_value(value)
    if extra_body:
        payload["extra_body"] = extra_body
    return payload


_CHAT_NON_CONTEXT_BEARING_FIELDS = frozenset(
    {
        "model",
        "n",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "reasoning",
        "reasoning_effort",
        "thinking",
        "enable_thinking",
        "timeout",
        "service_tier",
        "seed",
        "temperature",
        "top_p",
        "logprobs",
        "top_logprobs",
    }
)


def materialize_chat_context_bearing_wire_projection(
    *,
    call: ResolvedModelCall,
    root_policy: str | None,
    ordered_input_items: tuple[dict[str, Any], ...],
    tool_items: tuple[dict[str, Any], ...],
    tool_choice: str | None = None,
) -> dict[str, Any]:
    """Materialize the exact Chat fields that carry provider input context."""

    profile = call.target.model_profile.route_wire_profile
    messages: list[dict[str, Any]] = []
    if root_policy:
        messages.append({"role": "system", "content": root_policy})
    messages.extend(dict(item) for item in ordered_input_items)
    projection: dict[str, Any] = {"messages": messages}
    for key, value in profile.request_defaults.items():
        if key not in _CHAT_NON_CONTEXT_BEARING_FIELDS:
            projection.setdefault(key, mutable_provider_value(value))
    if tool_items:
        projection["tools"] = [dict(item) for item in tool_items]
    if tool_choice is not None:
        projection["tool_choice"] = tool_choice
    for key, value in profile.request_extra_body.items():
        # The OpenAI SDK merges ``extra_body`` into the JSON body, with the
        # extension mapping taking precedence.  The measured projection is
        # the resulting HTTP-body shape, not the SDK invocation kwargs.
        projection[key] = mutable_provider_value(value)
    return projection


def project_chat_context_bearing_payload_fields(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Project the same context fields from an actual Chat request payload."""

    merged = {key: value for key, value in payload.items() if key != "extra_body"}
    extra_body = payload.get("extra_body")
    if extra_body is not None:
        if not isinstance(extra_body, dict):
            raise TypeError("Chat extra_body must be an object")
        merged.update(extra_body)
    return {
        key: value
        for key, value in merged.items()
        if key not in _CHAT_NON_CONTEXT_BEARING_FIELDS
    }


def chat_semantic_wire_group(
    message: LLMMessage,
) -> tuple[dict[str, Any], ...]:
    """Return the exact generic wire group for one compiled message."""

    return tuple(item for item, _source in _chat_message_items_with_sources((message,)))


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


def _chat_message_items_with_sources(
    messages: tuple[LLMMessage, ...],
) -> list[tuple[dict[str, Any], LLMMessage | None]]:
    """Lower once for both dispatch and estimation, preserving image sources."""

    chat_messages: list[tuple[dict[str, Any], LLMMessage | None]] = []
    pending_tool_calls: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.TOOL_CALL:
            pending_tool_calls.append(_legacy_message_to_chat_tool_call(message))
            continue
        if pending_tool_calls:
            chat_messages.append(
                ({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": pending_tool_calls,
                }, None)
            )
            pending_tool_calls = []
        chat_messages.append((_message_to_chat_message(message), message))
    if pending_tool_calls:
        chat_messages.append(
            ({
                "role": "assistant",
                "content": "",
                "tool_calls": pending_tool_calls,
            }, None)
        )
    return chat_messages


def _is_empty_chat_extension_value(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


@dataclass(slots=True)
class ChatCompletionAccumulator:
    """Closed one-choice Chat response state; EOF is never acceptance."""

    builder: ProviderLiveItemBuilder
    route_wire_profile: RouteWireProfile
    tool_calls: "ChatToolCallAccumulator" = field(init=False)
    usage_report: TransportUsageReport | None = None
    _usage_field_values: dict[str, Any] = field(default_factory=dict)
    terminal: ProviderAdapterTerminal | None = None
    _terminal_finish_reason: str | None = None
    _text_parts: list[str] = field(default_factory=list)
    _content_observed: bool = False
    _text_field_chunks: dict[str, list[str]] = field(default_factory=dict)
    _array_field_items: dict[str, list[FrozenJsonValue]] = field(default_factory=dict)
    _replay_aggregate_bytes: int = 2
    _unknown_nonempty_field_seen: bool = False
    _live_reasoning_source: str | None = None
    _live_reasoning_text_field: str | None = None
    _live_reasoning_detail_key: tuple[object, ...] | None = None

    def __post_init__(self) -> None:
        self.tool_calls = ChatToolCallAccumulator(builder=self.builder)

    def apply(self, raw_chunk: Any) -> list[ProviderStreamPayload]:
        chunk = sdk_event_to_dict(raw_chunk)
        choices = chunk.get("choices")
        if choices is None:
            if self.terminal is not None:
                self._adopt_final_usage(chunk.get("usage"))
            return []
        if not isinstance(choices, list):
            raise LLMTransportContractError(
                "chat choices are not an array",
                reason_code="transport_chat_choice_contract_invalid",
            )
        if not choices:
            if self.terminal is not None:
                self._adopt_final_usage(chunk.get("usage"))
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
                self._adopt_final_usage(chunk.get("usage"))
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
            known_contracts = {
                item.field_name: item for item in CHAT_CLOSED_REASONING_FIELD_CONTRACTS
            }
            live_thinking_fields = _CHAT_LIVE_THINKING_FIELDS
            allowed = {
                "role",
                "content",
                "tool_calls",
                *known_contracts,
                *live_thinking_fields,
            }
            unknown = set(delta).difference(allowed)
            self._record_unknown_fields(delta, unknown)
            role = delta.get("role")
            if role not in (None, "", "assistant"):
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
                dict.fromkeys((*known_contracts, *sorted(live_thinking_fields)))
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
                contract = known_contracts.get(field_name)
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
            events.extend(self._project_live_reasoning(delta))
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
        if finish_reason is None or finish_reason == "":
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
            self._clear_replay_fields()
            self._adopt_final_usage(chunk.get("usage"))
            return events

        events.extend(self.tool_calls.close_active_tool_calls())
        self._reconcile_final_message(choice.get("message"))
        self._validate_unknown_fields_for_terminal()
        if self._live_reasoning_source is None and isinstance(
            choice.get("message"), dict
        ):
            events.extend(self._project_live_reasoning(choice["message"]))
        events.extend(self.builder.close_active_blocks())
        try:
            replay = self._freeze_completed_replay()
        finally:
            self._clear_replay_fields()
        self._terminal_finish_reason = finish_reason
        self.terminal = ProviderAdapterTerminal(
            ProviderAdapterTerminalKind.COMPLETED,
            completed_replay_payload=replay,
        )
        self._adopt_final_usage(chunk.get("usage"))
        return events

    def _adopt_final_usage(self, raw_usage: Any) -> None:
        # Only a terminal chunk (or a later usage-only carrier) can settle
        # cumulative Chat usage. Earlier snapshots are deliberately discarded.
        report = transport_usage_report_from_mapping(raw_usage, wire_api=OPENAI_CHAT_COMPLETIONS_API)
        if report.usage_status == "missing" and self.usage_report is None:
            return
        # Retain field-valid counts until all cumulative response carriers have
        # arrived. A later parent correction can make an earlier breakdown valid.
        raw = raw_usage.model_dump(mode="python", exclude_unset=True) if hasattr(raw_usage, "model_dump") else raw_usage
        def valid_count(value: object) -> bool:
            return type(value) is int and 0 <= value <= 2**63 - 1

        if isinstance(raw, dict):
            for primary, alias in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
                value = raw.get(primary) if primary in raw else raw.get(alias)
                if valid_count(value):
                    self._usage_field_values[primary] = value
            total = raw.get("total_tokens")
            if valid_count(total):
                self._usage_field_values["total_tokens"] = total
            for primary, alias, detail in (
                ("prompt_tokens_details", "input_tokens_details", "cached_tokens"),
                ("completion_tokens_details", "output_tokens_details", "reasoning_tokens"),
            ):
                details = raw.get(primary)
                if details is None:
                    details = raw.get(alias)
                value = details.get(detail) if isinstance(details, dict) else None
                if detail == "cached_tokens" and not valid_count(value):
                    value = raw.get("prompt_cache_hit_tokens")
                if valid_count(value):
                    self._usage_field_values.setdefault(primary, {})[detail] = value
        cumulative = transport_usage_report_from_mapping(self._usage_field_values, wire_api=OPENAI_CHAT_COMPLETIONS_API)
        combined = merge_response_usage_reports(self.usage_report, report)
        if cumulative.usage is not None:
            combined = replace(cumulative, provider_diagnostics=tuple(dict.fromkeys((*combined.provider_diagnostics, *cumulative.provider_diagnostics))))
        self.usage_report = combined

    def _project_live_reasoning(
        self, value: dict[str, Any]
    ) -> list[ProviderStreamPayload]:
        details = tuple(chat_reasoning_detail_parts(value.get("reasoning_details")))
        text = tuple(
            (name, value[name])
            for name in ("reasoning_content", "reasoning")
            if isinstance(value.get(name), str) and value[name]
        )
        # Keep one live carrier for the response so delayed mirror fields do
        # not repeat already displayed text. Canonical projection later derives
        # all distinct public blocks from the complete, unchanged replay body.
        if self._live_reasoning_source is None:
            if details:
                self._live_reasoning_source = "details"
            elif text:
                self._live_reasoning_source = "text"
                self._live_reasoning_text_field = text[0][0]
        events: list[ProviderStreamPayload] = []
        if self._live_reasoning_source == "details":
            for key, block in details:
                if key != self._live_reasoning_detail_key:
                    events.extend(self.builder.thinking_end())
                    self._live_reasoning_detail_key = key
                events.extend(
                    self.builder.thinking_delta(
                        block.text,
                        presentation_kind=block.presentation_kind,
                    )
                )
        elif self._live_reasoning_source == "text":
            # Two top-level Chat fields may be aliases. Stream only one field;
            # completed replay still retains both exact observed carriers.
            for name, part in text:
                if name == self._live_reasoning_text_field:
                    events.extend(self.builder.thinking_delta(part))
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
        if delta.get("role") not in (None, "", "assistant"):
            return False
        for field_name, value in delta.items():
            if field_name == "role":
                continue
            if not _is_empty_chat_extension_value(value):
                return False
        return True

    def _reconcile_final_message(self, raw_message: object) -> None:
        known_contracts = CHAT_CLOSED_REASONING_FIELD_CONTRACTS
        replay_contracts = {
            item.field_name: item for item in self.route_wire_profile.chat_replay_fields
        }
        if raw_message is None:
            if any(item.final_value_required for item in replay_contracts.values()):
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
            *(item.field_name for item in known_contracts),
        }
        self._record_unknown_fields(raw_message, set(raw_message).difference(allowed))
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
        for contract in known_contracts:
            present = contract.field_name in raw_message
            replay_contract = replay_contracts.get(contract.field_name)
            if (
                replay_contract is not None
                and replay_contract.final_value_required
                and not present
            ):
                raise LLMTransportContractError(
                    "chat final message lacks a required replay field",
                    reason_code="transport_chat_replay_field_missing",
                )
            if not present:
                continue
            raw_value = raw_message[contract.field_name]
            if raw_value is None:
                if replay_contract is not None and replay_contract.final_value_required:
                    raise LLMTransportContractError(
                        "chat final message has a null required replay field",
                        reason_code="transport_chat_replay_field_missing",
                    )
                continue
            if contract.accumulation_mode is (
                ProviderChatFieldAccumulationMode.TEXT_CONCAT
            ):
                if not isinstance(raw_value, str):
                    raise LLMTransportContractError(
                        "chat final replay text field is not text",
                        reason_code="transport_chat_replay_field_invalid",
                    )
                self._validate_final_replay_field_bound(
                    contract.field_name,
                    contract.accumulation_mode,
                    raw_value,
                )
                if not self._field_observed(contract.field_name):
                    self._accumulate_field(
                        contract.field_name,
                        contract.accumulation_mode,
                        raw_value,
                    )
                elif self._text_field_value(contract.field_name) != raw_value:
                    raise LLMTransportContractError(
                        "chat final replay field differs from its deltas",
                        reason_code="transport_chat_replay_field_conflict",
                    )
                continue
            if contract.accumulation_mode is (
                ProviderChatFieldAccumulationMode.ORDERED_ARRAY_APPEND
            ):
                self._validate_final_replay_field_bound(
                    contract.field_name,
                    contract.accumulation_mode,
                    raw_value,
                )
                frozen = freeze_json(raw_value)
                if not isinstance(frozen, FrozenJsonArrayFact):
                    raise LLMTransportContractError(
                        "chat final replay append field is not an array",
                        reason_code="transport_chat_replay_field_invalid",
                    )
                final_value = list(frozen.items)
            else:
                raise AssertionError("chat replay accumulation mode drifted")
            if not self._field_observed(contract.field_name):
                self._accumulate_field(
                    contract.field_name,
                    contract.accumulation_mode,
                    raw_value,
                )
            elif self._array_field_items[contract.field_name] != final_value:
                raise LLMTransportContractError(
                    "chat final replay field differs from its deltas",
                    reason_code="transport_chat_replay_field_conflict",
                )

    def finish(self) -> ProviderAdapterTerminal | ProviderStreamFailure:
        if self.terminal is None:
            self._clear_replay_fields()
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
            self._reserve_replay_append(field_name, mode, raw_value)
            chunks = self._text_field_chunks.setdefault(field_name, [])
            if raw_value:
                chunks.append(raw_value)
            return
        if not isinstance(raw_value, list):
            raise LLMTransportContractError(
                "chat replay append field is not an array",
                reason_code="transport_chat_replay_field_invalid",
            )
        self._reserve_replay_append(field_name, mode, raw_value)
        frozen = freeze_json(raw_value)
        if not isinstance(frozen, FrozenJsonArrayFact):
            raise LLMTransportContractError(
                "chat replay append field is not an array",
                reason_code="transport_chat_replay_field_invalid",
            )
        current = self._array_field_items.setdefault(field_name, [])
        current.extend(frozen.items)

    def _reserve_replay_append(
        self,
        field_name: str,
        mode: ProviderChatFieldAccumulationMode,
        raw_value: object,
    ) -> None:
        observed = self._field_observed(field_name)
        if mode is ProviderChatFieldAccumulationMode.TEXT_CONCAT:
            if not isinstance(raw_value, str):
                raise AssertionError("chat replay text quote shape drifted")
            encoded = self._canonical_replay_value_bytes(raw_value)
            additional_bytes = (
                self._new_replay_field_prefix_bytes(field_name) + len(encoded)
                if not observed
                else len(encoded) - 2
            )
        elif mode is ProviderChatFieldAccumulationMode.ORDERED_ARRAY_APPEND:
            if not isinstance(raw_value, list):
                raise AssertionError("chat replay array quote shape drifted")
            encoded = self._canonical_replay_value_bytes(raw_value)
            if not observed:
                additional_bytes = self._new_replay_field_prefix_bytes(
                    field_name
                ) + len(encoded)
            else:
                interior_bytes = len(encoded) - 2
                existing_items = self._array_field_items[field_name]
                additional_bytes = interior_bytes + (
                    1 if existing_items and raw_value else 0
                )
        else:  # pragma: no cover - closed enum.
            raise AssertionError("chat replay accumulation mode drifted")

        if (
            self._replay_aggregate_bytes + additional_bytes
            > MAX_CHAT_REASONING_REPLAY_AGGREGATE_BYTES
        ):
            self._fail_replay_limit()
        self._replay_aggregate_bytes += additional_bytes

    def _validate_final_replay_field_bound(
        self,
        field_name: str,
        mode: ProviderChatFieldAccumulationMode,
        raw_value: object,
    ) -> None:
        if mode is ProviderChatFieldAccumulationMode.TEXT_CONCAT:
            if not isinstance(raw_value, str):
                raise AssertionError("chat final replay text quote shape drifted")
        elif mode is ProviderChatFieldAccumulationMode.ORDERED_ARRAY_APPEND:
            if not isinstance(raw_value, list):
                raise LLMTransportContractError(
                    "chat final replay append field is not an array",
                    reason_code="transport_chat_replay_field_invalid",
                )
        else:  # pragma: no cover - closed enum.
            raise AssertionError("chat replay accumulation mode drifted")
        encoded = self._canonical_replay_value_bytes(raw_value)
        logical_bytes = 2 + len(canonical_json_bytes(field_name)) + 1 + len(encoded)
        if logical_bytes > MAX_CHAT_REASONING_REPLAY_AGGREGATE_BYTES:
            self._fail_replay_limit()

    @staticmethod
    def _canonical_replay_value_bytes(raw_value: object) -> bytes:
        try:
            return canonical_json_bytes(raw_value)
        except (TypeError, ValueError) as exc:
            raise LLMTransportContractError(
                "chat replay field is not canonical JSON",
                reason_code="transport_chat_replay_field_invalid",
            ) from exc

    def _new_replay_field_prefix_bytes(self, field_name: str) -> int:
        separator = 1 if self._text_field_chunks or self._array_field_items else 0
        return separator + len(canonical_json_bytes(field_name)) + 1

    def _field_observed(self, field_name: str) -> bool:
        return (
            field_name in self._text_field_chunks
            or field_name in self._array_field_items
        )

    def _text_field_value(self, field_name: str) -> str:
        return "".join(self._text_field_chunks[field_name])

    def _fail_replay_limit(self) -> None:
        self._clear_replay_fields()
        raise LLMTransportContractError(
            "chat reasoning replay exceeded its aggregate byte bound",
            reason_code="transport_source_payload_limit_exceeded",
        )

    def _clear_replay_fields(self) -> None:
        self._text_field_chunks.clear()
        self._array_field_items.clear()
        self._replay_aggregate_bytes = 2

    def _freeze_completed_replay(self):
        contracts = self.route_wire_profile.chat_replay_fields
        for contract in contracts:
            if contract.required_on_selected_response and (
                not self._field_observed(contract.field_name)
            ):
                raise LLMTransportContractError(
                    "completed chat response lacks a required replay field",
                    reason_code="transport_chat_replay_field_missing",
                )
        observed_contracts = tuple(
            contract
            for contract in contracts
            if self._field_observed(contract.field_name)
        )
        if not observed_contracts:
            return None
        message: dict[str, object] = {
            "role": "assistant",
            "content": "".join(self._text_parts) if self._content_observed else None,
        }
        if self.tool_calls.completed_calls:
            message["tool_calls"] = list(self.tool_calls.completed_calls)
        for contract in observed_contracts:
            if contract.accumulation_mode is (
                ProviderChatFieldAccumulationMode.TEXT_CONCAT
            ):
                message[contract.field_name] = self._text_field_value(
                    contract.field_name
                )
            else:
                message[contract.field_name] = [
                    thaw_json(item)
                    for item in self._array_field_items[contract.field_name]
                ]
        frozen = freeze_json(message)
        if not isinstance(frozen, FrozenJsonObjectFact):
            raise AssertionError("chat replay message did not freeze as an object")
        return freeze_provider_adapter_completed_replay_payload(
            codec_kind=self.route_wire_profile.assistant_replay_codec_kind,
            ordered_items=(frozen,),
        )

    def _record_unknown_fields(
        self, carrier: dict[str, Any], unknown_fields: set[str]
    ) -> None:
        for field_name in unknown_fields:
            if not _is_empty_chat_extension_value(carrier[field_name]):
                self._unknown_nonempty_field_seen = True
                return

    def _validate_unknown_fields_for_terminal(self) -> None:
        if not self._unknown_nonempty_field_seen:
            return
        if self.tool_calls.completed_calls:
            raise LLMTransportContractError(
                "chat tool continuation contains an unsupported replay carrier",
                reason_code="transport_chat_replay_field_unsupported",
            )
        if not "".join(self._text_parts):
            raise LLMTransportContractError(
                "chat response contains only an unsupported semantic carrier",
                reason_code="transport_chat_replay_field_unsupported",
            )


@dataclass(slots=True)
class _ChatToolCallState:
    tool_call_id: str | None = None
    name: str = ""
    pending_arguments: list[str] = field(default_factory=list)
    started: bool = False
    reported_indexes: set[int] = field(default_factory=set)


@dataclass(slots=True)
class ChatToolCallAccumulator:
    builder: ProviderLiveItemBuilder
    _ordered_states: list[_ChatToolCallState] = field(default_factory=list)
    _states_by_id: dict[str, _ChatToolCallState] = field(default_factory=dict)
    _states_by_index: dict[int, list[_ChatToolCallState]] = field(default_factory=dict)
    completed_calls: tuple[dict[str, object], ...] = ()

    def apply_tool_call_delta(
        self, raw_tool_call: dict[str, Any]
    ) -> list[ProviderStreamPayload]:
        if set(raw_tool_call).difference({"index", "id", "type", "function"}):
            raise LLMTransportContractError(
                "chat tool-call delta contains unsupported fields",
                reason_code="transport_tool_call_contract_invalid",
            )
        raw_index = raw_tool_call.get("index")
        if raw_index is not None and (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or raw_index < 0
        ):
            raise LLMTransportContractError(
                "chat tool-call index is invalid",
                reason_code="transport_tool_call_contract_invalid",
            )

        raw_call_id = raw_tool_call.get("id")
        if raw_call_id is not None and not isinstance(raw_call_id, str):
            raise LLMTransportContractError(
                "chat tool-call ID is invalid",
                reason_code="transport_tool_call_contract_invalid",
            )
        tool_call_id = raw_call_id if raw_call_id else None
        state = self._resolve_state(
            tool_call_id=tool_call_id,
            reported_index=raw_index,
        )
        raw_type = raw_tool_call.get("type")
        if raw_type not in (None, "", "function"):
            raise LLMTransportContractError(
                "chat tool-call type is unsupported",
                reason_code="transport_tool_call_contract_invalid",
            )
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
            if name is not None and not isinstance(name, str):
                raise LLMTransportContractError(
                    "chat tool-call name is invalid",
                    reason_code="transport_tool_call_contract_invalid",
                )
            if isinstance(name, str) and name:
                if state.name and name != state.name:
                    raise LLMTransportContractError(
                        "chat tool-call stream changed its frozen tool name",
                        reason_code="transport_tool_call_name_mismatch",
                    )
                state.name = name
            arguments = function.get("arguments")
            if arguments is not None and not isinstance(arguments, str):
                raise LLMTransportContractError(
                    "chat tool-call arguments delta is invalid",
                    reason_code="transport_tool_call_contract_invalid",
                )
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

    def _resolve_state(
        self,
        *,
        tool_call_id: str | None,
        reported_index: int | None,
    ) -> _ChatToolCallState:
        if tool_call_id is not None:
            exact = self._states_by_id.get(tool_call_id)
            if exact is not None:
                self._associate_index(exact, reported_index, require_compatible=True)
                return exact

            indexed = (
                self._states_by_index.get(reported_index, [])
                if reported_index is not None
                else []
            )
            provisional = [item for item in indexed if item.tool_call_id is None]
            if len(indexed) == 1 and len(provisional) == 1:
                state = provisional[0]
                self._bind_call_id(state, tool_call_id)
                return state
            if provisional:
                raise LLMTransportContractError(
                    "chat tool-call ID cannot be correlated with a reused index",
                    reason_code="transport_tool_call_correlation_ambiguous",
                )
            state = self._new_state(tool_call_id=tool_call_id)
            self._associate_index(state, reported_index, require_compatible=False)
            return state

        if reported_index is not None:
            indexed = self._states_by_index.get(reported_index, [])
            if len(indexed) == 1:
                return indexed[0]
            if len(indexed) > 1:
                raise LLMTransportContractError(
                    "chat tool-call index identifies more than one active call",
                    reason_code="transport_tool_call_correlation_ambiguous",
                )
            state = self._new_state(tool_call_id=None)
            self._associate_index(state, reported_index, require_compatible=False)
            return state

        if len(self._ordered_states) == 1:
            return self._ordered_states[0]
        raise LLMTransportContractError(
            "chat tool-call delta has no unambiguous call identity",
            reason_code="transport_tool_call_correlation_ambiguous",
        )

    def _new_state(self, *, tool_call_id: str | None) -> _ChatToolCallState:
        state = _ChatToolCallState(tool_call_id=tool_call_id)
        self._ordered_states.append(state)
        if tool_call_id is not None:
            self._bind_call_id(state, tool_call_id)
        return state

    def _bind_call_id(self, state: _ChatToolCallState, tool_call_id: str) -> None:
        existing = self._states_by_id.get(tool_call_id)
        if existing is not None and existing is not state:
            raise LLMTransportContractError(
                "chat tool-call ID was reused for another call",
                reason_code="transport_tool_call_identity_mismatch",
            )
        if state.tool_call_id is not None and state.tool_call_id != tool_call_id:
            raise LLMTransportContractError(
                "chat tool-call stream changed its frozen call ID",
                reason_code="transport_tool_call_identity_mismatch",
            )
        state.tool_call_id = tool_call_id
        self._states_by_id[tool_call_id] = state

    def _associate_index(
        self,
        state: _ChatToolCallState,
        reported_index: int | None,
        *,
        require_compatible: bool,
    ) -> None:
        if reported_index is None:
            return
        indexed = self._states_by_index.setdefault(reported_index, [])
        if any(item is state for item in indexed):
            return
        if require_compatible and indexed:
            raise LLMTransportContractError(
                "chat tool-call ID conflicts with its reported index",
                reason_code="transport_tool_call_correlation_ambiguous",
            )
        indexed.append(state)
        state.reported_indexes.add(reported_index)

    def close_active_tool_calls(self) -> list[ProviderStreamPayload]:
        if any(
            not state.started or not state.tool_call_id
            for state in self._ordered_states
        ):
            raise LLMTransportContractError(
                "tool-call stream ended before a named tool-call start",
                reason_code="transport_tool_call_start_missing",
            )
        events: list[ProviderStreamPayload] = []
        completed: list[dict[str, object]] = []
        for state in self._ordered_states:
            assert state.tool_call_id is not None
            arguments = (
                "".join(
                    self.builder.tool_call_argument_parts.get(state.tool_call_id, ())
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
        self._ordered_states.clear()
        self._states_by_id.clear()
        self._states_by_index.clear()
        return events


def _message_to_chat_message(
    message: LLMMessage,
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
            "content": join_text_content(message.content),
        }
    if message.role is MessageRole.ASSISTANT:
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": join_text_content(message.content),
        }
        if message.tool_calls:
            payload["tool_calls"] = [
                _tool_call_to_chat_tool_call(call) for call in message.tool_calls
            ]
        return payload
    return {
        "role": _chat_role(message.role),
        "content": _chat_message_content(message),
    }


def _chat_message_content(message: LLMMessage) -> str | list[dict[str, object]]:
    images = any(isinstance(part, LLMImagePart) for part in message.content)
    if not images:
        return join_text_content(message.content)
    if message.role is not MessageRole.USER:
        raise ValueError("Chat image content requires a USER message")
    content: list[dict[str, object]] = []
    for part in message.content:
        if isinstance(part, LLMTextPart):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, LLMImagePart):
            encoded = base64.b64encode(part.immutable_bytes).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{part.media_type};base64,{encoded}",
                        "detail": "auto",
                    },
                }
            )
        else:  # pragma: no cover - LLMMessage closes this union.
            raise TypeError("Chat message contains an invalid content part")
    return content


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


def _tool_to_chat_tool(tool: ToolSpec) -> dict[str, Any]:
    return openai_chat_function_tool(tool)
