"""Typed result and environment contracts for durable-runtime benchmarks."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
from time import monotonic
from typing import Literal

from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, model_validator

from postgres_sandbox import VerifiedBenchmarkDatabaseLease
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)


class FrozenResultFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GitBuildIdentityFact(FrozenResultFact):
    commit: str = Field(min_length=1, max_length=128)
    dirty: bool


class PythonRuntimeIdentityFact(FrozenResultFact):
    version: str = Field(min_length=1, max_length=128)
    implementation: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=512)
    machine: str = Field(min_length=1, max_length=128)


class PostgresRuntimeIdentityFact(FrozenResultFact):
    server_version: str = Field(min_length=1, max_length=128)
    configuration_fingerprint: str = Field(min_length=1, max_length=128)
    connection_pool_fingerprint: str = Field(min_length=1, max_length=128)
    migration_head_version: int = Field(ge=0)
    durable_registry_prefix_fingerprint: str = Field(min_length=1, max_length=128)
    deep_catalog_fingerprint: str = Field(min_length=1, max_length=128)
    pgvector_extension_version: str = Field(min_length=1, max_length=64)
    template_business_empty: bool


class RuntimeCapacityIdentityFact(FrozenResultFact):
    critical_ledger_workers: int = Field(ge=1)
    auxiliary_io_workers: int = Field(ge=1)
    postgres_pool_max_connections: int = Field(ge=1)
    postgres_critical_write_reserve: int = Field(ge=1)
    postgres_bounded_read_capacity: int = Field(ge=1)
    postgres_default_lease_timeout_seconds: float = Field(gt=0)
    configuration_fingerprint: str = Field(min_length=1, max_length=128)


class BenchmarkEnvironmentFact(FrozenResultFact):
    schema_version: Literal["pulsara.durable-runtime.environment.v2"]
    git: GitBuildIdentityFact
    python: PythonRuntimeIdentityFact
    runner_build_fingerprint: str = Field(min_length=1, max_length=128)
    postgres: PostgresRuntimeIdentityFact | None
    runtime_capacity: RuntimeCapacityIdentityFact | None


class BenchmarkMetricValueFact(FrozenResultFact):
    metric_id: str = Field(min_length=1, max_length=256)
    value: int | float
    unit: str = Field(min_length=1, max_length=64)


class BenchmarkMetricAggregateFact(FrozenResultFact):
    metric_id: str = Field(min_length=1, max_length=256)
    unit: str = Field(min_length=1, max_length=64)
    sample_count: int = Field(ge=1)
    minimum: int | float
    median: int | float
    p95_nearest_rank: int | float
    maximum: int | float

    @model_validator(mode="after")
    def _ordered_statistics(self) -> "BenchmarkMetricAggregateFact":
        if not (
            self.minimum
            <= self.median
            <= self.p95_nearest_rank
            <= self.maximum
        ):
            raise ValueError("aggregate metric statistics are not ordered")
        return self


class BenchmarkCaseAggregateFact(FrozenResultFact):
    case_id: str = Field(min_length=1, max_length=128)
    case_contract_fingerprint: str = Field(min_length=1, max_length=128)
    sample_count: int = Field(ge=1)
    metrics: tuple[BenchmarkMetricAggregateFact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_metrics(self) -> "BenchmarkCaseAggregateFact":
        metric_ids = tuple(item.metric_id for item in self.metrics)
        if metric_ids != tuple(sorted(metric_ids)):
            raise ValueError("aggregate metric IDs must be sorted")
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("aggregate metric IDs must be unique")
        if any(item.sample_count != self.sample_count for item in self.metrics):
            raise ValueError("aggregate metric sample counts drifted")
        return self


class BenchmarkMeasuredSampleFact(FrozenResultFact):
    sample_ordinal: int = Field(ge=0)
    phase: Literal["measured"]
    metric_values: tuple[BenchmarkMetricValueFact, ...] = Field(min_length=1)


class BenchmarkRawSampleVectorFact(FrozenResultFact):
    schema_version: Literal["pulsara.durable-runtime.raw-sample-vector.v1"]
    case_contract_fingerprint: str = Field(min_length=1, max_length=128)
    samples: tuple[BenchmarkMeasuredSampleFact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _sample_ordinals(self) -> "BenchmarkRawSampleVectorFact":
        ordinals = tuple(sample.sample_ordinal for sample in self.samples)
        if ordinals != tuple(range(len(self.samples))):
            raise ValueError("raw measured sample ordinals must be contiguous")
        return self


class ContractSmokeResultFact(FrozenResultFact):
    schema_version: Literal["pulsara.durable-runtime.contract-smoke-result.v2"]
    execution_kind: Literal["contract_smoke"]
    dataset_id: str = Field(min_length=1, max_length=128)
    manifest_contract_fingerprint: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    scenario_contract_fingerprint: str = Field(min_length=1, max_length=128)
    case_contract_fingerprint: str = Field(min_length=1, max_length=128)
    group: Literal["writer", "context"]
    case_ordinal: int = Field(ge=0)
    case_id: str = Field(min_length=1, max_length=128)
    case_kind: Literal[
        "production_valid",
        "sensitivity_analysis",
        "counterfactual_analysis",
    ]
    mode: str = Field(min_length=1, max_length=128)
    iteration: int = Field(ge=0)
    seed: int = Field(gt=0)
    worker_pid: int = Field(gt=0)
    elapsed_seconds: float = Field(ge=0)
    completed_at_utc: datetime
    external_network_access: Literal["forbidden"]
    allowed_local_services: tuple[Literal["postgresql"], ...]
    production_acceptance_eligible: bool
    semantic_grade_status: Literal["not_applicable_contract_smoke"]
    environment: BenchmarkEnvironmentFact

    @model_validator(mode="after")
    def _acceptance(self) -> "ContractSmokeResultFact":
        expected = self.case_kind == "production_valid"
        if self.production_acceptance_eligible != expected:
            raise ValueError("production acceptance eligibility drifted")
        return self


class SemanticGradeFact(FrozenResultFact):
    grader_id: str = Field(min_length=1, max_length=128)
    grader_version: str = Field(min_length=1, max_length=32)
    passed_assertion_ids: tuple[str, ...] = Field(min_length=1)


class WriterBenchmarkSampleResultFact(FrozenResultFact):
    schema_version: Literal["pulsara.durable-runtime.writer-sample-result.v1"]
    execution_kind: Literal["postgres_writer_benchmark"]
    benchmark_run_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    manifest_contract_fingerprint: str = Field(min_length=1, max_length=128)
    scenario_id: Literal["model-semantic-batch-matrix"]
    scenario_contract_fingerprint: str = Field(min_length=1, max_length=128)
    case_contract_fingerprint: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    case_kind: Literal[
        "production_valid",
        "sensitivity_analysis",
        "counterfactual_analysis",
    ]
    mode: Literal["default"]
    sample_ordinal: int = Field(ge=0)
    matrix_iteration: int = Field(ge=0)
    configured_warmup_iterations: int = Field(ge=0)
    configured_measured_iterations: int = Field(ge=1)
    measurement_contract_adhered: bool
    seed: int = Field(gt=0)
    production_acceptance_eligible: bool
    semantic_grade: SemanticGradeFact
    ordered_semantic_content_fingerprint: str = Field(min_length=1, max_length=128)
    terminal_projection_semantic_fingerprint: str = Field(
        min_length=1,
        max_length=128,
    )
    physical_settlement_valid: Literal[True]
    metric_values: tuple[BenchmarkMetricValueFact, ...] = Field(min_length=1)
    environment: BenchmarkEnvironmentFact

    @model_validator(mode="after")
    def _writer_sample(self) -> "WriterBenchmarkSampleResultFact":
        if self.environment.postgres is None:
            raise ValueError("PostgreSQL benchmark sample requires server identity")
        if self.environment.runtime_capacity is None:
            raise ValueError("writer benchmark requires runtime capacity identity")
        expected = (
            self.case_kind == "production_valid"
            and self.measurement_contract_adhered
            and not self.environment.git.dirty
        )
        if self.production_acceptance_eligible != expected:
            raise ValueError("writer sample acceptance eligibility drifted")
        return self


class ContextCompilePointResultFact(FrozenResultFact):
    point_id: str = Field(min_length=1, max_length=128)
    source_through_sequence: int = Field(ge=1)
    normalized_transcript_fingerprint: str = Field(min_length=1, max_length=128)
    provider_payload_fingerprint: str = Field(min_length=1, max_length=128)
    authority_plan_fingerprint: str = Field(min_length=1, max_length=128)
    projection_base_fingerprint: str = Field(min_length=1, max_length=128)
    projection_base_kind: Literal["run_seed", "checkpoint"]
    projection_base_id: str = Field(min_length=1, max_length=256)
    active_window_id: str = Field(min_length=1, max_length=256)
    active_window_generation: int = Field(ge=1)
    source_summary_artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    selected_subagent_result_count: int = Field(ge=0)
    subagent_graph_semantic_fingerprint: str = Field(
        min_length=1,
        max_length=128,
    )


class ContextBenchmarkSampleResultFact(FrozenResultFact):
    schema_version: Literal["pulsara.durable-runtime.context-sample-result.v1"]
    execution_kind: Literal["postgres_context_benchmark"]
    benchmark_run_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    manifest_contract_fingerprint: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    scenario_contract_fingerprint: str = Field(min_length=1, max_length=128)
    case_contract_fingerprint: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    case_kind: Literal[
        "production_valid",
        "sensitivity_analysis",
        "counterfactual_analysis",
    ]
    mode: str = Field(min_length=1, max_length=128)
    sample_ordinal: int = Field(ge=0)
    matrix_iteration: int = Field(ge=0)
    configured_warmup_iterations: int = Field(ge=0)
    configured_measured_iterations: int = Field(ge=1)
    measurement_contract_adhered: bool
    seed: int = Field(gt=0)
    production_acceptance_eligible: bool
    semantic_grade: SemanticGradeFact
    compile_points: tuple[ContextCompilePointResultFact, ...] = Field(min_length=1)
    metric_values: tuple[BenchmarkMetricValueFact, ...] = Field(min_length=1)
    environment: BenchmarkEnvironmentFact

    @model_validator(mode="after")
    def _context_sample(self) -> "ContextBenchmarkSampleResultFact":
        if self.environment.postgres is None:
            raise ValueError("context benchmark sample requires server identity")
        if self.environment.runtime_capacity is None:
            raise ValueError("context benchmark requires runtime capacity identity")
        expected = (
            self.case_kind == "production_valid"
            and self.measurement_contract_adhered
            and not self.environment.git.dirty
        )
        if self.production_acceptance_eligible != expected:
            raise ValueError("context sample acceptance eligibility drifted")
        point_ids = tuple(item.point_id for item in self.compile_points)
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("context compile point IDs must be unique")
        sequences = tuple(
            item.source_through_sequence for item in self.compile_points
        )
        if sequences != tuple(sorted(sequences)):
            raise ValueError("context compile point high-water must be monotonic")
        return self


class BenchmarkRunSummaryFact(FrozenResultFact):
    schema_version: Literal["pulsara.durable-runtime.run-summary.v1"]
    benchmark_run_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    manifest_contract_fingerprint: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=128)
    case_contract_fingerprints: tuple[str, ...] = Field(min_length=1)
    sample_count: int = Field(ge=1)
    raw_sample_vector_sha256: str = Field(min_length=1, max_length=128)
    percentile_contract: Literal["nearest_rank_v1"]
    case_aggregates: tuple[BenchmarkCaseAggregateFact, ...] = Field(min_length=1)
    measurement_contract_adhered: bool
    production_acceptance_passed: bool
    counterfactual_samples_excluded: bool
    environment: BenchmarkEnvironmentFact


def capture_benchmark_environment(
    *,
    repo_root: Path,
    runner_build_fingerprint: str,
    postgres_database_lease: VerifiedBenchmarkDatabaseLease | None = None,
) -> BenchmarkEnvironmentFact:
    from pulsara_agent.event_log.postgres_pool import (
        postgres_event_pool_capacity,
    )
    from pulsara_agent.runtime.blocking_executor import (
        blocking_executor_capacity,
    )

    blocking = blocking_executor_capacity()
    pool = postgres_event_pool_capacity()
    capacity_payload = {
        "critical_ledger_workers": blocking.critical_ledger_workers,
        "auxiliary_io_workers": blocking.auxiliary_io_workers,
        "postgres_pool_max_connections": pool.max_connections,
        "postgres_critical_write_reserve": pool.critical_write_reserve,
        "postgres_bounded_read_capacity": pool.bounded_read_capacity,
        "postgres_default_lease_timeout_seconds": (
            pool.default_lease_timeout_seconds
        ),
    }
    postgres = (
        None
        if postgres_database_lease is None
        else _capture_postgres_identity(
            postgres_database_lease,
        )
    )
    return BenchmarkEnvironmentFact(
        schema_version="pulsara.durable-runtime.environment.v2",
        git=GitBuildIdentityFact(
            commit=_git_output(repo_root, "rev-parse", "HEAD") or "unknown",
            dirty=bool(_git_output(repo_root, "status", "--porcelain")),
        ),
        python=PythonRuntimeIdentityFact(
            version=sys.version.split()[0],
            implementation=platform.python_implementation(),
            platform=platform.platform(),
            machine=platform.machine() or "unknown",
        ),
        runner_build_fingerprint=runner_build_fingerprint,
        postgres=postgres,
        runtime_capacity=RuntimeCapacityIdentityFact(
            **capacity_payload,
            configuration_fingerprint=_fingerprint(capacity_payload),
        ),
    )


def _git_output(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _capture_postgres_identity(
    database_lease: VerifiedBenchmarkDatabaseLease,
) -> PostgresRuntimeIdentityFact:
    setting_names = (
        "block_size",
        "checkpoint_timeout",
        "effective_cache_size",
        "fsync",
        "full_page_writes",
        "max_connections",
        "shared_buffers",
        "synchronous_commit",
        "wal_level",
    )
    with database_lease.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30.0,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("show server_version")
            version_row = cursor.fetchone()
            cursor.execute(
                """
                select name, setting, unit
                from pg_settings
                where name = any(%s)
                order by name
                """,
                (list(setting_names),),
            )
            settings = tuple(
                {
                    "name": row["name"],
                    "setting": row["setting"],
                    "unit": row["unit"],
                }
                for row in cursor.fetchall()
            )
    if version_row is None:
        raise RuntimeError("PostgreSQL server did not report its version")
    return PostgresRuntimeIdentityFact(
        server_version=str(version_row["server_version"]),
        configuration_fingerprint=_fingerprint(settings),
        connection_pool_fingerprint=(
            database_lease.connection_pool_policy_fingerprint
        ),
        migration_head_version=(
            database_lease.schema_binding.migration_head_version
        ),
        durable_registry_prefix_fingerprint=(
            database_lease.schema_binding.durable_registry_prefix_fingerprint
        ),
        deep_catalog_fingerprint=database_lease.deep_catalog_fingerprint,
        pgvector_extension_version=(
            database_lease.schema_binding.pgvector_extension_version
        ),
        template_business_empty=database_lease.business_empty,
    )
