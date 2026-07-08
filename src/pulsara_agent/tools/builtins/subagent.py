"""Built-in tools for Pulsara subagent orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.subagent import (
    SubagentContextPolicy,
    SubagentLimitExceeded,
    SubagentNotFound,
    SubagentNotReady,
    SubagentRuntime,
    SubagentRuntimeError,
)
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolRuntimeContext


_ALLOWED_ROLES = frozenset({"worker", "verifier", "synthesizer", "orchestrator"})
_ALLOWED_CONTEXT_MODES = frozenset({"isolated", "fork"})
_ALLOWED_TASK_PROFILES = frozenset({"research_worker", "review_worker", "verification_worker", "general_worker"})


@dataclass(slots=True)
class SpawnAgentTool:
    subagent_runtime: SubagentRuntime

    name: str = "spawn_agent"
    description: str = (
        "Start an isolated child agent runtime for a bounded subtask. The child runs as a "
        "runtime session, not as a script function; use wait_agent to explicitly collect its result."
    )
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Concrete task prompt for the child agent.",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for inspect and graph projection.",
                },
                "role": {
                    "type": "string",
                    "enum": ["worker", "verifier", "synthesizer", "orchestrator"],
                    "description": "Logical role for the child runtime.",
                },
                "context": {
                    "type": "string",
                    "enum": ["isolated", "fork"],
                    "description": "Context policy. Default is isolated; fork is explicit.",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        try:
            task = _required_str(call.arguments.get("task"), "task")
            label = _optional_str(call.arguments.get("label"))
            role = _optional_str(call.arguments.get("role")) or "worker"
            context_mode = _optional_str(call.arguments.get("context")) or "isolated"
            _require_member(role, _ALLOWED_ROLES, "role")
            _require_member(context_mode, _ALLOWED_CONTEXT_MODES, "context")
            subagent = await self.subagent_runtime.spawn_agent(
                task=task,
                label=label,
                role=role,  # type: ignore[arg-type]
                context_policy=SubagentContextPolicy(mode=context_mode),  # type: ignore[arg-type]
                event_context=runtime_context.event_context,
                parent_context_id=runtime_context.context_id,
                parent_model_call_index=runtime_context.model_call_index,
                spawning_tool_call_id=call.id,
                spawning_tool_name=call.name,
            )
        except ValueError as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        except SubagentLimitExceeded as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        return _json_result(
            call,
            ToolResultState.SUCCESS,
            {
                "status": "started",
                "subagent_run_id": subagent.subagent_run_id,
                "child_runtime_session_id": subagent.child_runtime_session_id,
                "label": subagent.label,
                "role": subagent.role,
                "context": subagent.context_policy.mode,
                "message": "Child agent started. Use wait_agent with subagent_run_id to collect the result.",
            },
        )


@dataclass(slots=True)
class WaitAgentTool:
    subagent_runtime: SubagentRuntime

    name: str = "wait_agent"
    description: str = (
        "Collect a completed child agent result. A successful wait marks the result as explicitly "
        "consumed so it is not also auto-injected as a background subagent result."
    )
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "subagent_run_id": {"type": "string"},
                "timeout_seconds": {
                    "type": "number",
                    "description": "Optional wait timeout. Defaults to a non-blocking check.",
                },
            },
            "required": ["subagent_run_id"],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        try:
            subagent_run_id = _required_str(call.arguments.get("subagent_run_id"), "subagent_run_id")
            timeout_seconds = _optional_float(call.arguments.get("timeout_seconds"))
            result = await self.subagent_runtime.wait_for_result(
                subagent_run_id,
                event_context=runtime_context.event_context,
                returned_to_tool_call_id=call.id,
                source_context_id=runtime_context.context_id,
                source_model_call_index=runtime_context.model_call_index,
                source_tool_name=call.name,
                timeout_seconds=timeout_seconds,
            )
        except ValueError as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        except SubagentNotReady:
            return _json_result(
                call,
                ToolResultState.SUCCESS,
                {
                    "status": "not_ready",
                    "subagent_run_id": subagent_run_id,
                    "message": "Child agent has not completed yet. Call wait_agent again later.",
                },
            )
        except (SubagentNotFound, SubagentRuntimeError) as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        return _json_result(
            call,
            ToolResultState.SUCCESS,
            {
                "status": result.status,
                "subagent_run_id": result.subagent_run_id,
                "result_id": result.result_id,
                "summary": result.summary,
                "output_preview": result.output_preview,
                "result_artifact_id": result.final_message_artifact_id,
                "artifact_ids": list(result.artifact_ids),
                "result_source": result.result_source,
                "diagnostics": [dict(item) for item in result.diagnostics],
            },
        )


@dataclass(slots=True)
class StopAgentTool:
    subagent_runtime: SubagentRuntime

    name: str = "stop_agent"
    description: str = "Cancel a running child agent runtime and record a parent graph cancellation event."
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "subagent_run_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["subagent_run_id"],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        try:
            subagent_run_id = _required_str(call.arguments.get("subagent_run_id"), "subagent_run_id")
            reason = _optional_str(call.arguments.get("reason"))
            run = await self.subagent_runtime.cancel(
                subagent_run_id,
                event_context=runtime_context.event_context,
                reason_message=reason,
            )
        except ValueError as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        except SubagentNotFound as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        return _json_result(
            call,
            ToolResultState.SUCCESS,
            {"status": run.status, "subagent_run_id": subagent_run_id},
        )


@dataclass(slots=True)
class ListAgentsTool:
    subagent_runtime: SubagentRuntime

    name: str = "list_agents"
    description: str = (
        "Return a bounded projection of child agent runs and task-board state. "
        "This is observability only; it never returns child raw transcripts."
    )
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "max_items": {
                    "type": "integer",
                    "description": "Maximum projected items to return. Default 50, maximum 100.",
                },
                "include_edges": {
                    "type": "boolean",
                    "description": "Include bounded graph edge summaries. Default false.",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        del runtime_context
        try:
            max_items = _optional_int(call.arguments.get("max_items"), default=50, minimum=1, maximum=100)
            include_edges = _optional_bool(call.arguments.get("include_edges"), default=False)
        except ValueError as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})

        graph = self.subagent_runtime.graph()
        task_run_ids = {task.current_run_id for task in graph.tasks if task.current_run_id}
        task_items = [
            {
                "item_kind": "task",
                "task_id": task.task_id,
                "subagent_run_id": task.current_run_id,
                "current_run_id": task.current_run_id,
                "child_runtime_session_id": _child_runtime_session_id_for_run(graph, task.current_run_id),
                "status": task.status,
                "pending_state": task.pending_state,
                "label": task.label,
                "task_key": task.task_key,
                "profile_id": task.profile_id,
                "display_role": task.display_role,
                "objective_preview": task.objective_preview,
                "depends_on": list(task.depends_on),
                "has_child_run": task.has_child_run,
                "run_index": task.run_index,
                "phase": task.phase,
                "result_id": task.result_id,
                "result_artifact_id": task.primary_result_artifact_id,
                "delivered": task.delivered,
                "consumed_by_wait": task.consumed_by_wait,
            }
            for task in graph.tasks
        ]
        run_items = [
            {
                "item_kind": "run",
                "task_id": None,
                "subagent_run_id": node.subagent_run_id,
                "current_run_id": node.subagent_run_id,
                "child_runtime_session_id": node.child_runtime_session_id,
                "status": node.status,
                "pending_state": None,
                "label": node.label,
                "role": node.role,
                "phase": node.phase,
                "run_index": None,
                "result_id": node.result_id,
                "result_artifact_id": node.result_artifact_id,
                "delivered": node.delivered,
                "consumed_by_wait": node.consumed_by_wait,
            }
            for node in graph.nodes
            if node.subagent_run_id not in task_run_ids
        ]
        items = [*task_items, *run_items]
        visible_items = items[:max_items]
        payload: dict[str, Any] = {
            "status": "ok",
            "parent_runtime_session_id": graph.parent_runtime_session_id,
            "items": visible_items,
            "truncated": len(items) > len(visible_items),
            "total_items": len(items),
            "diagnostics": [dict(item) for item in graph.diagnostics],
        }
        if include_edges:
            edges = graph.edges[:max_items]
            payload["edges"] = [
                {
                    "edge_id": edge.edge_id,
                    "edge_kind": edge.edge_kind,
                    "subagent_run_id": edge.subagent_run_id,
                    "source_tool_call_id": edge.source_tool_call_id,
                    "source_tool_name": edge.source_tool_name,
                    "result_id": edge.result_id,
                    "returned_to_tool_call_id": edge.returned_to_tool_call_id,
                    "created_at": _iso(edge.created_at),
                }
                for edge in edges
            ]
            payload["edges_truncated"] = len(graph.edges) > len(edges)
            payload["total_edges"] = len(graph.edges)
        return _json_result(call, ToolResultState.SUCCESS, payload)


@dataclass(slots=True)
class CreateAgentTasksTool:
    subagent_runtime: SubagentRuntime

    name: str = "create_agent_tasks"
    description: str = (
        "Create a batch of logical subagent tasks. Tasks with satisfied dependencies start immediately; "
        "tasks with unmet dependencies wait until upstream completion, and upstream failure blocks downstream tasks."
    )
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_key": {"type": "string"},
                            "label": {"type": "string"},
                            "profile": {
                                "type": "string",
                                "enum": ["research_worker", "review_worker", "verification_worker", "general_worker"],
                            },
                            "task": {"type": "string"},
                            "display_role": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["profile", "task"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["tasks"],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        batch_id = f"subagent_batch:{uuid4().hex}"
        try:
            task_specs = _parse_task_specs(call.arguments.get("tasks"))
            planned_task_ids = _planned_task_ids(task_specs)
            dependency_map = _resolve_dependency_map(
                task_specs,
                planned_task_ids=planned_task_ids,
                existing_task_ids={task.task_id for task in self.subagent_runtime.tasks},
            )
            initial_statuses = _initial_task_statuses(
                task_specs,
                planned_task_ids=planned_task_ids,
                dependency_map=dependency_map,
                existing_tasks=self.subagent_runtime.tasks,
            )
            immediate_start_count = sum(
                1 for status in initial_statuses.values()
                if status == "start"
            )
            if immediate_start_count:
                self.subagent_runtime.validate_can_start_batch(
                    runtime_context.event_context.run_id,
                    count=immediate_start_count,
                )
        except (ValueError, SubagentLimitExceeded) as exc:
            return _batch_failure_result(
                call,
                batch_id=batch_id,
                error_code="subagent_task_batch_preflight_failed",
                failed_stage="preflight",
                failed_task_keys=_task_keys_from_raw(call.arguments.get("tasks")),
                message=str(exc),
            )

        created_tasks = []
        started_runs = []
        try:
            plans = tuple(
                {
                    "objective": str(spec["task"]),
                    "task_id": planned_task_ids[_task_spec_key(spec)],
                    "profile_id": str(spec["profile"]),
                    "batch_id": batch_id,
                    "create_tool_call_id": call.id,
                    "task_key": spec.get("task_key") if isinstance(spec.get("task_key"), str) else None,
                    "label": spec.get("label") if isinstance(spec.get("label"), str) else None,
                    "display_role": spec.get("display_role") if isinstance(spec.get("display_role"), str) else None,
                    "depends_on": dependency_map[_task_spec_key(spec)],
                    "initial_status": initial_statuses[_task_spec_key(spec)],
                    "blocked_by_task_ids": _blocked_by_task_ids(
                        dependency_map[_task_spec_key(spec)],
                        planned_task_ids=planned_task_ids,
                        planned_statuses=initial_statuses,
                        existing_tasks=self.subagent_runtime.tasks,
                    ),
                }
                for spec in task_specs
            )
            created_tasks, started_runs = await self.subagent_runtime.materialize_task_batch(
                plans,
                event_context=runtime_context.event_context,
                parent_context_id=runtime_context.context_id,
                parent_model_call_index=runtime_context.model_call_index,
                spawn_initiator_id=call.id,
            )
        except Exception as exc:
            batch_tasks = tuple(
                task for task in self.subagent_runtime.tasks
                if task.batch_id == batch_id
            )
            batch_runs = tuple(
                run for run in self.subagent_runtime.runs
                if run.batch_id == batch_id
            )
            for run in batch_runs:
                if run.status in {"starting", "running", "suspended"}:
                    await self.subagent_runtime.cancel(
                        run.subagent_run_id,
                        event_context=runtime_context.event_context,
                        reason_code="subagent_task_batch_start_failed",
                        reason_message=str(exc),
                        cancelled_by="runtime",
                    )
            for task in batch_tasks:
                await self.subagent_runtime.cancel_materialized_task(
                    task.task_id,
                    event_context=runtime_context.event_context,
                    reason_code="subagent_task_batch_start_failed",
                    reason_message=str(exc),
                    cancelled_by="runtime",
                    force=True,
                )
            return _batch_failure_result(
                call,
                batch_id=batch_id,
                error_code="subagent_task_batch_start_failed",
                failed_stage="post_commit_start",
                failed_task_keys=[
                    task.task_key or task.task_id
                    for task in (batch_tasks or created_tasks)
                ],
                message=str(exc),
            )

        return _json_result(
            call,
            ToolResultState.SUCCESS,
            {
                "status": "accepted",
                "batch_id": batch_id,
                "started_count": len(started_runs),
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "task_key": task.task_key,
                        "label": task.label,
                        "profile": task.profile_id,
                        "status": self.subagent_runtime._tasks[task.task_id].status,  # noqa: SLF001 - projection.
                        "subagent_run_id": self.subagent_runtime._tasks[task.task_id].current_run_id,  # noqa: SLF001
                        "child_runtime_session_id": _child_runtime_session_id_for_run(
                            self.subagent_runtime.graph(),
                            self.subagent_runtime._tasks[task.task_id].current_run_id,  # noqa: SLF001
                        ),
                    }
                    for task in created_tasks
                ],
                "message": (
                    "Subagent task batch materialized. Runnable tasks were started; "
                    "dependency-waiting or dependency-blocked tasks are reported per item. "
                    "Use wait_agent_tasks to collect settled results."
                ),
            },
        )


@dataclass(slots=True)
class WaitAgentTasksTool:
    subagent_runtime: SubagentRuntime

    name: str = "wait_agent_tasks"
    description: str = (
        "Wait for one or more logical subagent tasks by task_id. "
        "Returns settled task results without cancelling still-running tasks on timeout."
    )
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"}},
                "settle": {"type": "string", "enum": ["all", "first"]},
                "timeout_seconds": {"type": "number"},
                "include_consumed": {"type": "boolean"},
            },
            "required": ["task_ids"],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        try:
            task_ids = _required_str_list(call.arguments.get("task_ids"), "task_ids")
            settle = _optional_str(call.arguments.get("settle")) or "all"
            timeout_seconds = _optional_float(call.arguments.get("timeout_seconds"))
            include_consumed = _optional_bool(call.arguments.get("include_consumed"), default=False)
            results = await self.subagent_runtime.wait_tasks(
                tuple(task_ids),
                event_context=runtime_context.event_context,
                consumer_tool_call_id=call.id,
                settle=settle,
                timeout_seconds=timeout_seconds,
                include_consumed=include_consumed,
            )
        except (ValueError, SubagentNotFound) as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        return _json_result(
            call,
            ToolResultState.SUCCESS,
            {
                "status": "ok",
                "settle": settle,
                "returned_count": len(results),
                "results": list(results),
            },
        )


@dataclass(slots=True)
class StopAgentTaskTool:
    subagent_runtime: SubagentRuntime

    name: str = "stop_agent_task"
    description: str = "Cancel a logical subagent task and its active child attempt, if any."
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        try:
            task_id = _required_str(call.arguments.get("task_id"), "task_id")
            reason = _optional_str(call.arguments.get("reason"))
            task = await self.subagent_runtime.cancel_task(
                task_id,
                event_context=runtime_context.event_context,
                reason_message=reason,
            )
        except (ValueError, SubagentNotFound) as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        return _json_result(
            call,
            ToolResultState.SUCCESS,
            {
                "status": task.status,
                "task_id": task.task_id,
                "subagent_run_id": task.current_run_id,
            },
        )


@dataclass(slots=True)
class ReportAgentPhaseTool:
    subagent_runtime: SubagentRuntime
    subagent_run_id: str

    name: str = "report_agent_phase"
    description: str = "Child-only tool for reporting current subagent progress without completing the run."
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "description": "Short phase label, for example investigating, implementing, verifying.",
                },
                "message": {
                    "type": "string",
                    "description": "Optional progress note for the parent task board.",
                },
                "progress": {
                    "type": "object",
                    "description": "Optional small structured progress metadata.",
                },
            },
            "required": ["phase"],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        try:
            phase = _required_str(call.arguments.get("phase"), "phase")
            message = _optional_str(call.arguments.get("message"))
            progress = _optional_dict(call.arguments.get("progress"))
            await self.subagent_runtime.report_phase(
                self.subagent_run_id,
                phase=phase,
                message=message,
                progress=progress,
                event_context=runtime_context.event_context,
                source_tool_call_id=call.id,
            )
        except ValueError as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        except SubagentNotFound as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        return _json_result(
            call,
            ToolResultState.SUCCESS,
            {
                "status": "phase_reported",
                "subagent_run_id": self.subagent_run_id,
                "phase": phase,
            },
        )


@dataclass(slots=True)
class ReportAgentResultTool:
    subagent_runtime: SubagentRuntime
    subagent_run_id: str

    name: str = "report_agent_result"
    description: str = (
        "Child-only tool for submitting the explicit final subagent result. "
        "After this tool succeeds, the child run ends at the next runtime safe point."
    )
    parameters: dict[str, Any] = None  # type: ignore[assignment]
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        self.parameters = {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Final result summary for the parent orchestrator.",
                },
                "output_preview": {
                    "type": "string",
                    "description": "Optional longer result body or evidence summary.",
                },
                "diagnostics": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional structured diagnostics.",
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        }

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        try:
            summary = _required_str(call.arguments.get("summary"), "summary")
            output_preview = _optional_str(call.arguments.get("output_preview"))
            diagnostics = _optional_diagnostics(call.arguments.get("diagnostics"))
            result = await self.subagent_runtime.submit_result(
                self.subagent_run_id,
                summary=summary,
                output_preview=output_preview,
                diagnostics=diagnostics,
                event_context=runtime_context.event_context,
                source_tool_call_id=call.id,
            )
        except ValueError as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        except (SubagentNotFound, SubagentRuntimeError) as exc:
            return _json_result(call, ToolResultState.ERROR, {"status": "error", "error": str(exc)})
        return _json_result(
            call,
            ToolResultState.SUCCESS,
            {
                "status": "result_submitted",
                "subagent_run_id": self.subagent_run_id,
                "result_id": result.result_id,
                "summary": result.summary,
                "message": "Explicit result submitted; the child run will stop at the next safe point.",
            },
        )


def _json_result(call: ToolCall, status: ToolResultState, payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=status,
        output=json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _batch_failure_result(
    call: ToolCall,
    *,
    batch_id: str,
    error_code: str,
    failed_stage: str,
    failed_task_keys: list[str],
    message: str,
) -> ToolExecutionResult:
    return _json_result(
        call,
        ToolResultState.ERROR,
        {
            "status": "error",
            "batch_id": batch_id,
            "error_code": error_code,
            "failed_stage": failed_stage,
            "failed_task_keys": failed_task_keys,
            "diagnostics": [{"message": message}],
        },
    )


def _parse_task_specs(value: Any) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise ValueError("tasks must be a non-empty array")
    seen_keys: set[str] = set()
    specs: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("each task must be an object")
        task = _required_str(item.get("task"), f"tasks[{index}].task")
        profile = _required_str(item.get("profile"), f"tasks[{index}].profile")
        _require_member(profile, _ALLOWED_TASK_PROFILES, f"tasks[{index}].profile")
        task_key = _optional_str(item.get("task_key"))
        if task_key is not None:
            if task_key.startswith("task:"):
                raise ValueError("task_key must not use the reserved task: prefix")
            if task_key in seen_keys:
                raise ValueError(f"duplicate task_key: {task_key}")
            seen_keys.add(task_key)
        depends_on = item.get("depends_on") or []
        if not isinstance(depends_on, list) or any(not isinstance(dep, str) or not dep.strip() for dep in depends_on):
            raise ValueError("depends_on must be an array of strings")
        specs.append(
            {
                "task": task,
                "profile": profile,
                "task_key": task_key,
                "label": _optional_str(item.get("label")),
                "display_role": _optional_str(item.get("display_role")),
                "depends_on": tuple(dep.strip() for dep in depends_on),
            }
        )
    return specs


def _task_spec_key(spec: dict[str, object]) -> str:
    task_key = spec.get("task_key")
    if isinstance(task_key, str) and task_key:
        return task_key
    return str(spec["__index"])


def _planned_task_ids(task_specs: list[dict[str, object]]) -> dict[str, str]:
    planned: dict[str, str] = {}
    for index, spec in enumerate(task_specs):
        spec["__index"] = index
        planned[_task_spec_key(spec)] = f"subagent_task:{uuid4().hex}"
    return planned


def _resolve_dependency_map(
    task_specs: list[dict[str, object]],
    *,
    planned_task_ids: dict[str, str],
    existing_task_ids: set[str],
) -> dict[str, tuple[str, ...]]:
    local_keys = {_task_spec_key(spec) for spec in task_specs if isinstance(spec.get("task_key"), str)}
    local_edges: dict[str, set[str]] = {_task_spec_key(spec): set() for spec in task_specs}
    dependency_map: dict[str, tuple[str, ...]] = {}
    for spec in task_specs:
        key = _task_spec_key(spec)
        resolved: list[str] = []
        for dependency_ref in spec.get("depends_on", ()):
            assert isinstance(dependency_ref, str)
            if dependency_ref.startswith("task:"):
                dependency_task_id = dependency_ref.removeprefix("task:")
                if dependency_task_id not in existing_task_ids:
                    raise ValueError(f"unknown dependency task_id: {dependency_task_id}")
                resolved.append(dependency_task_id)
                continue
            if dependency_ref not in local_keys:
                raise ValueError(f"unknown dependency task_key: {dependency_ref}")
            if dependency_ref == key:
                raise ValueError(f"task cannot depend on itself: {dependency_ref}")
            local_edges[key].add(dependency_ref)
            resolved.append(planned_task_ids[dependency_ref])
        dependency_map[key] = tuple(resolved)
    _reject_dependency_cycles(local_edges)
    return dependency_map


def _reject_dependency_cycles(edges: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError("dependency cycle detected")
        visiting.add(node)
        for dependency in edges.get(node, set()):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


def _dependencies_already_satisfied(dependency_ids: tuple[str, ...], tasks: tuple[Any, ...]) -> bool:
    task_by_id = {task.task_id: task for task in tasks}
    return all(task_by_id.get(dependency_id) is not None and task_by_id[dependency_id].status == "completed" for dependency_id in dependency_ids)


def _initial_task_statuses(
    task_specs: list[dict[str, object]],
    *,
    planned_task_ids: dict[str, str],
    dependency_map: dict[str, tuple[str, ...]],
    existing_tasks: tuple[Any, ...],
) -> dict[str, str]:
    task_by_id = {task.task_id: task for task in existing_tasks}
    planned_key_by_id = {task_id: key for key, task_id in planned_task_ids.items()}
    statuses: dict[str, str] = {}
    for spec in task_specs:
        key = _task_spec_key(spec)
        dependencies = dependency_map[key]
        if any(
            dependency_id in task_by_id
            and task_by_id[dependency_id].status in {"failed", "cancelled", "blocked_dependency_failed"}
            for dependency_id in dependencies
        ):
            statuses[key] = "blocked_dependency_failed"
        else:
            statuses[key] = "pending"

    changed = True
    while changed:
        changed = False
        for spec in task_specs:
            key = _task_spec_key(spec)
            if statuses[key] == "blocked_dependency_failed":
                continue
            if any(
                dependency_id in planned_key_by_id
                and statuses[planned_key_by_id[dependency_id]] == "blocked_dependency_failed"
                for dependency_id in dependency_map[key]
            ):
                statuses[key] = "blocked_dependency_failed"
                changed = True

    for spec in task_specs:
        key = _task_spec_key(spec)
        if statuses[key] == "blocked_dependency_failed":
            continue
        statuses[key] = (
            "start"
            if _dependencies_already_satisfied(dependency_map[key], existing_tasks)
            else "waiting_dependency"
        )
    return statuses


def _blocked_by_task_ids(
    dependency_ids: tuple[str, ...],
    *,
    planned_task_ids: dict[str, str],
    planned_statuses: dict[str, str],
    existing_tasks: tuple[Any, ...],
) -> tuple[str, ...]:
    task_by_id = {task.task_id: task for task in existing_tasks}
    planned_key_by_id = {task_id: key for key, task_id in planned_task_ids.items()}
    blocked: list[str] = []
    for dependency_id in dependency_ids:
        existing = task_by_id.get(dependency_id)
        if existing is not None and existing.status in {"failed", "cancelled", "blocked_dependency_failed"}:
            blocked.append(dependency_id)
            continue
        planned_key = planned_key_by_id.get(dependency_id)
        if planned_key is not None and planned_statuses.get(planned_key) == "blocked_dependency_failed":
            blocked.append(dependency_id)
    return tuple(blocked)


def _task_keys_from_raw(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    keys: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, dict) and isinstance(item.get("task_key"), str):
            keys.append(item["task_key"])
        else:
            keys.append(str(index))
    return keys


def _required_str_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty array")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name}[{index}] must be a string")
        result.append(item.strip())
    return result


def _required_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected string")
    stripped = value.strip()
    return stripped or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be a number") from exc


def _optional_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("max_items must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_items must be an integer") from exc
    return max(minimum, min(maximum, parsed))


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("include_edges must be a boolean")
    return value


def _optional_dict(value: Any) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("progress must be an object")
    return dict(value)


def _optional_diagnostics(value: Any) -> tuple[dict[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("diagnostics must be an array")
    diagnostics: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("diagnostics entries must be objects")
        diagnostics.append(dict(item))
    return tuple(diagnostics)


def _require_member(value: str, allowed: frozenset[str], name: str) -> None:
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {allowed_values}")


def _child_runtime_session_id_for_run(graph: Any, subagent_run_id: str | None) -> str | None:
    if subagent_run_id is None:
        return None
    for node in graph.nodes:
        if node.subagent_run_id == subagent_run_id:
            return node.child_runtime_session_id
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
