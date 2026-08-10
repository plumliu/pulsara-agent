"""Frozen contracts for the Kernel-native real-provider dogfood suite."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


SUITE_SCHEMA_VERSION = "pulsara.kernel-dogfood-suite.v2"
SCENARIO_SCHEMA_VERSION = "pulsara.kernel-dogfood-scenario.v2"
RESULT_SCHEMA_VERSION = "pulsara.kernel-dogfood-result.v2"
SUITE_ID = "pulsara-kernel-dogfood-v2"
EXPECTED_SCENARIO_IDS = (
    "cache-continuity",
    "canonical-resume",
    "long-context-continuity",
    "prompt-queue-fifo",
    "subagent-delegation",
    "workspace-patch",
)


class DogfoodContractError(ValueError):
    """A suite or scenario does not match its frozen contract."""


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SuiteScenarioEntry(FrozenContract):
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    path: str = Field(min_length=1, max_length=256)
    scenario_contract_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class SuiteManifestDocument(FrozenContract):
    schema_version: Literal["pulsara.kernel-dogfood-suite.v2"]
    suite_id: Literal["pulsara-kernel-dogfood-v2"]
    execution_policy: Literal["serial-real-provider"]
    external_network_access: Literal["required"]
    workspace_isolation: Literal["fresh-copy-hidden-verifier"]
    evidence_source: Literal["canonical-query+operational-hooks"]
    scenarios: tuple[SuiteScenarioEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_scenario_order(self) -> "SuiteManifestDocument":
        ids = tuple(item.scenario_id for item in self.scenarios)
        if ids != EXPECTED_SCENARIO_IDS:
            raise ValueError("Kernel dogfood v2 requires its exact ordered scenarios")
        paths = tuple(item.path for item in self.scenarios)
        if len(paths) != len(set(paths)):
            raise ValueError("suite scenario paths must be unique")
        return self


class DirectoryFixtureContract(FrozenContract):
    fixture_kind: Literal["directory_copy"]
    workdir: str = "workdir"


class LinkedChapterTrailFixtureContract(FrozenContract):
    fixture_kind: Literal["linked_chapter_trail"]
    chapter_count: int = Field(ge=4, le=12)
    trail_order: tuple[int, ...] = Field(min_length=4, max_length=12)
    filler_paragraph_count: int = Field(ge=20, le=500)
    first_marker: str = Field(min_length=3, max_length=64)
    final_marker: str = Field(min_length=3, max_length=64)

    @model_validator(mode="after")
    def _trail_is_a_permutation(self) -> "LinkedChapterTrailFixtureContract":
        expected = tuple(range(1, self.chapter_count + 1))
        if tuple(sorted(self.trail_order)) != expected or self.trail_order[0] != 1:
            raise ValueError("linked chapter trail must be a full permutation from 1")
        return self


FixtureContract = Annotated[
    DirectoryFixtureContract | LinkedChapterTrailFixtureContract,
    Field(discriminator="fixture_kind"),
]


class WorkspaceTaskWorkflow(FrozenContract):
    workflow_kind: Literal["workspace_task"]
    prompt: str = Field(min_length=20, max_length=16_000)


class CacheContinuityWorkflow(FrozenContract):
    workflow_kind: Literal["cache_continuity"]
    prompts: tuple[str, str, str]
    inter_turn_delay_seconds: float = Field(ge=0, le=30)


class CanonicalResumeWorkflow(FrozenContract):
    workflow_kind: Literal["canonical_resume"]
    first_prompt: str = Field(min_length=20, max_length=16_000)
    resumed_prompt: str = Field(min_length=20, max_length=16_000)


class LongContextContinuityWorkflow(FrozenContract):
    workflow_kind: Literal["long_context_continuity"]
    discovery_prompt: str = Field(min_length=20, max_length=16_000)
    recall_prompt: str = Field(min_length=20, max_length=16_000)


class PromptQueueFifoWorkflow(FrozenContract):
    workflow_kind: Literal["prompt_queue_fifo"]
    prompts: tuple[str, str]
    completion_timeout_seconds: int = Field(ge=30, le=600)


class SubagentDelegationWorkflow(FrozenContract):
    workflow_kind: Literal["subagent_delegation"]
    prompt: str = Field(min_length=20, max_length=16_000)


WorkflowContract = Annotated[
    WorkspaceTaskWorkflow
    | CacheContinuityWorkflow
    | CanonicalResumeWorkflow
    | LongContextContinuityWorkflow
    | PromptQueueFifoWorkflow
    | SubagentDelegationWorkflow,
    Field(discriminator="workflow_kind"),
]


class CommittedEventMinimum(FrozenContract):
    event_type: str = Field(pattern=r"^[A-Z][A-Za-z0-9]{2,127}$")
    minimum: int = Field(ge=1, le=10_000)


class ToolCountRequirement(FrozenContract):
    tool_name: str = Field(min_length=1, max_length=128)
    exact_count: int = Field(ge=1, le=128)


class RootTurnToolGate(FrozenContract):
    turn_selector: Literal["first", "last", "all"]
    required_exact_counts: tuple[ToolCountRequirement, ...] = ()
    forbidden_tool_names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _closed_tools(self) -> "RootTurnToolGate":
        required = tuple(item.tool_name for item in self.required_exact_counts)
        if required != tuple(sorted(required)) or len(required) != len(set(required)):
            raise ValueError("required tool counts must be sorted and unique")
        if self.forbidden_tool_names != tuple(sorted(self.forbidden_tool_names)):
            raise ValueError("forbidden tool names must be sorted")
        if len(self.forbidden_tool_names) != len(set(self.forbidden_tool_names)):
            raise ValueError("forbidden tool names must be unique")
        if set(required) & set(self.forbidden_tool_names):
            raise ValueError("a tool cannot be both required and forbidden")
        return self


class EvidenceGateContract(FrozenContract):
    min_root_turns: int = Field(ge=1, le=32)
    max_root_turns: int = Field(ge=1, le=32)
    min_all_turns: int = Field(ge=1, le=64)
    max_all_turns: int = Field(ge=1, le=64)
    min_model_calls: int = Field(ge=1, le=256)
    max_model_calls: int = Field(ge=1, le=256)
    min_tool_calls: int = Field(ge=0, le=512)
    max_tool_calls: int = Field(ge=0, le=512)
    committed_event_minimums: tuple[CommittedEventMinimum, ...] = ()
    forbidden_event_types: tuple[str, ...] = ()
    root_turn_tool_gate: RootTurnToolGate | None = None
    minimum_completed_subagent_tasks: int = Field(default=0, ge=0, le=8)
    minimum_child_tool_calls: int = Field(default=0, ge=0, le=128)
    expected_prompt_queue_statuses: tuple[
        Literal["PENDING", "CONSUMED", "CANCELLED", "REJECTED"], ...
    ] = ()
    require_positive_cached_input_tokens: bool = False
    expected_writer_generation_delta: int = Field(default=0, ge=0, le=8)

    @model_validator(mode="after")
    def _closed_bounds(self) -> "EvidenceGateContract":
        for lower, upper in (
            (self.min_root_turns, self.max_root_turns),
            (self.min_all_turns, self.max_all_turns),
            (self.min_model_calls, self.max_model_calls),
            (self.min_tool_calls, self.max_tool_calls),
        ):
            if lower > upper:
                raise ValueError("evidence minimum exceeds maximum")
        required = tuple(item.event_type for item in self.committed_event_minimums)
        if required != tuple(sorted(required)) or len(required) != len(set(required)):
            raise ValueError("committed event minimums must be sorted and unique")
        if self.forbidden_event_types != tuple(sorted(self.forbidden_event_types)):
            raise ValueError("forbidden event types must be sorted")
        if set(required) & set(self.forbidden_event_types):
            raise ValueError("event type cannot be required and forbidden")
        return self


class HiddenVerifierContract(FrozenContract):
    path: str = Field(min_length=1, max_length=256)
    timeout_seconds: int = Field(ge=1, le=120)


class CoreDogfoodScenarioContract(FrozenContract):
    schema_version: Literal["pulsara.kernel-dogfood-scenario.v2"]
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    description: str = Field(min_length=20, max_length=2_000)
    model_role: Literal["flash", "pro"]
    timeout_seconds: int = Field(ge=30, le=3_600)
    system_prompt: str = Field(min_length=20, max_length=24_000)
    fixture: FixtureContract
    workflow: WorkflowContract
    verifier: HiddenVerifierContract
    evidence_gate: EvidenceGateContract


class FileIdentityFact(FrozenContract):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AssertionResultFact(FrozenContract):
    assertion_id: str = Field(min_length=1, max_length=128)
    passed: bool
    detail: str = Field(max_length=2_000)


class HiddenVerifierResultFact(FrozenContract):
    passed: bool
    exit_code: int
    elapsed_seconds: float = Field(ge=0)
    stdout: str = Field(max_length=4_000)
    stderr: str = Field(max_length=4_000)


class ProviderUsageObservationFact(FrozenContract):
    ordinal: int = Field(ge=1)
    turn_id: str
    model_call_index: int = Field(ge=1)
    usage_status: Literal["reported", "missing"]
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    reported_model_id: str | None = None
    diagnostic_codes: tuple[str, ...] = ()


class CanonicalTurnEvidenceFact(FrozenContract):
    turn_id: str
    scope_kind: str
    status: str
    tool_names: tuple[str, ...]


class CoreDogfoodExecutionEnvironmentFact(FrozenContract):
    schema_version: Literal["pulsara.kernel-dogfood-environment.v2"]
    python_version: str
    platform: str
    llm_api: str
    llm_provider: str
    endpoint_origin: str | None
    pro_model: str
    flash_model: str
    api_key_set: bool
    postgres_dsn_set: bool
    redacted_settings_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    git_commit: str
    git_dirty: bool
    production_source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class CoreDogfoodScenarioResult(FrozenContract):
    schema_version: Literal["pulsara.kernel-dogfood-result.v2"]
    suite_id: Literal["pulsara-kernel-dogfood-v2"]
    suite_contract_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_id: str
    scenario_contract_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_build_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_id: str
    status: Literal["passed", "failed"]
    started_at_utc: datetime
    completed_at_utc: datetime
    elapsed_seconds: float = Field(ge=0)
    session_id: str | None
    writer_generations: tuple[int, ...]
    canonical_turns: tuple[CanonicalTurnEvidenceFact, ...]
    committed_event_counts: tuple[tuple[str, int], ...]
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    provider_usage: tuple[ProviderUsageObservationFact, ...]
    assertions: tuple[AssertionResultFact, ...]
    verifier: HiddenVerifierResultFact
    error: str | None = Field(default=None, max_length=4_000)
    workspace_path: str | None = None
    environment: CoreDogfoodExecutionEnvironmentFact


class CoreDogfoodSuiteSummary(FrozenContract):
    schema_version: Literal["pulsara.kernel-dogfood-summary.v2"]
    suite_id: Literal["pulsara-kernel-dogfood-v2"]
    suite_contract_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_build_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at_utc: datetime
    completed_at_utc: datetime
    elapsed_seconds: float = Field(ge=0)
    selected_scenario_ids: tuple[str, ...]
    not_run_scenario_ids: tuple[str, ...]
    passed_scenario_ids: tuple[str, ...]
    failed_scenario_ids: tuple[str, ...]
    result_files: tuple[str, ...]


class LoadedScenario(FrozenContract):
    scenario_root: Path
    contract: CoreDogfoodScenarioContract
    scenario_contract_fingerprint: str
    file_inventory: tuple[FileIdentityFact, ...]


class LoadedSuite(FrozenContract):
    root: Path
    manifest: SuiteManifestDocument
    suite_contract_fingerprint: str
    scenarios: tuple[LoadedScenario, ...]

    def select(self, scenario_ids: frozenset[str]) -> tuple[LoadedScenario, ...]:
        selected = tuple(
            item
            for item in self.scenarios
            if not scenario_ids or item.contract.scenario_id in scenario_ids
        )
        missing = scenario_ids - {item.contract.scenario_id for item in selected}
        if missing:
            raise DogfoodContractError(
                f"unknown scenario IDs: {', '.join(sorted(missing))}"
            )
        return selected


_SCENARIO_ADAPTER = TypeAdapter(CoreDogfoodScenarioContract)


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def load_suite(
    root: Path, *, verify_expected_fingerprints: bool = True
) -> LoadedSuite:
    root = root.expanduser().resolve()
    try:
        manifest = SuiteManifestDocument.model_validate_json(
            (root / "manifest.json").read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise DogfoodContractError(f"invalid suite manifest: {exc}") from exc
    loaded: list[LoadedScenario] = []
    for entry in manifest.scenarios:
        scenario_root = _resolve_beneath(root, entry.path)
        scenario_path = scenario_root / "scenario.json"
        try:
            contract = _SCENARIO_ADAPTER.validate_json(
                scenario_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise DogfoodContractError(
                f"invalid dogfood scenario {scenario_path}: {exc}"
            ) from exc
        if contract.scenario_id != entry.scenario_id:
            raise DogfoodContractError(
                f"manifest/scenario ID mismatch for {entry.scenario_id}"
            )
        inventory = scenario_inventory(scenario_root)
        fingerprint = scenario_fingerprint(contract, inventory)
        if verify_expected_fingerprints and (
            fingerprint != entry.scenario_contract_fingerprint
        ):
            raise DogfoodContractError(
                f"scenario fingerprint drift for {entry.scenario_id}: "
                f"expected {entry.scenario_contract_fingerprint}, got {fingerprint}"
            )
        verifier = _resolve_beneath(scenario_root, contract.verifier.path)
        if not verifier.is_file():
            raise DogfoodContractError(f"hidden verifier does not exist: {verifier}")
        if isinstance(contract.fixture, DirectoryFixtureContract):
            workdir = _resolve_beneath(scenario_root, contract.fixture.workdir)
            if not workdir.is_dir() or verifier.is_relative_to(workdir):
                raise DogfoodContractError("directory fixture/verifier boundary is invalid")
        loaded.append(
            LoadedScenario(
                scenario_root=scenario_root,
                contract=contract,
                scenario_contract_fingerprint=fingerprint,
                file_inventory=inventory,
            )
        )
    suite_fingerprint = canonical_sha256(
        {
            "manifest": manifest.model_dump(mode="json"),
            "resolved_scenarios": tuple(
                (item.contract.scenario_id, item.scenario_contract_fingerprint)
                for item in loaded
            ),
        }
    )
    return LoadedSuite(
        root=root,
        manifest=manifest,
        suite_contract_fingerprint=suite_fingerprint,
        scenarios=tuple(loaded),
    )


def scenario_fingerprint(
    contract: CoreDogfoodScenarioContract,
    inventory: tuple[FileIdentityFact, ...],
) -> str:
    return canonical_sha256(
        {
            "contract": contract.model_dump(mode="json"),
            "scenario_files": tuple(item.model_dump(mode="json") for item in inventory),
        }
    )


def runner_build_fingerprint(package_root: Path) -> str:
    identities = tuple(
        _file_identity(path, relative_to=package_root)
        for path in sorted(package_root.glob("*.py"))
        if path.name != "__pycache__"
    )
    return canonical_sha256(tuple(item.model_dump(mode="json") for item in identities))


def scenario_inventory(root: Path) -> tuple[FileIdentityFact, ...]:
    identities: list[FileIdentityFact] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DogfoodContractError(f"scenario files may not be symlinks: {path}")
        if path.is_file() and path.name != "scenario.json" and "__pycache__" not in path.parts:
            identities.append(_file_identity(path, relative_to=root))
    return tuple(identities)


def _file_identity(path: Path, *, relative_to: Path) -> FileIdentityFact:
    payload = path.read_bytes()
    return FileIdentityFact(
        path=path.relative_to(relative_to).as_posix(),
        size_bytes=len(payload),
        sha256=sha256(payload).hexdigest(),
    )


def _resolve_beneath(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise DogfoodContractError(f"path escapes suite root: {relative}")
    return candidate


__all__ = [
    "AssertionResultFact",
    "CacheContinuityWorkflow",
    "CanonicalResumeWorkflow",
    "CanonicalTurnEvidenceFact",
    "CoreDogfoodExecutionEnvironmentFact",
    "CoreDogfoodScenarioContract",
    "CoreDogfoodScenarioResult",
    "CoreDogfoodSuiteSummary",
    "DirectoryFixtureContract",
    "DogfoodContractError",
    "HiddenVerifierResultFact",
    "LinkedChapterTrailFixtureContract",
    "LoadedScenario",
    "LoadedSuite",
    "LongContextContinuityWorkflow",
    "PromptQueueFifoWorkflow",
    "ProviderUsageObservationFact",
    "RESULT_SCHEMA_VERSION",
    "SCENARIO_SCHEMA_VERSION",
    "SUITE_ID",
    "SUITE_SCHEMA_VERSION",
    "SubagentDelegationWorkflow",
    "WorkspaceTaskWorkflow",
    "canonical_sha256",
    "load_suite",
    "runner_build_fingerprint",
    "scenario_fingerprint",
    "scenario_inventory",
]
