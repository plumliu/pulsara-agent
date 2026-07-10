from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.capability.types import CapabilityResolveContext
from pulsara_agent.event import (
    CapabilityGateDecisionEvent,
    EventContext,
    ModelCallStartEvent,
    RunEndEvent,
    RunStartEvent,
    SubagentEdgeRecordedEvent,
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
    ToolResultEndEvent,
)
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime import LoopBudget
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.runtime.wiring import build_agent_runtime_wiring
from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.tools.base import ToolCall, ToolRuntimeContext
from pulsara_agent.tools.builtins.subagent import CreateAgentTasksTool


pytestmark = pytest.mark.real_llm


PARENT_SENTINEL = "PULSARA_SUBSYSTEM_PARENT_OK"
REVIEW_SENTINEL = "PULSARA_SUBSYSTEM_REVIEW_OK"
VERIFY_SENTINEL = "PULSARA_SUBSYSTEM_VERIFY_OK"
SPEC_SENTINEL = "PULSARA_SUBSYSTEM_SPEC_ALPHA"
INDEPENDENT_A_SENTINEL = "PULSARA_INDEPENDENT_A_OK"
INDEPENDENT_B_SENTINEL = "PULSARA_INDEPENDENT_B_OK"
RESTART_CHILD_SENTINEL = "PULSARA_RESTART_CHILD_OK"
RESTART_PARENT_SENTINEL = "PULSARA_RESTART_PARENT_OK"
FALLBACK_FILE_SENTINEL = "PULSARA_FALLBACK_FILE_OK"
FALLBACK_TERMINAL_SENTINEL = "PULSARA_FALLBACK_TERMINAL_OK"
FALLBACK_PARENT_SENTINEL = "PULSARA_FALLBACK_PARENT_OK"
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
    assert result["result_submitted_count"] == 2, result
    assert set(result["reported_phases"]) == {"reviewing", "verifying"}, result
    assert any(REVIEW_SENTINEL in item for item in result["submitted_summaries"]), result
    assert any(VERIFY_SENTINEL in item for item in result["submitted_summaries"]), result
    assert result["child_raw_event_count"] > 0, result
    assert result["child_raw_events_missing_subagent_metadata"] == 0, result

    assert result["review_task_id"], result
    assert result["verify_task_id"], result
    assert result["verify_waited_for_review"] is True, result
    assert result["verify_started_after_review_completed"] is True, result


def test_real_llm_subagent_independent_parallel_explicit_result_dogfood(tmp_path: Path) -> None:
    _require_system_dogfood()
    settings = _settings()
    _connect_or_skip(settings.storage.postgres_dsn)
    result = asyncio.run(_run_independent_parallel_explicit_result_dogfood(settings, tmp_path))
    print(
        "\nREAL_LLM_SUBAGENT_INDEPENDENT_EXPLICIT_DOGFOOD="
        + json.dumps(result, ensure_ascii=False, sort_keys=True)
    )

    assert result["task_created_count"] == 2, result
    assert result["task_started_count"] == 2, result
    assert result["task_completed_count"] == 2, result
    assert result["run_completed_count"] == 2, result
    assert result["result_submitted_count"] == 2, result
    assert result["all_starts_precede_first_completion"] is True, result
    assert result["result_sources"] == ["explicit", "explicit"], result
    assert result["child_report_result_calls"] == 2, result
    assert result["child_report_result_states"] == ["success", "success"], result
    assert all(item["decision"] == "allow" for item in result["child_gate_decisions"]), result
    assert result["child_followup_model_calls_after_report"] == 0, result
    assert result["graph_backend"] == "DurableGraphFacade", result
    assert result["oxigraph_backend"] == "OxigraphGraphStore", result
    assert INDEPENDENT_A_SENTINEL in result["summaries"], result
    assert INDEPENDENT_B_SENTINEL in result["summaries"], result


def test_real_llm_subagent_durable_restart_wait_dogfood(tmp_path: Path) -> None:
    _require_system_dogfood()
    settings = _settings()
    _connect_or_skip(settings.storage.postgres_dsn)
    result = asyncio.run(_run_durable_restart_wait_dogfood(settings, tmp_path))
    print(
        "\nREAL_LLM_SUBAGENT_RESTART_WAIT_DOGFOOD="
        + json.dumps(result, ensure_ascii=False, sort_keys=True)
    )

    assert result["status"] == "finished", result
    assert result["runtime_session_id_reused"] is True, result
    assert result["fresh_runtime_recovered_completed_run"] is True, result
    assert result["spawn_calls_after_restart"] == 0, result
    assert result["wait_calls_after_restart"] >= 1, result
    assert result["wait_edge_count"] == 1, result
    assert result["consumed_after_restart"] is True, result
    assert RESTART_CHILD_SENTINEL in result["final_text"], result
    assert RESTART_PARENT_SENTINEL in result["final_text"], result


