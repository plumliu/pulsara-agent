"""Transport-free model target contracts frozen at product boundaries.

These values may cross compiler, continuity, and purpose-permit boundaries.
They deliberately contain no resolved target, transport, client, registry,
credential, or provider-open callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pulsara_agent.llm.adapters.openai.function_tools import (
    openai_native_function_tool_contract_fingerprint,
)
from pulsara_agent.llm.model_catalog import ReasoningControlContract
from pulsara_agent.llm.model_target import ReasoningLowerer
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionAuthentication,
    ModelConnectionId,
)
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ResolvedModelTargetFact,
)

if TYPE_CHECKING:
    from pulsara_agent.llm.resolution import ResolvedModelCall, ResolvedModelTarget


@dataclass(frozen=True, slots=True)
class FrozenModelConnectionExecutionContract:
    connection_id: ModelConnectionId
    authentication_mode: ModelConnectionAuthentication


@dataclass(frozen=True, slots=True)
class FrozenProviderProjectionStrategy:
    """The exact pure adapter profile used to materialize provider input."""

    route_wire_profile: RouteWireProfile
    transport_binding_id: str
    transport_contract_version: str
    assistant_replay_contract: str
    supported_reasoning_families: frozenset[str]
    lower_reasoning: ReasoningLowerer
    context_materializer: object
    semantic_wire_group: object

    @property
    def native_function_tool_wire_contract_fingerprint(self) -> str:
        return openai_native_function_tool_contract_fingerprint(
            self.route_wire_profile.wire_api
        )


@dataclass(frozen=True, slots=True)
class FrozenTokenEstimatorStrategy:
    """Typed estimator behavior identity without retaining a mutable instance."""

    implementation_type: type[object]
    fact: object


@dataclass(frozen=True, slots=True)
class FrozenEpochModelTargetBundle:
    connection: FrozenModelConnectionExecutionContract
    target_fact: ResolvedModelTargetFact
    reasoning_contract: ReasoningControlContract
    projection_strategy: FrozenProviderProjectionStrategy
    estimator: FrozenTokenEstimatorStrategy

    def __post_init__(self) -> None:
        profile = self.projection_strategy.route_wire_profile
        if (
            profile.wire_api != self.target_fact.wire_api
            or profile.model_identity_policy.value
            != self.target_fact.model_identity_policy
            or self.estimator.fact != self.target_fact.token_estimator
        ):
            raise ValueError("frozen epoch model target bundle drifted")


@dataclass(frozen=True, slots=True)
class FrozenModelInputBudget:
    maximum_input_tokens: int
    maximum_output_tokens: int
    effective_input_budget_tokens: int
    effective_output_tokens: int

    def __post_init__(self) -> None:
        if (
            min(
                self.maximum_input_tokens,
                self.maximum_output_tokens,
                self.effective_input_budget_tokens,
                self.effective_output_tokens,
            )
            < 1
            or self.effective_input_budget_tokens > self.maximum_input_tokens
            or self.effective_output_tokens > self.maximum_output_tokens
        ):
            raise ValueError("frozen model input budget is invalid")


@dataclass(frozen=True, slots=True)
class FrozenProviderPhysicalCallTarget:
    purpose: ModelCallPurpose
    model_call_binding: ModelCallBinding
    target_bundle: FrozenEpochModelTargetBundle
    input_budget: FrozenModelInputBudget

    def __post_init__(self) -> None:
        fact = self.target_bundle.target_fact
        if (
            self.model_call_binding.connection_id
            != self.target_bundle.connection.connection_id
            or self.input_budget.effective_input_budget_tokens
            > fact.limits.max_input_tokens
            or self.input_budget.effective_output_tokens
            > fact.limits.max_output_tokens
            or self.input_budget.effective_input_budget_tokens
            + self.input_budget.effective_output_tokens
            + fact.limits.input_safety_margin_tokens
            > fact.limits.total_context_tokens
        ):
            raise ValueError("physical model call target does not exact-join")


@dataclass(frozen=True, slots=True)
class FrozenEpochModelCallTarget:
    session_id: str
    turn_id: str
    model_call_index: int
    physical_call_target: FrozenProviderPhysicalCallTarget

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or self.model_call_index < 1
            or self.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP
        ):
            raise ValueError("epoch model call target is invalid")

    @property
    def purpose(self) -> ModelCallPurpose:
        return self.physical_call_target.purpose

    @property
    def model_call_binding(self) -> ModelCallBinding:
        return self.physical_call_target.model_call_binding

    @property
    def target_bundle(self) -> FrozenEpochModelTargetBundle:
        return self.physical_call_target.target_bundle

    @property
    def input_budget(self) -> FrozenModelInputBudget:
        return self.physical_call_target.input_budget


def _freeze_provider_projection_strategy(
    target: "ResolvedModelTarget",
) -> FrozenProviderProjectionStrategy:
    route = target.contract.route_wire
    if route.profile.wire_api == "openai_responses":
        from pulsara_agent.llm.adapters.openai.responses import (
            materialize_responses_context_bearing_wire_projection,
            responses_semantic_wire_group,
        )

        context_materializer = materialize_responses_context_bearing_wire_projection
        semantic_wire_group = responses_semantic_wire_group
    elif route.profile.wire_api == "openai_chat_completions":
        from pulsara_agent.llm.adapters.openai.chat_completions import (
            chat_semantic_wire_group,
            materialize_chat_context_bearing_wire_projection,
        )

        context_materializer = materialize_chat_context_bearing_wire_projection
        semantic_wire_group = chat_semantic_wire_group
    else:
        raise ValueError("resolved target lacks a frozen provider materializer")
    return FrozenProviderProjectionStrategy(
        route_wire_profile=target.model_profile.route_wire_profile,
        transport_binding_id=route.transport_binding_id,
        transport_contract_version=route.transport_contract_version,
        assistant_replay_contract=route.assistant_replay_contract,
        supported_reasoning_families=frozenset(route.supported_reasoning_families),
        lower_reasoning=route.lower_reasoning,
        context_materializer=context_materializer,
        semantic_wire_group=semantic_wire_group,
    )


def _freeze_token_estimator_strategy(
    target: "ResolvedModelTarget",
) -> FrozenTokenEstimatorStrategy:
    return FrozenTokenEstimatorStrategy(
        implementation_type=type(target.token_estimator),
        fact=target.token_estimator.fact,
    )


def _freeze_provider_physical_call_target(
    *,
    target: "ResolvedModelTarget",
    call: "ResolvedModelCall",
    maximum_input_tokens: int,
    maximum_output_tokens: int,
) -> FrozenProviderPhysicalCallTarget:
    """Private boundary factory stripping every live resolution object."""

    if (
        call.target is not target
        or call.binding.connection_id != target.connection.id
        or maximum_input_tokens < 1
        or maximum_output_tokens < 1
        or target.context_budget.effective_output_tokens > maximum_output_tokens
    ):
        raise ValueError("resolved physical call target does not exact-join")
    bundle = FrozenEpochModelTargetBundle(
        connection=FrozenModelConnectionExecutionContract(
            connection_id=target.connection.id,
            authentication_mode=target.connection.authentication,
        ),
        target_fact=target.fact,
        reasoning_contract=target.contract.reasoning,
        projection_strategy=_freeze_provider_projection_strategy(target),
        estimator=_freeze_token_estimator_strategy(target),
    )
    return FrozenProviderPhysicalCallTarget(
        purpose=call.fact.purpose,
        model_call_binding=call.binding,
        target_bundle=bundle,
        input_budget=FrozenModelInputBudget(
            maximum_input_tokens=maximum_input_tokens,
            maximum_output_tokens=maximum_output_tokens,
            effective_input_budget_tokens=min(
                maximum_input_tokens,
                target.context_budget.input_budget_tokens,
            ),
            effective_output_tokens=target.context_budget.effective_output_tokens,
        ),
    )


__all__ = [
    "FrozenEpochModelCallTarget",
    "FrozenEpochModelTargetBundle",
    "FrozenModelConnectionExecutionContract",
    "FrozenModelInputBudget",
    "FrozenProviderPhysicalCallTarget",
    "FrozenProviderProjectionStrategy",
    "FrozenTokenEstimatorStrategy",
]
