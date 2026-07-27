"""Durable tool-action classification for rollout accounting and phase gates."""

from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Callable

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import FrozenJsonObjectFact, freeze_json
from pulsara_agent.primitives.long_horizon import (
    LongHorizonActionClass,
    LongHorizonToolPolicyFact,
    RolloutPhase,
    ToolActionClassificationFact,
    ToolActionClassifierContractFact,
)
from pulsara_agent.ports.tool_execution import ToolCall


class ToolActionClassifierContractError(RuntimeError):
    """A descriptor and its process-local action classifier disagree."""


ToolActionClassifier = Callable[[ToolCall], tuple[LongHorizonActionClass, int]]


@dataclass(frozen=True, slots=True)
class ToolActionClassifierBinding:
    contract: ToolActionClassifierContractFact
    classify: ToolActionClassifier
    implementation_build_fingerprint: str | None = None


class ToolActionClassifierRegistry:
    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], ToolActionClassifierBinding] = {}

    def register(self, binding: ToolActionClassifierBinding) -> None:
        key = (
            binding.contract.classifier_id,
            binding.contract.classifier_version,
        )
        existing = self._bindings.get(key)
        if existing is not None and (
            existing.contract.contract_fingerprint
            != binding.contract.contract_fingerprint
        ):
            raise ToolActionClassifierContractError(
                "same tool-action classifier ID/version has a different contract"
            )
        self._bindings[key] = binding

    def resolve_binding(
        self,
        classifier_id: str,
        classifier_version: str,
    ) -> ToolActionClassifierBinding:
        try:
            return self._bindings[(classifier_id, classifier_version)]
        except KeyError as exc:
            raise ToolActionClassifierContractError(
                f"tool-action classifier binding is unavailable: "
                f"{classifier_id}@{classifier_version}"
            ) from exc

    def classify(
        self,
        *,
        call: ToolCall,
        descriptor_id: str,
        descriptor_fingerprint: str,
        policy: LongHorizonToolPolicyFact,
    ) -> ToolActionClassificationFact:
        contract = policy.action_classifier_contract
        binding = self.resolve_binding(
            contract.classifier_id,
            contract.classifier_version,
        )
        if binding.contract != contract:
            raise ToolActionClassifierContractError(
                "tool-action classifier contract fingerprint mismatch"
            )
        action_class, rollout_cost_units = binding.classify(call)
        if action_class not in policy.allowed_action_classes:
            raise ToolActionClassifierContractError(
                "tool-action classifier returned a class outside descriptor policy"
            )
        if not 0 <= rollout_cost_units <= policy.max_rollout_cost_units:
            raise ToolActionClassifierContractError(
                "tool-action classifier returned an out-of-policy rollout cost"
            )
        frozen_arguments = freeze_json(call.arguments)
        if not isinstance(frozen_arguments, FrozenJsonObjectFact):
            raise ToolActionClassifierContractError(
                "tool action arguments must normalize to a JSON object"
            )
        normalized_action_fingerprint = context_fingerprint(
            "normalized-tool-action:v1",
            {
                "descriptor_id": descriptor_id,
                "tool_name": call.name,
                "arguments": frozen_arguments.model_dump(mode="json"),
                "classifier_contract_fingerprint": contract.contract_fingerprint,
            },
        )
        payload = {
            "schema_version": "tool_action_classification.v1",
            "tool_call_id": call.id,
            "descriptor_id": descriptor_id,
            "descriptor_fingerprint": descriptor_fingerprint,
            "action_class": action_class,
            "rollout_cost_units": rollout_cost_units,
            "normalized_action_fingerprint": normalized_action_fingerprint,
            "classifier_id": contract.classifier_id,
            "classifier_version": contract.classifier_version,
            "classifier_contract_fingerprint": contract.contract_fingerprint,
        }
        return ToolActionClassificationFact(
            **payload,
            classification_fingerprint=context_fingerprint(
                "tool-action-classification:v1", payload
            ),
        )