def test_real_llm_subagent_failed_dependency_parent_self_verifies_dogfood(tmp_path: Path) -> None:
    _require_system_dogfood()
    settings = _settings()
    _connect_or_skip(settings.storage.postgres_dsn)
    result = asyncio.run(_run_failed_dependency_parent_self_verifies_dogfood(settings, tmp_path))
    print(
        "\nREAL_LLM_SUBAGENT_FAILED_DEPENDENCY_FALLBACK_DOGFOOD="
        + json.dumps(result, ensure_ascii=False, sort_keys=True)
    )

    assert result["status"] == "finished", result
    assert result["upstream_status"] == "failed", result
    assert result["downstream_status"] == "blocked_dependency_failed", result
    assert result["list_agents_calls"] >= 1, result
    assert result["wait_agent_tasks_calls"] >= 1, result
    assert result["read_file_calls"] >= 1, result
    assert result["terminal_calls"] >= 1, result
    assert result["new_subagent_calls"] == 0, result
    assert result["consumed_task_count"] == 2, result
    assert FALLBACK_FILE_SENTINEL in result["final_text"], result
    assert FALLBACK_TERMINAL_SENTINEL in result["final_text"], result
    assert FALLBACK_PARENT_SENTINEL in result["final_text"], result


async def _run_independent_parallel_explicit_result_dogfood(
    settings: PulsaraSettings,
    tmp_path: Path,
) -> dict[str, object]:
    workspace_root = tmp_path / "independent-explicit-workspace"
    workspace_root.mkdir(parents=True)
    wiring = build_agent_runtime_wiring(
        settings,
        workspace_root,
        durable=True,
        model_role=ModelRole.PRO,
        options=LLMOptions(temperature=0, max_output_tokens=768),
        system_prompt=_independent_explicit_system_prompt(),
        memory_reflection=False,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
    )
    wiring.agent_runtime.budget = LoopBudget(
        max_turns=8,
        max_tool_calls=8,
        max_consecutive_model_failures=2,
        max_consecutive_tool_failures=2,
        max_subagent_results_per_parent_compile=0,
    )
    parent_session_id = wiring.runtime_wiring.runtime_session.runtime_session_id
    subagent_runtime = wiring.agent_runtime.subagent_runtime
    assert subagent_runtime is not None
    context = EventContext(
        run_id=f"run:independent-explicit:{uuid4().hex}",
        turn_id=f"turn:independent-explicit:{uuid4().hex}",
        reply_id=f"reply:independent-explicit:{uuid4().hex}",
    )
    child_session_ids: list[str] = []
    try:
        await _write_seed_run_start(
            wiring,
            context,
            "Launch two independent real-LLM children with explicit results.",
        )
        _prime_subagent_parent_capability_snapshot(wiring)
        tool = CreateAgentTasksTool(subagent_runtime)
        result = await tool.execute_async(
            ToolCall(
                id="tool:create-independent-explicit",
                name="create_agent_tasks",
                arguments={
                    "tasks": [
                        {
                            "task_key": "independent-a",
                            "profile": "review_worker",
                            "task": (
                                "INDEPENDENT_A_TASK: call report_agent_result exactly once with "
                                f"summary {INDEPENDENT_A_SENTINEL!r}; do not answer with plain text."
                            ),
                        },
                        {
                            "task_key": "independent-b",
                            "profile": "review_worker",
                            "task": (
                                "INDEPENDENT_B_TASK: call report_agent_result exactly once with "
                                f"summary {INDEPENDENT_B_SENTINEL!r}; do not answer with plain text."
                            ),
                        },
                    ]
                },
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id=parent_session_id,
                event_context=context,
            ),
        )
        assert result.status is ToolResultState.SUCCESS, result.output
        payload = json.loads(result.output)
        task_ids = tuple(item["task_id"] for item in payload["tasks"])
        waited = await subagent_runtime.wait_tasks(
            task_ids,
            event_context=context,
            consumer_tool_call_id="tool:wait-independent-explicit",
            settle="all",
            timeout_seconds=240,
        )
        assert len(waited) == 2, waited
        await wiring.runtime_wiring.runtime_session.write_event(
            RunEndEvent(
                **context.event_fields(),
                status="finished",
                stop_reason="subagent_independent_explicit_dogfood",
            )
        )

        parent_events = wiring.runtime_wiring.event_log.iter()
        task_created = [event for event in parent_events if isinstance(event, SubagentTaskCreatedEvent)]
        task_started = [event for event in parent_events if isinstance(event, SubagentTaskStartedEvent)]
        task_completed = [event for event in parent_events if isinstance(event, SubagentTaskCompletedEvent)]
        run_completed = [event for event in parent_events if isinstance(event, SubagentRunCompletedEvent)]
        submitted = [event for event in parent_events if isinstance(event, SubagentResultSubmittedEvent)]
        child_report_calls = 0
        child_followups = 0
        child_gate_decisions: list[dict[str, object]] = []
        child_report_result_states: list[str] = []
        for run in subagent_runtime.runs:
            child_session_ids.append(run.child_runtime_session_id)
            child_events = subagent_runtime.child_event_log(run.subagent_run_id).iter()
            report_sequences = [
                event.sequence or 0
                for event in child_events
                if isinstance(event, ToolCallStartEvent)
                and event.tool_call_name == "report_agent_result"
            ]
            child_report_calls += len(report_sequences)
            child_gate_decisions.extend(
                {
                    "tool_name": event.tool_name,
                    "decision": event.decision,
                    "reason_code": event.reason_code,
                    "reason_message": event.reason_message,
                    "policy_mode": event.policy_mode,
                }
                for event in child_events
                if isinstance(event, CapabilityGateDecisionEvent)
                and event.tool_name == "report_agent_result"
            )
            child_report_result_states.extend(
                event.state.value
                for event in child_events
                if isinstance(event, ToolResultEndEvent)
                and event.tool_call_id
                in {
                    call.tool_call_id
                    for call in child_events
                    if isinstance(call, ToolCallStartEvent)
                    and call.tool_call_name == "report_agent_result"
                }
            )
            if report_sequences:
                last_report_sequence = max(report_sequences)
                child_followups += sum(
                    1
                    for event in child_events
                    if isinstance(event, ModelCallStartEvent)
                    and (event.sequence or 0) > last_report_sequence
                )
        return {
            "task_created_count": len(task_created),
            "task_started_count": len(task_started),
            "task_completed_count": len(task_completed),
            "run_completed_count": len(run_completed),
            "result_submitted_count": len(submitted),
            "all_starts_precede_first_completion": bool(
                task_started
                and run_completed
                and max(event.sequence or 0 for event in task_started)
                < min(event.sequence or 0 for event in run_completed)
            ),
            "result_sources": sorted(event.result_source for event in task_completed),
            "child_report_result_calls": child_report_calls,
            "child_followup_model_calls_after_report": child_followups,
            "child_gate_decisions": child_gate_decisions,
            "child_report_result_states": child_report_result_states,
            "graph_backend": type(wiring.runtime_wiring.graph).__name__,
            "oxigraph_backend": type(getattr(wiring.runtime_wiring.graph, "oxigraph", None)).__name__,
            "summaries": [event.summary for event in submitted],
        }
    finally:
        child_session_ids = list(
            dict.fromkeys(
                [
                    *child_session_ids,
                    *(run.child_runtime_session_id for run in subagent_runtime.runs),
                ]
            )
        )
        wiring.agent_runtime.close()
        _delete_sessions(settings.storage.postgres_dsn, [*child_session_ids, parent_session_id])


