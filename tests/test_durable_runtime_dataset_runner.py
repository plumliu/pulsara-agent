from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = (
    REPO_ROOT
    / "benchmarks"
    / "durable-runtime"
    / "runners"
    / "run_dataset.py"
)
MANIFEST = (
    REPO_ROOT
    / "benchmarks"
    / "durable-runtime"
    / "datasets"
    / "v1"
    / "manifest.json"
)


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--manifest",
            str(MANIFEST),
            *arguments,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_dataset_runner_validates_all_offline_scenarios() -> None:
    completed = _run("validate")

    assert "writer_scenarios=5" in completed.stdout
    assert "context_scenarios=6" in completed.stdout
    assert "expanded_cases=33" in completed.stdout
    assert "production_valid_cases=27" in completed.stdout
    assert "sensitivity_analysis_cases=2" in completed.stdout
    assert "counterfactual_analysis_cases=4" in completed.stdout
    assert "external_network_access=forbidden" in completed.stdout
    assert "allowed_local_services=postgresql" in completed.stdout
    assert "status=valid" in completed.stdout


def test_dataset_runner_expands_context_cases_deterministically() -> None:
    completed = _run("plan", "--group", "context", "--json")
    payload = json.loads(completed.stdout)

    assert payload["dataset_id"] == "pulsara.durable-runtime.v1"
    assert payload["expanded_case_count"] == 14
    assert [case["ordinal"] for case in payload["cases"]] == list(range(14))
    assert all(case["group"] == "context" for case in payload["cases"])
    assert {
        case["case_key"]
        for case in payload["cases"]
        if case["scenario_id"] == "long-plan-prefix-growth"
    } == {
        "long-plan-prefix-growth:default:process_cold",
        "long-plan-prefix-growth:default:steady_state",
    }
    counterfactual = {
        case["case_key"]
        for case in payload["cases"]
        if case["case_kind"] == "counterfactual_analysis"
    }
    assert counterfactual == set()


def test_dataset_runner_marks_over_production_batch_cases_counterfactual() -> None:
    completed = _run(
        "plan",
        "--scenario",
        "model-semantic-batch-matrix",
        "--json",
    )
    payload = json.loads(completed.stdout)
    by_case = {case["case_id"]: case for case in payload["cases"]}

    assert by_case["batch-1"]["case_kind"] == "counterfactual_analysis"
    assert by_case["batch-1"]["production_acceptance_eligible"] is False
    assert by_case["batch-4"]["case_kind"] == "sensitivity_analysis"
    assert by_case["batch-4"]["production_acceptance_eligible"] is False
    assert by_case["batch-8"]["case_kind"] == "sensitivity_analysis"
    assert by_case["batch-8"]["production_acceptance_eligible"] is False
    assert by_case["batch-16"]["case_kind"] == "production_valid"
    assert by_case["batch-16"]["production_acceptance_eligible"] is True
    assert by_case["batch-32"]["case_kind"] == "counterfactual_analysis"
    assert by_case["batch-32"]["production_acceptance_eligible"] is False
    assert by_case["batch-64"]["case_kind"] == "counterfactual_analysis"
    assert by_case["batch-64"]["production_acceptance_eligible"] is False

    acceptance = _run(
        "plan",
        "--scenario",
        "model-semantic-batch-matrix",
        "--case-kind",
        "production_valid",
        "--json",
    )
    acceptance_payload = json.loads(acceptance.stdout)
    assert {
        case["case_id"] for case in acceptance_payload["cases"]
    } == {"batch-16"}

    sensitivity = _run(
        "plan",
        "--scenario",
        "model-semantic-batch-matrix",
        "--case-kind",
        "sensitivity_analysis",
        "--json",
    )
    sensitivity_payload = json.loads(sensitivity.stdout)
    assert {
        case["case_id"] for case in sensitivity_payload["cases"]
    } == {"batch-4", "batch-8"}


def test_dataset_runner_parallel_smoke_uses_isolated_processes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "smoke.jsonl"
    completed = _run(
        "smoke",
        "--group",
        "writer",
        "--jobs",
        "2",
        "--iterations",
        "2",
        "--output",
        str(output),
    )
    rows = [
        json.loads(line)
        for line in output.read_text(encoding="utf-8").splitlines()
    ]

    assert "expanded_cases=19" in completed.stdout
    assert "result_rows=38" in completed.stdout
    assert len(rows) == 38
    assert all(row["execution_kind"] == "contract_smoke" for row in rows)
    assert all(row["external_network_access"] == "forbidden" for row in rows)
    assert all(row["allowed_local_services"] == ["postgresql"] for row in rows)
    assert all(row["worker_pid"] != os.getpid() for row in rows)
    assert all(row["case_contract_fingerprint"].startswith("sha256:") for row in rows)
    assert [
        (row["case_ordinal"], row["iteration"])
        for row in rows
    ] == [
        (case_ordinal, iteration)
        for case_ordinal in range(19)
        for iteration in range(2)
    ]