def fixed_tool_action_policy(
    action_class: LongHorizonActionClass,
    *,
    rollout_cost_units: int = 1,
) -> LongHorizonToolPolicyFact:
    classifier_id = f"pulsara.tool_action.fixed.{action_class.value}"
    contract = _classifier_contract(
        classifier_id=classifier_id,
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


def default_tool_action_classifier_registry() -> ToolActionClassifierRegistry:
    registry = ToolActionClassifierRegistry()
    for action_class in LongHorizonActionClass:
        policy = fixed_tool_action_policy(action_class)
        contract = policy.action_classifier_contract
        registry.register(
            ToolActionClassifierBinding(
                contract=contract,
                classify=lambda _call, value=action_class: (value, 1),
            )
        )
    from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry

    process_entry = builtin_tool_catalog_entry("terminal_process")
    process_observe_actions = tuple(
        item.discriminator_value
        for item in process_entry.permission_contract.ordered_action_overrides
        if item.allowed_in_read_only
    )
    terminal_policy = process_entry.descriptor.long_horizon_policy
    registry.register(
        ToolActionClassifierBinding(
            contract=terminal_policy.action_classifier_contract,
            classify=_terminal_action_classifier(process_observe_actions),
        )
    )
    monitor_entry = builtin_tool_catalog_entry("terminal_monitor")
    monitor_observe_actions = tuple(
        item.discriminator_value
        for item in monitor_entry.permission_contract.ordered_action_overrides
        if item.allowed_in_read_only
    )
    terminal_monitor_policy = monitor_entry.descriptor.long_horizon_policy
    registry.register(
        ToolActionClassifierBinding(
            contract=terminal_monitor_policy.action_classifier_contract,
            classify=_terminal_action_classifier(monitor_observe_actions),
        )
    )
    terminal_command_policy = terminal_tool_action_policy()
    registry.register(
        ToolActionClassifierBinding(
            contract=terminal_command_policy.action_classifier_contract,
            classify=_classify_terminal_command,
        )
    )
    return registry


def builtin_tool_action_policy(tool_name: str) -> LongHorizonToolPolicyFact:
    """Return the catalog-owned policy for one built-in tool.

    This compatibility query deliberately contains no name/action taxonomy of
    its own.  The built-in catalog remains the only policy owner.
    """

    from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry

    try:
        return builtin_tool_catalog_entry(tool_name).descriptor.long_horizon_policy
    except KeyError:
        return fixed_tool_action_policy(LongHorizonActionClass.EXTERNAL_ACTION)


def mcp_tool_action_policy() -> LongHorizonToolPolicyFact:
    return fixed_tool_action_policy(LongHorizonActionClass.EXTERNAL_ACTION)


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
    payload = {
        "schema_version": "long_horizon_tool_policy.v1",
        "allowed_action_classes": contracts,
        "max_rollout_cost_units": max_rollout_cost_units,
        "allowed_in_phases": allowed_in_phases,
        "action_classifier_contract": contract,
    }
    return LongHorizonToolPolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint("long-horizon-tool-policy:v1", payload),
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


def _terminal_action_classifier(
    observe_actions: tuple[str, ...],
) -> ToolActionClassifier:
    allowed = frozenset(observe_actions)

    def classify(call: ToolCall) -> tuple[LongHorizonActionClass, int]:
        if call.arguments.get("action") in allowed:
            return LongHorizonActionClass.EVIDENCE_HYDRATION, 1
        return LongHorizonActionClass.PROCESS_CONTROL, 1

    return classify


_TERMINAL_READ_ONLY_COMMANDS = frozenset(
    {
        "cat",
        "cut",
        "env",
        "fd",
        "find",
        "git",
        "grep",
        "head",
        "ls",
        "nl",
        "pwd",
        "rg",
        "sed",
        "sort",
        "stat",
        "tail",
        "uniq",
        "wc",
        "which",
    }
)
_TERMINAL_VERIFICATION_COMMANDS = frozenset(
    {
        "cargo",
        "go",
        "mypy",
        "pytest",
        "pyright",
        "ruff",
    }
)
_TERMINAL_MUTATION_COMMANDS = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "install",
        "mkdir",
        "mv",
        "rm",
        "rmdir",
        "touch",
        "truncate",
    }
)
_TERMINAL_PROCESS_CONTROL_COMMANDS = frozenset(
    {"jobs", "kill", "killall", "pgrep", "pkill", "ps"}
)
_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "&"})