async def _run_durable_restart_wait_dogfood(
    settings: PulsaraSettings,
    tmp_path: Path,
) -> dict[str, object]:
    workspace_root = tmp_path / "restart-wait-workspace"
    workspace_root.mkdir(parents=True)
    runtime_session_id = f"runtime:real-restart:{uuid4().hex}"
    first = build_agent_runtime_wiring(
        settings,
        workspace_root,
        durable=True,
        runtime_session_id=runtime_session_id,
        model_role=ModelRole.PRO,
        options=LLMOptions(temperature=0, max_output_tokens=512),
        system_prompt=_restart_child_system_prompt(),
        memory_reflection=False,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
    )
    first.agent_runtime.budget = LoopBudget(
        max_turns=6,
        max_tool_calls=4,
        max_consecutive_model_failures=2,
        max_consecutive_tool_failures=2,
        max_subagent_results_per_parent_compile=0,
    )
    seed_context = EventContext(
        run_id=f"run:restart-seed:{uuid4().hex}",
        turn_id=f"turn:restart-seed:{uuid4().hex}",
        reply_id=f"reply:restart-seed:{uuid4().hex}",
    )
    first_subagents = first.agent_runtime.subagent_runtime
    assert first_subagents is not None
    child_session_id: str | None = None
    subagent_run_id: str | None = None
    try:
        await _write_seed_run_start(first, seed_context, "Seed a completed child before restart.")
        child = await first_subagents.spawn_agent(
            task=(
                "RESTART_CHILD_TASK: do not call tools; reply exactly "
                f"{RESTART_CHILD_SENTINEL}"
            ),
            event_context=seed_context,
        )
        subagent_run_id = child.subagent_run_id
        child_session_id = child.child_runtime_session_id
        await _wait_for_terminal_run(first_subagents, subagent_run_id, timeout_seconds=240)
        completed_before_restart = first_subagents.result_for_run(subagent_run_id)
        assert completed_before_restart is not None
        assert RESTART_CHILD_SENTINEL in completed_before_restart.summary
        await first.runtime_wiring.runtime_session.write_event(
            RunEndEvent(
                **seed_context.event_fields(),
                status="finished",
                stop_reason="restart_seed_complete",
            )
        )
    except Exception:
        first.agent_runtime.close()
        _delete_sessions(
            settings.storage.postgres_dsn,
            [runtime_session_id, child_session_id],
        )
        raise
    else:
        first.agent_runtime.close()

    second = build_agent_runtime_wiring(
        settings,
        workspace_root,
        durable=True,
        runtime_session_id=runtime_session_id,
        model_role=ModelRole.PRO,
        options=LLMOptions(temperature=0, max_output_tokens=768),
        system_prompt=_restart_parent_system_prompt(subagent_run_id or ""),
        memory_reflection=False,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
    )
    second.agent_runtime.budget = LoopBudget(
        max_turns=8,
        max_tool_calls=8,
        max_consecutive_model_failures=2,
        max_consecutive_tool_failures=2,
        max_subagent_results_per_parent_compile=0,
    )
    try:
        second_subagents = second.agent_runtime.subagent_runtime
        assert second_subagents is not None
        recovered = next(
            run for run in second_subagents.runs if run.subagent_run_id == subagent_run_id
        )
        run_result = await second.agent_runtime.run_task(
            "After the process-style runtime rebuild, retrieve the completed child with wait_agent now."
        )
        current_events = second.runtime_wiring.event_log.iter(run_id=run_result.state.run_id)
        tool_names = [
            event.tool_call_name
            for event in current_events
            if isinstance(event, ToolCallStartEvent)
        ]
        all_events = second.runtime_wiring.event_log.iter()
        wait_edges = [
            event
            for event in all_events
            if isinstance(event, SubagentEdgeRecordedEvent)
            and event.edge_kind == "wait"
            and event.subagent_run_id == subagent_run_id
        ]
        projection = second_subagents.graph()
        recovered_node = next(
            node for node in projection.nodes if node.subagent_run_id == subagent_run_id
        )
        return {
            "status": run_result.status.value,
            "final_text": run_result.final_text,
            "runtime_session_id_reused": (
                second.runtime_wiring.runtime_session.runtime_session_id == runtime_session_id
            ),
            "fresh_runtime_recovered_completed_run": recovered.status == "completed",
            "spawn_calls_after_restart": tool_names.count("spawn_agent"),
            "wait_calls_after_restart": tool_names.count("wait_agent"),
            "wait_edge_count": len(wait_edges),
            "consumed_after_restart": recovered_node.consumed_by_wait,
        }
    finally:
        second.agent_runtime.close()
        _delete_sessions(
            settings.storage.postgres_dsn,
            [runtime_session_id, child_session_id],
        )


