#!/usr/bin/env python3
"""Validate, expand, and smoke-test durable-runtime datasets."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from math import ceil
import os
from pathlib import Path
from statistics import median
import sys
import tempfile
from time import perf_counter
from typing import Any

from psycopg.conninfo import conninfo_to_dict

RUNNER_DIRECTORY = Path(__file__).resolve().parent
BENCHMARK_ROOT = RUNNER_DIRECTORY.parent
REPO_ROOT = BENCHMARK_ROOT.parents[1]
for import_root in (RUNNER_DIRECTORY, BENCHMARK_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from dataset_contract import (  # noqa: E402
    DatasetContractError,
    ResolvedBenchmarkCase,
    canonical_sha256,
    expand_benchmark_cases,
    load_dataset_manifest,
    recompute_case_contract_fingerprint,
    select_case_kind,
)
from context_journal import (  # noqa: E402
    ContextResultJournal,
    ContextSuiteJournal,
)
from network_guard import external_network_guard  # noqa: E402
from postgres_sandbox import PostgresTemplateDatabaseSandbox  # noqa: E402
from progress import BenchmarkProgressReporter  # noqa: E402
from result_contract import (  # noqa: E402
    BenchmarkEnvironmentFact,
    BenchmarkCaseAggregateFact,
    BenchmarkMetricAggregateFact,
    BenchmarkMetricValueFact,
    BenchmarkRunSummaryFact,
    ContextBenchmarkSampleResultFact,
    ContextCompilePointResultFact,
    ContractSmokeResultFact,
    SemanticGradeFact,
    WriterBenchmarkSampleResultFact,
    capture_benchmark_environment,
)
from graders.semantic import grade_semantic_assertions  # noqa: E402
from generators.model_semantic_batch import (  # noqa: E402
    ModelSemanticBatchObservation,
    run_model_semantic_batch_sample,
)
from generators.context_preparation import (  # noqa: E402
    ContextPreparationObservation,
    run_context_preparation_sample,
)
from scenario_contracts import (  # noqa: E402
    ArtifactHeavyToolsScenario,
    CheckpointRebaseRestartScenario,
    IncrementalActiveWindowScenario,
    LongPlanPrefixGrowthScenario,
    ModelSemanticBatchMatrixScenario,
    SemanticBatchCase,
    SingleLongCompactionScenario,
    SubagentTwoChildrenScenario,
)


DEFAULT_MANIFEST = (
    RUNNER_DIRECTORY.parent / "datasets" / "v1" / "manifest.json"
)
_EXECUTABLE_CONTEXT_SCENARIO_IDS = frozenset(
    {
        "artifact-heavy-tools",
        "checkpoint-rebase-and-restart",
        "incremental-active-window",
        "long-plan-prefix-growth",
        "single-long-compaction",
        "subagent-two-children",
    }
)
_DEFAULT_CONTEXT_SCENARIO_ID = "long-plan-prefix-growth"


@dataclass(frozen=True, slots=True)
class ContextBenchmarkRunResult:
    scenario_id: str
    output_path: Path
    summary_path: Path
    summary: BenchmarkRunSummaryFact
    wall_seconds: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pulsara durable-runtime offline dataset runner"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="dataset manifest path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate the dataset contract")
    _add_selection_arguments(validate)

    plan = subparsers.add_parser(
        "plan",
        help="print deterministic benchmark case expansion",
    )
    _add_selection_arguments(plan)
    plan.add_argument("--json", action="store_true", help="emit JSON")

    smoke = subparsers.add_parser(
        "smoke",
        help="exercise deterministic case loading in isolated worker processes",
    )
    _add_selection_arguments(smoke)
    smoke.add_argument(
        "--jobs",
        type=_positive_integer,
        default=min(4, os.cpu_count() or 1),
        help="parallel worker process count",
    )
    smoke.add_argument(
        "--iterations",
        type=_positive_integer,
        default=1,
        help="contract reloads per expanded case",
    )
    smoke.add_argument(
        "--output",
        type=Path,
        required=True,
        help="coordinator-owned JSONL result path",
    )

    benchmark = subparsers.add_parser(
        "benchmark-writer",
        help="run the model semantic batching matrix on local PostgreSQL",
    )
    benchmark.set_defaults(
        group="writer",
        scenario=["model-semantic-batch-matrix"],
        case_kind="production_valid",
    )
    benchmark.add_argument(
        "--case-kind",
        choices=("production_valid", "sensitivity_analysis"),
        default="production_valid",
        help=(
            "run the batch-16 production baseline or the batch-4/8 "
            "sensitivity matrix"
        ),
    )
    benchmark.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="diagnostic case filter; repeatable",
    )
    benchmark.add_argument(
        "--postgres-dsn",
        default=os.getenv("PULSARA_POSTGRES_DSN", ""),
        help="loopback/Unix-socket PostgreSQL DSN",
    )
    benchmark.add_argument(
        "--postgres-admin-dsn",
        default=os.getenv("PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN", ""),
        help="local CREATEDB-capable DSN used only for clone/drop",
    )
    benchmark.add_argument(
        "--template-database",
        default=None,
        help="clean schema database cloned before every sample",
    )
    benchmark.add_argument(
        "--diagnostic-warmup-iterations",
        type=_non_negative_integer,
        default=None,
        help="override warmups; results are not production-acceptance eligible",
    )
    benchmark.add_argument(
        "--diagnostic-measured-iterations",
        type=_positive_integer,
        default=None,
        help="override samples; results are not production-acceptance eligible",
    )
    benchmark.add_argument(
        "--output",
        type=Path,
        required=True,
        help="writer sample JSONL output path",
    )

    context_benchmark = subparsers.add_parser(
        "benchmark-context",
        help="run deterministic context preparation scenarios on local PostgreSQL",
    )
    context_benchmark.set_defaults(
        group="context",
        scenario=[],
        case_kind="production_valid",
    )
    context_benchmark.add_argument(
        "--scenario",
        action="append",
        default=[],
        help=(
            "context scenario ID; one scenario per invocation; defaults to "
            "long-plan-prefix-growth"
        ),
    )
    context_benchmark.add_argument(
        "--mode",
        action="append",
        default=[],
        help="context mode filter; repeatable",
    )
    context_benchmark.add_argument(
        "--postgres-dsn",
        default=os.getenv("PULSARA_POSTGRES_DSN", ""),
        help="loopback/Unix-socket PostgreSQL DSN",
    )
    context_benchmark.add_argument(
        "--postgres-admin-dsn",
        default=os.getenv("PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN", ""),
        help="local CREATEDB-capable DSN used only for clone/drop",
    )
    context_benchmark.add_argument(
        "--template-database",
        default=None,
        help="clean schema database cloned before every sample",
    )
    context_benchmark.add_argument(
        "--diagnostic-warmup-iterations",
        type=_non_negative_integer,
        default=None,
        help="override warmups; results are not production-acceptance eligible",
    )
    context_benchmark.add_argument(
        "--diagnostic-measured-iterations",
        type=_positive_integer,
        default=None,
        help="override samples; results are not production-acceptance eligible",
    )
    context_benchmark.add_argument(
        "--output",
        type=Path,
        required=True,
        help="context sample JSONL output path",
    )
    context_benchmark.add_argument(
        "--progress-log",
        type=Path,
        default=None,
        help="optional operational progress JSONL path",
    )
    context_benchmark.add_argument(
        "--replace-incomplete",
        action="store_true",
        help="delete stale in-progress files for this output before rerunning",
    )

    context_suite = subparsers.add_parser(
        "benchmark-context-suite",
        help="run all deterministic context scenarios serially",
    )
    context_suite.set_defaults(
        group="context",
        scenario=[],
        case_kind="production_valid",
    )
    context_suite.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="optional scenario subset; repeatable; defaults to all six",
    )
    context_suite.add_argument(
        "--postgres-dsn",
        default=os.getenv("PULSARA_POSTGRES_DSN", ""),
        help="loopback/Unix-socket PostgreSQL DSN",
    )
    context_suite.add_argument(
        "--postgres-admin-dsn",
        default=os.getenv("PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN", ""),
        help="local CREATEDB-capable DSN used only for clone/drop",
    )
    context_suite.add_argument(
        "--template-database",
        default=None,
        help="clean schema database cloned before every sample",
    )
    context_suite.add_argument(
        "--diagnostic-warmup-iterations",
        type=_non_negative_integer,
        default=None,
        help="override warmups; suite results are not production eligible",
    )
    context_suite.add_argument(
        "--diagnostic-measured-iterations",
        type=_positive_integer,
        default=None,
        help="override samples; suite results are not production eligible",
    )
    context_suite.add_argument(
        "--output-directory",
        type=Path,
        required=True,
        help="repository-external directory for scenario results and journals",
    )
    context_suite.add_argument(
        "--progress-log",
        type=Path,
        default=None,
        help="operational progress JSONL; defaults inside output directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = load_dataset_manifest(args.manifest)
        selected_scenario_ids = frozenset(args.scenario)
        if args.command == "benchmark-context" and not selected_scenario_ids:
            selected_scenario_ids = frozenset({_DEFAULT_CONTEXT_SCENARIO_ID})
        if args.command == "benchmark-context-suite" and not selected_scenario_ids:
            selected_scenario_ids = _EXECUTABLE_CONTEXT_SCENARIO_IDS
        scenarios = manifest.select(
            group=args.group,
            scenario_ids=selected_scenario_ids,
        )
        cases = expand_benchmark_cases(manifest, scenarios)
        cases = select_case_kind(cases, case_kind=args.case_kind)
        if args.command == "benchmark-writer" and args.case_id:
            selected_case_ids = frozenset(args.case_id)
            cases = tuple(
                case for case in cases if case.case_id in selected_case_ids
            )
            missing_case_ids = selected_case_ids - {
                case.case_id for case in cases
            }
            if missing_case_ids:
                raise DatasetContractError(
                    "unknown or filtered benchmark case IDs: "
                    + ", ".join(sorted(missing_case_ids))
                )
        if args.command == "benchmark-context" and args.mode:
            selected_modes = frozenset(args.mode)
            cases = tuple(case for case in cases if case.mode in selected_modes)
            missing_modes = selected_modes - {case.mode for case in cases}
            if missing_modes:
                raise DatasetContractError(
                    "unknown or filtered context modes: "
                    + ", ".join(sorted(missing_modes))
                )
        if args.command == "validate":
            _print_validation(manifest.dataset_id, scenarios, cases)
            return 0
        if args.command == "plan":
            _print_plan(manifest.dataset_id, cases, as_json=args.json)
            return 0
        if args.command == "smoke":
            _run_smoke(
                cases=cases,
                jobs=args.jobs,
                iterations=args.iterations,
                output=args.output,
            )
            return 0
        if args.command == "benchmark-writer":
            _run_writer_benchmark(
                manifest=manifest,
                cases=cases,
                postgres_dsn=args.postgres_dsn,
                postgres_admin_dsn=args.postgres_admin_dsn,
                template_database=args.template_database,
                diagnostic_warmup_iterations=(
                    args.diagnostic_warmup_iterations
                ),
                diagnostic_measured_iterations=(
                    args.diagnostic_measured_iterations
                ),
                case_filter_applied=bool(args.case_id),
                output=args.output,
            )
            return 0
        if args.command == "benchmark-context":
            _run_context_benchmark(
                manifest=manifest,
                cases=cases,
                postgres_dsn=args.postgres_dsn,
                postgres_admin_dsn=args.postgres_admin_dsn,
                template_database=args.template_database,
                diagnostic_warmup_iterations=(
                    args.diagnostic_warmup_iterations
                ),
                diagnostic_measured_iterations=(
                    args.diagnostic_measured_iterations
                ),
                output=args.output,
                progress_log=args.progress_log,
                replace_incomplete=args.replace_incomplete,
            )
            return 0
        if args.command == "benchmark-context-suite":
            _run_context_suite(
                manifest=manifest,
                cases=cases,
                postgres_dsn=args.postgres_dsn,
                postgres_admin_dsn=args.postgres_admin_dsn,
                template_database=args.template_database,
                diagnostic_warmup_iterations=(
                    args.diagnostic_warmup_iterations
                ),
                diagnostic_measured_iterations=(
                    args.diagnostic_measured_iterations
                ),
                output_directory=args.output_directory,
                progress_log=args.progress_log,
            )
            return 0
    except (DatasetContractError, FileExistsError) as error:
        parser.error(str(error))
    raise AssertionError("unreachable command")


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--group",
        choices=("all", "writer", "context"),
        default="all",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="select one scenario ID; repeatable",
    )
    parser.add_argument(
        "--case-kind",
        choices=(
            "all",
            "production_valid",
            "sensitivity_analysis",
            "counterfactual_analysis",
        ),
        default="all",
        help="filter production, sensitivity, or counterfactual cases",
    )


def _print_validation(dataset_id: str, scenarios: tuple[Any, ...], cases: tuple[Any, ...]) -> None:
    writer_count = sum(scenario.group == "writer" for scenario in scenarios)
    context_count = sum(scenario.group == "context" for scenario in scenarios)
    production_count = sum(
        case.case_kind == "production_valid" for case in cases
    )
    sensitivity_count = sum(
        case.case_kind == "sensitivity_analysis" for case in cases
    )
    counterfactual_count = sum(
        case.case_kind == "counterfactual_analysis" for case in cases
    )
    print(f"dataset_id={dataset_id}")
    print(f"writer_scenarios={writer_count}")
    print(f"context_scenarios={context_count}")
    print(f"expanded_cases={len(cases)}")
    print(f"production_valid_cases={production_count}")
    print(f"sensitivity_analysis_cases={sensitivity_count}")
    print(f"counterfactual_analysis_cases={counterfactual_count}")
    print("external_network_access=forbidden")
    print("allowed_local_services=postgresql")
    print("status=valid")


def _print_plan(
    dataset_id: str,
    cases: tuple[ResolvedBenchmarkCase, ...],
    *,
    as_json: bool,
) -> None:
    payload = {
        "dataset_id": dataset_id,
        "expanded_case_count": len(cases),
        "cases": [_case_payload(case) for case in cases],
    }
    if as_json:
        print(json.dumps(payload, sort_keys=True, indent=2))
        return
    print(f"dataset_id={dataset_id}")
    for case in cases:
        print(
            f"{case.ordinal:03d} {case.group:<7} "
            f"{case.scenario_id:<36} {case.case_id:<32} {case.mode}"
        )
    print(f"expanded_case_count={len(cases)}")


def _run_smoke(
    *,
    cases: tuple[ResolvedBenchmarkCase, ...],
    jobs: int,
    iterations: int,
    output: Path,
) -> None:
    output_path = output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    environment = capture_benchmark_environment(
        repo_root=REPO_ROOT,
        runner_build_fingerprint=_runner_build_fingerprint(),
    )
    indexed_results: list[tuple[int, dict[str, Any]]] = []
    work = [
        (case, iteration, environment)
        for case in cases
        for iteration in range(iterations)
    ]
    with ProcessPoolExecutor(max_workers=jobs) as executor:
        future_indexes = {
            executor.submit(_smoke_worker, case, iteration, environment): index
            for index, (case, iteration, environment) in enumerate(work)
        }
        for future in as_completed(future_indexes):
            indexed_results.append((future_indexes[future], future.result()))
    indexed_results.sort(key=lambda item: item[0])
    with output_path.open("w", encoding="utf-8") as stream:
        for _, result in indexed_results:
            stream.write(json.dumps(result, sort_keys=True))
            stream.write("\n")
    elapsed = perf_counter() - started
    print(f"expanded_cases={len(cases)}")
    print(f"iterations_per_case={iterations}")
    print(f"worker_processes={jobs}")
    print(f"result_rows={len(indexed_results)}")
    print(f"wall_seconds={elapsed:.6f}")
    print(f"output={output_path}")
    print("status=valid")


def _smoke_worker(
    case: ResolvedBenchmarkCase,
    iteration: int,
    environment: BenchmarkEnvironmentFact,
) -> dict[str, Any]:
    started = perf_counter()
    with external_network_guard():
        observed_case_fingerprint = recompute_case_contract_fingerprint(case)
        if observed_case_fingerprint != case.case_contract_fingerprint:
            raise DatasetContractError(
                f"resolved case drifted after planning: {case.case_key}"
            )
        result = ContractSmokeResultFact(
            schema_version="pulsara.durable-runtime.contract-smoke-result.v2",
            execution_kind="contract_smoke",
            dataset_id=case.dataset_id,
            manifest_contract_fingerprint=case.manifest_contract_fingerprint,
            scenario_id=case.scenario_id,
            scenario_contract_fingerprint=case.scenario_contract_fingerprint,
            case_contract_fingerprint=case.case_contract_fingerprint,
            group=case.group,
            case_ordinal=case.ordinal,
            case_id=case.case_id,
            case_kind=case.case_kind,
            mode=case.mode,
            iteration=iteration,
            seed=case.seed,
            worker_pid=os.getpid(),
            elapsed_seconds=perf_counter() - started,
            completed_at_utc=datetime.now(UTC),
            external_network_access="forbidden",
            allowed_local_services=("postgresql",),
            production_acceptance_eligible=(
                case.production_acceptance_eligible
            ),
            semantic_grade_status="not_applicable_contract_smoke",
            environment=environment,
        )
    return result.model_dump(mode="json")


def _run_writer_benchmark(
    *,
    manifest,
    cases: tuple[ResolvedBenchmarkCase, ...],
    postgres_dsn: str,
    postgres_admin_dsn: str,
    template_database: str | None,
    diagnostic_warmup_iterations: int | None,
    diagnostic_measured_iterations: int | None,
    case_filter_applied: bool,
    output: Path,
) -> None:
    if not postgres_dsn.strip():
        raise DatasetContractError(
            "benchmark-writer requires --postgres-dsn or PULSARA_POSTGRES_DSN"
        )
    admin_dsn = postgres_admin_dsn.strip() or postgres_dsn
    if not cases:
        raise DatasetContractError("benchmark-writer selected no cases")
    if any(case.scenario_id != "model-semantic-batch-matrix" for case in cases):
        raise DatasetContractError(
            "benchmark-writer currently supports only model-semantic-batch-matrix"
        )
    executable_case_kinds = {"production_valid", "sensitivity_analysis"}
    if any(case.case_kind not in executable_case_kinds for case in cases):
        raise DatasetContractError(
            "production writer adapter rejects counterfactual batching targets"
        )
    selected_case_kinds = {case.case_kind for case in cases}
    if len(selected_case_kinds) != 1:
        raise DatasetContractError(
            "writer benchmark cannot mix baseline and sensitivity cases"
        )
    scenario = cases[0].scenario_contract
    if not isinstance(scenario, ModelSemanticBatchMatrixScenario):
        raise DatasetContractError("writer benchmark scenario binding drifted")
    if any(case.scenario_contract != scenario for case in cases):
        raise DatasetContractError("writer benchmark cases span multiple scenarios")
    template = template_database or conninfo_to_dict(postgres_dsn).get("dbname")
    if not template:
        raise DatasetContractError(
            "template database is required when the DSN omits dbname"
        )
    configured_warmups = cases[0].warmup_iterations
    configured_measured = cases[0].measured_iterations
    warmups = (
        configured_warmups
        if diagnostic_warmup_iterations is None
        else diagnostic_warmup_iterations
    )
    measured = (
        configured_measured
        if diagnostic_measured_iterations is None
        else diagnostic_measured_iterations
    )
    expected_case_ids = {
        item.case_id
        for item in scenario.execution_matrix
        if item.case_kind in selected_case_kinds
    }
    measurement_contract_adhered = (
        warmups == configured_warmups and measured == configured_measured
        and {case.case_id for case in cases} == expected_case_ids
        and not case_filter_applied
    )
    output_path = output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    benchmark_run_id = _benchmark_run_id(
        manifest.manifest_contract_fingerprint
    )
    environment = capture_benchmark_environment(
        repo_root=REPO_ROOT,
        runner_build_fingerprint=_runner_build_fingerprint(),
        postgres_dsn=postgres_dsn,
    )
    sample_rows: list[WriterBenchmarkSampleResultFact] = []
    sample_ordinal = 0
    database_ordinal = 0
    started = perf_counter()
    with external_network_guard(), tempfile.TemporaryDirectory(
        prefix="pulsara-durable-runtime-"
    ) as workspace:
        workspace_root = Path(workspace)
        for phase, iteration_count in (
            ("warmup", warmups),
            ("measured", measured),
        ):
            for matrix_iteration in range(iteration_count):
                observations: dict[str, ModelSemanticBatchObservation] = {}
                for case in _rotated_cases(cases, matrix_iteration):
                    execution_case = case.execution_case
                    if not isinstance(execution_case, SemanticBatchCase):
                        raise DatasetContractError(
                            "writer benchmark execution case binding drifted"
                        )
                    sandbox = PostgresTemplateDatabaseSandbox(
                        application_dsn=postgres_dsn,
                        admin_dsn=admin_dsn,
                        template_database=template,
                        benchmark_run_id=benchmark_run_id,
                        case_contract_fingerprint=(
                            case.case_contract_fingerprint
                        ),
                        iteration=database_ordinal,
                    )
                    database_ordinal += 1
                    with sandbox as iteration_database:
                        try:
                            observation = asyncio.run(
                                run_model_semantic_batch_sample(
                                    scenario=scenario,
                                    execution_case=execution_case,
                                    dsn=iteration_database.dsn,
                                    workspace_root=workspace_root,
                                    sample_identity=(
                                        f"{benchmark_run_id}:{phase}:"
                                        f"{matrix_iteration}:{case.case_id}"
                                    ),
                                )
                            )
                        finally:
                            from pulsara_agent.event_log.postgres_pool import (
                                close_postgres_event_pool,
                            )

                            close_postgres_event_pool(iteration_database.dsn)
                    observations[case.case_id] = observation
                if phase == "warmup":
                    continue
                reference = observations.get(
                    scenario.semantic_reference_case_id
                )
                if reference is None:
                    reference = _reference_for_selected_cases(
                        cases,
                        observations,
                    )
                for case in cases:
                    observation = observations[case.case_id]
                    semantic_grade = _grade_batch_observation(
                        case=case,
                        observation=observation,
                        reference=reference,
                    )
                    sample_rows.append(
                        _writer_sample_result(
                            benchmark_run_id=benchmark_run_id,
                            case=case,
                            observation=observation,
                            semantic_grade=semantic_grade,
                            sample_ordinal=sample_ordinal,
                            matrix_iteration=matrix_iteration,
                            configured_warmup_iterations=(
                                configured_warmups
                            ),
                            configured_measured_iterations=(
                                configured_measured
                            ),
                            measurement_contract_adhered=(
                                measurement_contract_adhered
                            ),
                            environment=environment,
                        )
                    )
                    sample_ordinal += 1
    encoded_rows = b"".join(
        (
            json.dumps(
                row.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for row in sample_rows
    )
    output_path.write_bytes(encoded_rows)
    summary = BenchmarkRunSummaryFact(
        schema_version="pulsara.durable-runtime.run-summary.v1",
        benchmark_run_id=benchmark_run_id,
        dataset_id=manifest.dataset_id,
        manifest_contract_fingerprint=(
            manifest.manifest_contract_fingerprint
        ),
        scenario_id=scenario.scenario_id,
        case_contract_fingerprints=tuple(
            case.case_contract_fingerprint for case in cases
        ),
        sample_count=len(sample_rows),
        raw_sample_vector_sha256=(
            f"sha256:{sha256(encoded_rows).hexdigest()}"
        ),
        percentile_contract="nearest_rank_v1",
        case_aggregates=_aggregate_writer_samples(sample_rows),
        measurement_contract_adhered=measurement_contract_adhered,
        production_acceptance_passed=(
            measurement_contract_adhered
            and all(row.production_acceptance_eligible for row in sample_rows)
        ),
        counterfactual_samples_excluded=all(
            case.case_kind != "counterfactual_analysis" for case in cases
        ),
        environment=environment,
    )
    summary_path = output_path.with_suffix(output_path.suffix + ".summary.json")
    summary_path.write_text(
        json.dumps(summary.model_dump(mode="json"), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(f"benchmark_run_id={benchmark_run_id}")
    print(f"cases={len(cases)}")
    print(f"warmup_iterations={warmups}")
    print(f"measured_iterations={measured}")
    print(f"sample_rows={len(sample_rows)}")
    print(f"measurement_contract_adhered={measurement_contract_adhered}")
    print(f"wall_seconds={perf_counter() - started:.6f}")
    print(f"output={output_path}")
    print(f"summary={summary_path}")
    print("status=valid")


def _run_context_suite(
    *,
    manifest,
    cases: tuple[ResolvedBenchmarkCase, ...],
    postgres_dsn: str,
    postgres_admin_dsn: str,
    template_database: str | None,
    diagnostic_warmup_iterations: int | None,
    diagnostic_measured_iterations: int | None,
    output_directory: Path,
    progress_log: Path | None,
) -> None:
    if not postgres_dsn.strip():
        raise DatasetContractError(
            "benchmark-context-suite requires --postgres-dsn or "
            "PULSARA_POSTGRES_DSN"
        )
    if not cases:
        raise DatasetContractError("benchmark-context-suite selected no cases")
    scenario_ids = tuple(sorted({case.scenario_id for case in cases}))
    if not set(scenario_ids) <= _EXECUTABLE_CONTEXT_SCENARIO_IDS:
        raise DatasetContractError(
            "context suite includes a scenario without a production adapter"
        )
    formal_run = (
        diagnostic_warmup_iterations is None
        and diagnostic_measured_iterations is None
    )
    if formal_run and set(scenario_ids) != _EXECUTABLE_CONTEXT_SCENARIO_IDS:
        raise DatasetContractError(
            "formal context suite must include all executable scenarios"
        )
    environment = capture_benchmark_environment(
        repo_root=REPO_ROOT,
        runner_build_fingerprint=_runner_build_fingerprint(),
        postgres_dsn=postgres_dsn,
    )
    if formal_run and environment.git.dirty:
        raise DatasetContractError(
            "formal context suite requires a clean Git worktree"
        )
    output_root = _repository_external_output_directory(output_directory)
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(
            f"context suite output directory is not empty: {output_root}"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    actual_progress_log = (
        progress_log.expanduser().resolve()
        if progress_log is not None
        else output_root / "context-suite-progress.jsonl"
    )
    _require_repository_external_path(actual_progress_log)
    grouped = {
        scenario_id: tuple(
            case for case in cases if case.scenario_id == scenario_id
        )
        for scenario_id in scenario_ids
    }
    total_trajectories = sum(
        _context_trajectory_count(
            scenario_cases,
            diagnostic_warmup_iterations=diagnostic_warmup_iterations,
            diagnostic_measured_iterations=diagnostic_measured_iterations,
        )
        for scenario_cases in grouped.values()
    )
    reporter = BenchmarkProgressReporter(
        total_trajectories=total_trajectories,
        jsonl_path=actual_progress_log,
    )
    suite_journal = ContextSuiteJournal(
        output_directory=output_root,
        dataset_id=manifest.dataset_id,
        manifest_contract_fingerprint=(
            manifest.manifest_contract_fingerprint
        ),
        git_commit=environment.git.commit,
        expected_scenario_ids=scenario_ids,
        total_trajectories=total_trajectories,
    )
    started = perf_counter()
    try:
        for scenario_id in scenario_ids:
            output_path = (
                output_root
                / f"{scenario_id}-{environment.git.commit[:8]}.jsonl"
            )
            result = _run_context_benchmark(
                manifest=manifest,
                cases=grouped[scenario_id],
                postgres_dsn=postgres_dsn,
                postgres_admin_dsn=postgres_admin_dsn,
                template_database=template_database,
                diagnostic_warmup_iterations=(
                    diagnostic_warmup_iterations
                ),
                diagnostic_measured_iterations=(
                    diagnostic_measured_iterations
                ),
                output=output_path,
                progress_log=None,
                replace_incomplete=False,
                progress_reporter=reporter,
            )
            suite_journal.scenario_completed(
                scenario_id=scenario_id,
                output_file=result.output_path.name,
                summary_file=result.summary_path.name,
                summary_file_sha256=(
                    f"sha256:{sha256(result.summary_path.read_bytes()).hexdigest()}"
                ),
                benchmark_run_id=result.summary.benchmark_run_id,
                sample_count=result.summary.sample_count,
                raw_sample_vector_sha256=(
                    result.summary.raw_sample_vector_sha256
                ),
                measurement_contract_adhered=(
                    result.summary.measurement_contract_adhered
                ),
                production_acceptance_passed=(
                    result.summary.production_acceptance_passed
                ),
            )
        suite_journal.finalize()
    except BaseException as error:
        suite_journal.mark_failed(error)
        raise
    print(f"scenario_count={len(scenario_ids)}")
    print(f"total_trajectories={total_trajectories}")
    print(f"wall_seconds={perf_counter() - started:.6f}")
    print(f"output_directory={output_root}")
    print(f"progress_log={actual_progress_log}")
    print(f"suite_summary={suite_journal.summary_path}")
    print("status=valid")


def _run_context_benchmark(
    *,
    manifest,
    cases: tuple[ResolvedBenchmarkCase, ...],
    postgres_dsn: str,
    postgres_admin_dsn: str,
    template_database: str | None,
    diagnostic_warmup_iterations: int | None,
    diagnostic_measured_iterations: int | None,
    output: Path,
    progress_log: Path | None,
    replace_incomplete: bool,
    progress_reporter: BenchmarkProgressReporter | None = None,
) -> ContextBenchmarkRunResult:
    if not postgres_dsn.strip():
        raise DatasetContractError(
            "benchmark-context requires --postgres-dsn or PULSARA_POSTGRES_DSN"
        )
    if not cases:
        raise DatasetContractError("benchmark-context selected no cases")
    scenario_ids = {case.scenario_id for case in cases}
    if len(scenario_ids) != 1:
        raise DatasetContractError(
            "benchmark-context runs one scenario per invocation so its "
            "measurement contract and summary remain unambiguous"
        )
    scenario_id = next(iter(scenario_ids))
    if scenario_id not in _EXECUTABLE_CONTEXT_SCENARIO_IDS:
        raise DatasetContractError(
            f"context production adapter is not implemented for {scenario_id!r}"
        )
    scenario = cases[0].scenario_contract
    if not isinstance(
        scenario,
        (
            LongPlanPrefixGrowthScenario,
            IncrementalActiveWindowScenario,
            ArtifactHeavyToolsScenario,
            CheckpointRebaseRestartScenario,
            SingleLongCompactionScenario,
            SubagentTwoChildrenScenario,
        ),
    ):
        raise DatasetContractError("context benchmark scenario binding drifted")
    if any(case.scenario_contract != scenario for case in cases):
        raise DatasetContractError("context benchmark cases span multiple scenarios")
    if any(case.case_kind != "production_valid" for case in cases):
        raise DatasetContractError(
            "context production adapter rejects counterfactual cases"
        )
    admin_dsn = postgres_admin_dsn.strip() or postgres_dsn
    template = template_database or conninfo_to_dict(postgres_dsn).get("dbname")
    if not template:
        raise DatasetContractError(
            "template database is required when the DSN omits dbname"
        )
    configured_warmups = cases[0].warmup_iterations
    configured_measured = cases[0].measured_iterations
    if any(
        case.warmup_iterations != configured_warmups
        or case.measured_iterations != configured_measured
        for case in cases
    ):
        raise DatasetContractError("context mode measurement contracts drifted")
    warmups = (
        configured_warmups
        if diagnostic_warmup_iterations is None
        else diagnostic_warmup_iterations
    )
    measured = (
        configured_measured
        if diagnostic_measured_iterations is None
        else diagnostic_measured_iterations
    )
    expected_modes = {
        case.mode
        for case in expand_benchmark_cases(
            manifest,
            manifest.select(
                group="context",
                scenario_ids=frozenset({scenario_id}),
            ),
        )
    }
    measurement_contract_adhered = (
        warmups == configured_warmups
        and measured == configured_measured
        and {case.mode for case in cases} == expected_modes
    )
    output_path = output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    benchmark_run_id = _benchmark_run_id(
        manifest.manifest_contract_fingerprint
    )
    environment = capture_benchmark_environment(
        repo_root=REPO_ROOT,
        runner_build_fingerprint=_runner_build_fingerprint(),
        postgres_dsn=postgres_dsn,
    )
    owns_reporter = progress_reporter is None
    reporter = progress_reporter or BenchmarkProgressReporter(
        total_trajectories=(warmups + measured) * len(cases),
        jsonl_path=progress_log,
    )
    journal = ContextResultJournal(
        output_path=output_path,
        benchmark_run_id=benchmark_run_id,
        scenario_id=scenario_id,
        expected_rows=measured * len(cases),
        git_commit=environment.git.commit,
        replace_incomplete=replace_incomplete,
    )
    sample_rows: list[ContextBenchmarkSampleResultFact] = []
    sample_ordinal = 0
    database_ordinal = 0
    started = perf_counter()
    try:
        with external_network_guard(), tempfile.TemporaryDirectory(
            prefix="pulsara-durable-context-"
        ) as workspace:
            workspace_root = Path(workspace)
            for phase, iteration_count in (
                ("warmup", warmups),
                ("measured", measured),
            ):
                for matrix_iteration in range(iteration_count):
                    for case in _rotated_cases(cases, matrix_iteration):
                        token = reporter.start(
                            scenario_id=scenario_id,
                            case_id=case.case_key,
                            mode=case.mode,
                            phase=phase,
                            matrix_iteration=matrix_iteration,
                        )
                        try:
                            sandbox = PostgresTemplateDatabaseSandbox(
                                application_dsn=postgres_dsn,
                                admin_dsn=admin_dsn,
                                template_database=template,
                                benchmark_run_id=benchmark_run_id,
                                case_contract_fingerprint=(
                                    case.case_contract_fingerprint
                                ),
                                iteration=database_ordinal,
                            )
                            database_ordinal += 1
                            with sandbox as iteration_database:
                                try:
                                    observation = asyncio.run(
                                        run_context_preparation_sample(
                                            scenario=scenario,
                                            execution_case=(
                                                case.execution_case
                                                if isinstance(
                                                    scenario,
                                                    CheckpointRebaseRestartScenario,
                                                )
                                                else None
                                            ),
                                            mode=case.mode,
                                            dsn=iteration_database.dsn,
                                            workspace_root=workspace_root,
                                            sample_identity=(
                                                f"{benchmark_run_id}:{phase}:"
                                                f"{matrix_iteration}:"
                                                f"{case.case_key}"
                                            ),
                                        )
                                    )
                                finally:
                                    from pulsara_agent.event_log.postgres_pool import (
                                        close_postgres_event_pool,
                                    )

                                    close_postgres_event_pool(
                                        iteration_database.dsn
                                    )
                            if phase == "measured":
                                semantic_grade = _grade_context_observation(
                                    case=case,
                                    observation=observation,
                                )
                                row = _context_sample_result(
                                    benchmark_run_id=benchmark_run_id,
                                    case=case,
                                    observation=observation,
                                    semantic_grade=semantic_grade,
                                    sample_ordinal=sample_ordinal,
                                    matrix_iteration=matrix_iteration,
                                    configured_warmup_iterations=(
                                        configured_warmups
                                    ),
                                    configured_measured_iterations=(
                                        configured_measured
                                    ),
                                    measurement_contract_adhered=(
                                        measurement_contract_adhered
                                    ),
                                    environment=environment,
                                )
                                journal.append(row.model_dump(mode="json"))
                                sample_rows.append(row)
                                sample_ordinal += 1
                            reporter.passed(token)
                        except BaseException as error:
                            reporter.failed(token, error)
                            raise
        summary = BenchmarkRunSummaryFact(
            schema_version="pulsara.durable-runtime.run-summary.v1",
            benchmark_run_id=benchmark_run_id,
            dataset_id=manifest.dataset_id,
            manifest_contract_fingerprint=(
                manifest.manifest_contract_fingerprint
            ),
            scenario_id=scenario_id,
            case_contract_fingerprints=tuple(
                case.case_contract_fingerprint for case in cases
            ),
            sample_count=len(sample_rows),
            raw_sample_vector_sha256=journal.raw_sample_vector_sha256,
            percentile_contract="nearest_rank_v1",
            case_aggregates=_aggregate_context_samples(sample_rows),
            measurement_contract_adhered=measurement_contract_adhered,
            production_acceptance_passed=(
                measurement_contract_adhered
                and all(
                    row.production_acceptance_eligible for row in sample_rows
                )
            ),
            counterfactual_samples_excluded=True,
            environment=environment,
        )
        journal.finalize(summary.model_dump(mode="json"))
    except BaseException as error:
        journal.mark_failed(error)
        raise
    wall_seconds = perf_counter() - started
    print(f"benchmark_run_id={benchmark_run_id}")
    print(f"scenario_id={scenario_id}")
    print(f"modes={len(cases)}")
    print(f"warmup_iterations={warmups}")
    print(f"measured_iterations={measured}")
    print(f"sample_rows={len(sample_rows)}")
    print(f"measurement_contract_adhered={measurement_contract_adhered}")
    print(f"wall_seconds={wall_seconds:.6f}")
    print(f"output={journal.output_path}")
    print(f"summary={journal.summary_path}")
    if owns_reporter and progress_log is not None:
        print(f"progress_log={progress_log.expanduser().resolve()}")
    print("status=valid")
    return ContextBenchmarkRunResult(
        scenario_id=scenario_id,
        output_path=journal.output_path,
        summary_path=journal.summary_path,
        summary=summary,
        wall_seconds=wall_seconds,
    )


def _context_trajectory_count(
    cases: tuple[ResolvedBenchmarkCase, ...],
    *,
    diagnostic_warmup_iterations: int | None,
    diagnostic_measured_iterations: int | None,
) -> int:
    if not cases:
        raise DatasetContractError("context trajectory count requires cases")
    configured_warmups = cases[0].warmup_iterations
    configured_measured = cases[0].measured_iterations
    warmups = (
        configured_warmups
        if diagnostic_warmup_iterations is None
        else diagnostic_warmup_iterations
    )
    measured = (
        configured_measured
        if diagnostic_measured_iterations is None
        else diagnostic_measured_iterations
    )
    return (warmups + measured) * len(cases)


def _repository_external_output_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    _require_repository_external_path(resolved)
    return resolved


def _require_repository_external_path(path: Path) -> None:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return
    raise DatasetContractError(
        "context suite outputs must remain outside the Git worktree"
    )


def _context_sample_result(
    *,
    benchmark_run_id: str,
    case: ResolvedBenchmarkCase,
    observation: ContextPreparationObservation,
    semantic_grade,
    sample_ordinal: int,
    matrix_iteration: int,
    configured_warmup_iterations: int,
    configured_measured_iterations: int,
    measurement_contract_adhered: bool,
    environment: BenchmarkEnvironmentFact,
) -> ContextBenchmarkSampleResultFact:
    prepare_values = tuple(
        point.context_prepare_wall_seconds
        for point in observation.compile_points
    )
    compile_values = tuple(
        point.context_compile_wall_seconds
        for point in observation.compile_points
    )
    final = observation.compile_points[-1]
    metrics = (
        BenchmarkMetricValueFact(
            metric_id="context_prepare_total_wall_seconds",
            value=sum(prepare_values),
            unit="seconds",
        ),
        BenchmarkMetricValueFact(
            metric_id="context_prepare_mean_wall_seconds",
            value=sum(prepare_values) / len(prepare_values),
            unit="seconds",
        ),
        BenchmarkMetricValueFact(
            metric_id="context_prepare_max_wall_seconds",
            value=max(prepare_values),
            unit="seconds",
        ),
        BenchmarkMetricValueFact(
            metric_id="pure_context_compile_total_wall_seconds",
            value=sum(compile_values),
            unit="seconds",
        ),
        BenchmarkMetricValueFact(
            metric_id="compile_point_count",
            value=len(observation.compile_points),
            unit="count",
        ),
        BenchmarkMetricValueFact(
            metric_id="generated_semantic_delta_count",
            value=observation.generated_semantic_delta_count,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="generated_tool_result_count",
            value=observation.generated_tool_result_count,
            unit="count",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_authority_event_count",
            value=final.authority_event_count,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_semantic_delta_event_count",
            value=final.semantic_delta_event_count,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_stable_entry_count",
            value=final.stable_entry_count,
            unit="count",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_terminal_document_count",
            value=final.terminal_document_count,
            unit="count",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_terminal_projection_source_delta_count",
            value=final.terminal_projection_source_delta_count,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_artifact_backed_terminal_content_count",
            value=final.artifact_backed_terminal_content_count,
            unit="count",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_max_stable_entry_bytes",
            value=final.max_stable_entry_bytes,
            unit="bytes",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_active_window_generation",
            value=final.active_window_generation,
            unit="count",
        ),
        BenchmarkMetricValueFact(
            metric_id="final_selected_subagent_result_count",
            value=final.selected_subagent_result_count,
            unit="count",
        ),
    )
    return ContextBenchmarkSampleResultFact(
        schema_version="pulsara.durable-runtime.context-sample-result.v1",
        execution_kind="postgres_context_benchmark",
        benchmark_run_id=benchmark_run_id,
        dataset_id=case.dataset_id,
        manifest_contract_fingerprint=case.manifest_contract_fingerprint,
        scenario_id=case.scenario_id,
        scenario_contract_fingerprint=case.scenario_contract_fingerprint,
        case_contract_fingerprint=case.case_contract_fingerprint,
        case_id=case.case_key,
        case_kind=case.case_kind,
        mode=case.mode,
        sample_ordinal=sample_ordinal,
        matrix_iteration=matrix_iteration,
        configured_warmup_iterations=configured_warmup_iterations,
        configured_measured_iterations=configured_measured_iterations,
        measurement_contract_adhered=measurement_contract_adhered,
        seed=case.seed,
        production_acceptance_eligible=(
            case.case_kind == "production_valid"
            and measurement_contract_adhered
            and not environment.git.dirty
        ),
        semantic_grade=SemanticGradeFact(
            grader_id=semantic_grade.grader_id,
            grader_version=semantic_grade.grader_version,
            passed_assertion_ids=semantic_grade.passed_assertion_ids,
        ),
        compile_points=tuple(
            ContextCompilePointResultFact(
                point_id=point.point_id,
                source_through_sequence=point.source_through_sequence,
                normalized_transcript_fingerprint=(
                    point.normalized_transcript_fingerprint
                ),
                provider_payload_fingerprint=point.provider_payload_fingerprint,
                authority_plan_fingerprint=point.authority_plan_fingerprint,
                projection_base_fingerprint=point.projection_base_fingerprint,
                projection_base_kind=point.projection_base_kind,
                projection_base_id=point.projection_base_id,
                active_window_id=point.active_window_id,
                active_window_generation=point.active_window_generation,
                source_summary_artifact_id=point.source_summary_artifact_id,
                selected_subagent_result_count=(
                    point.selected_subagent_result_count
                ),
                subagent_graph_semantic_fingerprint=(
                    point.subagent_graph_semantic_fingerprint
                ),
            )
            for point in observation.compile_points
        ),
        metric_values=metrics,
        environment=environment,
    )


def _grade_context_observation(
    *,
    case: ResolvedBenchmarkCase,
    observation: ContextPreparationObservation,
):
    contract = case.grader_contract
    points = observation.compile_points
    high_waters = tuple(point.source_through_sequence for point in points)
    common = {
        "fingerprints_valid": all(
            value.startswith("sha256:")
            for point in points
            for value in (
                point.projection_base_fingerprint,
                point.semantic_source_fingerprint,
                point.authority_plan_fingerprint,
                point.normalized_transcript_fingerprint,
                point.provider_payload_fingerprint,
            )
        ),
        "high_water_monotonic": high_waters == tuple(sorted(high_waters)),
        "repeated_semantics_equal": (
            observation.repeated_final_compile_semantics_equal
        ),
    }
    if isinstance(case.scenario_contract, LongPlanPrefixGrowthScenario):
        assertions = {
            "projection_base_stable": len(
                {point.projection_base_fingerprint for point in points}
            )
            == 1,
            "semantic_delta_total_exact": (
                observation.generated_semantic_delta_count
                == sum(
                    case.scenario_contract.ledger.semantic_delta_events_per_call
                )
            ),
            "authority_manifest_fingerprints_valid": common[
                "fingerprints_valid"
            ],
            "normalized_transcript_equal": common[
                "repeated_semantics_equal"
            ],
        }
    elif isinstance(case.scenario_contract, IncrementalActiveWindowScenario):
        ledger = case.scenario_contract.ledger
        assertions = {
            "high_water_monotonic": (
                common["high_water_monotonic"]
                and len(set(high_waters)) == len(high_waters)
            ),
            "delta_increment_exact": (
                observation.generated_semantic_delta_count
                == ledger.model_calls
                * ledger.semantic_delta_events_per_call
            ),
            "provider_transcript_deterministic": common[
                "repeated_semantics_equal"
            ],
            "durable_delta_references_complete": (
                points[-1].terminal_projection_source_delta_count
                >= observation.generated_semantic_delta_count
            ),
        }
    elif isinstance(case.scenario_contract, ArtifactHeavyToolsScenario):
        expected_results = case.scenario_contract.ledger.tool_results.total_results
        assertions = {
            "artifact_identity_verified": (
                observation.generated_tool_result_count == expected_results
                and points[-1].normalized_tool_result_count >= expected_results
                and points[-1].terminal_document_count >= expected_results
            ),
            "cache_semantics_equal": common["repeated_semantics_equal"],
            "checkpoint_root_excludes_large_content": (
                points[-1].max_stable_entry_bytes
                < case.scenario_contract.ledger.tool_results.canonical_result_characters
            ),
            "authority_fingerprint_stable": common[
                "repeated_semantics_equal"
            ],
        }
    elif isinstance(case.scenario_contract, CheckpointRebaseRestartScenario):
        selected_ids = tuple(point.projection_base_id for point in points)
        assertions = {
            "checkpoint_is_acceleration_only": (
                observation.reopen_provider_semantics_equal is True
            ),
            "rebase_selection_deterministic": (
                observation.expected_selected_checkpoint_id is not None
                and selected_ids
                and selected_ids[0]
                == observation.expected_selected_checkpoint_id
                and all(
                    selected == observation.expected_selected_checkpoint_id
                    for selected in selected_ids
                )
            ),
            "cold_reopen_semantics_equal": (
                observation.reopen_provider_semantics_equal is True
            ),
            "delta_read_bounded": (
                all(point.projection_base_kind == "checkpoint" for point in points)
                and max(point.semantic_delta_event_count for point in points)
                < observation.generated_semantic_delta_count
            ),
            "provider_semantic_identity_stable": common[
                "repeated_semantics_equal"
            ],
        }
    elif isinstance(case.scenario_contract, SingleLongCompactionScenario):
        generations = tuple(point.active_window_generation for point in points)
        assertions = {
            "window_base_correct": (
                len(points) == 4
                and generations[0] == 1
                and generations[1:] == (2, 2, 2)
                and points[0].source_summary_artifact_id is None
                and all(
                    point.source_summary_artifact_id is not None
                    for point in points[1:]
                )
            ),
            "post_compaction_authority_bounded": (
                points[1].authority_event_count
                < points[0].authority_event_count
                and points[-1].semantic_delta_event_count
                < observation.generated_semantic_delta_count
            ),
            "summary_source_verified": (
                observation.compaction_status == "compacted"
                and observation.compaction_source_artifact_verified is True
            ),
            "cold_warm_transcript_equal": common[
                "repeated_semantics_equal"
            ],
        }
    elif isinstance(case.scenario_contract, SubagentTwoChildrenScenario):
        child_points = {
            point.point_id: point for point in points if point.point_id.startswith(
                "after_child_"
            )
        }
        assertions = {
            "ledger_identity_isolated": (
                observation.child_ledger_identity_isolated is True
                and len(observation.child_runtime_session_ids) == 2
                and len(set(observation.child_runtime_session_ids)) == 2
            ),
            "child_terminal_reference_exact": (
                observation.child_terminal_references_exact is True
                and len(observation.child_terminal_event_ids) == 2
            ),
            "dependency_order_valid": (
                observation.child_dependency_order_valid is True
            ),
            "checkpoint_restore_semantics_equal": (
                observation.subagent_graph_checkpoint_id is not None
                and common["repeated_semantics_equal"]
            ),
            "child_result_selected_once": (
                child_points["after_child_review_result"].selected_subagent_result_count
                == 1
                and child_points[
                    "after_child_verify_result"
                ].selected_subagent_result_count
                == 2
                and max(
                    point.selected_subagent_result_count for point in points
                )
                == 2
                and len(set(observation.child_result_ids)) == 2
            ),
        }
    else:
        raise DatasetContractError(
            f"unsupported context grader: {case.scenario_id}"
        )
    return grade_semantic_assertions(
        grader_id=contract.grader_id,
        grader_version=contract.grader_version,
        required_assertion_ids=contract.assertion_ids,
        observed_assertions=assertions,
    )


_AGGREGATED_CONTEXT_METRIC_IDS = frozenset(
    {
        "context_prepare_max_wall_seconds",
        "context_prepare_mean_wall_seconds",
        "context_prepare_total_wall_seconds",
        "final_authority_event_count",
        "final_semantic_delta_event_count",
        "pure_context_compile_total_wall_seconds",
    }
)


def _aggregate_context_samples(
    samples: list[ContextBenchmarkSampleResultFact],
) -> tuple[BenchmarkCaseAggregateFact, ...]:
    grouped: dict[str, list[ContextBenchmarkSampleResultFact]] = {}
    for sample in samples:
        grouped.setdefault(sample.case_id, []).append(sample)
    aggregates: list[BenchmarkCaseAggregateFact] = []
    for case_id in sorted(grouped):
        case_samples = grouped[case_id]
        fingerprints = {
            sample.case_contract_fingerprint for sample in case_samples
        }
        if len(fingerprints) != 1:
            raise DatasetContractError(
                f"context aggregate case fingerprint drifted: {case_id}"
            )
        metric_rows: dict[str, list[BenchmarkMetricValueFact]] = {}
        for sample in case_samples:
            for metric in sample.metric_values:
                if metric.metric_id in _AGGREGATED_CONTEXT_METRIC_IDS:
                    metric_rows.setdefault(metric.metric_id, []).append(metric)
        aggregates.append(
            BenchmarkCaseAggregateFact(
                case_id=case_id,
                case_contract_fingerprint=next(iter(fingerprints)),
                sample_count=len(case_samples),
                metrics=tuple(
                    _aggregate_metric(metric_id, metric_rows[metric_id])
                    for metric_id in sorted(metric_rows)
                ),
            )
        )
    return tuple(aggregates)


def _writer_sample_result(
    *,
    benchmark_run_id: str,
    case: ResolvedBenchmarkCase,
    observation: ModelSemanticBatchObservation,
    semantic_grade,
    sample_ordinal: int,
    matrix_iteration: int,
    configured_warmup_iterations: int,
    configured_measured_iterations: int,
    measurement_contract_adhered: bool,
    environment: BenchmarkEnvironmentFact,
) -> WriterBenchmarkSampleResultFact:
    batch_sizes = observation.semantic_batch_sizes
    source_item_count = sum(batch_sizes)
    average_batch_size = sum(batch_sizes) / len(batch_sizes)
    metric_values = (
        BenchmarkMetricValueFact(
            metric_id="model_stream_wall_seconds",
            value=observation.model_stream_wall_seconds,
            unit="seconds",
        ),
        BenchmarkMetricValueFact(
            metric_id="start_commit_port_wall_seconds",
            value=observation.start_commit_port_wall_seconds,
            unit="seconds",
        ),
        BenchmarkMetricValueFact(
            metric_id="semantic_commit_port_wall_seconds",
            value=observation.semantic_commit_port_wall_seconds,
            unit="seconds",
        ),
        BenchmarkMetricValueFact(
            metric_id="terminal_commit_port_wall_seconds",
            value=observation.terminal_commit_port_wall_seconds,
            unit="seconds",
        ),
        BenchmarkMetricValueFact(
            metric_id="logical_semantic_batch_count",
            value=observation.logical_semantic_batch_count,
            unit="count",
        ),
        BenchmarkMetricValueFact(
            metric_id="logical_model_commit_count",
            value=observation.logical_semantic_batch_count + 2,
            unit="count",
        ),
        BenchmarkMetricValueFact(
            metric_id="source_item_count",
            value=source_item_count,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="average_semantic_batch_size",
            value=average_batch_size,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="max_semantic_batch_size",
            value=max(batch_sizes),
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="ledger_event_delta",
            value=observation.ledger_event_delta,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="ledger_candidate_payload_bytes",
            value=observation.ledger_candidate_payload_byte_delta,
            unit="bytes",
        ),
        BenchmarkMetricValueFact(
            metric_id="writer_seconds_per_1000_source_items",
            value=(
                observation.semantic_commit_port_wall_seconds
                * 1_000
                / source_item_count
            ),
            unit="seconds_per_1000_source_items",
        ),
        BenchmarkMetricValueFact(
            metric_id="ledger_events_per_1000_source_items",
            value=observation.ledger_event_delta * 1_000 / source_item_count,
            unit="events_per_1000_source_items",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_charged_candidate_events",
            value=observation.physical_charged_candidate_events,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_charged_candidate_payload_bytes",
            value=observation.physical_charged_candidate_payload_bytes,
            unit="bytes",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_charged_wrapper_bytes",
            value=observation.physical_charged_wrapper_bytes,
            unit="bytes",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_charged_bookkeeping_events",
            value=observation.physical_charged_bookkeeping_events,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_charged_bookkeeping_bytes",
            value=observation.physical_charged_bookkeeping_bytes,
            unit="bytes",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_total_charged_events",
            value=observation.physical_total_charged_events,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_total_charged_payload_bytes",
            value=observation.physical_total_charged_payload_bytes,
            unit="bytes",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_released_events",
            value=observation.physical_released_events,
            unit="events",
        ),
        BenchmarkMetricValueFact(
            metric_id="physical_released_payload_bytes",
            value=observation.physical_released_payload_bytes,
            unit="bytes",
        ),
        BenchmarkMetricValueFact(
            metric_id="postgres_cluster_wal_lsn_delta_bytes",
            value=observation.postgres_cluster_wal_lsn_delta_bytes,
            unit="bytes",
        ),
    )
    return WriterBenchmarkSampleResultFact(
        schema_version="pulsara.durable-runtime.writer-sample-result.v1",
        execution_kind="postgres_writer_benchmark",
        benchmark_run_id=benchmark_run_id,
        dataset_id=case.dataset_id,
        manifest_contract_fingerprint=case.manifest_contract_fingerprint,
        scenario_id="model-semantic-batch-matrix",
        scenario_contract_fingerprint=case.scenario_contract_fingerprint,
        case_contract_fingerprint=case.case_contract_fingerprint,
        case_id=case.case_id,
        case_kind=case.case_kind,
        mode="default",
        sample_ordinal=sample_ordinal,
        matrix_iteration=matrix_iteration,
        configured_warmup_iterations=configured_warmup_iterations,
        configured_measured_iterations=configured_measured_iterations,
        measurement_contract_adhered=measurement_contract_adhered,
        seed=case.seed,
        production_acceptance_eligible=(
            case.case_kind == "production_valid"
            and measurement_contract_adhered
            and not environment.git.dirty
        ),
        semantic_grade=SemanticGradeFact(
            grader_id=semantic_grade.grader_id,
            grader_version=semantic_grade.grader_version,
            passed_assertion_ids=semantic_grade.passed_assertion_ids,
        ),
        ordered_semantic_content_fingerprint=(
            observation.ordered_semantic_content_fingerprint
        ),
        terminal_projection_semantic_fingerprint=(
            observation.terminal_projection_semantic_fingerprint
        ),
        physical_settlement_valid=observation.physical_settlement_valid,
        metric_values=metric_values,
        environment=environment,
    )


def _grade_batch_observation(
    *,
    case: ResolvedBenchmarkCase,
    observation: ModelSemanticBatchObservation,
    reference: ModelSemanticBatchObservation,
):
    contract = case.grader_contract
    return grade_semantic_assertions(
        grader_id=contract.grader_id,
        grader_version=contract.grader_version,
        required_assertion_ids=contract.assertion_ids,
        observed_assertions={
            "ordered_semantic_content_equal": (
                observation.ordered_semantic_content_fingerprint
                == reference.ordered_semantic_content_fingerprint
            ),
            "terminal_projection_equal": (
                observation.terminal_projection_semantic_fingerprint
                == reference.terminal_projection_semantic_fingerprint
            ),
            "physical_settlement_valid": observation.physical_settlement_valid,
            "accounted_writer_path_only": (
                observation.accounted_writer_path_only
            ),
        },
    )


def _reference_for_selected_cases(
    cases: tuple[ResolvedBenchmarkCase, ...],
    observations: dict[str, ModelSemanticBatchObservation],
) -> ModelSemanticBatchObservation:
    production = tuple(
        case for case in cases if case.case_kind == "production_valid"
    )
    selected = production or cases
    reference_case = max(
        selected,
        key=lambda item: (
            getattr(
                item.execution_case,
                "max_business_events_per_commit",
                0,
            ),
            item.case_id,
        ),
    )
    return observations[reference_case.case_id]


_AGGREGATED_WRITER_METRIC_IDS = frozenset(
    {
        "average_semantic_batch_size",
        "ledger_event_delta",
        "ledger_events_per_1000_source_items",
        "logical_model_commit_count",
        "logical_semantic_batch_count",
        "model_stream_wall_seconds",
        "semantic_commit_port_wall_seconds",
        "writer_seconds_per_1000_source_items",
    }
)


def _aggregate_writer_samples(
    samples: list[WriterBenchmarkSampleResultFact],
) -> tuple[BenchmarkCaseAggregateFact, ...]:
    grouped: dict[str, list[WriterBenchmarkSampleResultFact]] = {}
    for sample in samples:
        grouped.setdefault(sample.case_id, []).append(sample)
    aggregates: list[BenchmarkCaseAggregateFact] = []
    for case_id in sorted(grouped):
        case_samples = grouped[case_id]
        fingerprints = {
            sample.case_contract_fingerprint for sample in case_samples
        }
        if len(fingerprints) != 1:
            raise DatasetContractError(
                f"aggregate case fingerprint drifted: {case_id}"
            )
        metric_rows: dict[str, list[BenchmarkMetricValueFact]] = {}
        for sample in case_samples:
            for metric in sample.metric_values:
                if metric.metric_id in _AGGREGATED_WRITER_METRIC_IDS:
                    metric_rows.setdefault(metric.metric_id, []).append(metric)
        metrics = tuple(
            _aggregate_metric(metric_id, metric_rows[metric_id])
            for metric_id in sorted(metric_rows)
        )
        aggregates.append(
            BenchmarkCaseAggregateFact(
                case_id=case_id,
                case_contract_fingerprint=next(iter(fingerprints)),
                sample_count=len(case_samples),
                metrics=metrics,
            )
        )
    return tuple(aggregates)


def _aggregate_metric(
    metric_id: str,
    metrics: list[BenchmarkMetricValueFact],
) -> BenchmarkMetricAggregateFact:
    units = {metric.unit for metric in metrics}
    if len(units) != 1:
        raise DatasetContractError(f"aggregate metric unit drifted: {metric_id}")
    values = sorted(metric.value for metric in metrics)
    rank_index = max(0, ceil(0.95 * len(values)) - 1)
    return BenchmarkMetricAggregateFact(
        metric_id=metric_id,
        unit=next(iter(units)),
        sample_count=len(values),
        minimum=values[0],
        median=median(values),
        p95_nearest_rank=values[rank_index],
        maximum=values[-1],
    )


def _rotated_cases(
    cases: tuple[ResolvedBenchmarkCase, ...],
    matrix_iteration: int,
) -> tuple[ResolvedBenchmarkCase, ...]:
    if not cases:
        return ()
    offset = matrix_iteration % len(cases)
    return cases[offset:] + cases[:offset]


def _benchmark_run_id(manifest_fingerprint: str) -> str:
    payload = (
        f"{manifest_fingerprint}:{datetime.now(UTC).isoformat()}:{os.getpid()}"
    )
    return f"benchmark:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _case_payload(case: ResolvedBenchmarkCase) -> dict[str, Any]:
    scenario_payload = case.scenario_contract.model_dump(mode="json")
    return {
        "ordinal": case.ordinal,
        "dataset_id": case.dataset_id,
        "manifest_contract_fingerprint": case.manifest_contract_fingerprint,
        "group": case.group,
        "scenario_path": str(case.scenario_path),
        "scenario_id": case.scenario_id,
        "scenario_contract_fingerprint": (
            case.scenario_contract_fingerprint
        ),
        "resolved_workload": (
            scenario_payload.get("workload")
            or scenario_payload.get("ledger")
        ),
        "execution_case": case.execution_case.model_dump(mode="json"),
        "case_id": case.case_id,
        "case_kind": case.case_kind,
        "mode_contract": case.mode_contract.model_dump(mode="json"),
        "mode": case.mode,
        "warmup_iterations": case.warmup_iterations,
        "measured_iterations": case.measured_iterations,
        "reset_policy": case.reset_policy,
        "clock_contract": case.clock_contract.model_dump(mode="json"),
        "identity_contract": case.identity_contract.model_dump(mode="json"),
        "database_lifecycle_contract": (
            case.database_lifecycle_contract.model_dump(mode="json")
        ),
        "result_contract": case.result_contract.model_dump(mode="json"),
        "generator_contract": case.generator_contract.model_dump(mode="json"),
        "grader_contract": case.grader_contract.model_dump(mode="json"),
        "case_contract_fingerprint": case.case_contract_fingerprint,
        "production_acceptance_eligible": (
            case.production_acceptance_eligible
        ),
        "case_key": case.case_key,
    }


def _runner_build_fingerprint() -> str:
    return canonical_sha256(
        {
            path.name: path.read_text(encoding="utf-8")
            for path in (
                Path(__file__).resolve(),
                RUNNER_DIRECTORY / "dataset_contract.py",
                RUNNER_DIRECTORY / "scenario_contracts.py",
                RUNNER_DIRECTORY / "result_contract.py",
                RUNNER_DIRECTORY / "network_guard.py",
                RUNNER_DIRECTORY / "postgres_sandbox.py",
                BENCHMARK_ROOT / "graders" / "semantic.py",
                BENCHMARK_ROOT
                / "generators"
                / "model_semantic_batch.py",
                BENCHMARK_ROOT
                / "generators"
                / "context_preparation.py",
                BENCHMARK_ROOT / "generators" / "runtime_fixture.py",
            )
        }
    )


def _positive_integer(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def _non_negative_integer(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
