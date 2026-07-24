"""Public-boundary runner for the frozen core real-LLM dogfood suite."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterator
from uuid import uuid4

from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.inspector import InspectorService, PostgresInspectorStore
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.plan import PlanExitResolution, PlanQuestionResolution
from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.storage.schema_verification_service import (
    acquire_verified_postgres_access_sync,
)

from benchmarks.suites.contracts import (
    AssertionResultFact,
    CacheContinuityWorkflow,
    CoreDogfoodExecutionEnvironmentFact,
    CoreDogfoodScenarioResult,
    CoreDogfoodSuiteSummary,
    DirectoryFixtureContract,
    DurableResumeWorkflow,
    GitExecutionIdentityFact,
    LinkedChapterTrailFixtureContract,
    LoadedScenario,
    LoadedSuite,
    ManualCompactionWorkflow,
    PlanWorkflow,
    SubagentDelegationWorkflow,
    WorkspaceTaskWorkflow,
    canonical_sha256,
    runner_build_fingerprint,
)
from benchmarks.suites.graders import grade_durable_evidence, run_hidden_verifier


ProgressSink = Callable[[str], None]


class CoreDogfoodRunner:
    """Runs each frozen scenario against the production HostCore boundary."""

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
        self.environment_identity = _execution_environment(self.settings)

    async def run_selected(
        self,
        scenarios: tuple[LoadedScenario, ...],
        *,
        fail_fast: bool = False,
    ) -> tuple[CoreDogfoodScenarioResult, ...]:
        if self.results_root.exists() and any(self.results_root.iterdir()):
            raise RuntimeError(
                f"results directory must be empty for an attributable run: {self.results_root}"
            )
        self.results_root.mkdir(parents=True, exist_ok=True)
        results: list[CoreDogfoodScenarioResult] = []
        jsonl_path = self.results_root / "results.jsonl"
        for ordinal, scenario in enumerate(scenarios, start=1):
            self.progress(
                f"[{ordinal}/{len(scenarios)}] START {scenario.contract.scenario_id}"
            )
            result = await self.run_scenario(scenario)
            results.append(result)
            scenario_path = self.results_root / f"{scenario.contract.scenario_id}.json"
            scenario_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
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

    async def run_scenario(self, scenario: LoadedScenario) -> CoreDogfoodScenarioResult:
        execution_id = f"dogfood:{scenario.contract.scenario_id}:{uuid4().hex}"
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        temp_root = Path(
            tempfile.mkdtemp(prefix=f"pulsara-core-{scenario.contract.scenario_id}-")
        )
        workspace = temp_root / "workspace"
        pulsara_home = temp_root / "pulsara-home"
        pulsara_home.mkdir(parents=True)
        _prepare_fixture(scenario, workspace)

        runtime_session_id: str | None = None
        root_run_texts: OrderedDict[str, str] = OrderedDict()
        execution_state: dict[str, str] = {}
        execution_error: str | None = None
        with _temporary_environment("PULSARA_HOME", str(pulsara_home)):
            try:
                async with asyncio.timeout(scenario.contract.timeout_seconds):
                    runtime_session_id = await self._execute_workflow(
                        scenario=scenario,
                        workspace=workspace,
                        execution_id=execution_id,
                        root_run_texts=root_run_texts,
                        execution_state=execution_state,
                    )
            except Exception as exc:
                runtime_session_id = execution_state.get("runtime_session_id")
                execution_error = f"{type(exc).__name__}: {exc}"
                self.progress(
                    f"{scenario.contract.scenario_id}: execution error: {execution_error}"
                )

        verifier = await asyncio.to_thread(
            run_hidden_verifier,
            scenario_root=scenario.scenario_root,
            verifier_path=scenario.contract.verifier.path,
            workspace=workspace,
            timeout_seconds=scenario.contract.verifier.timeout_seconds,
        )

        session_report: dict[str, object] = {}
        root_run_reports: list[dict[str, object]] = []
        inspected_texts: list[str] = []
        if runtime_session_id is not None:
            postgres_lease = await asyncio.to_thread(
                acquire_verified_postgres_access_sync,
                self.settings.storage.postgres_dsn,
                deadline_monotonic=time.monotonic() + 30.0,
            )
            try:
                inspector = InspectorService(
                    PostgresInspectorStore(postgres_lease.connection_provider),
                    oxigraph_url=self.settings.storage.oxigraph_url,
                )
                session_report = await asyncio.to_thread(
                    inspector.inspect_session,
                    runtime_session_id,
                    limit_events=0,
                )
                for run_id, final_text in root_run_texts.items():
                    report = await asyncio.to_thread(
                        inspector.inspect_run,
                        run_id,
                        limit_events=0,
                    )
                    root_run_reports.append(report)
                    inspected_texts.append(final_text)
            except Exception as exc:
                inspector_error = f"{type(exc).__name__}: {exc}"
                execution_error = (
                    f"{execution_error}; inspector={inspector_error}"
                    if execution_error
                    else f"inspector={inspector_error}"
                )
            finally:
                postgres_lease.release()

        graded = grade_durable_evidence(
            scenario=scenario.contract,
            session_report=session_report,
            root_run_reports=tuple(root_run_reports),
            final_texts=tuple(inspected_texts),
            verifier=verifier,
        )
        if execution_error is not None:
            assertions = graded.assertions + (
                AssertionResultFact(
                    assertion_id="workflow_execution_completed",
                    passed=False,
                    detail=execution_error,
                ),
            )
        else:
            assertions = graded.assertions + (
                AssertionResultFact(
                    assertion_id="workflow_execution_completed",
                    passed=True,
                    detail="workflow and close drain completed",
                ),
            )
        status = "passed" if all(item.passed for item in assertions) else "failed"
        preserve_workspace = self.keep_workspaces or status == "failed"
        workspace_path = str(workspace) if preserve_workspace else None
        completed_at = datetime.now(timezone.utc)
        result = CoreDogfoodScenarioResult(
            schema_version="pulsara.core-dogfood-result.v1",
            suite_id="pulsara-core-dogfood-v1",
            suite_contract_fingerprint=self.suite.suite_contract_fingerprint,
            scenario_id=scenario.contract.scenario_id,
            scenario_contract_fingerprint=scenario.scenario_contract_fingerprint,
            runner_build_fingerprint=self.runner_fingerprint,
            execution_id=execution_id,
            status=status,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            elapsed_seconds=time.monotonic() - started_monotonic,
            runtime_session_id=runtime_session_id,
            root_runs=graded.root_runs,
            all_run_count=graded.all_run_count,
            event_count=graded.event_count,
            event_counts=graded.event_counts,
            model_call_count=graded.model_call_count,
            tool_call_count=graded.tool_call_count,
            total_tokens=graded.total_tokens,
            cached_input_tokens=graded.cached_input_tokens,
            provider_cache_calls=graded.provider_cache_calls,
            provider_input_generation_count=graded.provider_input_generation_count,
            provider_input_rollover_count=graded.provider_input_rollover_count,
            assertions=assertions,
            verifier=verifier,
            error=execution_error,
            workspace_path=workspace_path,
            environment=self.environment_identity,
        )
        if not preserve_workspace:
            shutil.rmtree(temp_root, ignore_errors=True)
        return result

    async def _execute_workflow(
        self,
        *,
        scenario: LoadedScenario,
        workspace: Path,
        execution_id: str,
        root_run_texts: OrderedDict[str, str],
        execution_state: dict[str, str],
    ) -> str:
        workflow = scenario.contract.workflow
        if isinstance(workflow, DurableResumeWorkflow):
            return await self._execute_durable_resume(
                scenario=scenario,
                workspace=workspace,
                execution_id=execution_id,
                root_run_texts=root_run_texts,
                execution_state=execution_state,
            )

        core = HostCore(settings=self.settings, durable=True)
        session = None
        try:
            session = await self._open_session(
                core=core,
                scenario=scenario,
                workspace=workspace,
                execution_id=execution_id,
            )
            runtime_session_id = session.runtime_session_id
            execution_state["runtime_session_id"] = runtime_session_id
            if isinstance(workflow, WorkspaceTaskWorkflow):
                await self._run_turn(
                    session,
                    workflow.prompt,
                    root_run_texts,
                    scenario.contract.scenario_id,
                )
            elif isinstance(workflow, CacheContinuityWorkflow):
                for index, prompt in enumerate(workflow.prompts, start=1):
                    await self._run_turn(
                        session,
                        prompt,
                        root_run_texts,
                        f"{scenario.contract.scenario_id}:turn-{index}",
                    )
                    if (
                        index < len(workflow.prompts)
                        and workflow.inter_turn_delay_seconds
                    ):
                        await asyncio.sleep(workflow.inter_turn_delay_seconds)
            elif isinstance(workflow, ManualCompactionWorkflow):
                await self._run_turn(
                    session,
                    workflow.discovery_prompt,
                    root_run_texts,
                    f"{scenario.contract.scenario_id}:discovery",
                )
                self.progress(
                    f"{scenario.contract.scenario_id}: manual compaction START"
                )
                compaction = await session.compact_now()
                if not bool(compaction.get("compacted")):
                    raise RuntimeError("manual compaction returned compacted=false")
                self.progress(
                    f"{scenario.contract.scenario_id}: manual compaction FULL "
                    f"id={compaction.get('compaction_id')}"
                )
                await self._run_turn(
                    session,
                    workflow.post_compaction_prompt,
                    root_run_texts,
                    f"{scenario.contract.scenario_id}:post-compaction",
                )
            elif isinstance(workflow, SubagentDelegationWorkflow):
                await self._run_turn(
                    session,
                    workflow.prompt,
                    root_run_texts,
                    scenario.contract.scenario_id,
                )
            elif isinstance(workflow, PlanWorkflow):
                await self._execute_plan_workflow(
                    session=session,
                    workflow=workflow,
                    root_run_texts=root_run_texts,
                    label=scenario.contract.scenario_id,
                )
            else:  # pragma: no cover - guarded by the discriminated union.
                raise TypeError(f"unsupported workflow: {type(workflow).__name__}")
            await core.close_session(session.host_session_id, close_conversation=True)
            session = None
            return runtime_session_id
        finally:
            if session is not None:
                try:
                    await core.close_session(
                        session.host_session_id, close_conversation=True
                    )
                except BaseException:
                    pass
            await core.shutdown()

    async def _execute_durable_resume(
        self,
        *,
        scenario: LoadedScenario,
        workspace: Path,
        execution_id: str,
        root_run_texts: OrderedDict[str, str],
        execution_state: dict[str, str],
    ) -> str:
        workflow = scenario.contract.workflow
        assert isinstance(workflow, DurableResumeWorkflow)
        first_core = HostCore(settings=self.settings, durable=True)
        first_session = None
        runtime_session_id: str | None = None
        try:
            first_session = await self._open_session(
                core=first_core,
                scenario=scenario,
                workspace=workspace,
                execution_id=execution_id,
            )
            runtime_session_id = first_session.runtime_session_id
            execution_state["runtime_session_id"] = runtime_session_id
            await self._run_turn(
                first_session,
                workflow.first_prompt,
                root_run_texts,
                f"{scenario.contract.scenario_id}:before-resume",
            )
            await first_core.detach_session(first_session.host_session_id)
            first_session = None
        finally:
            if first_session is not None:
                try:
                    await first_core.detach_session(first_session.host_session_id)
                except BaseException:
                    pass
            await first_core.shutdown()

        if runtime_session_id is None:
            raise RuntimeError("durable resume lost runtime session identity")
        self.progress(
            f"{scenario.contract.scenario_id}: reopening {runtime_session_id} in new HostCore"
        )
        second_core = HostCore(settings=self.settings, durable=True)
        resumed = None
        try:
            resumed = await second_core.resume_session(
                runtime_session_id,
                model_role=ModelRole(scenario.contract.model_role),
                options=LLMOptions(reasoning_effort=scenario.contract.reasoning_effort),
                system_prompt=scenario.contract.system_prompt,
                memory_reflection=False,
                permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
            )
            await self._run_turn(
                resumed,
                workflow.resumed_prompt,
                root_run_texts,
                f"{scenario.contract.scenario_id}:after-resume",
            )
            await second_core.close_session(
                resumed.host_session_id, close_conversation=True
            )
            resumed = None
        finally:
            if resumed is not None:
                try:
                    await second_core.close_session(
                        resumed.host_session_id, close_conversation=True
                    )
                except BaseException:
                    pass
            await second_core.shutdown()
        return runtime_session_id

    async def _execute_plan_workflow(
        self,
        *,
        session,
        workflow: PlanWorkflow,
        root_run_texts: OrderedDict[str, str],
        label: str,
    ) -> None:
        session.enter_plan(reason=workflow.plan_reason)
        result = await self._run_turn(
            session, workflow.plan_prompt, root_run_texts, f"{label}:plan"
        )
        answer_index = 0
        approved = False
        for interaction_index in range(workflow.max_interactions):
            pending = session.get_pending_interaction()
            if pending is None:
                break
            self.progress(
                f"{label}: plan interaction {interaction_index + 1} kind={pending.kind}"
            )
            if pending.kind == "question":
                if answer_index >= len(workflow.question_answers):
                    raise RuntimeError(
                        "plan asked more questions than the frozen answer budget"
                    )
                result = await session.resolve_plan_interaction(
                    PlanQuestionResolution(
                        interaction_id=pending.interaction_id,
                        answer_text=workflow.question_answers[answer_index],
                    )
                )
                answer_index += 1
                _record_result(result, root_run_texts)
                continue
            if pending.kind == "exit":
                result = await session.resolve_plan_interaction(
                    PlanExitResolution(
                        interaction_id=pending.interaction_id,
                        decision="approve",
                        user_feedback=workflow.approval_feedback,
                    )
                )
                approved = True
                _record_result(result, root_run_texts)
                continue
            raise RuntimeError(f"unexpected plan interaction kind: {pending.kind}")
        if session.get_pending_interaction() is not None:
            raise RuntimeError("plan workflow exceeded its interaction budget")
        if answer_index != len(workflow.question_answers):
            raise RuntimeError(
                f"plan answered {answer_index} questions, expected "
                f"{len(workflow.question_answers)}"
            )
        if not approved:
            raise RuntimeError(
                "plan workflow never produced an exit request to approve"
            )
        await self._run_turn(
            session,
            workflow.implementation_prompt,
            root_run_texts,
            f"{label}:implementation",
        )

    async def _open_session(
        self,
        *,
        core: HostCore,
        scenario: LoadedScenario,
        workspace: Path,
        execution_id: str,
    ):
        return await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                display_label=scenario.contract.scenario_id,
                memory_domain_id=f"core_dogfood_{scenario.contract.scenario_id}_{uuid4().hex}",
            ),
            host_session_id=f"host:{execution_id}:{uuid4().hex[:12]}",
            conversation_id=f"conversation:{execution_id}:{uuid4().hex[:12]}",
            model_role=ModelRole(scenario.contract.model_role),
            options=LLMOptions(reasoning_effort=scenario.contract.reasoning_effort),
            system_prompt=scenario.contract.system_prompt,
            memory_reflection=False,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )

    async def _run_turn(
        self,
        session,
        prompt: str,
        root_run_texts: OrderedDict[str, str],
        label: str,
    ):
        self.progress(f"{label}: run START")
        result = await session.run_turn(prompt)
        _record_result(result, root_run_texts)
        self.progress(
            f"{label}: run {result.status.value.upper()} run_id={result.state.run_id}"
        )
        return result


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
    completed_at = datetime.now(timezone.utc)
    summary = CoreDogfoodSuiteSummary(
        schema_version="pulsara.core-dogfood-summary.v1",
        suite_id="pulsara-core-dogfood-v1",
        suite_contract_fingerprint=suite.suite_contract_fingerprint,
        runner_build_fingerprint=runner_fingerprint,
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        elapsed_seconds=elapsed_seconds,
        selected_scenario_ids=selected_ids,
        not_run_scenario_ids=tuple(
            scenario_id
            for scenario_id in selected_ids
            if scenario_id not in {item.scenario_id for item in results}
        ),
        passed_scenario_ids=tuple(
            item.scenario_id for item in results if item.status == "passed"
        ),
        failed_scenario_ids=tuple(
            item.scenario_id for item in results if item.status == "failed"
        ),
        result_files=tuple(f"{item.scenario_id}.json" for item in results),
    )
    results_root.mkdir(parents=True, exist_ok=True)
    (results_root / "summary.json").write_text(
        summary.model_dump_json(indent=2), encoding="utf-8"
    )
    (results_root / "summary.md").write_text(
        _summary_markdown(summary, results), encoding="utf-8"
    )
    return summary


def _prepare_fixture(scenario: LoadedScenario, workspace: Path) -> None:
    fixture = scenario.contract.fixture
    if isinstance(fixture, DirectoryFixtureContract):
        shutil.copytree(scenario.scenario_root / fixture.workdir, workspace)
        return
    if isinstance(fixture, LinkedChapterTrailFixtureContract):
        workspace.mkdir(parents=True)
        _generate_linked_chapter_trail(workspace, fixture)
        return
    raise TypeError(f"unsupported fixture: {type(fixture).__name__}")


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
                f"The river village named at the start of the trail is {fixture.first_marker}."
            )
        for index in range(fixture.filler_paragraph_count):
            paragraphs.append(
                "Archive observation "
                f"{chapter:02d}-{index:03d}: the survey records slate bridges, "
                "orchard ledgers, weathered maps, and ordinary trade notes. "
                "This filler is intentionally non-authoritative; only the explicit "
                "trail markers and final instruction carry the requested answer."
            )
        if position + 1 == len(fixture.trail_order):
            paragraphs.append(
                f"TRAIL_END. The noble House named at the end is {fixture.final_marker}."
            )
        else:
            next_chapter = fixture.trail_order[position + 1]
            paragraphs.append(f"NEXT: story/chapter-{next_chapter}.md")
        (story / f"chapter-{chapter}.md").write_text(
            "\n\n".join(paragraphs) + "\n", encoding="utf-8"
        )


def _record_result(result, root_run_texts: OrderedDict[str, str]) -> None:
    root_run_texts[result.state.run_id] = result.final_text


@contextmanager
def _temporary_environment(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _git_identity() -> GitExecutionIdentityFact:
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
    return GitExecutionIdentityFact(
        commit=commit or "unknown",
        dirty=dirty,
        production_source_fingerprint=_production_source_fingerprint(root),
    )


def _execution_environment(
    settings: PulsaraSettings,
) -> CoreDogfoodExecutionEnvironmentFact:
    redacted = settings.redacted_dict()
    llm = redacted["llm"]
    storage = redacted["storage"]
    return CoreDogfoodExecutionEnvironmentFact(
        schema_version="pulsara.core-dogfood-environment.v1",
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
        oxigraph_url=str(storage["oxigraph_url"]),
        postgres_dsn_set=bool(storage["postgres_dsn_set"]),
        redacted_settings_fingerprint=canonical_sha256(redacted),
        git=_git_identity(),
    )


def _production_source_fingerprint(root: Path) -> str:
    paths = sorted((root / "src" / "pulsara_agent").rglob("*.py"))
    paths.extend(
        path for path in (root / "pyproject.toml", root / "uv.lock") if path.exists()
    )
    return canonical_sha256(
        tuple(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
            for path in paths
        )
    )


def _summary_markdown(
    summary: CoreDogfoodSuiteSummary,
    results: tuple[CoreDogfoodScenarioResult, ...],
) -> str:
    lines = [
        "# Pulsara Core Dogfood Result",
        "",
        f"- Suite fingerprint: `{summary.suite_contract_fingerprint}`",
        f"- Runner fingerprint: `{summary.runner_build_fingerprint}`",
        f"- Elapsed: `{summary.elapsed_seconds:.1f}s`",
        f"- Passed: `{len(summary.passed_scenario_ids)}`",
        f"- Failed: `{len(summary.failed_scenario_ids)}`",
        f"- Not run: `{len(summary.not_run_scenario_ids)}`",
        "",
        "| Scenario | Status | Seconds | Calls | Tools | Tokens | Cached |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.scenario_id} | {result.status} | {result.elapsed_seconds:.1f} "
            f"| {result.model_call_count} | {result.tool_call_count} "
            f"| {result.total_tokens} | {result.cached_input_tokens} |"
        )
    lines.append("")
    return "\n".join(lines)
