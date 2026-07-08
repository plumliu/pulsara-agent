from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path

import psycopg
import pytest

from pulsara_agent.event import (
    SubagentPhaseReportedEvent,
    SubagentResultConsumedEvent,
    SubagentResultSubmittedEvent,
    SubagentRunCompletedEvent,
    SubagentRunStartedEvent,
    SubagentTaskBlockedEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskCreatedEvent,
    SubagentTaskScheduledEvent,
    SubagentTaskStartedEvent,
    ToolCallStartEvent,
)
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.runtime import LoopBudget
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.runtime.wiring import build_agent_runtime_wiring
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm


PARENT_SENTINEL = "PULSARA_SUBSYSTEM_PARENT_OK"
REVIEW_SENTINEL = "PULSARA_SUBSYSTEM_REVIEW_OK"
VERIFY_SENTINEL = "PULSARA_SUBSYSTEM_VERIFY_OK"
SPEC_SENTINEL = "PULSARA_SUBSYSTEM_SPEC_ALPHA"
VERIFY_COMMAND = (
    "uv run python -c \"from subsystem_spec import subsystem_marker; "
    f"assert subsystem_marker() == '{SPEC_SENTINEL}'; print('{VERIFY_SENTINEL}')\""
)


def test_real_llm_subagent_task_system_dogfood(tmp_path: Path) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    if os.getenv("PULSARA_RUN_DOGFOOD_SUBAGENT_SYSTEM") != "1":
        pytest.skip(
            "Set PULSARA_RUN_DOGFOOD_SUBAGENT_SYSTEM=1 to run subsystem-agent real LLM dogfood."
        )

    settings = _settings()
    _connect_or_skip(settings.storage.postgres_dsn)
    result = asyncio.run(_run_subagent_task_system_dogfood(settings, tmp_path))
    print("\nREAL_LLM_SUBAGENT_SYSTEM_DOGFOOD=" + json.dumps(result, ensure_ascii=False, sort_keys=True))

    assert result["status"] == "finished", result
    assert result["error_message"] is None, result
    assert PARENT_SENTINEL in result["final_text"], result
    assert REVIEW_SENTINEL in result["final_text"], result
    assert VERIFY_SENTINEL in result["final_text"], result
    assert SPEC_SENTINEL in result["final_text"], result

    assert result["create_agent_tasks_calls"] == 1, result
    assert result["list_agents_calls"] >= 1, result
    assert result["wait_agent_tasks_calls"] >= 1, result
    assert result["parent_read_file_calls"] >= 1, result
    assert result["primitive_spawn_or_wait_calls"] == 0, result

    assert result["task_created_count"] == 2, result
    assert result["task_started_count"] == 2, result
    assert result["task_completed_count"] == 2, result
    assert result["run_started_count"] == 2, result
    assert result["run_completed_count"] == 2, result
    assert result["result_consumed_count"] >= 2, result
    assert result["child_raw_event_count"] > 0, result
    assert result["child_raw_events_missing_subagent_metadata"] == 0, result

    assert result["review_task_id"], result
    assert result["verify_task_id"], result
    assert result["verify_waited_for_review"] is True, result
    assert result["verify_started_after_review_completed"] is True, result


