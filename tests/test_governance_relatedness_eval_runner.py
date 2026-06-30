from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

FIXTURE = Path("evals/governance_relatedness/fixtures/v1_semantic.jsonl")
_RUNNER_PATH = Path(__file__).resolve().parents[1] / "evals" / "governance_relatedness" / "runner.py"
_SPEC = importlib.util.spec_from_file_location("governance_relatedness_eval_runner", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


def test_versioned_relatedness_fixture_covers_required_positive_and_negative_slices() -> None:
    cases = runner.load_cases(FIXTURE)

    assert len(cases) == 8
    assert {case.slice for case in cases} == {
        "cross_lingual",
        "alias",
        "paraphrase",
        "hard_negative",
    }
    manifest = Path("evals/governance_relatedness/config.yaml").read_text(encoding="utf-8")
    assert 'fixture_version: "governance-relatedness-fixture:v1"' in manifest
    assert 'embedding_fingerprint: "openai_compatible:text-embedding-v4:1024"' in manifest
    assert "overall_recall_at_k_min: 0.95" in manifest
    assert "candidate_limit: 5" in manifest


def test_relatedness_eval_reports_recall_miss_rate_and_noise_as_independent_gate() -> None:
    cases = runner.load_cases(FIXTURE)
    predictions = {
        case.case_id: [case.relevant_ids[0]]
        for case in cases
    }
    action_predictions = {case.case_id: [] for case in cases}

    report = runner.evaluate_predictions(
        cases,
        predictions,
        k=10,
        destructive_action_predictions=action_predictions,
    )

    assert report.recall_at_k == 1.0
    assert report.miss_rate == 0.0
    assert report.irrelevant_candidate_count == 0
    assert report.destructive_action_false_positive_count == 0
    assert report.destructive_action_gate_evaluated
    assert report.gate_passed


def test_relatedness_eval_fails_when_high_threshold_style_misses_hide_behind_low_noise() -> None:
    cases = runner.load_cases(FIXTURE)
    predictions = {case.case_id: [] for case in cases}

    report = runner.evaluate_predictions(
        cases,
        predictions,
        k=10,
        destructive_action_predictions={case.case_id: [] for case in cases},
    )

    assert report.irrelevant_candidate_count == 0
    assert report.recall_at_k == 0.0
    assert not report.gate_passed


def test_candidate_noise_does_not_masquerade_as_destructive_action_false_positive() -> None:
    cases = runner.load_cases(FIXTURE)
    predictions = {
        case.case_id: [case.relevant_ids[0], "irrelevant:noise"]
        for case in cases
    }

    report = runner.evaluate_predictions(
        cases,
        predictions,
        k=10,
        destructive_action_predictions={case.case_id: [] for case in cases},
    )

    assert report.recall_at_k == 1.0
    assert report.miss_rate == 0.0
    assert report.irrelevant_candidate_count == len(cases)
    assert report.destructive_action_false_positive_count == 0
    assert report.gate_passed


def test_destructive_action_false_positive_bound_is_wired_into_gate() -> None:
    cases = runner.load_cases(FIXTURE)
    predictions = {case.case_id: [case.relevant_ids[0]] for case in cases}
    action_predictions = {
        case.case_id: ["supersede_and_submit"]
        for case in cases
    }

    report = runner.evaluate_predictions(
        cases,
        predictions,
        k=10,
        destructive_action_predictions=action_predictions,
    )

    assert report.recall_at_k == 1.0
    assert report.irrelevant_candidate_count == 0
    assert report.destructive_action_false_positive_count == len(cases)
    assert not report.gate_passed


def test_relatedness_eval_gate_fails_closed_without_planner_action_predictions() -> None:
    cases = runner.load_cases(FIXTURE)
    predictions = {case.case_id: [case.relevant_ids[0]] for case in cases}

    report = runner.evaluate_predictions(cases, predictions, k=10)

    assert report.recall_at_k == 1.0
    assert not report.destructive_action_gate_evaluated
    assert report.destructive_action_eval_case_count == 0
    assert not report.gate_passed


def test_relatedness_eval_gate_is_driven_by_config_yaml() -> None:
    config = runner.load_config()

    assert config.candidate_limit == 5
    assert config.gates.overall_recall_at_k_min == 0.95
    assert config.gates.positive_slice_recall_at_k_min == 0.90
    assert config.gates.overall_miss_rate_max == 0.05
    assert config.gates.destructive_action_false_positive_max == 0


def test_gate_entrypoint_requires_and_consumes_structured_action_predictions(tmp_path) -> None:
    cases = runner.load_cases(FIXTURE)
    candidate_predictions = {
        case.case_id: [case.relevant_ids[0]]
        for case in cases
    }
    structured = tmp_path / "structured.json"
    structured.write_text(
        json.dumps(
            {
                "candidate_predictions": candidate_predictions,
                "destructive_action_predictions": {
                    case.case_id: [] for case in cases
                },
            }
        ),
        encoding="utf-8",
    )
    candidate_only = tmp_path / "candidate-only.json"
    candidate_only.write_text(json.dumps(candidate_predictions), encoding="utf-8")

    assert runner.main(
        ["--fixture", str(FIXTURE), "--predictions", str(structured), "--gate"]
    ) == 0
    assert runner.main(
        ["--fixture", str(FIXTURE), "--predictions", str(candidate_only), "--gate"]
    ) == 1