async def _run_failed_dependency_parent_self_verifies_dogfood(
    settings: PulsaraSettings,
    tmp_path: Path,
) -> dict[str, object]:
    workspace_root = tmp_path / "failed-dependency-workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "fallback_fact.txt").write_text(
        FALLBACK_FILE_SENTINEL + "\n",
        encoding="utf-8",
    )
    terminal_command = (
        "uv run python -c \"from pathlib import Path; "
        f"assert Path('fallback_fact.txt').read_text().strip() == '{FALLBACK_FILE_SENTINEL}'; "
        f"print('{FALLBACK_TERMINAL_SENTINEL}')\""
    )
    wiring = build_agent_runtime_wiring(
        settings,
        workspace_root,
        durable=True,
        model_role=ModelRole.PRO,
        options=LLMOptions(temperature=0, max_output_tokens=1024),
        system_prompt=_failed_dependency_parent_system_prompt(terminal_command),
        memory_reflection=False,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
    )
    wiring.agent_runtime.budget = LoopBudget(
        max_turns=12,
        max_tool_calls=12,
        max_consecutive_model_failures=2,
        max_consecutive_tool_failures=3,
        max_subagent_results_per_parent_compile=0,
    )
    parent_session_id = wiring.runtime_wiring.runtime_session.runtime_session_id
    subagent_runtime = wiring.agent_runtime.subagent_runtime
    assert subagent_runtime is not None
    seed_context = EventContext(
        run_id=f"run:failed-dependency-seed:{uuid4().hex}",
        turn_id=f"turn:failed-dependency-seed:{uuid4().hex}",
        reply_id=f"reply:failed-dependency-seed:{uuid4().hex}",
    )
    try:
        await _write_seed_run_start(wiring, seed_context, "Seed failed and blocked task facts.")
        upstream = await subagent_runtime.create_task(
            objective="This upstream task failed before producing evidence.",
            event_context=seed_context,
            profile_id="review_worker",
            task_key="failed-upstream",
        )
        await subagent_runtime.fail_materialized_task(
            upstream.task_id,
            event_context=seed_context,
            reason_code="real_dogfood_upstream_failed",
            reason_message="The seeded upstream task failed.",
        )
        downstream = await subagent_runtime.create_task(
            objective="This downstream task depends on the failed upstream task.",
            event_context=seed_context,
            profile_id="verification_worker",
            task_key="blocked-downstream",
            depends_on=(upstream.task_id,),
        )
        await subagent_runtime.block_task(
            downstream.task_id,
            event_context=seed_context,
            status="blocked_dependency_failed",
            blocked_reason="dependency_failed",
            blocked_by_task_ids=(upstream.task_id,),
        )
        await wiring.runtime_wiring.runtime_session.write_event(
            RunEndEvent(
                **seed_context.event_fields(),
                status="finished",
                stop_reason="failed_dependency_seed_complete",
            )
        )

        run_result = await wiring.agent_runtime.run_task(
            _failed_dependency_parent_user_prompt(
                upstream_task_id=upstream.task_id,
                downstream_task_id=downstream.task_id,
                terminal_command=terminal_command,
            )
        )
        current_events = wiring.runtime_wiring.event_log.iter(run_id=run_result.state.run_id)
        tool_names = [
            event.tool_call_name
            for event in current_events
            if isinstance(event, ToolCallStartEvent)
        ]
        current_consumptions = [
            event
            for event in current_events
            if isinstance(event, SubagentResultConsumedEvent)
            and event.kind == "wait_task"
        ]
        graph = {task.task_id: task for task in subagent_runtime.graph().tasks}
        return {
            "status": run_result.status.value,
            "final_text": run_result.final_text,
            "upstream_status": graph[upstream.task_id].status,
            "downstream_status": graph[downstream.task_id].status,
            "list_agents_calls": tool_names.count("list_agents"),
            "wait_agent_tasks_calls": tool_names.count("wait_agent_tasks"),
            "read_file_calls": tool_names.count("read_file"),
            "terminal_calls": tool_names.count("terminal"),
            "new_subagent_calls": tool_names.count("spawn_agent") + tool_names.count("create_agent_tasks"),
            "consumed_task_count": len(current_consumptions),
        }
    finally:
        wiring.agent_runtime.close()
        _delete_sessions(settings.storage.postgres_dsn, [parent_session_id])


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
            child_raw_events.extend(
                subagent_runtime.child_event_log(run.subagent_run_id).iter()
            )
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


