"""Pure long-horizon tool policies owned by the Builtin catalog."""

from __future__ import annotations

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    LongHorizonActionClass,
    LongHorizonToolPolicyFact,
    RolloutPhase,
    ToolActionClassifierContractFact,
)


def fixed_tool_action_policy(
    action_class: LongHorizonActionClass,
    *,
    rollout_cost_units: int = 1,
) -> LongHorizonToolPolicyFact:
    contract = _classifier_contract(
        classifier_id=f"pulsara.tool_action.fixed.{action_class.value}",
        policy_payload={
            "kind": "fixed",
            "action_class": action_class.value,
            "rollout_cost_units": rollout_cost_units,
        },
    )
    return _tool_policy(
        contracts=(action_class,),
        max_rollout_cost_units=rollout_cost_units,
        allowed_in_phases=_allowed_phases(action_class),
        contract=contract,
    )


def terminal_process_tool_action_policy(
    *,
    observe_actions: tuple[str, ...],
) -> LongHorizonToolPolicyFact:
    contract = _classifier_contract(
        classifier_id="pulsara.tool_action.terminal_process",
        policy_payload={
            "kind": "terminal_process_action",
            "observe_actions": list(observe_actions),
            "default_action_class": LongHorizonActionClass.PROCESS_CONTROL.value,
            "rollout_cost_units": 1,
        },
    )
    return _tool_policy(
        contracts=(
            LongHorizonActionClass.EVIDENCE_HYDRATION,
            LongHorizonActionClass.PROCESS_CONTROL,
        ),
        max_rollout_cost_units=1,
        allowed_in_phases=(
            RolloutPhase.EXPLORATION,
            RolloutPhase.WARNING,
            RolloutPhase.RESTRICTED,
            RolloutPhase.FINALIZATION_ONLY,
        ),
        contract=contract,
    )


def terminal_monitor_tool_action_policy(
    *,
    observe_actions: tuple[str, ...],
) -> LongHorizonToolPolicyFact:
    contract = _classifier_contract(
        classifier_id="pulsara.tool_action.terminal_monitor",
        policy_payload={
            "kind": "terminal_monitor_action",
            "observe_actions": list(observe_actions),
            "default_action_class": LongHorizonActionClass.PROCESS_CONTROL.value,
            "rollout_cost_units": 1,
        },
    )
    return _tool_policy(
        contracts=(
            LongHorizonActionClass.EVIDENCE_HYDRATION,
            LongHorizonActionClass.PROCESS_CONTROL,
        ),
        max_rollout_cost_units=1,
        allowed_in_phases=(
            RolloutPhase.EXPLORATION,
            RolloutPhase.WARNING,
            RolloutPhase.RESTRICTED,
            RolloutPhase.FINALIZATION_ONLY,
        ),
        contract=contract,
    )


def terminal_tool_action_policy() -> LongHorizonToolPolicyFact:
    contract = _classifier_contract(
        classifier_id="pulsara.tool_action.terminal_command",
        policy_payload={
            "kind": "terminal_command_v1",
            "unknown_or_dynamic_shell": LongHorizonActionClass.EXTERNAL_ACTION.value,
            "rollout_cost_units": 1,
        },
    )
    return _tool_policy(
        contracts=tuple(LongHorizonActionClass),
        max_rollout_cost_units=1,
        allowed_in_phases=(
            RolloutPhase.EXPLORATION,
            RolloutPhase.WARNING,
            RolloutPhase.RESTRICTED,
            RolloutPhase.FINALIZATION_ONLY,
        ),
        contract=contract,
    )


def _classifier_contract(
    *,
    classifier_id: str,
    policy_payload: dict[str, object],
) -> ToolActionClassifierContractFact:
    payload = {
        "schema_version": "tool_action_classifier_contract.v1",
        "classifier_id": classifier_id,
        "classifier_version": "1",
        "input_schema_fingerprint": context_fingerprint(
            "tool-action-classifier-input:v1",
            {"tool_call_id": "str", "tool_name": "str", "arguments": "json_object"},
        ),
        "output_schema_fingerprint": context_fingerprint(
            "tool-action-classifier-output:v1",
            {"action_class": "LongHorizonActionClass", "rollout_cost_units": "int"},
        ),
        "classification_policy_fingerprint": context_fingerprint(
            "tool-action-classifier-policy:v1", policy_payload
        ),
    }
    return ToolActionClassifierContractFact(
        **payload,
        contract_fingerprint=context_fingerprint(
            "tool-action-classifier-contract:v1", payload
        ),
    )


def _tool_policy(
    *,
    contracts: tuple[LongHorizonActionClass, ...],
    max_rollout_cost_units: int,
    allowed_in_phases: tuple[RolloutPhase, ...],
    contract: ToolActionClassifierContractFact,
) -> LongHorizonToolPolicyFact:
    return LongHorizonToolPolicyFact(
        schema_version="long_horizon_tool_policy.v1",
        allowed_action_classes=contracts,
        max_rollout_cost_units=max_rollout_cost_units,
        allowed_in_phases=allowed_in_phases,
        action_classifier_contract=contract,
    )


def _allowed_phases(
    action_class: LongHorizonActionClass,
) -> tuple[RolloutPhase, ...]:
    if action_class is LongHorizonActionClass.EVIDENCE_ACQUISITION:
        return (
            RolloutPhase.EXPLORATION,
            RolloutPhase.WARNING,
            RolloutPhase.RESTRICTED,
        )
    if action_class is LongHorizonActionClass.EXTERNAL_ACTION:
        return (RolloutPhase.EXPLORATION, RolloutPhase.WARNING)
    return (
        RolloutPhase.EXPLORATION,
        RolloutPhase.WARNING,
        RolloutPhase.RESTRICTED,
        RolloutPhase.FINALIZATION_ONLY,
    )


__all__ = [
    "fixed_tool_action_policy",
    "terminal_monitor_tool_action_policy",
    "terminal_process_tool_action_policy",
    "terminal_tool_action_policy",
]