def test_dataset_typed_contract_rejects_invalid_long_plan_vector(
    tmp_path: Path,
) -> None:
    dataset_root = tmp_path / "v1"
    writer_root = dataset_root / "writer-scenarios"
    context_root = dataset_root / "context-scenarios"
    writer_root.mkdir(parents=True)
    context_root.mkdir()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    selected = "context-scenarios/long-plan-prefix-growth.json"
    manifest["writer_scenarios"] = [
        "writer-scenarios/model-semantic-batch-matrix.json"
    ]
    manifest["context_scenarios"] = [selected]
    (dataset_root / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    source_writer = (
        MANIFEST.parent
        / "writer-scenarios"
        / "model-semantic-batch-matrix.json"
    )
    (writer_root / source_writer.name).write_text(
        source_writer.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    scenario = json.loads(
        (MANIFEST.parent / selected).read_text(encoding="utf-8")
    )
    scenario["ledger"]["semantic_delta_events_per_call"] = [432]
    (dataset_root / selected).write_text(
        json.dumps(scenario),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--manifest",
            str(dataset_root / "manifest.json"),
            "validate",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "semantic delta vector must match model call count" in completed.stderr


@pytest.mark.parametrize(
    ("dsn", "allowed"),
    [
        ("postgresql:///pulsara", True),
        ("postgresql://localhost/pulsara", True),
        ("postgresql://127.0.0.1/pulsara", True),
        ("postgresql://[::1]/pulsara", True),
        ("postgresql://db.example.com/pulsara", False),
    ],
)
def test_offline_postgres_contract_rejects_external_hosts(
    dsn: str,
    allowed: bool,
) -> None:
    sys.path.insert(0, str(RUNNER.parent))
    from network_guard import (  # noqa: PLC0415
        ExternalNetworkAccessDenied,
        validate_local_postgres_dsn,
    )

    if allowed:
        validate_local_postgres_dsn(dsn)
    else:
        with pytest.raises(ExternalNetworkAccessDenied):
            validate_local_postgres_dsn(dsn)


def test_executable_grader_rejects_missing_or_failed_assertion() -> None:
    sys.path.insert(0, str(RUNNER.parent.parent))
    from graders.semantic import (  # noqa: PLC0415
        SemanticGradeError,
        grade_semantic_assertions,
    )

    required = (
        "ordered_semantic_content_equal",
        "terminal_projection_equal",
        "physical_settlement_valid",
        "accounted_writer_path_only",
    )
    with pytest.raises(SemanticGradeError, match="missing assertions"):
        grade_semantic_assertions(
            grader_id="pulsara.writer.model-semantic-equivalence",
            grader_version="1",
            required_assertion_ids=required,
            observed_assertions={},
        )
    with pytest.raises(SemanticGradeError, match="failed assertions"):
        grade_semantic_assertions(
            grader_id="pulsara.writer.model-semantic-equivalence",
            grader_version="1",
            required_assertion_ids=required,
            observed_assertions={
                assertion: assertion != "terminal_projection_equal"
                for assertion in required
            },
        )


def test_iteration_database_name_is_run_scoped_and_deterministic() -> None:
    sys.path.insert(0, str(RUNNER.parent))
    from postgres_sandbox import iteration_database_name  # noqa: PLC0415

    first = iteration_database_name("benchmark-run:a", "sha256:case", 3)
    repeated = iteration_database_name("benchmark-run:a", "sha256:case", 3)
    another_run = iteration_database_name("benchmark-run:b", "sha256:case", 3)

    assert first == repeated
    assert first != another_run
    assert first.startswith("pulsara_bench_")
    assert len(first) <= 63


def test_writer_benchmark_command_is_explicitly_serial_and_diagnostic_safe() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "benchmark-writer", "--help"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--diagnostic-warmup-iterations" in completed.stdout
    assert "--diagnostic-measured-iterations" in completed.stdout
    assert "--case-id" in completed.stdout
    assert "--jobs" not in completed.stdout


def test_writer_benchmark_rejects_counterfactual_case_execution(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "benchmark-writer",
            "--case-id",
            "batch-32",
            "--postgres-dsn",
            "postgresql://localhost/pulsara",
            "--output",
            str(tmp_path / "unused.jsonl"),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "unknown or filtered benchmark case IDs: batch-32" in completed.stderr


def test_context_benchmark_command_is_serial_and_mode_selectable() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "benchmark-context", "--help"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--scenario" in completed.stdout
    assert "--mode" in completed.stdout
    assert "--diagnostic-warmup-iterations" in completed.stdout
    assert "--diagnostic-measured-iterations" in completed.stdout
    assert "--jobs" not in completed.stdout


def test_all_context_scenarios_have_production_adapters() -> None:
    sys.path.insert(0, str(RUNNER.parent))
    from dataset_contract import load_dataset_manifest  # noqa: PLC0415
    from run_dataset import (  # noqa: PLC0415
        DEFAULT_MANIFEST,
        _EXECUTABLE_CONTEXT_SCENARIO_IDS,
    )

    manifest = load_dataset_manifest(DEFAULT_MANIFEST)
    context_ids = {
        scenario.scenario_id
        for scenario in manifest.scenarios
        if scenario.group == "context"
    }

    assert context_ids == set(_EXECUTABLE_CONTEXT_SCENARIO_IDS)


def test_writer_metric_aggregate_uses_nearest_rank_p95() -> None:
    sys.path.insert(0, str(RUNNER.parent))
    from result_contract import BenchmarkMetricValueFact  # noqa: PLC0415
    from run_dataset import _aggregate_metric  # noqa: PLC0415

    aggregate = _aggregate_metric(
        "writer_seconds_per_1000_source_items",
        [
            BenchmarkMetricValueFact(
                metric_id="writer_seconds_per_1000_source_items",
                value=value,
                unit="seconds_per_1000_source_items",
            )
            for value in range(1, 21)
        ],
    )

    assert aggregate.minimum == 1
    assert aggregate.median == 10.5
    assert aggregate.p95_nearest_rank == 19
    assert aggregate.maximum == 20
