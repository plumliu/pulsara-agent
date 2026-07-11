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
    if context.tools and not target_fact.supports_tools:
        raise ModelTargetCapabilityMismatch("model target does not support tools")
    transport = call.target.transport
    if (
        transport.binding_id != target_fact.transport_binding_id
        or transport.contract_version != target_fact.transport_contract_version
    ):
        raise ModelTargetBindingMismatch(
            "transport binding changed after target resolution"
        )
    effective_options = call.target.effective_options
    options_fact = target_fact.effective_options
    if effective_options.reasoning_effort != options_fact.reasoning_effort:
        raise ModelTargetBindingMismatch(
            "effective options changed after target resolution"
        )

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