async def _run_subagent_task_system_dogfood(settings: PulsaraSettings, tmp_path: Path) -> dict[str, object]:
    workspace_root = tmp_path / "workspace"
    _prepare_workspace(workspace_root)
    wiring = build_agent_runtime_wiring(
        settings,
        workspace_root,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=1536),
        system_prompt=_system_prompt(),
        memory_reflection=False,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
    )
    wiring.agent_runtime.budget = LoopBudget(
        max_turns=18,
        max_tool_calls=24,
        max_consecutive_model_failures=2,
        max_consecutive_tool_failures=4,
        max_subagent_results_per_parent_compile=0,
    )
    try:
        run_result = await wiring.agent_runtime.run_task(_parent_user_prompt())
        parent_events = wiring.runtime_wiring.event_log.iter(run_id=run_result.state.run_id)
        all_parent_events = wiring.runtime_wiring.event_log.iter()
        subagent_runtime = wiring.agent_runtime.subagent_runtime
        assert subagent_runtime is not None

        tool_names = [
            event.tool_call_name
            for event in parent_events
            if isinstance(event, ToolCallStartEvent)
        ]
        task_created = [event for event in all_parent_events if isinstance(event, SubagentTaskCreatedEvent)]
        task_started = [event for event in all_parent_events if isinstance(event, SubagentTaskStartedEvent)]
        task_completed = [event for event in all_parent_events if isinstance(event, SubagentTaskCompletedEvent)]
        task_scheduled = [event for event in all_parent_events if isinstance(event, SubagentTaskScheduledEvent)]
        task_blocked = [event for event in all_parent_events if isinstance(event, SubagentTaskBlockedEvent)]
        run_started = [event for event in all_parent_events if isinstance(event, SubagentRunStartedEvent)]
        run_completed = [event for event in all_parent_events if isinstance(event, SubagentRunCompletedEvent)]
        phases = [event for event in all_parent_events if isinstance(event, SubagentPhaseReportedEvent)]
        submitted = [event for event in all_parent_events if isinstance(event, SubagentResultSubmittedEvent)]
        consumed = [event for event in all_parent_events if isinstance(event, SubagentResultConsumedEvent)]

        task_id_by_key = {
            event.task_key: event.task_id
            for event in task_created
            if event.task_key is not None
        }
        review_task_id = task_id_by_key.get("review")
        verify_task_id = task_id_by_key.get("verify")
        started_sequence_by_task = {
            event.task_id: event.sequence or 0
            for event in task_started
        }
        completed_sequence_by_task = {
            event.task_id: event.sequence or 0
            for event in task_completed
        }
        verify_waited_for_review = any(
            event.task_id == verify_task_id
            and event.status == "waiting_dependency"
            for event in task_blocked
        ) and any(event.schedule_reason == "dependency_satisfied" for event in task_scheduled)
        verify_started_after_review_completed = bool(
            review_task_id
            and verify_task_id
            and completed_sequence_by_task.get(review_task_id, 0)
            and started_sequence_by_task.get(verify_task_id, 0)
            and completed_sequence_by_task[review_task_id] < started_sequence_by_task[verify_task_id]
        )

        child_raw_events = []
        for run in subagent_runtime.runs:
            child_raw_events.extend(subagent_runtime.child_runtime_session(run.subagent_run_id).event_log.iter())
        child_metadata_missing = [
            event
            for event in child_raw_events
            if not isinstance(event.metadata.get("subagent"), dict)
            or not event.metadata["subagent"].get("subagent_run_id")
            or not event.metadata["subagent"].get("parent_runtime_session_id")
        ]

        return {
            "status": run_result.status.value,
            "stop_reason": run_result.stop_reason,
            "error_message": run_result.error_message,
            "final_text": run_result.final_text,
            "tool_names": tool_names,
            "create_agent_tasks_calls": tool_names.count("create_agent_tasks"),
            "list_agents_calls": tool_names.count("list_agents"),
            "wait_agent_tasks_calls": tool_names.count("wait_agent_tasks"),
            "parent_read_file_calls": tool_names.count("read_file"),
            "primitive_spawn_or_wait_calls": tool_names.count("spawn_agent") + tool_names.count("wait_agent"),
            "task_created_count": len(task_created),
            "task_started_count": len(task_started),
            "task_completed_count": len(task_completed),
            "task_scheduled_reasons": [event.schedule_reason for event in task_scheduled],
            "task_blocked_events": [
                {
                    "task_id": event.task_id,
                    "status": event.status,
                    "blocked_by_task_ids": event.blocked_by_task_ids,
                    "dependency_status_snapshot": event.dependency_status_snapshot,
                }
                for event in task_blocked
            ],
            "task_blocked_statuses": [event.status for event in task_blocked],
            "run_started_count": len(run_started),
            "run_completed_count": len(run_completed),
            "result_submitted_count": len(submitted),
            "result_consumed_count": len(consumed),
            "reported_phases": [event.phase for event in phases],
            "submitted_summaries": [event.summary for event in submitted],
            "review_task_id": review_task_id,
            "verify_task_id": verify_task_id,
            "verify_waited_for_review": verify_waited_for_review,
            "verify_started_after_review_completed": verify_started_after_review_completed,
            "child_raw_event_count": len(child_raw_events),
            "child_raw_events_missing_subagent_metadata": len(child_metadata_missing),
        }
    finally:
        subagent_runtime = wiring.agent_runtime.subagent_runtime
        child_session_ids = [
            run.child_runtime_session_id
            for run in (subagent_runtime.runs if subagent_runtime is not None else ())
        ]
        wiring.agent_runtime.close()
        _delete_sessions(
            settings.storage.postgres_dsn,
            [*child_session_ids, wiring.runtime_wiring.runtime_session.runtime_session_id],
        )


