"""Resolved-model token allocation for one long-horizon context window."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    ContextWindowFact,
    ContextWindowProjectionState,
    LongHorizonContextAllocationPolicyFact,
    LongHorizonContextBudgetDecisionFact,
    LongHorizonProjectionPressureShadowFact,
    ProjectionTargetUnreachableAuditFact,
)


@dataclass(frozen=True, slots=True)
class LongHorizonContextBudgetMeasurement:
    decision: LongHorizonContextBudgetDecisionFact
    pressure_shadow: LongHorizonProjectionPressureShadowFact
    soft_target_exceeded: bool
    window_trigger_exceeded: bool


def measure_long_horizon_context_budget(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
    estimate: TokenEstimate,
    window: ContextWindowFact,
    projection_state: ContextWindowProjectionState,
    policy: LongHorizonContextAllocationPolicyFact,
) -> LongHorizonContextBudgetMeasurement:
    """Decompose the exact provider-neutral payload without a second estimator.

    Tool-result message framing and identity remain fixed input. Only the
    content contribution is projection-controlled. L1 conservatively treats
    the current representation as the minimum; L2 replaces that input with its
    deterministic representation lattice before executing a rewrite.
    """

    target = call.target
    if estimate != target.token_estimator.estimate_context(context):
        raise ValueError("long-horizon budget estimate differs from compiler estimate")
    if (
        window.resolved_model_target_fingerprint != target.fact.target_fingerprint
        or window.token_estimator_fingerprint
        != target.fact.token_estimator.estimator_fingerprint
        or window.input_budget_tokens != target.context_budget.input_budget_tokens
    ):
        raise ValueError("long-horizon window/model target identity mismatch")
    if (
        projection_state.window_id != window.window_id
        or projection_state.window_generation != window.generation
    ):
        raise ValueError("long-horizon projection/window identity mismatch")
    if len(context.messages) != len(estimate.message_tokens_by_index):
        raise ValueError("long-horizon message token breakdown is incomplete")

    projected_tool_tokens = 0
    for index, message in enumerate(context.messages):
        if message.role not in {
            MessageRole.TOOL_RESULT,
            MessageRole.RUNTIME_OBSERVATION,
        }:
            continue
        message_tokens = estimate.message_tokens_by_index[index]
        content_tokens = sum(
            target.token_estimator.estimate_text(part) for part in message.content
        )
        fixed_tokens = message_tokens - content_tokens
        if fixed_tokens > message_tokens:
            raise ValueError("tool-result fixed framing exceeds message estimate")
        projected_tool_tokens += message_tokens - fixed_tokens

    fixed_non_result_tokens = estimate.total_input_tokens - projected_tool_tokens
    if fixed_non_result_tokens < 0:
        raise ValueError("long-horizon fixed token decomposition is negative")
    input_budget = target.context_budget.input_budget_tokens
    hard_available = max(0, input_budget - fixed_non_result_tokens)
    soft_target = min(
        hard_available,
        input_budget * policy.tool_projection_soft_ratio_ppm // 1_000_000,
    )
    post_target = min(
        hard_available,
        input_budget * policy.tool_projection_post_rewrite_ratio_ppm // 1_000_000,
    )
    minimum_projection = projected_tool_tokens
    window_trigger = (
        input_budget * policy.window_compaction_trigger_ratio_ppm // 1_000_000
    )
    unit_count = len(projection_state.unit_projections)
    unit_limit_exceeded = unit_count > policy.max_projection_units_per_window

    if fixed_non_result_tokens + minimum_projection > input_budget:
        decision_code = "protected_tail_unreachable"
    elif estimate.total_input_tokens > window_trigger:
        decision_code = "window_compaction_required"
    elif projected_tool_tokens > soft_target:
        decision_code = "projection_rewrite"
    else:
        decision_code = "within_soft_target"
    after_projection = (
        projected_tool_tokens if decision_code == "within_soft_target" else None
    )
    final_after = (
        estimate.total_input_tokens if decision_code == "within_soft_target" else None
    )
    decision_payload = {
        "window_id": window.window_id,
        "source_through_sequence": projection_state.through_sequence,
        "input_budget_tokens": input_budget,
        "fixed_non_result_tokens": fixed_non_result_tokens,
        "projected_tool_tokens_before": projected_tool_tokens,
        "minimum_result_projection_tokens": minimum_projection,
        "soft_tool_projection_tokens": soft_target,
        "post_rewrite_target_tokens": post_target,
        "projected_tool_tokens_after": after_projection,
        "final_input_tokens_after": final_after,
        "active_projection_unit_count": unit_count,
        "max_projection_units_per_window": policy.max_projection_units_per_window,
        "unit_count_limit_exceeded": unit_limit_exceeded,
        "decision": decision_code,
        "estimator_fingerprint": target.fact.token_estimator.estimator_fingerprint,
    }
    decision = LongHorizonContextBudgetDecisionFact(
        **decision_payload,
        decision_fingerprint=context_fingerprint(
            "long-horizon-context-budget-decision:v1",
            {
                "schema_version": "long_horizon_context_budget_decision.v1",
                **decision_payload,
            },
        ),
    )
    shadow_payload = {
        "window_id": window.window_id,
        "source_through_sequence": projection_state.through_sequence,
        "active_projection_unit_count": unit_count,
        "max_projection_units_per_window": policy.max_projection_units_per_window,
        "unit_count_limit_exceeded": unit_limit_exceeded,
        "enforcement_mode": "diagnostic_only",
    }
    shadow = LongHorizonProjectionPressureShadowFact(
        **shadow_payload,
        operational_fingerprint=context_fingerprint(
            "long-horizon-projection-pressure-shadow:v1",
            {
                "schema_version": "long_horizon_projection_pressure_shadow.v1",
                **shadow_payload,
            },
        ),
    )
    return LongHorizonContextBudgetMeasurement(
        decision=decision,
        pressure_shadow=shadow,
        soft_target_exceeded=projected_tool_tokens > soft_target,
        window_trigger_exceeded=estimate.total_input_tokens > window_trigger,
    )


def long_horizon_context_diagnostics(
    *,
    measurement: LongHorizonContextBudgetMeasurement,
    target_unreachable: ProjectionTargetUnreachableAuditFact | None,
) -> tuple[dict[str, object], ...]:
    """Render canonical event diagnostics from manifest-safe facts."""

    diagnostics: list[dict[str, object]] = []
    if target_unreachable is not None:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "context_projection_soft_target_exceeded",
                "message": (
                    "The deterministic minimum projection remains above "
                    "the soft target but fits the resolved hard input budget."
                ),
                "target_projected_tokens": (
                    target_unreachable.target_projected_tokens
                ),
                "minimum_projected_tokens": (
                    target_unreachable.minimum_projected_tokens
                ),
            }
        )
    elif measurement.soft_target_exceeded:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "context_projection_soft_target_exceeded",
                "message": (
                    "Tool observation projection exceeds its resolved soft "
                    "token target; L1 records pressure without rewriting."
                ),
            }
        )
    if measurement.pressure_shadow.unit_count_limit_exceeded:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "context_projection_unit_count_limit_exceeded",
                "message": (
                    "Active projection unit count exceeds the frozen window "
                    "policy; enforcement remains diagnostic-only until L4."
                ),
            }
        )
    return tuple(diagnostics)


__all__ = [
    "LongHorizonContextBudgetMeasurement",
    "long_horizon_context_diagnostics",
    "measure_long_horizon_context_budget",
]
