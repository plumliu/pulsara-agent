"""Versioned historical bindings for model-call downstream control facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.event import (
    AgentEvent,
    CapabilityGateDecisionEvent,
    EventType,
    RolloutBudgetReservationCreatedEvent,
    RunEndEvent,
    ToolExecutionSuspendedEvent,
    ToolResultEndEvent,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives.model_call import (
    ModelCallControlDisposition,
    sha256_fingerprint,
)
from pulsara_agent.primitives.run_boundary import (
    ModelCallControlDownstreamPredicateContractFact,
    ModelCallControlDownstreamPredicateFact,
)
from pulsara_agent.primitives.run_lifecycle import RunTerminalizationKind


class ModelCallControlDownstreamContractError(RuntimeError):
    """A frozen downstream predicate contract cannot be rebound exactly."""


PredicatePolicy = Literal[
    "accepted_only",
    "accepted_or_termination_suppressed",
    "accepted_or_recovery_suppressed",
]


@dataclass(frozen=True, slots=True)
class _PredicateDefinition:
    code: str
    event_type: EventType
    policy: PredicatePolicy
    variant_conditions: tuple[tuple[str, str], ...]


_V1_DEFINITIONS = (
    _PredicateDefinition(
        "capability_gate_decision",
        EventType.CAPABILITY_GATE_DECISION,
        "accepted_only",
        (("tool_call_id", "result_tool_call_id"),),
    ),
    _PredicateDefinition(
        "tool_rollout_reservation",
        EventType.ROLLOUT_BUDGET_RESERVATION_CREATED,
        "accepted_only",
        (
            ("reservation.owner_kind", "equals:tool_call"),
            ("reservation.owner_id", "result_tool_call_id"),
        ),
    ),
    _PredicateDefinition(
        "tool_execution_suspended",
        EventType.TOOL_EXECUTION_SUSPENDED,
        "accepted_only",
        (("tool_call_id", "result_tool_call_id"),),
    ),
    _PredicateDefinition(
        "tool_result_terminal",
        EventType.TOOL_RESULT_END,
        "accepted_only",
        (("tool_call_id", "result_tool_call_id"),),
    ),
    _PredicateDefinition(
        "run_end_normal",
        EventType.RUN_END,
        "accepted_only",
        (("terminalization_kind", "equals:normal"),),
    ),
    _PredicateDefinition(
        "run_end_user_stop",
        EventType.RUN_END,
        "accepted_or_termination_suppressed",
        (("terminalization_kind", "equals:user_stop"),),
    ),
    _PredicateDefinition(
        "run_end_host_teardown",
        EventType.RUN_END,
        "accepted_or_termination_suppressed",
        (("terminalization_kind", "equals:host_teardown"),),
    ),
    _PredicateDefinition(
        "run_end_execution_failure",
        EventType.RUN_END,
        "accepted_only",
        (("terminalization_kind", "equals:execution_failure"),),
    ),
    _PredicateDefinition(
        "run_end_recovered_interrupted",
        EventType.RUN_END,
        "accepted_or_recovery_suppressed",
        (("terminalization_kind", "equals:recovered_interrupted"),),
    ),
)


@dataclass(frozen=True, slots=True)
class ModelCallControlDownstreamBinding:
    contract: ModelCallControlDownstreamPredicateContractFact
    definitions: tuple[_PredicateDefinition, ...]

    def match(
        self,
        event: AgentEvent,
        *,
        result_tool_call_ids: frozenset[str],
    ) -> ModelCallControlDownstreamPredicateFact | None:
        code = _matching_predicate_code(
            event,
            result_tool_call_ids=result_tool_call_ids,
        )
        if code is None:
            return None
        return next(
            (item for item in self.contract.predicates if item.predicate_code == code),
            None,
        )

    @staticmethod
    def allowed_dispositions(
        predicate: ModelCallControlDownstreamPredicateFact,
    ) -> frozenset[ModelCallControlDisposition]:
        if predicate.required_prior_disposition_policy == "accepted_only":
            return frozenset({ModelCallControlDisposition.ACCEPTED})
        if (
            predicate.required_prior_disposition_policy
            == "accepted_or_termination_suppressed"
        ):
            return frozenset(
                {
                    ModelCallControlDisposition.ACCEPTED,
                    ModelCallControlDisposition.SUPPRESSED_BY_TERMINATION,
                }
            )
        return frozenset(
            {
                ModelCallControlDisposition.ACCEPTED,
                ModelCallControlDisposition.SUPPRESSED_BY_RECOVERY,
            }
        )


class ModelCallControlDownstreamBindingRegistry:
    """Process-local implementations keyed by durable semantic contract identity."""

    def __init__(self) -> None:
        self._bindings: dict[str, ModelCallControlDownstreamBinding] = {}
        self._domain_event_types: set[str] = set()

    def register(
        self,
        *,
        contract_id: str,
        contract_version: str,
        definitions: tuple[_PredicateDefinition, ...],
    ) -> ModelCallControlDownstreamBinding:
        contract = _build_contract(
            contract_id=contract_id,
            contract_version=contract_version,
            definitions=definitions,
        )
        binding = ModelCallControlDownstreamBinding(
            contract=contract,
            definitions=definitions,
        )
        existing = self._bindings.get(contract.contract_fingerprint)
        if existing is not None and existing != binding:
            raise ModelCallControlDownstreamContractError(
                "model control downstream contract identity collision"
            )
        self._bindings[contract.contract_fingerprint] = binding
        self._domain_event_types.update(str(item.event_type) for item in definitions)
        return binding

    def resolve(
        self,
        contract: ModelCallControlDownstreamPredicateContractFact,
    ) -> ModelCallControlDownstreamBinding:
        try:
            binding = self._bindings[contract.contract_fingerprint]
        except KeyError as exc:
            raise ModelCallControlDownstreamContractError(
                "unsupported model control downstream predicate contract"
            ) from exc
        if binding.contract != contract:
            raise ModelCallControlDownstreamContractError(
                "model control downstream contract payload drifted"
            )
        return binding

    def is_domain_event(self, event: AgentEvent) -> bool:
        return str(event.type) in self._domain_event_types


def _build_contract(
    *,
    contract_id: str,
    contract_version: str,
    definitions: tuple[_PredicateDefinition, ...],
) -> ModelCallControlDownstreamPredicateContractFact:
    predicates: list[ModelCallControlDownstreamPredicateFact] = []
    for definition in definitions:
        event_type = str(definition.event_type)
        event_contract = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(
            event_type
        )
        variant_payload = {
            "event_type": event_type,
            "event_schema_version": event_contract.event_schema_version,
            "event_schema_fingerprint": event_contract.event_schema_fingerprint,
            "event_domain_contract_fingerprint": (
                event_contract.domain_contract_fingerprint
            ),
            "variant_conditions": definition.variant_conditions,
        }
        payload = {
            "predicate_code": definition.code,
            "event_type": event_type,
            "event_schema_version": event_contract.event_schema_version,
            "event_variant_contract_fingerprint": sha256_fingerprint(
                "model-control-downstream-event-variant:v1",
                variant_payload,
            ),
            "required_prior_disposition_policy": definition.policy,
        }
        predicates.append(
            ModelCallControlDownstreamPredicateFact(
                **payload,
                predicate_fingerprint=sha256_fingerprint(
                    "model-call-control-downstream-predicate:v1", payload
                ),
            )
        )
    domain_payload = tuple(
        (
            item.event_type,
            item.event_schema_version,
            item.event_variant_contract_fingerprint,
            item.required_prior_disposition_policy,
        )
        for item in predicates
    )
    contract_payload = {
        "schema_version": "model_call_control_downstream_contract.v1",
        "contract_id": contract_id,
        "contract_version": contract_version,
        "predicates": tuple(predicates),
        "control_event_domain_registry_fingerprint": sha256_fingerprint(
            "model-control-event-domain-registry:v1", domain_payload
        ),
    }
    return ModelCallControlDownstreamPredicateContractFact(
        **contract_payload,
        contract_fingerprint=sha256_fingerprint(
            "model-call-control-downstream-contract:v1",
            {
                **contract_payload,
                "predicates": tuple(
                    item.model_dump(mode="json") for item in predicates
                ),
            },
        ),
    )


def _matching_predicate_code(
    event: AgentEvent,
    *,
    result_tool_call_ids: frozenset[str],
) -> str | None:
    if isinstance(event, CapabilityGateDecisionEvent):
        return (
            "capability_gate_decision"
            if event.tool_call_id in result_tool_call_ids
            else None
        )
    if isinstance(event, RolloutBudgetReservationCreatedEvent):
        reservation = event.reservation
        return (
            "tool_rollout_reservation"
            if reservation.owner_kind == "tool_call"
            and reservation.owner_id in result_tool_call_ids
            else None
        )
    if isinstance(event, ToolExecutionSuspendedEvent):
        return (
            "tool_execution_suspended"
            if event.tool_call_id in result_tool_call_ids
            else None
        )
    if isinstance(event, ToolResultEndEvent):
        return (
            "tool_result_terminal"
            if event.tool_call_id in result_tool_call_ids
            else None
        )
    if isinstance(event, RunEndEvent):
        return {
            RunTerminalizationKind.NORMAL: "run_end_normal",
            RunTerminalizationKind.USER_STOP: "run_end_user_stop",
            RunTerminalizationKind.HOST_TEARDOWN: "run_end_host_teardown",
            RunTerminalizationKind.EXECUTION_FAILURE: "run_end_execution_failure",
            RunTerminalizationKind.RECOVERED_INTERRUPTED: (
                "run_end_recovered_interrupted"
            ),
        }[event.terminalization_kind]
    return None


MODEL_CALL_CONTROL_DOWNSTREAM_BINDINGS = (
    ModelCallControlDownstreamBindingRegistry()
)
CURRENT_MODEL_CALL_CONTROL_DOWNSTREAM_BINDING = (
    MODEL_CALL_CONTROL_DOWNSTREAM_BINDINGS.register(
        contract_id="pulsara.model-control-downstream",
        contract_version="v1",
        definitions=_V1_DEFINITIONS,
    )
)
CURRENT_MODEL_CALL_CONTROL_DOWNSTREAM_CONTRACT = (
    CURRENT_MODEL_CALL_CONTROL_DOWNSTREAM_BINDING.contract
)


def build_model_call_control_downstream_contract(
) -> ModelCallControlDownstreamPredicateContractFact:
    return CURRENT_MODEL_CALL_CONTROL_DOWNSTREAM_CONTRACT.model_copy(deep=True)


__all__ = [
    "CURRENT_MODEL_CALL_CONTROL_DOWNSTREAM_BINDING",
    "CURRENT_MODEL_CALL_CONTROL_DOWNSTREAM_CONTRACT",
    "MODEL_CALL_CONTROL_DOWNSTREAM_BINDINGS",
    "ModelCallControlDownstreamBinding",
    "ModelCallControlDownstreamBindingRegistry",
    "ModelCallControlDownstreamContractError",
    "build_model_call_control_downstream_contract",
]