def _prepare_workspace(workspace_root: Path) -> None:
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "pyproject.toml").write_text(
        """
[project]
name = "pulsara-subagent-system-dogfood"
version = "0.1.0"
requires-python = ">=3.12"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (workspace_root / "README.md").write_text(
        f"""
# Pulsara Subagent System Dogfood Fixture

The canonical subsystem marker is `{SPEC_SENTINEL}`.
Review workers should find this marker by reading `subsystem_spec.py`.
Verification workers should run the exact command provided by the parent.
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (workspace_root / "subsystem_spec.py").write_text(
        f"""
SPEC_SENTINEL = "{SPEC_SENTINEL}"


def subsystem_marker() -> str:
    return SPEC_SENTINEL
""".lstrip(),
        encoding="utf-8",
    )


def _system_prompt() -> str:
    review_task = (
        "REVIEW_SUBTASK: You are the review child. You MUST call report_agent_phase "
        'with phase "reviewing"; then read_file("subsystem_spec.py"); then call '
        f"report_agent_result with a summary containing {REVIEW_SENTINEL} and {SPEC_SENTINEL}. "
        "Do not finish with plain final text before report_agent_result."
    )
    verify_task = (
        "VERIFY_SUBTASK: You are the verification child. You MUST call report_agent_phase "
        f'with phase "verifying"; then call terminal with command exactly {VERIFY_COMMAND!r}; '
        f"then call report_agent_result with a summary containing {VERIFY_SENTINEL} and {SPEC_SENTINEL}. "
        "Do not finish with plain final text before report_agent_result."
    )
    return f"""
You are running a Pulsara real-LLM subsystem-agent dogfood.

Child task rules:
- If the current task starts with "REVIEW_SUBTASK", you are the review child.
  1. Call report_agent_phase with phase exactly "reviewing".
  2. Call read_file on "subsystem_spec.py".
  3. Call report_agent_result with a summary containing both {REVIEW_SENTINEL} and {SPEC_SENTINEL}.
  4. Do not call terminal, create_agent_tasks, spawn_agent, wait_agent, or wait_agent_tasks.
- If the current task starts with "VERIFY_SUBTASK", you are the verification child.
  1. Call report_agent_phase with phase exactly "verifying".
  2. Call terminal with command exactly: {VERIFY_COMMAND}
  3. Call report_agent_result with a summary containing both {VERIFY_SENTINEL} and {SPEC_SENTINEL}.
  4. Do not call create_agent_tasks, spawn_agent, wait_agent, or wait_agent_tasks.

Parent orchestrator rules:
1. Do not call primitive spawn_agent or wait_agent.
2. Call create_agent_tasks exactly once with two tasks:
   - task_key "review", profile "review_worker", label "Review fixture", task exactly: {review_task!r}
   - task_key "verify", profile "verification_worker", label "Verify fixture", depends_on ["review"], task exactly: {verify_task!r}
3. After create_agent_tasks returns, call list_agents at least once.
4. Then call wait_agent_tasks with both returned task_ids, settle "all", timeout_seconds 240.
5. After wait_agent_tasks returns, personally call read_file on "subsystem_spec.py" to verify {SPEC_SENTINEL}.
6. Final-answer with one short line containing all four sentinels:
   {PARENT_SENTINEL} {REVIEW_SENTINEL} {VERIFY_SENTINEL} {SPEC_SENTINEL}

Do not use memory tools. Do not inspect secrets. Do not modify files.
""".strip()


def _parent_user_prompt() -> str:
    return (
        "Run the subsystem-agent dogfood now. Use create_agent_tasks, list_agents, "
        "wait_agent_tasks, then personally verify subsystem_spec.py before final response."
    )


def _settings() -> PulsaraSettings:
    env_file = os.getenv("PULSARA_REAL_LLM_ENV_FILE")
    if env_file:
        return PulsaraSettings.from_env_file(env_file)
    path = Path(".env")
    if path.exists():
        return PulsaraSettings.from_env_file(path)
    return PulsaraSettings.from_env()


def _connect_or_skip(dsn: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select 1")
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _delete_sessions(dsn: str, runtime_session_ids: Iterable[str | None]) -> None:
    session_ids = [session_id for session_id in dict.fromkeys(runtime_session_ids) if session_id]
    if not session_ids:
        return
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for runtime_session_id in session_ids:
                cursor.execute("delete from sessions where id = %s", (runtime_session_id,))
