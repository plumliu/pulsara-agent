from __future__ import annotations

import pytest

from pulsara_agent.primitives.long_horizon import (
    LongHorizonActionClass,
    RolloutPhase,
)
from pulsara_agent.runtime.long_horizon.coordinator import (
    allowed_action_classes_for_phase,
)
from pulsara_agent.runtime.tool_action import (
    default_tool_action_classifier_registry,
    terminal_process_tool_action_policy,
    terminal_tool_action_policy,
)
from pulsara_agent.tools import ToolCall


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        ("rg -n TODO src", LongHorizonActionClass.EVIDENCE_ACQUISITION),
        ("git status --short", LongHorizonActionClass.EVIDENCE_ACQUISITION),
        ("uv run pytest -q", LongHorizonActionClass.BOUNDED_VERIFICATION),
        ("python -m pytest -q", LongHorizonActionClass.BOUNDED_VERIFICATION),
        ("sed -i 's/a/b/' file.txt", LongHorizonActionClass.SYNTHESIS_MUTATION),
        ("rm -f output.txt", LongHorizonActionClass.SYNTHESIS_MUTATION),
        ("kill 123", LongHorizonActionClass.PROCESS_CONTROL),
        ("curl https://example.test", LongHorizonActionClass.EXTERNAL_ACTION),
        ("rg TODO > findings.txt", LongHorizonActionClass.EXTERNAL_ACTION),
        ("echo $(cat secret)", LongHorizonActionClass.EXTERNAL_ACTION),
    ),
)
def test_terminal_invocation_classifier_distinguishes_search_write_and_verification(
    command: str,
    expected: LongHorizonActionClass,
) -> None:
    registry = default_tool_action_classifier_registry()
    policy = terminal_tool_action_policy()

    fact = registry.classify(
        call=ToolCall(id="call:terminal", name="terminal", arguments={"command": command}),
        descriptor_id="builtin:terminal",
        descriptor_fingerprint="sha256:terminal",
        policy=policy,
    )

    assert fact.action_class is expected
    assert fact.rollout_cost_units == 1


@pytest.mark.parametrize(
    ("action", "expected"),
    (
        ("list", LongHorizonActionClass.EVIDENCE_HYDRATION),
        ("log", LongHorizonActionClass.EVIDENCE_HYDRATION),
        ("poll", LongHorizonActionClass.EVIDENCE_HYDRATION),
        ("wait", LongHorizonActionClass.EVIDENCE_HYDRATION),
        ("write", LongHorizonActionClass.PROCESS_CONTROL),
        ("submit", LongHorizonActionClass.PROCESS_CONTROL),
        ("kill", LongHorizonActionClass.PROCESS_CONTROL),
    ),
)
def test_terminal_process_action_classifier_separates_observation_from_control(
    action: str,
    expected: LongHorizonActionClass,
) -> None:
    registry = default_tool_action_classifier_registry()
    policy = terminal_process_tool_action_policy()

    fact = registry.classify(
        call=ToolCall(
            id="call:terminal-process",
            name="terminal_process",
            arguments={"action": action},
        ),
        descriptor_id="builtin:terminal_process",
        descriptor_fingerprint="sha256:terminal-process",
        policy=policy,
    )

    assert fact.action_class is expected


def test_finalization_action_classes_preserve_hydration_mutation_and_verification() -> None:
    allowed = set(allowed_action_classes_for_phase(RolloutPhase.FINALIZATION_ONLY))

    assert LongHorizonActionClass.EVIDENCE_HYDRATION in allowed
    assert LongHorizonActionClass.SYNTHESIS_MUTATION in allowed
    assert LongHorizonActionClass.BOUNDED_VERIFICATION in allowed
    assert LongHorizonActionClass.PROCESS_CONTROL in allowed
    assert LongHorizonActionClass.EVIDENCE_ACQUISITION not in allowed
    assert LongHorizonActionClass.EXTERNAL_ACTION not in allowed


def test_exhausted_and_emergency_phases_allow_no_tool_action_class() -> None:
    assert allowed_action_classes_for_phase(RolloutPhase.EXHAUSTED) == ()
    assert allowed_action_classes_for_phase(RolloutPhase.EMERGENCY_HARD_STOP) == ()
