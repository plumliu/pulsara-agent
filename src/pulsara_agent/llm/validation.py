"""Final provider-neutral model context validation."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.llm.errors import (
    ModelContextIdentityMismatch,
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelTargetBindingMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.estimator import TokenEstimate, estimate_model_context_for_call
from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    MessageRole,
    content_has_image,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.primitives.model_call import ModelContextMode


@dataclass(frozen=True, slots=True)
class ModelContextValidationResult:
    estimate: TokenEstimate


def validate_model_context_for_call(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> ModelContextValidationResult:
    validate_model_context_shape_for_call(call=call, context=context)
    fact = call.fact
    target_fact = call.target.fact
    estimate = estimate_model_context_for_call(call=call, context=context)
    if estimate.total_input_tokens > target_fact.context_budget.input_budget_tokens:
        exc = ModelInputBudgetExceeded(
            f"model input estimate {estimate.total_input_tokens} exceeds budget "
            f"{target_fact.context_budget.input_budget_tokens}"
        )
        exc.estimate = estimate  # type: ignore[attr-defined]
        raise exc
    if fact.context_mode is ModelContextMode.COMPILED and (
        context.compiler_estimated_input_tokens is None
        or context.compiler_estimated_input_tokens != estimate.total_input_tokens
    ):
        exc = ModelInputEstimateMismatch(
            "compiled model context is missing its final estimate"
            if context.compiler_estimated_input_tokens is None
            else "compiler and pre-send model input estimates differ"
        )
        exc.estimate = estimate  # type: ignore[attr-defined]
        raise exc
    return ModelContextValidationResult(estimate=estimate)


def validate_model_context_shape_for_call(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> None:
    """Validate shape, target and transport binding without a token gate."""

    fact = call.fact
    target_fact = call.target.fact
    if not context.context_id:
        raise ModelContextIdentityMismatch("LLMContext.context_id is required")
    if context.resolved_model_call_id != fact.resolved_model_call_id:
        raise ModelContextIdentityMismatch("LLMContext resolved call identity mismatch")
    if context.target_fingerprint != target_fact.target_fingerprint:
        raise ModelContextIdentityMismatch("LLMContext target fingerprint mismatch")
    if (
        fact.context_mode is ModelContextMode.COMPILED
        and context.model_call_index is None
    ):
        raise ModelContextIdentityMismatch(
            "compiled model context requires model_call_index"
        )
    if context.tools and call.target.contract.target_facts.tool_call is False:
        raise ModelTargetCapabilityMismatch("model target does not support tools")
    if any(message.role is MessageRole.SYSTEM for message in context.messages):
        raise ModelContextIdentityMismatch(
            "ordered model history cannot contain a privileged system message"
        )
    validate_model_message_content_for_call(
        call=call,
        messages=context.messages,
    )
    transport = call.target.transport
    if (
        transport.binding_id != target_fact.transport_binding_id
        or transport.contract_version != target_fact.transport_contract_version
    ):
        raise ModelTargetBindingMismatch(
            "transport binding changed after target resolution"
        )
    if call.binding != call.fact.binding:
        raise ModelTargetBindingMismatch(
            "model call binding changed after target resolution"
        )


def validate_model_message_content_for_call(
    *,
    call: ResolvedModelCall,
    messages: tuple[LLMMessage, ...],
) -> None:
    """Validate the shared content/target shape without identity or token gates."""

    target_fact = call.target.fact
    has_image = False
    for message in messages:
        if any(
            not isinstance(part, (LLMTextPart, LLMImagePart))
            for part in message.content
        ):
            raise ModelContextIdentityMismatch(
                "provider message contains an invalid content part"
            )
        message_has_image = content_has_image(message.content)
        has_image = has_image or message_has_image
        if message.role is not MessageRole.USER and message_has_image:
            raise ModelContextIdentityMismatch(
                "image content is only valid in a USER message"
            )
        if message.role is MessageRole.USER and (
            not message.content
            or message.tool_call_id is not None
            or message.name is not None
            or message.arguments is not None
            or message.tool_calls
        ):
            raise ModelContextIdentityMismatch(
                "user provider message has an invalid closed shape"
            )
    modalities = target_fact.input_modalities
    if has_image and modalities is not None and "image" not in modalities:
        raise ModelTargetCapabilityMismatch(
            "model target does not declare image input"
        )