def _independent_explicit_system_prompt() -> str:
    return f"""
You are a child in a Pulsara hard-cut subagent dogfood.

If the task starts with INDEPENDENT_A_TASK:
- Do not call any tool except report_agent_result.
- Your first and only action must call report_agent_result with summary exactly
  {INDEPENDENT_A_SENTINEL}
- Do not answer with ordinary assistant text.

If the task starts with INDEPENDENT_B_TASK:
- Do not call any tool except report_agent_result.
- Your first and only action must call report_agent_result with summary exactly
  {INDEPENDENT_B_SENTINEL}
- Do not answer with ordinary assistant text.
""".strip()


def _restart_child_system_prompt() -> str:
    return f"""
If the current task starts with RESTART_CHILD_TASK, call no tools and answer exactly:
{RESTART_CHILD_SENTINEL}
""".strip()


def _restart_parent_system_prompt(subagent_run_id: str) -> str:
    return f"""
You are the parent after a durable Pulsara runtime rebuild.
- Do not call spawn_agent or create_agent_tasks.
- Call wait_agent exactly once with subagent_run_id {subagent_run_id!r}.
- After the completed result is returned, answer exactly:
  {RESTART_PARENT_SENTINEL} {RESTART_CHILD_SENTINEL}
- Do not call any other tools.
""".strip()