def _classify_terminal_command(
    call: ToolCall,
) -> tuple[LongHorizonActionClass, int]:
    command = call.arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return LongHorizonActionClass.EXTERNAL_ACTION, 1
    if any(marker in command for marker in ("`", "$(", "${", ">", "<")):
        return LongHorizonActionClass.EXTERNAL_ACTION, 1
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return LongHorizonActionClass.EXTERNAL_ACTION, 1
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_OPERATORS:
            if not segments[-1]:
                return LongHorizonActionClass.EXTERNAL_ACTION, 1
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments or any(not segment for segment in segments):
        return LongHorizonActionClass.EXTERNAL_ACTION, 1
    classes = tuple(_classify_terminal_segment(segment) for segment in segments)
    if LongHorizonActionClass.EXTERNAL_ACTION in classes:
        return LongHorizonActionClass.EXTERNAL_ACTION, 1
    if LongHorizonActionClass.SYNTHESIS_MUTATION in classes:
        return LongHorizonActionClass.SYNTHESIS_MUTATION, 1
    if all(item is LongHorizonActionClass.BOUNDED_VERIFICATION for item in classes):
        return LongHorizonActionClass.BOUNDED_VERIFICATION, 1
    if all(item is LongHorizonActionClass.PROCESS_CONTROL for item in classes):
        return LongHorizonActionClass.PROCESS_CONTROL, 1
    return LongHorizonActionClass.EVIDENCE_ACQUISITION, 1


def _classify_terminal_segment(tokens: list[str]) -> LongHorizonActionClass:
    executable = tokens[0].rsplit("/", 1)[-1]
    arguments = tokens[1:]
    if executable == "env":
        command_index = 0
        while command_index < len(arguments) and (
            arguments[command_index].startswith("-") or "=" in arguments[command_index]
        ):
            command_index += 1
        if command_index == len(arguments):
            return LongHorizonActionClass.EVIDENCE_ACQUISITION
        return _classify_terminal_segment(arguments[command_index:])
    if executable == "uv" and len(arguments) >= 2 and arguments[0] == "run":
        return _classify_terminal_segment(arguments[1:])
    if executable in {"python", "python3"}:
        if len(arguments) >= 2 and arguments[0] == "-m":
            return _classify_terminal_segment(arguments[1:])
        return LongHorizonActionClass.EXTERNAL_ACTION
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        if arguments and arguments[0] in {"test", "lint", "check"}:
            return LongHorizonActionClass.BOUNDED_VERIFICATION
        return LongHorizonActionClass.EXTERNAL_ACTION
    if executable in _TERMINAL_VERIFICATION_COMMANDS:
        if executable == "cargo" and (
            not arguments or arguments[0] not in {"check", "clippy", "test"}
        ):
            return LongHorizonActionClass.EXTERNAL_ACTION
        if executable == "go" and (not arguments or arguments[0] != "test"):
            return LongHorizonActionClass.EXTERNAL_ACTION
        return LongHorizonActionClass.BOUNDED_VERIFICATION
    if executable in _TERMINAL_MUTATION_COMMANDS:
        return LongHorizonActionClass.SYNTHESIS_MUTATION
    if executable in _TERMINAL_PROCESS_CONTROL_COMMANDS:
        return LongHorizonActionClass.PROCESS_CONTROL
    if executable == "git":
        if not arguments or arguments[0] not in {
            "diff",
            "log",
            "show",
            "status",
        }:
            return LongHorizonActionClass.EXTERNAL_ACTION
        return LongHorizonActionClass.EVIDENCE_ACQUISITION
    if executable == "sed" and any(
        argument == "-i" or argument.startswith("-i") for argument in arguments
    ):
        return LongHorizonActionClass.SYNTHESIS_MUTATION
    if executable == "find" and any(
        argument in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        for argument in arguments
    ):
        return LongHorizonActionClass.EXTERNAL_ACTION
    if executable == "sort" and any(
        argument == "-o" or argument.startswith("--output") for argument in arguments
    ):
        return LongHorizonActionClass.SYNTHESIS_MUTATION
    if executable in _TERMINAL_READ_ONLY_COMMANDS:
        return LongHorizonActionClass.EVIDENCE_ACQUISITION
    return LongHorizonActionClass.EXTERNAL_ACTION


__all__ = [
    "ToolActionClassifierBinding",
    "ToolActionClassifierContractError",
    "ToolActionClassifierRegistry",
    "builtin_tool_action_policy",
    "default_tool_action_classifier_registry",
    "fixed_tool_action_policy",
    "mcp_tool_action_policy",
    "terminal_process_tool_action_policy",
    "terminal_tool_action_policy",
]
