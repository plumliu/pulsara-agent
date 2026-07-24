"""Semantic and durable-evidence graders for the core dogfood suite."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from benchmarks.suites.contracts import (
    AssertionResultFact,
    CoreDogfoodScenarioContract,
    HiddenVerifierResultFact,
    ProviderCacheCallObservationFact,
    RootRunEvidenceFact,
)


@dataclass(frozen=True, slots=True)
class GradedEvidence:
    assertions: tuple[AssertionResultFact, ...]
    root_runs: tuple[RootRunEvidenceFact, ...]
    all_run_count: int
    event_count: int
    event_counts: tuple[tuple[str, int], ...]
    model_call_count: int
    tool_call_count: int
    total_tokens: int
    cached_input_tokens: int | None
    provider_cache_calls: tuple[ProviderCacheCallObservationFact, ...]
    provider_input_generation_count: int
    provider_input_rollover_count: int

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


def grade_durable_evidence(
    *,
    scenario: CoreDogfoodScenarioContract,
    session_report: dict[str, Any],
    root_run_reports: tuple[dict[str, Any], ...],
    final_texts: tuple[str, ...],
    verifier: HiddenVerifierResultFact,
) -> GradedEvidence:
    assertions: list[AssertionResultFact] = []

    def check(assertion_id: str, passed: bool, detail: str) -> None:
        assertions.append(
            AssertionResultFact(
                assertion_id=assertion_id,
                passed=passed,
                detail=_bounded(detail, limit=2_000),
            )
        )

    counts = {
        str(key): int(value)
        for key, value in dict(session_report.get("event_counts") or {}).items()
    }
    runs = tuple(session_report.get("runs") or ())
    root_run_count = len(root_run_reports)
    all_run_count = len(runs)
    model_call_count = counts.get("MODEL_CALL_START", 0)
    tool_call_count = counts.get("TOOL_CALL_START", 0)

    gate = scenario.evidence_gate
    check(
        "root_run_count_in_bounds",
        gate.min_root_runs <= root_run_count <= gate.max_root_runs,
        f"root_run_count={root_run_count}, expected={gate.min_root_runs}..{gate.max_root_runs}",
    )
    check(
        "all_run_count_in_bounds",
        gate.min_all_runs <= all_run_count <= gate.max_all_runs,
        f"all_run_count={all_run_count}, expected={gate.min_all_runs}..{gate.max_all_runs}",
    )
    check(
        "model_call_count_in_bounds",
        gate.min_model_calls <= model_call_count <= gate.max_model_calls,
        f"model_call_count={model_call_count}, expected={gate.min_model_calls}..{gate.max_model_calls}",
    )
    check(
        "tool_call_count_in_bounds",
        gate.min_tool_calls <= tool_call_count <= gate.max_tool_calls,
        f"tool_call_count={tool_call_count}, expected={gate.min_tool_calls}..{gate.max_tool_calls}",
    )

    run_statuses = tuple(str(item.get("status")) for item in runs)
    check(
        "all_durable_runs_finished",
        bool(run_statuses) and all(status == "finished" for status in run_statuses),
        f"run_statuses={run_statuses}",
    )
    root_statuses = tuple(
        str((report.get("run") or {}).get("status")) for report in root_run_reports
    )
    check(
        "all_root_runs_finished",
        bool(root_statuses) and all(status == "finished" for status in root_statuses),
        f"root_statuses={root_statuses}",
    )

    error_diagnostics = tuple(
        item
        for item in session_report.get("diagnostics") or ()
        if str(item.get("severity", "")).lower() == "error"
    )
    check(
        "inspector_has_no_error_diagnostics",
        not error_diagnostics,
        "error_diagnostic_codes="
        + repr(
            tuple(str(item.get("code", "unknown")) for item in error_diagnostics[:20])
        ),
    )

    for assertion_id, start_type, terminal_type in (
        ("run_lifecycle_balanced", "RUN_START", "RUN_END"),
        ("model_lifecycle_balanced", "MODEL_CALL_START", "MODEL_CALL_END"),
        ("tool_lifecycle_balanced", "TOOL_CALL_START", "TOOL_RESULT_END"),
        (
            "physical_reservations_balanced",
            "PHYSICAL_OPERATION_RESERVATION_CREATED",
            "PHYSICAL_OPERATION_RESERVATION_SETTLED",
        ),
        (
            "checkpoint_barriers_balanced",
            "CHECKPOINT_DISPATCH_BARRIER_INSTALLED",
            "CHECKPOINT_DISPATCH_BARRIER_RELEASED",
        ),
        (
            "provider_generations_balanced",
            "PROVIDER_INPUT_GENERATION_STARTED",
            "PROVIDER_INPUT_GENERATION_CLOSED",
        ),
    ):
        start_count = counts.get(start_type, 0)
        terminal_count = counts.get(terminal_type, 0)
        check(
            assertion_id,
            start_count == terminal_count,
            f"{start_type}={start_count}, {terminal_type}={terminal_count}",
        )

    for requirement in gate.event_count_minimums:
        actual = counts.get(requirement.event_type, 0)
        check(
            f"event_minimum:{requirement.event_type}",
            actual >= requirement.minimum,
            f"actual={actual}, minimum={requirement.minimum}",
        )
    for event_type in gate.forbidden_event_types:
        actual = counts.get(event_type, 0)
        check(
            f"event_forbidden:{event_type}",
            actual == 0,
            f"actual={actual}",
        )

    total_tokens, cached_input_tokens = _usage_totals(session_report)
    usage_rows = tuple(session_report.get("model_usage_by_run") or ())
    reported_usage_calls = sum(
        int(item.get("reported_call_count") or 0) for item in usage_rows
    )
    missing_usage_calls = sum(
        int(item.get("missing_usage_call_count") or 0) for item in usage_rows
    )
    check(
        "provider_usage_is_complete",
        reported_usage_calls == model_call_count and missing_usage_calls == 0,
        f"reported={reported_usage_calls}, missing={missing_usage_calls}, "
        f"model_calls={model_call_count}",
    )
    if gate.require_positive_cached_input_tokens:
        continuation_cache_hit = _has_positive_continuation_cache_hit(session_report)
        check(
            "provider_reported_positive_continuation_cache_hit",
            cached_input_tokens is not None
            and cached_input_tokens > 0
            and continuation_cache_hit,
            f"cached_input_tokens={cached_input_tokens}, "
            f"continuation_cache_hit={continuation_cache_hit}",
        )

    generations = tuple(session_report.get("provider_input_generations") or ())
    provider_cache_calls = _provider_cache_observations(session_report)
    check(
        "model_calls_join_provider_generations",
        len(provider_cache_calls) == model_call_count,
        f"generation_calls={len(provider_cache_calls)}, model_calls={model_call_count}",
    )
    rollover_count = sum(1 for item in generations if item.get("rollover") is not None)
    check(
        "provider_input_rollover_bound",
        rollover_count <= gate.max_provider_input_rollovers,
        f"rollovers={rollover_count}, maximum={gate.max_provider_input_rollovers}",
    )

    if gate.root_run_tool_gate is not None:
        selected = _select_run_reports(
            root_run_reports, gate.root_run_tool_gate.run_selector
        )
        selected_tools = tuple(
            tool for report in selected for tool in _tool_names(report)
        )
        tool_counts = Counter(selected_tools)
        for requirement in gate.root_run_tool_gate.required_exact_counts:
            actual = tool_counts[requirement.tool_name]
            check(
                f"tool_exact:{requirement.tool_name}",
                actual == requirement.exact_count,
                f"actual={actual}, expected={requirement.exact_count}, tools={selected_tools}",
            )
        for tool_name in gate.root_run_tool_gate.forbidden_tool_names:
            actual = tool_counts[tool_name]
            check(
                f"tool_forbidden:{tool_name}",
                actual == 0,
                f"actual={actual}, tools={selected_tools}",
            )

    check(
        "hidden_workspace_verifier",
        verifier.passed,
        f"exit_code={verifier.exit_code}, stderr={verifier.stderr}",
    )

    root_evidence = tuple(
        RootRunEvidenceFact(
            run_id=str((report.get("run") or {}).get("id", "missing")),
            status=str((report.get("run") or {}).get("status", "missing")),
            tool_names=_tool_names(report),
            final_text_sha256=sha256(text.encode("utf-8")).hexdigest(),
            final_text_characters=len(text),
        )
        for report, text in zip(root_run_reports, final_texts, strict=True)
    )
    return GradedEvidence(
        assertions=tuple(assertions),
        root_runs=root_evidence,
        all_run_count=all_run_count,
        event_count=int(session_report.get("event_count") or sum(counts.values())),
        event_counts=tuple(sorted(counts.items())),
        model_call_count=model_call_count,
        tool_call_count=tool_call_count,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
        provider_cache_calls=provider_cache_calls,
        provider_input_generation_count=len(generations),
        provider_input_rollover_count=rollover_count,
    )


def _usage_totals(report: dict[str, Any]) -> tuple[int, int | None]:
    usages = tuple(report.get("model_usage_by_run") or ())
    total = sum(int(item.get("total_tokens") or 0) for item in usages)
    cached_values = tuple(item.get("cached_input_tokens") for item in usages)
    cached = (
        None
        if any(value is None for value in cached_values)
        else sum(int(value) for value in cached_values)
    )
    return total, cached


def _has_positive_continuation_cache_hit(report: dict[str, Any]) -> bool:
    for generation in report.get("provider_input_generations") or ():
        calls = tuple(generation.get("model_calls") or ())
        if any(int(call.get("cached_input_tokens") or 0) > 0 for call in calls[1:]):
            return True
    return False


def _provider_cache_observations(
    report: dict[str, Any],
) -> tuple[ProviderCacheCallObservationFact, ...]:
    observations: list[ProviderCacheCallObservationFact] = []
    for generation in report.get("provider_input_generations") or ():
        generation_id = str(generation.get("generation_id") or "unknown")
        for call in generation.get("model_calls") or ():
            observations.append(
                ProviderCacheCallObservationFact(
                    call_ordinal=len(observations),
                    generation_id=generation_id,
                    generation_revision=(
                        int(call["generation_revision"])
                        if call.get("generation_revision") is not None
                        else None
                    ),
                    resolved_model_call_id=(
                        str(call["resolved_model_call_id"])
                        if call.get("resolved_model_call_id") is not None
                        else None
                    ),
                    cached_input_tokens=(
                        int(call["cached_input_tokens"])
                        if call.get("cached_input_tokens") is not None
                        else None
                    ),
                    cache_ratio=(
                        float(call["cache_ratio"])
                        if call.get("cache_ratio") is not None
                        else None
                    ),
                )
            )
    return tuple(observations)


def _tool_names(report: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    timeline = report.get("timeline") or {}
    for item in timeline.get("items") or ():
        if item.get("kind") != "tool_call":
            continue
        metadata = item.get("metadata") or {}
        tool_name = metadata.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            names.append(tool_name)
    return tuple(names)


def _select_run_reports(
    reports: tuple[dict[str, Any], ...], selector: str
) -> tuple[dict[str, Any], ...]:
    if not reports:
        return ()
    if selector == "first":
        return reports[:1]
    if selector == "last":
        return reports[-1:]
    return reports


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )


def _bounded(value: str, *, limit: int = 4_000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 32] + "\n...[bounded output omitted]..."
