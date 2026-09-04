"""Resolve one immutable connection and binding into an executable model call."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import uuid4

from pulsara_agent.llm.errors import (
    ModelInputBudgetUnavailable,
    ModelTargetBindingMismatch,
    ModelTransportUnavailable,
)
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1, TokenEstimator
from pulsara_agent.llm.model_catalog import SelectableModelCatalog
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionConfig,
    ReasoningSelection,
)
from pulsara_agent.llm.model_target import (
    ModelTargetContract,
    RouteWireRegistry,
    resolve_model_target_contract,
    validate_reasoning_selection,
)
from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ModelContextLimits,
    ModelContextMode,
    ResolvedModelCallFact,
    ResolvedModelContextBudgetFact,
    ResolvedModelTargetFact,
    resolved_model_target_fingerprint,
)


@dataclass(frozen=True, slots=True)
class ResolvedModelTarget:
    connection: ModelConnectionConfig
    contract: ModelTargetContract
    model_profile: ModelProfile
    transport: NormalizedLLMTransport
    limits: ModelContextLimits
    context_budget: ResolvedModelContextBudgetFact
    token_estimator: TokenEstimator
    fact: ResolvedModelTargetFact


@dataclass(frozen=True, slots=True)
class ResolvedModelCall:
    target: ResolvedModelTarget
    binding: ModelCallBinding
    fact: ResolvedModelCallFact

    @property
    def resolved_model_call_id(self) -> str:
        return self.fact.resolved_model_call_id

    @property
    def selected_reasoning(self) -> ReasoningSelection | None:
        return self.binding.reasoning


def resolve_model_target(
    *,
    connection: ModelConnectionConfig,
    binding: ModelCallBinding,
    catalog: SelectableModelCatalog,
    route_wires: RouteWireRegistry,
    registry: NormalizedLLMTransportRegistry,
) -> ResolvedModelTarget:
    if binding.connection_id != connection.id:
        raise ModelTargetBindingMismatch(
            "model call binding names a different immutable connection"
        )
    contract = resolve_model_target_contract(
        catalog=catalog,
        connection=connection,
        route_wires=route_wires,
    )
    validate_reasoning_selection(contract.reasoning, binding.reasoning)
    try:
        transport = registry.get(contract.key.wire_api.value)
    except KeyError as exc:
        raise ModelTransportUnavailable(str(exc)) from exc
    adapter = contract.route_wire
    if (
        transport.binding_id != adapter.transport_binding_id
        or transport.contract_version != adapter.transport_contract_version
    ):
        raise ModelTransportUnavailable("route/wire transport contract drifted")
    limits = contract.catalog_facts.limits
    output = limits.default_output_tokens
    pre_margin = min(
        limits.max_input_tokens,
        limits.total_context_tokens - output,
    )
    input_budget = pre_margin - limits.input_safety_margin_tokens
    if input_budget < 1:
        raise ModelInputBudgetUnavailable("resolved model input budget is non-positive")
    budget = ResolvedModelContextBudgetFact(
        effective_output_tokens=output,
        pre_margin_input_tokens=pre_margin,
        safety_margin_tokens=limits.input_safety_margin_tokens,
        input_budget_tokens=input_budget,
    )
    estimator = PulsaraHeuristicTokenEstimatorV1()
    canonical_endpoint = contract.canonical_endpoint_base_url
    endpoint_fingerprint = context_fingerprint(
        "pulsara.model-endpoint:v2", canonical_endpoint
    )
    target_payload = {
        "route_id": contract.key.route_id,
        "wire_api": contract.key.wire_api.value,
        "model_id": contract.key.model_id,
        "canonical_endpoint_base_url": canonical_endpoint,
        "transport_binding_id": adapter.transport_binding_id,
        "transport_contract_version": adapter.transport_contract_version,
        "model_identity_policy": adapter.model_identity_policy.value,
        "limits": limits.model_dump(mode="json"),
    }
    fact = ResolvedModelTargetFact(
        target_fingerprint=resolved_model_target_fingerprint(target_payload),
        endpoint_fingerprint=endpoint_fingerprint,
        context_budget=budget,
        token_estimator=estimator.fact,
        **target_payload,
    )
    return ResolvedModelTarget(
        connection=connection,
        contract=contract,
        model_profile=ModelProfile(
            id=contract.key.model_id,
            route_id=contract.key.route_id,
            wire_api=contract.key.wire_api,
            base_url=canonical_endpoint,
            route_wire_profile=adapter.profile,
        ),
        transport=transport,
        limits=limits,
        context_budget=budget,
        token_estimator=estimator,
        fact=fact,
    )


def resolve_model_call(
    *,
    target: ResolvedModelTarget,
    binding: ModelCallBinding,
    purpose: ModelCallPurpose,
    resolved_model_call_id: str | None = None,
) -> ResolvedModelCall:
    if binding.connection_id != target.connection.id:
        raise ModelTargetBindingMismatch("call binding differs from resolved target")
    validate_reasoning_selection(target.contract.reasoning, binding.reasoning)
    mode = (
        ModelContextMode.COMPILED
        if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        else ModelContextMode.DIRECT
    )
    fact = ResolvedModelCallFact(
        resolved_model_call_id=(resolved_model_call_id or f"model_call:{uuid4().hex}"),
        purpose=purpose,
        context_mode=mode,
        target=target.fact,
        binding=binding,
    )
    return ResolvedModelCall(target=target, binding=binding, fact=fact)


def with_call_output_cap(
    target: ResolvedModelTarget,
    maximum_output_tokens: int,
) -> ResolvedModelTarget:
    """Apply a purpose-local output bound without changing target identity."""

    if maximum_output_tokens < 1:
        raise ValueError("model output cap must be positive")
    cap = min(maximum_output_tokens, target.limits.max_output_tokens)
    pre_margin = min(
        target.limits.max_input_tokens,
        target.limits.total_context_tokens - cap,
    )
    margin = min(target.limits.input_safety_margin_tokens, pre_margin - 1)
    if pre_margin - margin < 1:
        raise ModelInputBudgetUnavailable(
            "purpose-local model input budget is non-positive"
        )
    budget = ResolvedModelContextBudgetFact(
        effective_output_tokens=cap,
        pre_margin_input_tokens=pre_margin,
        safety_margin_tokens=margin,
        input_budget_tokens=pre_margin - margin,
    )
    return replace(target, context_budget=budget)


def rebind_model_target(
    *,
    connection: ModelConnectionConfig,
    binding: ModelCallBinding,
    catalog: SelectableModelCatalog,
    route_wires: RouteWireRegistry,
    registry: NormalizedLLMTransportRegistry,
    fact: ResolvedModelTargetFact,
) -> ResolvedModelTarget:
    target = resolve_model_target(
        connection=connection,
        binding=binding,
        catalog=catalog,
        route_wires=route_wires,
        registry=registry,
    )
    if target.fact.target_fingerprint != fact.target_fingerprint:
        raise ModelTargetBindingMismatch(
            "current runtime cannot reproduce the persisted model target"
        )
    return target


__all__ = [
    "ResolvedModelCall",
    "ResolvedModelTarget",
    "rebind_model_target",
    "resolve_model_call",
    "resolve_model_target",
    "with_call_output_cap",
]
