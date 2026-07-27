"""Thin model-facing adapters for the runtime-owned subagent command port."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.subagent import (
    SubagentControlPort,
    SubagentInventoryOutcome,
    SubagentPhaseReportedOutcome,
    SubagentResultSubmittedOutcome,
    SubagentRunStoppedOutcome,
    SubagentRunTerminalWithoutResultOutcome,
    SubagentSpawnedOutcome,
    SubagentTaskBatchAcceptedOutcome,
    SubagentTaskStoppedOutcome,
    SubagentTasksWaitedOutcome,
    SubagentToolNotReadyOutcome,
    SubagentToolOutcome,
    SubagentToolRejectedOutcome,
    SubagentWaitCompletedOutcome,
    build_subagent_command_owner,
    build_subagent_tool_command,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
    ToolRuntimeContext,
)
from pulsara_agent.primitives.context import thaw_json


@dataclass(slots=True)
class _ParentSubagentTool:
    control_port: SubagentControlPort
    name: ClassVar[str]

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        return await _execute(
            control_port=self.control_port,
            call=call,
            runtime_context=runtime_context,
            bound_child_subagent_run_id=None,
        )


@dataclass(slots=True)
class _ChildSubagentTool:
    control_port: SubagentControlPort
    subagent_run_id: str
    name: ClassVar[str]

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        return await _execute(
            control_port=self.control_port,
            call=call,
            runtime_context=runtime_context,
            bound_child_subagent_run_id=self.subagent_run_id,
        )


class SpawnAgentTool(_ParentSubagentTool):
    name = "spawn_agent"


class WaitAgentTool(_ParentSubagentTool):
    name = "wait_agent"


class StopAgentTool(_ParentSubagentTool):
    name = "stop_agent"


class ListAgentsTool(_ParentSubagentTool):
    name = "list_agents"


class CreateAgentTasksTool(_ParentSubagentTool):
    name = "create_agent_tasks"


class WaitAgentTasksTool(_ParentSubagentTool):
    name = "wait_agent_tasks"


class StopAgentTaskTool(_ParentSubagentTool):
    name = "stop_agent_task"


class ReportAgentPhaseTool(_ChildSubagentTool):
    name = "report_agent_phase"


class ReportAgentResultTool(_ChildSubagentTool):
    name = "report_agent_result"


async def _execute(
    *,
    control_port: SubagentControlPort,
    call: ToolCall,
    runtime_context: ToolRuntimeContext,
    bound_child_subagent_run_id: str | None,
) -> ToolExecutionResult:
    try:
        owner = build_subagent_command_owner(
            runtime_session_id=runtime_context.runtime_session_id,
            tool_call_id=call.id,
            tool_name=call.name,
            event_context=runtime_context.event_context,
            parent_context_id=runtime_context.context_id,
            parent_model_call_index=runtime_context.model_call_index,
            invocation_owner_kind=runtime_context.owner_kind,
            permission=runtime_context.permission,
            bound_child_subagent_run_id=bound_child_subagent_run_id,
        )
        command = build_subagent_tool_command(
            owner=owner,
            arguments=call.arguments,
        )
    except ValueError as exc:
        return _json_result(
            call,
            ToolResultState.ERROR,
            {
                "status": "error",
                "error_code": "malformed_arguments",
                "error": str(exc),
            },
        )
    outcome = await control_port.execute(command)
    state = (
        ToolResultState.ERROR
        if isinstance(outcome, SubagentToolRejectedOutcome)
        else ToolResultState.SUCCESS
    )
    return _json_result(call, state, _outcome_payload(outcome))


def _outcome_payload(outcome: SubagentToolOutcome) -> dict[str, object]:
    if isinstance(outcome, SubagentToolRejectedOutcome):
        public_error_code = {
            "batch_preflight_failed": "subagent_task_batch_preflight_failed",
            "batch_start_failed": "subagent_task_batch_start_failed",
        }.get(outcome.reject_code.value, outcome.reject_code.value)
        payload: dict[str, object] = {
            "status": "error",
            "error_code": public_error_code,
            "error": outcome.sanitized_message,
        }
        if outcome.batch_id is not None:
            payload.update(
                {
                    "batch_id": outcome.batch_id,
                    "failed_stage": outcome.failed_stage,
                    "failed_task_keys": list(outcome.failed_task_keys),
                    "diagnostics": [thaw_json(item) for item in outcome.diagnostics],
                }
            )
        return payload
    if isinstance(outcome, SubagentSpawnedOutcome):
        return {
            "status": "started",
            "subagent_run_id": outcome.subagent_run_id,
            "child_runtime_session_id": outcome.child_runtime_session_id,
            "label": outcome.label,
            "role": outcome.role,
            "context": outcome.context_mode,
            "message": (
                "Child agent started. Use wait_agent with subagent_run_id to "
                "collect the result."
            ),
        }
    if isinstance(outcome, SubagentWaitCompletedOutcome):
        result = outcome.result
        return {
            "status": result.status,
            "subagent_run_id": result.subagent_run_id,
            "task_id": result.task_id,
            "result_id": result.result_id,
            "summary": result.summary,
            "output_preview": result.output_preview,
            "result_artifact_id": result.result_artifact_id,
            "artifact_ids": list(result.artifact_ids),
            "result_source": result.result_source,
            "diagnostics": [thaw_json(item) for item in result.diagnostics],
        }
    if isinstance(outcome, SubagentRunTerminalWithoutResultOutcome):
        return {
            "status": outcome.status,
            "subagent_run_id": outcome.subagent_run_id,
            "task_id": outcome.task_id,
            "reason_code": outcome.reason_code,
            "terminal_event_id": outcome.terminal_event_id,
            "result_id": None,
            "result_artifact_id": None,
            "message": "Child agent reached a terminal state without a result.",
        }
    if isinstance(outcome, SubagentToolNotReadyOutcome):
        return {
            "status": "not_ready",
            "subagent_run_id": outcome.subagent_run_ids[0],
            "message": (
                "Child agent has not completed yet. Call wait_agent again later."
            ),
        }
    if isinstance(outcome, SubagentRunStoppedOutcome):
        return {
            "status": outcome.status,
            "subagent_run_id": outcome.subagent_run_id,
        }
    if isinstance(outcome, SubagentInventoryOutcome):
        items: list[dict[str, object]] = []
        for item in outcome.items:
            row = {
                key: value
                for key, value in _public_dataclass_payload(item).items()
                if key != "item_fingerprint"
            }
            if isinstance(row.get("depends_on"), tuple):
                row["depends_on"] = list(row["depends_on"])
            if row.get("item_kind") == "run":
                row["task_id"] = None
            row["current_run_id"] = row.get("subagent_run_id")
            items.append(row)
        payload = {
            "status": "ok",
            "parent_runtime_session_id": outcome.parent_runtime_session_id,
            "items": items,
            "truncated": outcome.items_truncated,
            "total_items": outcome.total_items,
            "diagnostics": [thaw_json(item) for item in outcome.diagnostics],
        }
        if outcome.edges:
            payload.update(
                {
                    "edges": [
                        {
                            key: value
                            for key, value in _public_dataclass_payload(edge).items()
                            if key != "edge_fingerprint"
                        }
                        for edge in outcome.edges
                    ],
                    "edges_truncated": outcome.edges_truncated,
                    "total_edges": outcome.total_edges,
                }
            )
        return payload
    if isinstance(outcome, SubagentTaskBatchAcceptedOutcome):
        return {
            "status": "accepted",
            "batch_id": outcome.batch_id,
            "started_count": outcome.started_count,
            "tasks": [
                {
                    "task_id": item.task_id,
                    "task_key": item.task_key,
                    "label": item.label,
                    "profile": item.profile,
                    "status": item.status,
                    "subagent_run_id": item.subagent_run_id,
                    "child_runtime_session_id": item.child_runtime_session_id,
                }
                for item in outcome.tasks
            ],
            "message": (
                "Subagent task batch materialized. Runnable tasks were started; "
                "dependency-waiting or dependency-blocked tasks are reported per "
                "item. Use wait_agent_tasks to collect settled results."
            ),
        }
    if isinstance(outcome, SubagentTasksWaitedOutcome):
        return {
            "status": "ok",
            "settle": outcome.settle,
            "returned_count": len(outcome.results),
            "results": [
                {
                    key: (list(value) if isinstance(value, tuple) else value)
                    for key, value in _public_dataclass_payload(item).items()
                    if key != "view_fingerprint"
                }
                for item in outcome.results
            ],
        }
    if isinstance(outcome, SubagentTaskStoppedOutcome):
        return {
            "status": outcome.status,
            "task_id": outcome.task_id,
            "subagent_run_id": outcome.subagent_run_id,
        }
    if isinstance(outcome, SubagentPhaseReportedOutcome):
        return {
            "status": "phase_reported",
            "subagent_run_id": outcome.subagent_run_id,
            "phase": outcome.phase,
        }
    if isinstance(outcome, SubagentResultSubmittedOutcome):
        return {
            "status": "result_submitted",
            "subagent_run_id": outcome.subagent_run_id,
            "result_id": outcome.result_id,
            "summary": outcome.summary,
            "message": (
                "Explicit result submitted; the child run will stop at the next "
                "safe point."
            ),
        }
    raise AssertionError(type(outcome))


def _public_dataclass_payload(value: object) -> dict[str, object]:
    from dataclasses import asdict

    return asdict(value)


def _json_result(
    call: ToolCall,
    status: ToolResultState,
    payload: dict[str, object],
) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=status,
        output=json.dumps(payload, ensure_ascii=False, indent=2),
    )


__all__ = [
    "CreateAgentTasksTool",
    "ListAgentsTool",
    "ReportAgentPhaseTool",
    "ReportAgentResultTool",
    "SpawnAgentTool",
    "StopAgentTaskTool",
    "StopAgentTool",
    "WaitAgentTasksTool",
    "WaitAgentTool",
]
