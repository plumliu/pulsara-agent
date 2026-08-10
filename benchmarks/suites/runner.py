"""Kernel-native public-boundary runner for real-provider dogfood."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import yaml

from benchmarks.suites.contracts import (
    AssertionResultFact,
    CacheContinuityWorkflow,
    CanonicalResumeWorkflow,
    CoreDogfoodExecutionEnvironmentFact,
    CoreDogfoodScenarioResult,
    CoreDogfoodSuiteSummary,
    DirectoryFixtureContract,
    LinkedChapterTrailFixtureContract,
    LoadedScenario,
    LoadedSuite,
    LongContextContinuityWorkflow,
    PromptQueueFifoWorkflow,
    ProviderUsageObservationFact,
    SubagentDelegationWorkflow,
    WorkspaceTaskWorkflow,
    canonical_sha256,
    runner_build_fingerprint,
)
from benchmarks.suites.graders import grade_kernel_evidence, run_hidden_verifier
from pulsara_agent.conversation_kernel.extensions import (
    ExtensionDelivery,
    ExtensionDeliveryKind,
    ExtensionPlane,
    ExtensionProjectionProfile,
    ExtensionRegistrationRequest,
    OperationalHookType,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore, KernelHostSession
from pulsara_agent.conversation_kernel.query import (
    CanonicalConversationQuery,
    CanonicalInspectorView,
)
from pulsara_agent.host import HostWorkspaceInput
from pulsara_agent.llm import ModelRole
from pulsara_agent.mcp_config import load_mcp_server_configs
from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.storage.schema_verification_service import (
    acquire_verified_postgres_access_sync,
)


ProgressSink = Callable[[str], None]
_EXTENSION_PRINCIPAL = "extension:kernel-dogfood-usage-recorder"


@dataclass(slots=True)
class _ScenarioState:
    settings: PulsaraSettings
    scenario: LoadedScenario
    workspace: Path
    execution_id: str
    usages: list[ProviderUsageObservationFact] = field(default_factory=list)
    writer_generations: list[int] = field(default_factory=list)
    cores: list[KernelHostCore] = field(default_factory=list)
    active_core: KernelHostCore | None = None
    active_session: KernelHostSession | None = None
    session_id: str | None = None


class CoreDogfoodRunner:
    """Runs the frozen suite through only current Kernel product boundaries."""

    def __init__(
        self,
        *,
        suite: LoadedSuite,
        settings: PulsaraSettings,
        results_root: Path,
        keep_workspaces: bool = False,
        progress: ProgressSink = print,
    ) -> None:
        self.suite = suite
        self.settings = settings
        self.results_root = results_root.resolve()
        self.keep_workspaces = keep_workspaces
        self.progress = progress
        self.runner_fingerprint = runner_build_fingerprint(Path(__file__).parent)
        self.environment_identity = _execution_environment(settings)

    async def run_selected(
        self,
        scenarios: tuple[LoadedScenario, ...],
        *,
        fail_fast: bool = False,
    ) -> tuple[CoreDogfoodScenarioResult, ...]:
        if self.results_root.exists() and any(self.results_root.iterdir()):
            raise RuntimeError("results directory must be empty")
        self.results_root.mkdir(parents=True, exist_ok=True)
        results: list[CoreDogfoodScenarioResult] = []
        jsonl_path = self.results_root / "results.jsonl"
        for ordinal, scenario in enumerate(scenarios, start=1):
            self.progress(f"[{ordinal}/{len(scenarios)}] START {scenario.contract.scenario_id}")
            result = await self.run_scenario(scenario)
            results.append(result)
            (self.results_root / f"{scenario.contract.scenario_id}.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
            with jsonl_path.open("a", encoding="utf-8") as stream:
                stream.write(result.model_dump_json() + "\n")
            self.progress(
                f"[{ordinal}/{len(scenarios)}] {result.status.upper()} "
                f"{scenario.contract.scenario_id} {result.elapsed_seconds:.1f}s "
                f"calls={result.model_call_count} tools={result.tool_call_count} "
                f"tokens={result.total_tokens} cached={result.cached_input_tokens}"
            )
            if fail_fast and result.status == "failed":
                break
        return tuple(results)

    async def run_scenario(
        self, scenario: LoadedScenario
    ) -> CoreDogfoodScenarioResult:
        execution_id = f"dogfood:{scenario.contract.scenario_id}:{uuid4().hex}"
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        temp_root = Path(
            tempfile.mkdtemp(prefix=f"pulsara-kernel-{scenario.contract.scenario_id}-")
        )
        workspace = temp_root / "workspace"
        _prepare_fixture(scenario, workspace)
        _disable_detected_mcp_for_workspace(workspace)
        state = _ScenarioState(
            settings=self.settings,
            scenario=scenario,
            workspace=workspace,
            execution_id=execution_id,
        )
        execution_error: str | None = None
        view: CanonicalInspectorView | None = None
        try:
            async with asyncio.timeout(scenario.contract.timeout_seconds):
                await self._execute_workflow(state)
            if state.session_id is None:
                raise RuntimeError("workflow did not establish a canonical session")
            view = await asyncio.to_thread(
                _inspect_session,
                self.settings,
                state.session_id,
            )
            expected_usage = sum(
                str(item["entry_kind"])
                in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST"}
                for item in view.conversation.entries
            )
            await _wait_for_usage(state.usages, expected_usage, timeout_seconds=5)
        except Exception as exc:
            execution_error = f"{type(exc).__name__}: {exc}"
            self.progress(
                f"{scenario.contract.scenario_id}: execution error: {execution_error}"
            )
            if state.session_id is not None:
                try:
                    view = await asyncio.to_thread(
                        _inspect_session,
                        self.settings,
                        state.session_id,
                    )
                except Exception as inspect_exc:
                    execution_error += (
                        f"; canonical_inspect={type(inspect_exc).__name__}: {inspect_exc}"
                    )

        verifier = await asyncio.to_thread(
            run_hidden_verifier,
            scenario_root=scenario.scenario_root,
            verifier_path=scenario.contract.verifier.path,
            workspace=workspace,
            timeout_seconds=scenario.contract.verifier.timeout_seconds,
        )
        if view is not None:
            graded = grade_kernel_evidence(
                scenario=scenario.contract,
                view=view,
                provider_usage=tuple(state.usages),
                writer_generations=tuple(state.writer_generations),
                verifier=verifier,
            )
            assertions = list(graded.assertions)
            canonical_turns = graded.canonical_turns
            committed_event_counts = graded.committed_event_counts
            model_call_count = graded.model_call_count
            tool_call_count = graded.tool_call_count
            total_tokens = graded.total_tokens
            cached_input_tokens = graded.cached_input_tokens
        else:
            assertions = [
                AssertionResultFact(
                    assertion_id="canonical_inspection_available",
                    passed=False,
                    detail="canonical session could not be inspected",
                ),
                AssertionResultFact(
                    assertion_id="hidden_verifier_passed",
                    passed=verifier.passed,
                    detail=f"exit_code={verifier.exit_code}",
                ),
            ]
            canonical_turns = ()
            committed_event_counts = ()
            model_call_count = len(state.usages)
            tool_call_count = 0
            total_tokens, cached_input_tokens = _usage_totals(state.usages)
        assertions.append(
            AssertionResultFact(
                assertion_id="workflow_execution_completed",
                passed=execution_error is None,
                detail=execution_error or "workflow and close-drain boundary completed",
            )
        )
        status = "passed" if all(item.passed for item in assertions) else "failed"
        preserve_workspace = self.keep_workspaces or status == "failed"
        try:
            await _close_state(state, close_conversation=status == "passed")
        except Exception as close_exc:
            status = "failed"
            preserve_workspace = True
            execution_error = (
                f"{execution_error}; close={type(close_exc).__name__}: {close_exc}"
                if execution_error
                else f"close={type(close_exc).__name__}: {close_exc}"
            )
            assertions.append(
                AssertionResultFact(
                    assertion_id="host_close_completed",
                    passed=False,
                    detail=execution_error,
                )
            )
        completed_at = datetime.now(timezone.utc)
        result = CoreDogfoodScenarioResult(
            schema_version="pulsara.kernel-dogfood-result.v2",
            suite_id="pulsara-kernel-dogfood-v2",
            suite_contract_fingerprint=self.suite.suite_contract_fingerprint,
            scenario_id=scenario.contract.scenario_id,
            scenario_contract_fingerprint=scenario.scenario_contract_fingerprint,
            runner_build_fingerprint=self.runner_fingerprint,
            execution_id=execution_id,
            status=status,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            elapsed_seconds=time.monotonic() - started_monotonic,
            session_id=state.session_id,
            writer_generations=tuple(state.writer_generations),
            canonical_turns=canonical_turns,
            committed_event_counts=committed_event_counts,
            model_call_count=model_call_count,
            tool_call_count=tool_call_count,
            total_tokens=total_tokens,
            cached_input_tokens=cached_input_tokens,
            provider_usage=tuple(state.usages),
            assertions=tuple(assertions),
            verifier=verifier,
            error=execution_error,
            workspace_path=str(workspace) if preserve_workspace else None,
            environment=self.environment_identity,
        )
        if not preserve_workspace:
            shutil.rmtree(temp_root, ignore_errors=True)
        return result

    async def _execute_workflow(self, state: _ScenarioState) -> None:
        workflow = state.scenario.contract.workflow
        await _open_session(state)
        if isinstance(workflow, WorkspaceTaskWorkflow):
            await _run_turn(state, workflow.prompt, "workspace")
            return
        if isinstance(workflow, CacheContinuityWorkflow):
            for index, prompt in enumerate(workflow.prompts, start=1):
                await _run_turn(state, prompt, f"cache-{index}")
                if index < len(workflow.prompts):
                    await asyncio.sleep(workflow.inter_turn_delay_seconds)
            return
        if isinstance(workflow, CanonicalResumeWorkflow):
            first_usage = len(state.usages)
            result = await _run_turn(state, workflow.first_prompt, "before-resume")
            await _wait_for_usage(
                state.usages,
                first_usage + result.model_call_count,
                timeout_seconds=5,
            )
            await _detach_active_session(state)
            await _open_session(state, resume=True)
            await _run_turn(state, workflow.resumed_prompt, "after-resume")
            return
        if isinstance(workflow, LongContextContinuityWorkflow):
            await _run_turn(state, workflow.discovery_prompt, "discover")
            await _run_turn(state, workflow.recall_prompt, "recall")
            return
        if isinstance(workflow, PromptQueueFifoWorkflow):
            await _run_prompt_queue(state, workflow)
            return
        if isinstance(workflow, SubagentDelegationWorkflow):
            await _run_turn(state, workflow.prompt, "subagent")
            return
        raise TypeError(type(workflow).__name__)


async def _open_session(state: _ScenarioState, *, resume: bool = False) -> None:
    core = KernelHostCore.production(
        settings=state.settings,
        authenticated_first_party_extension_ids=frozenset({_EXTENSION_PRINCIPAL}),
    )
    state.cores.append(core)
    workspace_input = HostWorkspaceInput(
        workspace_kind="project", workspace_root=state.workspace
    )
    role = (
        ModelRole.PRO
        if state.scenario.contract.model_role == "pro"
        else ModelRole.FLASH
    )
    if resume:
        if state.session_id is None:
            raise RuntimeError("resume requires an existing session identity")
        session = await core.resume_session(
            state.session_id,
            workspace_input=workspace_input,
            model_role=role,
            system_prompt=state.scenario.contract.system_prompt,
        )
    else:
        session = await core.open_session(
            workspace_input,
            model_role=role,
            system_prompt=state.scenario.contract.system_prompt,
        )
        state.session_id = session.session_id
    state.active_core = core
    state.active_session = session
    state.writer_generations.append(session.writer_generation)
    await _register_usage_recorder(session, state.usages, state.execution_id)


async def _register_usage_recorder(
    session: KernelHostSession,
    usages: list[ProviderUsageObservationFact],
    execution_id: str,
) -> None:
    async def callback(delivery: ExtensionDelivery) -> None:
        if (
            delivery.kind is not ExtensionDeliveryKind.EVENT
            or delivery.event_type
            != OperationalHookType.PROVIDER_USAGE_OBSERVED.value
        ):
            return
        payload = delivery.payload
        usages.append(
            ProviderUsageObservationFact(
                ordinal=len(usages) + 1,
                turn_id=str(payload["turn_id"]),
                model_call_index=int(payload["model_call_index"]),
                usage_status=str(payload["usage_status"]),
                input_tokens=_optional_int(payload.get("input_tokens")),
                cached_input_tokens=_optional_int(payload.get("cached_input_tokens")),
                output_tokens=_optional_int(payload.get("output_tokens")),
                reasoning_output_tokens=_optional_int(
                    payload.get("reasoning_output_tokens")
                ),
                total_tokens=_optional_int(payload.get("total_tokens")),
                reported_model_id=(
                    str(payload["reported_model_id"])
                    if payload.get("reported_model_id") is not None
                    else None
                ),
                diagnostic_codes=tuple(
                    str(item) for item in payload.get("diagnostic_codes", ())
                ),
            )
        )

    await session.register_extension(
        ExtensionRegistrationRequest(
            principal=session.authenticate_extension_principal(
                extension_principal_id=_EXTENSION_PRINCIPAL
            ),
            handler_id=f"handler:kernel-dogfood:{execution_id}",
            manifest_digest="sha256:" + sha256(execution_id.encode()).hexdigest(),
            plane=ExtensionPlane.OPERATIONAL,
            session_id=session.session_id,
            turn_id=None,
            event_types=frozenset(
                {OperationalHookType.PROVIDER_USAGE_OBSERVED.value}
            ),
            projection_major=1,
            projection_profile=ExtensionProjectionProfile.REDACTED,
            capability_set=frozenset(),
            lease_seconds=3_600,
            maximum_queue_events=256,
            maximum_queue_bytes=256 * 1024,
            callback_deadline_seconds=1,
            callback=callback,
        )
    )


async def _run_turn(
    state: _ScenarioState, prompt: str, label: str
):
    session = state.active_session
    if session is None:
        raise RuntimeError("workflow has no active Host session")
    return await session.run_turn(
        prompt,
        command_id=f"command:{state.execution_id}:{label}",
    )


async def _run_prompt_queue(
    state: _ScenarioState, workflow: PromptQueueFifoWorkflow
) -> None:
    session = state.active_session
    if session is None:
        raise RuntimeError("prompt queue workflow has no active Host session")
    command_ids = tuple(
        f"command:{state.execution_id}:queue-{index}"
        for index in range(1, len(workflow.prompts) + 1)
    )
    for command_id, prompt in zip(command_ids, workflow.prompts, strict=True):
        outcome = await session.submit_prompt(command_id=command_id, text=prompt)
        if outcome.status != "PENDING":
            raise RuntimeError(f"prompt queue rejected {command_id}: {outcome.public_code}")
    deadline = time.monotonic() + workflow.completion_timeout_seconds
    while time.monotonic() < deadline:
        outcomes = tuple([await session.query_command(item) for item in command_ids])
        if all(item is not None and item.status == "SUCCEEDED" for item in outcomes):
            return
        rejected = tuple(
            item for item in outcomes if item is not None and item.status == "REJECTED"
        )
        if rejected:
            raise RuntimeError(f"queued prompt failed: {rejected[0].public_code}")
        await asyncio.sleep(0.1)
    raise TimeoutError("prompt queue workflow did not settle")


async def _detach_active_session(state: _ScenarioState) -> None:
    session = state.active_session
    core = state.active_core
    if session is None or core is None:
        raise RuntimeError("detach requires an active Host session")
    await core.close_session(session.host_session_id, close_conversation=False)
    await core.shutdown()
    state.active_session = None
    state.active_core = None


async def _close_state(
    state: _ScenarioState, *, close_conversation: bool
) -> None:
    if state.active_session is not None and state.active_core is not None:
        await state.active_core.close_session(
            state.active_session.host_session_id,
            close_conversation=close_conversation,
        )
        state.active_session = None
        state.active_core = None
    errors: list[BaseException] = []
    for core in state.cores:
        try:
            await core.shutdown()
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError(f"Host shutdown failed: {type(errors[0]).__name__}")


def _inspect_session(
    settings: PulsaraSettings, session_id: str
) -> CanonicalInspectorView:
    lease = acquire_verified_postgres_access_sync(
        settings.storage.postgres_dsn,
        deadline_monotonic=time.monotonic() + 30,
    )
    try:
        return CanonicalConversationQuery(lease.connection_provider).inspect(
            session_id=session_id,
            maximum_entries=512,
            maximum_events=1_024,
            deadline_monotonic=time.monotonic() + 30,
        )
    finally:
        lease.release()


async def _wait_for_usage(
    usages: list[ProviderUsageObservationFact],
    expected: int,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while len(usages) < expected and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    if len(usages) != expected:
        raise RuntimeError(
            f"operational usage join mismatch: observed={len(usages)}, expected={expected}"
        )


def _prepare_fixture(scenario: LoadedScenario, workspace: Path) -> None:
    fixture = scenario.contract.fixture
    if isinstance(fixture, DirectoryFixtureContract):
        shutil.copytree(scenario.scenario_root / fixture.workdir, workspace)
        return
    if isinstance(fixture, LinkedChapterTrailFixtureContract):
        workspace.mkdir(parents=True)
        _generate_linked_chapter_trail(workspace, fixture)
        return
    raise TypeError(type(fixture).__name__)


def _generate_linked_chapter_trail(
    workspace: Path, fixture: LinkedChapterTrailFixtureContract
) -> None:
    story = workspace / "story"
    story.mkdir()
    position_by_chapter = {
        chapter: index for index, chapter in enumerate(fixture.trail_order)
    }
    for chapter in range(1, fixture.chapter_count + 1):
        position = position_by_chapter[chapter]
        paragraphs = [f"Chapter {chapter} is archive leaf {position + 1}."]
        if position == 0:
            paragraphs.append(
                f"The river village named at the start is {fixture.first_marker}."
            )
        for index in range(fixture.filler_paragraph_count):
            paragraphs.append(
                "Archive observation "
                f"{chapter:02d}-{index:03d}: slate bridges, orchard ledgers, "
                "weathered maps, and ordinary trade notes. This filler is "
                "non-authoritative; only explicit trail markers carry the answer."
            )
        if position + 1 == len(fixture.trail_order):
            paragraphs.append(
                f"TRAIL_END. The noble House named at the end is {fixture.final_marker}."
            )
        else:
            paragraphs.append(
                f"NEXT: story/chapter-{fixture.trail_order[position + 1]}.md"
            )
        (story / f"chapter-{chapter}.md").write_text(
            "\n\n".join(paragraphs) + "\n", encoding="utf-8"
        )


def _disable_detected_mcp_for_workspace(workspace: Path) -> None:
    enabled = tuple(item.server_id for item in load_mcp_server_configs() if item.enabled)
    if not enabled:
        return
    config_dir = workspace / ".pulsara"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "mcp.yaml"
    if config_path.exists():
        raise RuntimeError("dogfood fixture already owns an MCP config")
    config_path.write_text(
        yaml.safe_dump(
            {"servers": {server_id: {"enabled": False} for server_id in enabled}}
        ),
        encoding="utf-8",
    )


def _usage_totals(
    usages: list[ProviderUsageObservationFact],
) -> tuple[int | None, int | None]:
    if not usages or any(item.usage_status != "reported" for item in usages):
        return None, None
    return (
        sum(int(item.total_tokens or 0) for item in usages),
        sum(int(item.cached_input_tokens or 0) for item in usages),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _execution_environment(
    settings: PulsaraSettings,
) -> CoreDogfoodExecutionEnvironmentFact:
    redacted = settings.redacted_dict()
    llm = redacted["llm"]
    storage = redacted["storage"]
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return CoreDogfoodExecutionEnvironmentFact(
        schema_version="pulsara.kernel-dogfood-environment.v2",
        python_version=sys.version,
        platform=platform.platform(),
        llm_api=str(llm["api"]),
        llm_provider=str(llm["provider"]),
        endpoint_origin=(
            str(llm["endpoint_origin"])
            if llm.get("endpoint_origin") is not None
            else None
        ),
        pro_model=str(llm["pro_model"]),
        flash_model=str(llm["flash_model"]),
        api_key_set=bool(llm["api_key_set"]),
        postgres_dsn_set=bool(storage["postgres_dsn_set"]),
        redacted_settings_fingerprint=canonical_sha256(redacted),
        git_commit=commit or "unknown",
        git_dirty=dirty,
        production_source_fingerprint=_production_source_fingerprint(root),
    )


def _production_source_fingerprint(root: Path) -> str:
    source = root / "src" / "pulsara_agent"
    identities = []
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        identities.append(
            (path.relative_to(root).as_posix(), len(payload), sha256(payload).hexdigest())
        )
    return canonical_sha256(identities)


def write_suite_summary(
    *,
    suite: LoadedSuite,
    runner_fingerprint: str,
    results_root: Path,
    selected_ids: tuple[str, ...],
    results: tuple[CoreDogfoodScenarioResult, ...],
    started_at: datetime,
    elapsed_seconds: float,
) -> CoreDogfoodSuiteSummary:
    summary = CoreDogfoodSuiteSummary(
        schema_version="pulsara.kernel-dogfood-summary.v2",
        suite_id="pulsara-kernel-dogfood-v2",
        suite_contract_fingerprint=suite.suite_contract_fingerprint,
        runner_build_fingerprint=runner_fingerprint,
        started_at_utc=started_at,
        completed_at_utc=datetime.now(timezone.utc),
        elapsed_seconds=elapsed_seconds,
        selected_scenario_ids=selected_ids,
        not_run_scenario_ids=tuple(
            item for item in selected_ids if item not in {r.scenario_id for r in results}
        ),
        passed_scenario_ids=tuple(r.scenario_id for r in results if r.status == "passed"),
        failed_scenario_ids=tuple(r.scenario_id for r in results if r.status == "failed"),
        result_files=tuple(f"{r.scenario_id}.json" for r in results),
    )
    results_root.mkdir(parents=True, exist_ok=True)
    (results_root / "summary.json").write_text(
        summary.model_dump_json(indent=2), encoding="utf-8"
    )
    lines = [
        "# Pulsara Kernel Dogfood v2",
        "",
        f"- Passed: {len(summary.passed_scenario_ids)}",
        f"- Failed: {len(summary.failed_scenario_ids)}",
        f"- Elapsed: {summary.elapsed_seconds:.1f}s",
        "",
        "| Scenario | Status | Calls | Tools | Tokens | Cached |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.scenario_id} | {result.status} | {result.model_call_count} "
            f"| {result.tool_call_count} | {result.total_tokens} "
            f"| {result.cached_input_tokens} |"
        )
    (results_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


__all__ = ["CoreDogfoodRunner", "write_suite_summary"]
