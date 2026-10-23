"""Single-attempt, memory-only provider execution for advisory product requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum

from pulsara_agent.identity import random_id
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.frozen_target import (
    FrozenProviderPhysicalCallTarget,
    _freeze_provider_physical_call_target,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.llm.provider_open import (
    EphemeralProbeCredentialOwner,
    _issue_connection_probe_provider_open_permit,
    _issue_saved_ephemeral_provider_open_permit,
)
from pulsara_agent.llm.request import (
    EphemeralProviderWireInput,
    FrozenProviderWireMaterialization,
    LLMContext,
)
from pulsara_agent.llm.resolution import (
    resolve_model_call,
    resolve_model_target,
    with_call_output_cap,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.stream_limits import (
    MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES,
)
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    ThinkingDeltaPayload,
    DataDeltaPayload,
    ToolCallStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
)
from pulsara_agent.ports.provider_stream import (
    ProviderGenerationRetry,
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.model_call import ModelCallPurpose

EPHEMERAL_PURPOSES = frozenset(
    {
        ModelCallPurpose.CONNECTION_PROBE,
        ModelCallPurpose.SESSION_TITLE,
        ModelCallPurpose.CONVERSATION_SUGGESTION,
    }
)


class EphemeralRequestFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class EphemeralResultStatus(StrEnum):
    COMPLETED = "COMPLETED"
    OUTPUT_INCOMPLETE = "OUTPUT_INCOMPLETE"
    TOOL_CALL = "TOOL_CALL"


@dataclass(frozen=True, slots=True)
class EphemeralResult:
    status: EphemeralResultStatus
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedEphemeralRequest:
    call_target: FrozenProviderPhysicalCallTarget
    context: LLMContext = field(repr=False)
    timeout_policy: OpenAITransportTimeoutPolicy = field(repr=False)
    credential_owner: EphemeralProbeCredentialOwner | None = field(
        default=None, repr=False
    )

    def __post_init__(self):
        if self.call_target.purpose not in EPHEMERAL_PURPOSES:
            raise ValueError("request purpose is not ephemeral")
        if (self.call_target.purpose is ModelCallPurpose.CONNECTION_PROBE) != (
            self.credential_owner is not None
        ):
            raise ValueError("ephemeral credential owner does not match purpose")
        if self.context.model_call_index is not None:
            raise ValueError("ephemeral request cannot own a kernel model index")
        if (
            self.call_target.purpose is ModelCallPurpose.SESSION_TITLE
            and self.context.tools
        ):
            raise ValueError("title request cannot advertise tools")
        if (
            self.call_target.purpose is ModelCallPurpose.CONVERSATION_SUGGESTION
            and self.context.tool_choice != "auto"
        ):
            raise ValueError("suggestion preserves automatic tool choice")


def validate_ephemeral_text(text: str) -> str | None:
    text = text.strip()
    if not text or any(
        ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in text
    ):
        return None
    return text


def prepare_text_ephemeral_request(
    *,
    runtime: ModelRuntime,
    binding: ModelCallBinding,
    purpose: ModelCallPurpose,
    messages: tuple[LLMMessage, ...],
    system_prompt: str,
    timeout_policy: OpenAITransportTimeoutPolicy,
    frozen_target: FrozenProviderPhysicalCallTarget | None = None,
    resolution_snapshot: FrozenModelResolutionSnapshot | None = None,
) -> PreparedEphemeralRequest:
    """Prepare the isolated title input through the ordinary saved target owner."""
    if purpose is not ModelCallPurpose.SESSION_TITLE:
        raise ValueError("minimal text preparation is only for session titles")
    if frozen_target is not None:
        if frozen_target.model_call_binding != binding:
            raise ValueError("title binding differs from accepted input")
        target = runtime.resolve_frozen_target_bundle(
            frozen_target.target_bundle, binding=binding, timeout_policy=timeout_policy
        )
        target = with_call_output_cap(
            target, frozen_target.input_budget.effective_output_tokens
        )
        maximum_input = frozen_target.input_budget.effective_input_budget_tokens
    elif resolution_snapshot is not None:
        captured = resolution_snapshot.validate(binding)
        target = resolve_model_target(
            connection=captured.config,
            binding=binding,
            catalog=runtime.catalog.selectable(),
            route_wires=runtime.route_wires,
            registry=runtime.transport_registry(timeout_policy),
        )
        if target.contract != captured.target:
            raise ValueError("accepted title target changed before preparation")
        maximum_input = target.context_budget.input_budget_tokens
    else:
        target = runtime.resolve_target(binding, timeout_policy=timeout_policy)
        maximum_input = target.context_budget.input_budget_tokens
    call = resolve_model_call(target=target, binding=binding, purpose=purpose)
    context = LLMContext(
        messages=messages,
        system_prompt=system_prompt,
        context_id=random_id("context"),
        resolved_model_call_id=call.resolved_model_call_id,
        model_call_index=None,
    )
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        chat_semantic_wire_group,
        materialize_chat_context_bearing_wire_projection,
        build_chat_completions_payload,
    )
    from pulsara_agent.llm.adapters.openai.responses import (
        responses_semantic_wire_group,
        materialize_responses_context_bearing_wire_projection,
        build_responses_payload,
    )

    if target.fact.wire_api == "openai_chat_completions":
        lower, materialize, payload = (
            chat_semantic_wire_group,
            materialize_chat_context_bearing_wire_projection,
            build_chat_completions_payload,
        )
    else:
        lower, materialize, payload = (
            responses_semantic_wire_group,
            materialize_responses_context_bearing_wire_projection,
            build_responses_payload,
        )
    groups = tuple(lower(message) for message in messages)
    items = tuple(item for group in groups for item in group)
    fixed = materialize(
        call=call,
        root_policy=context.system_prompt,
        tool_items=(),
        ordered_input_items=(),
    )
    projection = materialize(
        call=call,
        root_policy=context.system_prompt,
        tool_items=(),
        ordered_input_items=items,
    )
    tokens = target.token_estimator.estimate_final_wire_json_components(
        fixed_context=fixed,
        ordered_input_items=items,
        ordered_input_sources=tuple(
            message
            for message, group in zip(messages, groups, strict=True)
            for _ in group
        ),
    ).total_input_tokens
    wire = EphemeralProviderWireInput(
        wire_api=target.fact.wire_api,
        materialization=FrozenProviderWireMaterialization(
            freeze_json(context.system_prompt),
            (),
            tuple(freeze_json(item) for item in items),
            freeze_json(projection),
        ),
        budget_input_tokens=tokens,
        effective_input_budget_tokens=maximum_input,
    )
    context = replace(context, ephemeral_wire_input=wire)
    payload(
        call=call, context=context
    )  # Actual adapter output-window/capability preflight, before borrow/open.
    return PreparedEphemeralRequest(
        _freeze_provider_physical_call_target(
            target=target, call=call, maximum_input_tokens=maximum_input
        ),
        context,
        timeout_policy,
    )


async def _physically_close(execution) -> bool:
    """Repeated caller cancellation cannot detach physical cleanup."""

    async def close():
        await execution.aclose()
        physical = await execution.wait_physical_completion()
        if physical.status is not ProviderPhysicalCompletionStatus.COMPLETED:
            raise EphemeralRequestFailure(
                "transport_protocol_error", "临时请求的物理连接未完整释放。"
            )

    task = asyncio.create_task(close())
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            return cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                task.result()
                return cancelled


async def execute_ephemeral_request(
    *, runtime: ModelRuntime, prepared: PreparedEphemeralRequest
) -> EphemeralResult:
    """No repository, dispatcher, transcript, usage observer, or retry owner."""
    if prepared.credential_owner is None:
        permit = _issue_saved_ephemeral_provider_open_permit(
            call_target=prepared.call_target,
            resolved_model_call_id=prepared.context.resolved_model_call_id,
            timeout_policy=prepared.timeout_policy,
        )
    else:
        permit = _issue_connection_probe_provider_open_permit(
            probe_call_target=prepared.call_target,
            resolved_model_call_id=prepared.context.resolved_model_call_id,
            timeout_policy=prepared.timeout_policy,
            credential_owner=prepared.credential_owner,
        )
    borrowed = execution = None
    try:
        borrowed = runtime.borrow_transport(permit)
        execution = borrowed.open_stream(context=prepared.context)
        text: list[str] = []
        received_bytes = 0
        while True:
            item = await execution.read_next()
            if item is None or isinstance(item, ProviderGenerationRetry):
                raise EphemeralRequestFailure(
                    "transport_protocol_error", "临时请求在协议终态前结束或尝试重试。"
                )
            if isinstance(
                item, (TextDeltaPayload, ThinkingDeltaPayload, ToolCallDeltaPayload)
            ):
                received_bytes += len(item.delta.encode("utf-8"))
            elif isinstance(item, DataDeltaPayload):
                received_bytes += len(item.data.encode("utf-8"))
            if received_bytes > MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES:
                raise EphemeralRequestFailure(
                    "transport_source_payload_limit_exceeded",
                    "临时请求返回的内容超过缓冲边界。",
                )
            if isinstance(item, TextDeltaPayload):
                text.append(item.delta)
            if isinstance(
                item, (ToolCallStartPayload, ToolCallDeltaPayload, ToolCallEndPayload)
            ):
                return EphemeralResult(EphemeralResultStatus.TOOL_CALL, "")
            if not isinstance(item, ProviderStreamTerminal):
                continue
            if item.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR:
                assert item.error is not None
                raise EphemeralRequestFailure(item.error.code.value, item.error.message)
            status = (
                EphemeralResultStatus.COMPLETED
                if item.terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
                else EphemeralResultStatus.OUTPUT_INCOMPLETE
            )
            return EphemeralResult(status, "".join(text))
    finally:
        cancelled = False
        try:
            if execution is not None:
                try:
                    cancelled = await _physically_close(execution)
                except BaseException as close_error:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError from close_error
                    raise
        finally:
            if borrowed is not None:
                borrowed.close()
            elif prepared.credential_owner is not None:
                prepared.credential_owner.close()
        if cancelled:
            raise asyncio.CancelledError
