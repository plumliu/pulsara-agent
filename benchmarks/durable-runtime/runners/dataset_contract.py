"""Typed loading and deterministic resolution for durable-runtime datasets."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scenario_contracts import (
    CONTEXT_SCENARIO_ADAPTER,
    WRITER_SCENARIO_ADAPTER,
    ContextScenarioContract,
    FrozenContract,
    GeneratorContract,
    GraderContract,
    ScenarioContract,
    WriterScenarioContract,
    execution_cases,
    measurement_modes,
)


DATASET_MANIFEST_SCHEMA = "pulsara.durable-runtime.dataset-manifest.v1"
NETWORK_ACCESS_FORBIDDEN = "forbidden"

ScenarioGroup = Literal["writer", "context"]


class DatasetContractError(ValueError):
    """The declarative benchmark dataset violates its frozen contract."""


class ManifestContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetClockContract(ManifestContract):
    origin_utc: Literal["2026-01-01T00:00:00Z"]
    logical_tick_microseconds: int = Field(ge=1, le=1_000_000)


class DatasetIdentityContract(ManifestContract):
    algorithm: Literal["sha256-scenario-seed-owner-ordinal-v1"]
    timestamps: Literal["origin-plus-logical-tick-v1"]
    payload_text: Literal["ascii-counter-pattern-v1"]


class DatabaseLifecycleContract(ManifestContract):
    contract_id: Literal["pulsara.durable-runtime.database-lifecycle"]
    contract_version: Literal["1"]
    iteration_isolation: Literal["fresh_database_from_template"]
    setup_timing: Literal["excluded"]
    cleanup_timing: Literal["excluded"]
    postgres_cache_policy: Literal["recorded_not_forced"]
    baseline_outer_parallelism: Literal[
        "serial_unless_dedicated_postgres_instance_per_worker"
    ]


class BenchmarkResultContract(ManifestContract):
    contract_id: Literal["pulsara.durable-runtime.result"]
    contract_version: Literal["1"]
    raw_sample_vector_required: Literal[True]
    production_acceptance_excludes_counterfactual: Literal[True]
    production_acceptance_excludes_sensitivity: Literal[True]
    production_acceptance_requires_clean_git: Literal[True]
    required_production_environment_fields: tuple[
        Literal[
            "git.commit",
            "git.dirty",
            "python.version",
            "python.implementation",
            "runner_build_fingerprint",
            "postgres.server_version",
            "postgres.configuration_fingerprint",
            "postgres.connection_pool_fingerprint",
            "runtime_capacity.configuration_fingerprint",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def _environment_fields(self) -> "BenchmarkResultContract":
        expected = (
            "git.commit",
            "git.dirty",
            "python.version",
            "python.implementation",
            "runner_build_fingerprint",
            "postgres.server_version",
            "postgres.configuration_fingerprint",
            "postgres.connection_pool_fingerprint",
            "runtime_capacity.configuration_fingerprint",
        )
        if self.required_production_environment_fields != expected:
            raise ValueError("production environment attribution fields drifted")
        return self


class DatasetManifestDocument(ManifestContract):
    schema_version: Literal["pulsara.durable-runtime.dataset-manifest.v1"]
    dataset_id: str = Field(min_length=1, max_length=128)
    external_network_access: Literal["forbidden"]
    allowed_local_services: tuple[Literal["postgresql"], ...]
    clock_contract: DatasetClockContract
    identity_contract: DatasetIdentityContract
    database_lifecycle_contract: DatabaseLifecycleContract
    result_contract: BenchmarkResultContract
    writer_scenarios: tuple[str, ...] = Field(min_length=1)
    context_scenarios: tuple[str, ...] = Field(min_length=1)


class ResolvedBenchmarkMode(ManifestContract):
    mode: str = Field(min_length=1, max_length=128)
    process_cache_state: Literal["cold", "warm", "case_defined"]
    artifact_cache_state: Literal[
        "cold",
        "warm",
        "verified_warm",
        "case_defined",
    ]
    runtime_session_policy: Literal[
        "fresh_process_reopen",
        "reuse_worker_session",
        "scenario_defined",
    ]


@dataclass(frozen=True, slots=True)
class LoadedScenario:
    group: ScenarioGroup
    path: Path
    contract: ScenarioContract
    scenario_contract_fingerprint: str

    @property
    def scenario_id(self) -> str:
        return self.contract.scenario_id

    @property
    def seed(self) -> int:
        return self.contract.seed

    @property
    def description(self) -> str:
        return self.contract.description


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    root: Path
    document: DatasetManifestDocument
    manifest_contract_fingerprint: str
    scenarios: tuple[LoadedScenario, ...]

    @property
    def dataset_id(self) -> str:
        return self.document.dataset_id

    def select(
        self,
        *,
        group: Literal["all", "writer", "context"] = "all",
        scenario_ids: frozenset[str] = frozenset(),
    ) -> tuple[LoadedScenario, ...]:
        selected = tuple(
            scenario
            for scenario in self.scenarios
            if (group == "all" or scenario.group == group)
            and (not scenario_ids or scenario.scenario_id in scenario_ids)
        )
        missing = scenario_ids - {scenario.scenario_id for scenario in selected}
        if missing:
            raise DatasetContractError(
                f"unknown or filtered scenario IDs: {', '.join(sorted(missing))}"
            )
        return selected


@dataclass(frozen=True, slots=True)
class ResolvedBenchmarkCase:
    ordinal: int
    dataset_id: str
    manifest_contract_fingerprint: str
    group: ScenarioGroup
    scenario_path: Path
    scenario_contract: ScenarioContract
    scenario_contract_fingerprint: str
    execution_case: FrozenContract
    mode_contract: ResolvedBenchmarkMode
    warmup_iterations: int
    measured_iterations: int
    reset_policy: str
    clock_contract: DatasetClockContract
    identity_contract: DatasetIdentityContract
    database_lifecycle_contract: DatabaseLifecycleContract
    result_contract: BenchmarkResultContract
    generator_contract: GeneratorContract
    grader_contract: GraderContract
    case_contract_fingerprint: str

    @property
    def scenario_id(self) -> str:
        return self.scenario_contract.scenario_id

    @property
    def seed(self) -> int:
        return self.scenario_contract.seed

    @property
    def case_id(self) -> str:
        return str(self.execution_case.case_id)

    @property
    def case_kind(self) -> Literal[
        "production_valid",
        "sensitivity_analysis",
        "counterfactual_analysis",
    ]:
        return self.execution_case.case_kind

    @property
    def mode(self) -> str:
        return self.mode_contract.mode

    @property
    def case_key(self) -> str:
        return f"{self.scenario_id}:{self.case_id}:{self.mode}"

    @property
    def production_acceptance_eligible(self) -> bool:
        return self.case_kind == "production_valid"


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        _json_value(payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    return f"sha256:{sha256(canonical_json_bytes(payload)).hexdigest()}"


def load_dataset_manifest(path: Path) -> DatasetManifest:
    manifest_path = path.expanduser().resolve()
    root = manifest_path.parent
    raw_manifest = _read_json_object(manifest_path)
    try:
        document = DatasetManifestDocument.model_validate(raw_manifest)
    except ValueError as error:
        raise DatasetContractError(f"invalid dataset manifest: {error}") from error
    if document.allowed_local_services != ("postgresql",):
        raise DatasetContractError(
            "offline dataset may only allow local PostgreSQL"
        )
    listed_paths = (*document.writer_scenarios, *document.context_scenarios)
    if len(set(listed_paths)) != len(listed_paths):
        raise DatasetContractError("dataset manifest contains duplicate scenario paths")

    scenarios = tuple(
        _load_scenario(root, relative, "writer")
        for relative in document.writer_scenarios
    ) + tuple(
        _load_scenario(root, relative, "context")
        for relative in document.context_scenarios
    )
    scenario_ids = [scenario.scenario_id for scenario in scenarios]
    seeds = [scenario.seed for scenario in scenarios]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise DatasetContractError("scenario IDs must be unique within a dataset")
    if len(set(seeds)) != len(seeds):
        raise DatasetContractError("scenario seeds must be unique within a dataset")

    actual_paths = {
        str(candidate.relative_to(root))
        for directory in ("writer-scenarios", "context-scenarios")
        for candidate in (root / directory).glob("*.json")
    }
    if actual_paths != set(listed_paths):
        missing = sorted(set(listed_paths) - actual_paths)
        unlisted = sorted(actual_paths - set(listed_paths))
        raise DatasetContractError(
            f"manifest/file mismatch; missing={missing}, unlisted={unlisted}"
        )
    return DatasetManifest(
        root=root,
        document=document,
        manifest_contract_fingerprint=canonical_sha256(document),
        scenarios=scenarios,
    )


def expand_benchmark_cases(
    manifest: DatasetManifest,
    scenarios: tuple[LoadedScenario, ...],
) -> tuple[ResolvedBenchmarkCase, ...]:
    cases: list[ResolvedBenchmarkCase] = []
    for loaded in scenarios:
        scenario = loaded.contract
        measurement = scenario.measurement
        reset_policy = getattr(
            measurement,
            "reset_mode",
            manifest.document.database_lifecycle_contract.iteration_isolation,
        )
        for execution_case in execution_cases(scenario):
            for mode in measurement_modes(scenario):
                mode_contract = _resolve_mode(mode)
                fingerprint_payload = {
                    "schema_version": "resolved-durable-runtime-case.v1",
                    "dataset_id": manifest.dataset_id,
                    "manifest_contract_fingerprint": (
                        manifest.manifest_contract_fingerprint
                    ),
                    "scenario_contract_fingerprint": (
                        loaded.scenario_contract_fingerprint
                    ),
                    "execution_case": execution_case,
                    "mode_contract": mode_contract,
                    "warmup_iterations": measurement.warmup_iterations,
                    "measured_iterations": measurement.measured_iterations,
                    "reset_policy": reset_policy,
                    "clock_contract": manifest.document.clock_contract,
                    "identity_contract": manifest.document.identity_contract,
                    "database_lifecycle_contract": (
                        manifest.document.database_lifecycle_contract
                    ),
                    "result_contract": manifest.document.result_contract,
                    "generator_contract": scenario.generator_contract,
                    "grader_contract": scenario.grader_contract,
                }
                cases.append(
                    ResolvedBenchmarkCase(
                        ordinal=len(cases),
                        dataset_id=manifest.dataset_id,
                        manifest_contract_fingerprint=(
                            manifest.manifest_contract_fingerprint
                        ),
                        group=loaded.group,
                        scenario_path=loaded.path,
                        scenario_contract=scenario,
                        scenario_contract_fingerprint=(
                            loaded.scenario_contract_fingerprint
                        ),
                        execution_case=execution_case,
                        mode_contract=mode_contract,
                        warmup_iterations=measurement.warmup_iterations,
                        measured_iterations=measurement.measured_iterations,
                        reset_policy=reset_policy,
                        clock_contract=manifest.document.clock_contract,
                        identity_contract=manifest.document.identity_contract,
                        database_lifecycle_contract=(
                            manifest.document.database_lifecycle_contract
                        ),
                        result_contract=manifest.document.result_contract,
                        generator_contract=scenario.generator_contract,
                        grader_contract=scenario.grader_contract,
                        case_contract_fingerprint=canonical_sha256(
                            fingerprint_payload
                        ),
                    )
                )
    return tuple(cases)


def select_case_kind(
    cases: tuple[ResolvedBenchmarkCase, ...],
    *,
    case_kind: Literal[
        "all",
        "production_valid",
        "sensitivity_analysis",
        "counterfactual_analysis",
    ],
) -> tuple[ResolvedBenchmarkCase, ...]:
    if case_kind == "all":
        return cases
    return tuple(case for case in cases if case.case_kind == case_kind)


def recompute_case_contract_fingerprint(case: ResolvedBenchmarkCase) -> str:
    return canonical_sha256(
        {
            "schema_version": "resolved-durable-runtime-case.v1",
            "dataset_id": case.dataset_id,
            "manifest_contract_fingerprint": case.manifest_contract_fingerprint,
            "scenario_contract_fingerprint": (
                case.scenario_contract_fingerprint
            ),
            "execution_case": case.execution_case,
            "mode_contract": case.mode_contract,
            "warmup_iterations": case.warmup_iterations,
            "measured_iterations": case.measured_iterations,
            "reset_policy": case.reset_policy,
            "clock_contract": case.clock_contract,
            "identity_contract": case.identity_contract,
            "database_lifecycle_contract": case.database_lifecycle_contract,
            "result_contract": case.result_contract,
            "generator_contract": case.generator_contract,
            "grader_contract": case.grader_contract,
        }
    )


def _load_scenario(
    root: Path,
    relative: str,
    group: ScenarioGroup,
) -> LoadedScenario:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise DatasetContractError(f"unsafe scenario path: {relative}")
    expected_directory = (
        "writer-scenarios" if group == "writer" else "context-scenarios"
    )
    if not relative_path.parts or relative_path.parts[0] != expected_directory:
        raise DatasetContractError(
            f"{group} scenario is outside {expected_directory}: {relative}"
        )
    path = (root / relative_path).resolve()
    if root not in path.parents:
        raise DatasetContractError(f"scenario escapes dataset root: {relative}")
    raw_scenario = _read_json_object(path)
    try:
        contract: WriterScenarioContract | ContextScenarioContract
        if group == "writer":
            contract = WRITER_SCENARIO_ADAPTER.validate_python(raw_scenario)
        else:
            contract = CONTEXT_SCENARIO_ADAPTER.validate_python(raw_scenario)
    except ValueError as error:
        raise DatasetContractError(
            f"invalid typed scenario {relative}: {error}"
        ) from error
    if contract.scenario_id != path.stem:
        raise DatasetContractError(
            f"scenario ID must match its filename: {relative}"
        )
    _validate_binding_contracts(contract)
    return LoadedScenario(
        group=group,
        path=path,
        contract=contract,
        scenario_contract_fingerprint=canonical_sha256(contract),
    )


def _resolve_mode(mode: str) -> ResolvedBenchmarkMode:
    if mode == "process_cold":
        return ResolvedBenchmarkMode(
            mode=mode,
            process_cache_state="cold",
            artifact_cache_state="cold",
            runtime_session_policy="fresh_process_reopen",
        )
    if mode == "steady_state":
        return ResolvedBenchmarkMode(
            mode=mode,
            process_cache_state="warm",
            artifact_cache_state="warm",
            runtime_session_policy="reuse_worker_session",
        )
    if mode == "verified_artifact_cache_warm":
        return ResolvedBenchmarkMode(
            mode=mode,
            process_cache_state="warm",
            artifact_cache_state="verified_warm",
            runtime_session_policy="reuse_worker_session",
        )
    if mode == "default":
        return ResolvedBenchmarkMode(
            mode=mode,
            process_cache_state="case_defined",
            artifact_cache_state="case_defined",
            runtime_session_policy="scenario_defined",
        )
    raise DatasetContractError(f"unsupported benchmark mode: {mode}")


def _validate_binding_contracts(contract: ScenarioContract) -> None:
    from graders.semantic import validate_grader_contract

    expected_generator = {
        "model-semantic-batch-matrix": "pulsara.writer.model-semantic-batch",
        "model-semantic-structural-grouping": (
            "pulsara.writer.model-semantic-structural-grouping"
        ),
        "multi-session-contention": "pulsara.writer.multi-session-contention",
        "stable-confirmation-faults": (
            "pulsara.writer.stable-confirmation-faults"
        ),
        "mixed-runtime-accounting": "pulsara.writer.mixed-runtime-accounting",
        "long-plan-prefix-growth": "pulsara.context.long-plan-prefix-growth",
        "incremental-active-window": "pulsara.context.incremental-active-window",
        "single-long-compaction": "pulsara.context.single-long-compaction",
        "subagent-two-children": "pulsara.context.subagent-two-children",
        "artifact-heavy-tools": "pulsara.context.artifact-heavy-tools",
        "checkpoint-rebase-and-restart": (
            "pulsara.context.checkpoint-rebase-and-restart"
        ),
    }[contract.scenario_id]
    if (
        contract.generator_contract.generator_id != expected_generator
        or contract.generator_contract.generator_version != "1"
    ):
        raise DatasetContractError(
            f"generator binding drifted for {contract.scenario_id}"
        )
    validate_grader_contract(
        grader_id=contract.grader_contract.grader_id,
        grader_version=contract.grader_contract.grader_version,
        assertion_ids=contract.grader_contract.assertion_ids,
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetContractError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(payload, dict):
        raise DatasetContractError(f"expected JSON object: {path}")
    return payload


def _json_value(payload: Any) -> Any:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, tuple):
        return [_json_value(item) for item in payload]
    if isinstance(payload, list):
        return [_json_value(item) for item in payload]
    if isinstance(payload, dict):
        return {
            str(key): _json_value(value)
            for key, value in payload.items()
        }
    return payload
