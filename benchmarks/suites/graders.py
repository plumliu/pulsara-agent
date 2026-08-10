"""Canonical and operational graders for Kernel-native dogfood runs."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from benchmarks.suites.contracts import (
    AssertionResultFact,
    CanonicalTurnEvidenceFact,
    CoreDogfoodScenarioContract,
    HiddenVerifierResultFact,
    ProviderUsageObservationFact,
)
from pulsara_agent.conversation_kernel.query import CanonicalInspectorView


@dataclass(frozen=True, slots=True)
class GradedKernelEvidence:
    assertions: tuple[AssertionResultFact, ...]
    canonical_turns: tuple[CanonicalTurnEvidenceFact, ...]
    committed_event_counts: tuple[tuple[str, int], ...]
    model_call_count: int
    tool_call_count: int
    total_tokens: int | None
    cached_input_tokens: int | None

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.assertions)


def run_hidden_verifier(
    *,
    scenario_root: Path,
    verifier_path: str,
    workspace: Path,
    timeout_seconds: int,
) -> HiddenVerifierResultFact:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, str(scenario_root / verifier_path), str(workspace)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=workspace,
        )
        return HiddenVerifierResultFact(
            passed=completed.returncode == 0,
            exit_code=completed.returncode,
            elapsed_seconds=time.monotonic() - started,
            stdout=_bounded(completed.stdout),
            stderr=_bounded(completed.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return HiddenVerifierResultFact(
            passed=False,
            exit_code=124,
            elapsed_seconds=time.monotonic() - started,
            stdout=_bounded(_as_text(exc.stdout)),
            stderr=_bounded(
                f"verifier timed out after {timeout_seconds}s\n{_as_text(exc.stderr)}"
            ),
        )


def grade_kernel_evidence(
    *,
    scenario: CoreDogfoodScenarioContract,
    view: CanonicalInspectorView,
    provider_usage: tuple[ProviderUsageObservationFact, ...],
    writer_generations: tuple[int, ...],
    verifier: HiddenVerifierResultFact,
) -> GradedKernelEvidence:
    assertions: list[AssertionResultFact] = []

    def check(assertion_id: str, passed: bool, detail: str) -> None:
        assertions.append(
            AssertionResultFact(
                assertion_id=assertion_id,
                passed=passed,
                detail=_bounded(detail, limit=2_000),
            )
        )

    gate = scenario.evidence_gate
    turns = tuple(view.turns)
    root_turns = tuple(
        item for item in turns if str(item["conversation_scope_kind"]) == "ROOT"
    )
    tools_by_turn, child_tool_count = _tool_inventory(view)
    canonical_turns = tuple(
        CanonicalTurnEvidenceFact(
            turn_id=str(item["id"]),
            scope_kind=str(item["conversation_scope_kind"]),
            status=str(item["status"]),
            tool_names=tuple(tools_by_turn.get(str(item["id"]), ())),
        )
        for item in turns
    )
    event_counts = Counter(str(item["event_type"]) for item in view.selective_events)
    tool_call_count = len(view.tool_attempts)
    assistant_entry_count = sum(
        str(item["entry_kind"])
        in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST"}
        for item in view.conversation.entries
    )
    model_call_count = len(provider_usage)

    check(
        "root_turn_count_in_bounds",
        gate.min_root_turns <= len(root_turns) <= gate.max_root_turns,
        f"root_turns={len(root_turns)}, expected={gate.min_root_turns}..{gate.max_root_turns}",
    )
    check(
        "all_turn_count_in_bounds",
        gate.min_all_turns <= len(turns) <= gate.max_all_turns,
        f"all_turns={len(turns)}, expected={gate.min_all_turns}..{gate.max_all_turns}",
    )
    check(
        "all_turns_completed",
        bool(turns) and all(str(item["status"]) == "COMPLETED" for item in turns),
        f"statuses={tuple(str(item['status']) for item in turns)}",
    )
    check(
        "model_call_count_in_bounds",
        gate.min_model_calls <= model_call_count <= gate.max_model_calls,
        f"model_calls={model_call_count}, expected={gate.min_model_calls}..{gate.max_model_calls}",
    )
    check(
        "usage_exactly_joins_accepted_assistant_entries",
        model_call_count == assistant_entry_count,
        f"usage={model_call_count}, assistant_entries={assistant_entry_count}",
    )
    check(
        "tool_call_count_in_bounds",
        gate.min_tool_calls <= tool_call_count <= gate.max_tool_calls,
        f"tool_calls={tool_call_count}, expected={gate.min_tool_calls}..{gate.max_tool_calls}",
    )
    check(
        "tool_attempts_have_terminal_results",
        all(item.get("result_state") is not None for item in view.tool_attempts),
        "result_states=" + repr(tuple(item.get("result_state") for item in view.tool_attempts)),
    )
    check(
        "hidden_verifier_passed",
        verifier.passed,
        f"exit_code={verifier.exit_code}, stderr={verifier.stderr}",
    )

    for minimum in gate.committed_event_minimums:
        observed = event_counts[minimum.event_type]
        check(
            f"committed_event_{minimum.event_type}",
            observed >= minimum.minimum,
            f"observed={observed}, minimum={minimum.minimum}",
        )
    forbidden = sorted(set(gate.forbidden_event_types) & set(event_counts))
    check(
        "forbidden_committed_events_absent",
        not forbidden,
        f"present={tuple(forbidden)}",
    )

    if gate.root_turn_tool_gate is not None:
        selected = _selected_root_turns(root_turns, gate.root_turn_tool_gate.turn_selector)
        selected_tools = tuple(
            tool
            for turn in selected
            for tool in tools_by_turn.get(str(turn["id"]), ())
        )
        selected_counts = Counter(selected_tools)
        for requirement in gate.root_turn_tool_gate.required_exact_counts:
            check(
                f"root_tool_exact_{requirement.tool_name}",
                selected_counts[requirement.tool_name] == requirement.exact_count,
                f"observed={selected_counts[requirement.tool_name]}, expected={requirement.exact_count}",
            )
        forbidden_tools = sorted(
            set(selected_tools) & set(gate.root_turn_tool_gate.forbidden_tool_names)
        )
        check(
            "root_forbidden_tools_absent",
            not forbidden_tools,
            f"present={tuple(forbidden_tools)}",
        )

    completed_subagents = sum(
        str(item["status"]) == "COMPLETED" for item in view.subagent_tasks
    )
    if gate.minimum_completed_subagent_tasks:
        check(
            "completed_subagent_count",
            completed_subagents >= gate.minimum_completed_subagent_tasks,
            f"completed={completed_subagents}, minimum={gate.minimum_completed_subagent_tasks}",
        )
    if gate.minimum_child_tool_calls:
        check(
            "child_tool_call_minimum",
            child_tool_count >= gate.minimum_child_tool_calls,
            f"child_tools={child_tool_count}, minimum={gate.minimum_child_tool_calls}",
        )

    if gate.expected_prompt_queue_statuses:
        statuses = tuple(str(item["status"]) for item in view.prompt_queue)
        sequences = tuple(int(item["queue_sequence"]) for item in view.prompt_queue)
        check(
            "prompt_queue_terminal_statuses",
            statuses == gate.expected_prompt_queue_statuses,
            f"statuses={statuses}, expected={gate.expected_prompt_queue_statuses}",
        )
        check(
            "prompt_queue_sequence_is_strict_fifo",
            sequences == tuple(sorted(sequences)) and len(sequences) == len(set(sequences)),
            f"queue_sequences={sequences}",
        )

    generation_delta = (
        writer_generations[-1] - writer_generations[0]
        if writer_generations
        else -1
    )
    check(
        "writer_generation_delta",
        generation_delta == gate.expected_writer_generation_delta,
        f"delta={generation_delta}, expected={gate.expected_writer_generation_delta}",
    )

    reported = tuple(item for item in provider_usage if item.usage_status == "reported")
    total_tokens = (
        sum(int(item.total_tokens or 0) for item in reported)
        if len(reported) == len(provider_usage)
        else None
    )
    cached_input_tokens = (
        sum(int(item.cached_input_tokens or 0) for item in reported)
        if len(reported) == len(provider_usage)
        else None
    )
    if gate.require_positive_cached_input_tokens:
        check(
            "provider_reported_positive_cache_hit",
            cached_input_tokens is not None and cached_input_tokens > 0,
            f"cached_input_tokens={cached_input_tokens}",
        )

    return GradedKernelEvidence(
        assertions=tuple(assertions),
        canonical_turns=canonical_turns,
        committed_event_counts=tuple(sorted(event_counts.items())),
        model_call_count=model_call_count,
        tool_call_count=tool_call_count,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
    )


def _tool_inventory(
    view: CanonicalInspectorView,
) -> tuple[dict[str, list[str]], int]:
    by_turn: dict[str, list[str]] = {}
    child_count = 0
    for entry in view.conversation.entries:
        turn_id = str(entry["turn_id"])
        for block in entry["blocks"]:
            if str(block.get("kind")) != "TOOL_CALL":
                continue
            by_turn.setdefault(turn_id, []).append(str(block.get("tool_name")))
            if str(entry["conversation_scope_kind"]) == "SUBAGENT_TASK":
                child_count += 1
    return by_turn, child_count


def _selected_root_turns(
    turns: tuple[dict[str, Any], ...] | tuple[Any, ...], selector: str
) -> tuple[Any, ...]:
    if selector == "first":
        return turns[:1]
    if selector == "last":
        return turns[-1:]
    return turns


def _bounded(value: str, *, limit: int = 4_000) -> str:
    return value if len(value) <= limit else value[: limit - 16] + "\n...[truncated]"


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


__all__ = ["GradedKernelEvidence", "grade_kernel_evidence", "run_hidden_verifier"]