def _failed_dependency_parent_system_prompt(terminal_command: str) -> str:
    return f"""
You are the main Pulsara agent. Two existing subagent tasks are already terminal:
one failed and its dependent is blocked. Subagent output is secondary evidence.

You must:
1. Call list_agents.
2. Call wait_agent_tasks for the exact task ids supplied by the user, settle="all",
   timeout_seconds=0.
3. Do not spawn or create replacement subagents.
4. Personally call read_file on fallback_fact.txt.
5. Personally call terminal with this exact command:
   {terminal_command}
6. Only after both direct checks succeed, answer with one line containing:
   {FALLBACK_PARENT_SENTINEL} {FALLBACK_FILE_SENTINEL} {FALLBACK_TERMINAL_SENTINEL}

Do not use memory tools and do not inspect secret files.
""".strip()


def _failed_dependency_parent_user_prompt(
    *,
    upstream_task_id: str,
    downstream_task_id: str,
    terminal_command: str,
) -> str:
    return (
        "The upstream task failed and its dependent is blocked. "
        f"Use list_agents, then wait_agent_tasks on {[upstream_task_id, downstream_task_id]!r} "
        "with settle='all' and timeout_seconds=0. Do not create another subagent. "
        "Then personally read fallback_fact.txt and run this exact terminal command: "
        f"{terminal_command}. Finish with the required three sentinels."
    )


async def _write_seed_run_start(wiring, context: EventContext, user_input: str) -> None:
    await wiring.runtime_wiring.runtime_session.write_event(
        RunStartEvent(
            **context.event_fields(),
            user_input_chars=len(user_input),
            permission_snapshot_id=f"permission_snapshot:{context.run_id}",
            permission_mode=PermissionMode.BYPASS_PERMISSIONS.value,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS).to_dict(),
            permission_snapshot_source="session_default",
            metadata={"user_input": user_input},
        )
    )


def _prime_subagent_parent_capability_snapshot(wiring) -> None:
    agent = wiring.agent_runtime
    subagent_runtime = agent.subagent_runtime
    assert subagent_runtime is not None
    policy = preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)
    exposure = agent.capability_runtime.resolve_for_turn(
        CapabilityResolveContext(
            workspace_root=agent.runtime_session.workspace_root,
            workspace_kind=agent.workspace_kind,
            memory_domain=agent.memory_domain,
            available_tool_names=frozenset(agent.tool_executor.registry.names()),
            user_input="Programmatic real-LLM subagent dogfood setup.",
            prior_messages=(),
            active_skill_names=frozenset(),
        ),
        tool_registry=agent.tool_executor.registry,
        permission_policy=policy,
        plan_active=False,
    )
    subagent_runtime.refresh_parent_capability_snapshot(
        exposure=exposure,
        permission_mode=PermissionMode.BYPASS_PERMISSIONS.value,
        permission_policy=policy.to_dict(),
    )


async def _wait_for_terminal_run(
    subagent_runtime,
    subagent_run_id: str,
    *,
    timeout_seconds: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        run = next(
            item
            for item in subagent_runtime.runs
            if item.subagent_run_id == subagent_run_id
        )
        if run.status in {"completed", "failed", "cancelled"}:
            if run.status != "completed":
                raise AssertionError(f"real child terminated as {run.status}: {run}")
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"real child did not complete within {timeout_seconds}s")


def _require_system_dogfood() -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    if os.getenv("PULSARA_RUN_DOGFOOD_SUBAGENT_SYSTEM") != "1":
        pytest.skip(
            "Set PULSARA_RUN_DOGFOOD_SUBAGENT_SYSTEM=1 to run subsystem-agent real LLM dogfood."
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
