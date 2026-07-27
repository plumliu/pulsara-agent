"""Runtime-owned implementation of the closed subagent tool command port."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Mapping, cast
from uuid import uuid4

from pulsara_agent.ports.subagent import (
    CreateAgentTaskSpec,
    CreateAgentTasksCommand,
    ListAgentsCommand,
    ReportAgentPhaseCommand,
    ReportAgentResultCommand,
    SpawnAgentCommand,
    StopAgentCommand,
    StopAgentTaskCommand,
    SubagentCollectedResultView,
    SubagentGraphEdgeView,
    SubagentInventoryOutcome,
    SubagentPhaseReportedOutcome,
    SubagentProjectionItemView,
    SubagentResultSubmittedOutcome,
    SubagentRunProjectionView,
    SubagentRunStoppedOutcome,
    SubagentRunTerminalWithoutResultOutcome,
    SubagentSpawnedOutcome,
    SubagentTaskBatchAcceptedOutcome,
    SubagentTaskProjectionView,
    SubagentTaskStartedView,
    SubagentTaskStoppedOutcome,
    SubagentTaskWaitResultView,
    SubagentTasksWaitedOutcome,
    SubagentToolCommand,
    SubagentToolNotReadyOutcome,
    SubagentToolOutcome,
    SubagentToolRejectCode,
    SubagentToolRejectedOutcome,
    SubagentWaitCompletedOutcome,
    WaitAgentCommand,
    WaitAgentTasksCommand,
    process_local_subagent_fingerprint,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
)
from pulsara_agent.primitives.subagent import SubagentContextPolicy
from pulsara_agent.runtime.subagent.runtime import (
    SubagentLimitExceeded,
    SubagentNotFound,
    SubagentNotReady,
    SubagentRuntime,
    SubagentRuntimeError,
)


@dataclass(slots=True)
class RuntimeSubagentControlPort:
    subagent_runtime: SubagentRuntime

    async def execute(self, command: SubagentToolCommand) -> SubagentToolOutcome:
        try:
            if isinstance(command, SpawnAgentCommand):
                return await self._spawn(command)
            if isinstance(command, WaitAgentCommand):
                return await self._wait(command)
            if isinstance(command, StopAgentCommand):
                return await self._stop(command)
            if isinstance(command, ListAgentsCommand):
                return self._list(command)
            if isinstance(command, CreateAgentTasksCommand):
                return await self._create_tasks(command)
            if isinstance(command, WaitAgentTasksCommand):
                return await self._wait_tasks(command)
            if isinstance(command, StopAgentTaskCommand):
                return await self._stop_task(command)
            if isinstance(command, ReportAgentPhaseCommand):
                return await self._report_phase(command)
            if isinstance(command, ReportAgentResultCommand):
                return await self._report_result(command)
        except SubagentNotFound as exc:
            return _rejected(
                command,
                code=SubagentToolRejectCode.NOT_FOUND,
                message=str(exc),
            )
        except SubagentLimitExceeded as exc:
            return _rejected(
                command,
                code=SubagentToolRejectCode.LIMIT_EXCEEDED,
                message=str(exc),
            )
        except (SubagentRuntimeError, ValueError) as exc:
            return _rejected(
                command,
                code=(
                    SubagentToolRejectCode.CONTRACT_MISMATCH
                    if isinstance(exc, ValueError)
                    else SubagentToolRejectCode.INVALID_TRANSITION
                ),
                message=str(exc),
            )
        raise AssertionError(type(command))

    async def _spawn(self, command: SpawnAgentCommand) -> SubagentSpawnedOutcome:
        run = await self.subagent_runtime.spawn_agent(
            task=command.task,
            label=command.label,
            role=command.role,
            context_policy=SubagentContextPolicy(mode=command.context_mode),
            event_context=command.owner.event_context,
            parent_context_id=command.owner.parent_context_id,
            parent_model_call_index=command.owner.parent_model_call_index,
            spawning_tool_name=command.owner.tool_name,
            spawn_initiator_kind="tool_call",
            spawn_initiator_id=command.owner.tool_call_id,
        )
        payload = {
            "outcome_kind": "spawned",
            "subagent_run_id": run.subagent_run_id,
            "child_runtime_session_id": run.child_runtime_session_id,
            "label": run.label,
            "role": run.role,
            "context_mode": run.context_policy.mode,
        }
        return SubagentSpawnedOutcome(
            **payload,
            outcome_fingerprint=_outcome_fingerprint("spawned", payload),
        )

    async def _wait(self, command: WaitAgentCommand) -> SubagentToolOutcome:
        try:
            result = await self.subagent_runtime.wait_for_result(
                command.subagent_run_id,
                event_context=command.owner.event_context,
                returned_to_tool_call_id=command.owner.tool_call_id,
                source_context_id=command.owner.parent_context_id,
                source_model_call_index=command.owner.parent_model_call_index,
                source_tool_name=command.owner.tool_name,
                timeout_seconds=command.timeout_seconds,
            )
        except SubagentNotReady:
            terminal = self.subagent_runtime.terminal_outcome_for_run(
                command.subagent_run_id
            )
            if terminal is not None:
                payload = {
                    "outcome_kind": "terminal_without_result",
                    "subagent_run_id": terminal.subagent_run_id,
                    "task_id": terminal.task_id,
                    "status": terminal.status,
                    "reason_code": terminal.reason_code,
                    "terminal_event_id": terminal.terminal_event_id,
                }
                return SubagentRunTerminalWithoutResultOutcome(
                    **payload,
                    outcome_fingerprint=_outcome_fingerprint(
                        "terminal-without-result", payload
                    ),
                )
            payload = {
                "outcome_kind": "not_ready",
                "command_kind": "wait_agent",
                "subagent_run_ids": (command.subagent_run_id,),
            }
            return SubagentToolNotReadyOutcome(
                **payload,
                outcome_fingerprint=_outcome_fingerprint("not-ready", payload),
            )
        view = _collected_result_view(result)
        payload = {
            "outcome_kind": "wait_completed",
            "result": asdict(view),
        }
        return SubagentWaitCompletedOutcome(
            outcome_kind="wait_completed",
            result=view,
            outcome_fingerprint=_outcome_fingerprint("wait-completed", payload),
        )

    async def _stop(self, command: StopAgentCommand) -> SubagentRunStoppedOutcome:
        run = await self.subagent_runtime.cancel(
            command.subagent_run_id,
            event_context=command.owner.event_context,
            reason_message=command.reason,
        )
        status = cast(str, run.status)
        if status not in {"cancelled", "completed", "failed"}:
            raise SubagentRuntimeError(
                "subagent cancellation did not reach a terminal state"
            )
        payload = {
            "outcome_kind": "run_stopped",
            "subagent_run_id": command.subagent_run_id,
            "status": status,
        }
        return SubagentRunStoppedOutcome(
            **payload,  # type: ignore[arg-type]
            outcome_fingerprint=_outcome_fingerprint("run-stopped", payload),
        )

    def _list(self, command: ListAgentsCommand) -> SubagentInventoryOutcome:
        graph = self.subagent_runtime.graph()
        task_run_ids = {
            task.current_run_id for task in graph.tasks if task.current_run_id
        }
        items: list[SubagentProjectionItemView] = [
            _task_projection_view(task, graph=graph) for task in graph.tasks
        ]
        items.extend(
            _run_projection_view(node)
            for node in graph.nodes
            if node.subagent_run_id not in task_run_ids
        )
        visible_items = tuple(items[: command.maximum_items])
        visible_edges = (
            tuple(_edge_view(edge) for edge in graph.edges[: command.maximum_items])
            if command.include_edges
            else ()
        )
        diagnostics = tuple(_freeze_object(item) for item in graph.diagnostics)
        payload = {
            "outcome_kind": "inventory",
            "parent_runtime_session_id": graph.parent_runtime_session_id,
            "items": tuple(asdict(item) for item in visible_items),
            "edges": tuple(asdict(edge) for edge in visible_edges),
            "total_items": len(items),
            "total_edges": len(graph.edges) if command.include_edges else 0,
            "items_truncated": len(items) > len(visible_items),
            "edges_truncated": (
                command.include_edges and len(graph.edges) > len(visible_edges)
            ),
            "diagnostics": diagnostics,
        }
        return SubagentInventoryOutcome(
            outcome_kind="inventory",
            parent_runtime_session_id=graph.parent_runtime_session_id,
            items=visible_items,
            edges=visible_edges,
            total_items=len(items),
            total_edges=len(graph.edges) if command.include_edges else 0,
            items_truncated=len(items) > len(visible_items),
            edges_truncated=(
                command.include_edges and len(graph.edges) > len(visible_edges)
            ),
            diagnostics=diagnostics,
            outcome_fingerprint=_outcome_fingerprint("inventory", payload),
        )

    async def _create_tasks(
        self, command: CreateAgentTasksCommand
    ) -> SubagentToolOutcome:
        batch_id = f"subagent_batch:{uuid4().hex}"
        planned_task_ids = {
            _task_spec_key(spec, index=index): f"subagent_task:{uuid4().hex}"
            for index, spec in enumerate(command.ordered_tasks)
        }
        try:
            dependency_map = _resolve_dependency_map(
                command.ordered_tasks,
                planned_task_ids=planned_task_ids,
                existing_task_ids={
                    task.task_id for task in self.subagent_runtime.tasks
                },
            )
            initial_statuses = _initial_task_statuses(
                command.ordered_tasks,
                planned_task_ids=planned_task_ids,
                dependency_map=dependency_map,
                existing_tasks=self.subagent_runtime.tasks,
            )
            immediate_start_count = sum(
                status == "start" for status in initial_statuses.values()
            )
            if immediate_start_count:
                self.subagent_runtime.validate_can_start_batch(
                    command.owner.run_id,
                    count=immediate_start_count,
                )
        except (ValueError, SubagentLimitExceeded) as exc:
            return _rejected(
                command,
                code=SubagentToolRejectCode.BATCH_PREFLIGHT_FAILED,
                message=str(exc),
                batch_id=batch_id,
                failed_stage="preflight",
                failed_task_keys=tuple(
                    spec.task_key or str(index)
                    for index, spec in enumerate(command.ordered_tasks)
                ),
                diagnostics=(_freeze_object({"message": str(exc)}),),
            )

        created_tasks = ()
        try:
            plans = tuple(
                {
                    "objective": spec.task,
                    "task_id": planned_task_ids[_task_spec_key(spec, index=index)],
                    "profile_id": spec.profile,
                    "batch_id": batch_id,
                    "create_tool_call_id": command.owner.tool_call_id,
                    "task_key": spec.task_key,
                    "label": spec.label,
                    "display_role": spec.display_role,
                    "depends_on": dependency_map[_task_spec_key(spec, index=index)],
                    "initial_status": initial_statuses[
                        _task_spec_key(spec, index=index)
                    ],
                    "blocked_by_task_ids": _blocked_by_task_ids(
                        dependency_map[_task_spec_key(spec, index=index)],
                        planned_task_ids=planned_task_ids,
                        planned_statuses=initial_statuses,
                        existing_tasks=self.subagent_runtime.tasks,
                    ),
                }
                for index, spec in enumerate(command.ordered_tasks)
            )
            (
                created_tasks,
                started_runs,
            ) = await self.subagent_runtime.materialize_task_batch(
                plans,
                event_context=command.owner.event_context,
                parent_context_id=command.owner.parent_context_id,
                parent_model_call_index=command.owner.parent_model_call_index,
                spawn_initiator_id=command.owner.tool_call_id,
            )
        except Exception as exc:
            batch_tasks = tuple(
                task
                for task in self.subagent_runtime.tasks
                if task.batch_id == batch_id
            )
            await self.subagent_runtime.repair_materialized_batch(
                batch_id,
                event_context=command.owner.event_context,
                repair_id=f"subagent_repair:{uuid4().hex}",
                reason_code="subagent_task_batch_start_failed",
                reason_message=(
                    "The materialized subagent task batch could not finish starting; "
                    "the entire batch was terminalized."
                ),
            )
            return _rejected(
                command,
                code=SubagentToolRejectCode.BATCH_START_FAILED,
                message=(
                    "The materialized subagent task batch could not finish starting."
                ),
                batch_id=batch_id,
                failed_stage="post_commit_start",
                failed_task_keys=tuple(
                    task.task_key or task.task_id
                    for task in (batch_tasks or created_tasks)
                ),
                diagnostics=(
                    _freeze_object(
                        {
                            "message": (
                                "The materialized subagent task batch could not "
                                "finish starting."
                            ),
                            "error_type": type(exc).__name__,
                        }
                    ),
                ),
            )

        graph = self.subagent_runtime.graph()
        tasks = tuple(_task_started_view(task, graph=graph) for task in created_tasks)
        payload = {
            "outcome_kind": "task_batch_accepted",
            "batch_id": batch_id,
            "started_count": len(started_runs),
            "tasks": tuple(asdict(item) for item in tasks),
        }
        return SubagentTaskBatchAcceptedOutcome(
            outcome_kind="task_batch_accepted",
            batch_id=batch_id,
            started_count=len(started_runs),
            tasks=tasks,
            outcome_fingerprint=_outcome_fingerprint("task-batch-accepted", payload),
        )

    async def _wait_tasks(
        self, command: WaitAgentTasksCommand
    ) -> SubagentTasksWaitedOutcome:
        results = await self.subagent_runtime.wait_tasks(
            command.task_ids,
            event_context=command.owner.event_context,
            consumer_tool_call_id=command.owner.tool_call_id,
            settle=command.settle,
            timeout_seconds=command.timeout_seconds,
            include_consumed=command.include_consumed,
        )
        views = tuple(_task_wait_result_view(item) for item in results)
        payload = {
            "outcome_kind": "tasks_waited",
            "settle": command.settle,
            "results": tuple(asdict(item) for item in views),
        }
        return SubagentTasksWaitedOutcome(
            outcome_kind="tasks_waited",
            settle=command.settle,
            results=views,
            outcome_fingerprint=_outcome_fingerprint("tasks-waited", payload),
        )

    async def _stop_task(
        self, command: StopAgentTaskCommand
    ) -> SubagentTaskStoppedOutcome:
        task = await self.subagent_runtime.cancel_task(
            command.task_id,
            event_context=command.owner.event_context,
            reason_message=command.reason,
        )
        payload = {
            "outcome_kind": "task_stopped",
            "task_id": task.task_id,
            "status": task.status,
            "subagent_run_id": task.current_run_id,
        }
        return SubagentTaskStoppedOutcome(
            **payload,
            outcome_fingerprint=_outcome_fingerprint("task-stopped", payload),
        )

    async def _report_phase(
        self, command: ReportAgentPhaseCommand
    ) -> SubagentPhaseReportedOutcome:
        await self.subagent_runtime.report_phase(
            command.subagent_run_id,
            phase=command.phase,
            message=command.message,
            progress=command.progress,
            event_context=command.owner.event_context,
            source_tool_call_id=command.owner.tool_call_id,
        )
        payload = {
            "outcome_kind": "phase_reported",
            "subagent_run_id": command.subagent_run_id,
            "phase": command.phase,
        }
        return SubagentPhaseReportedOutcome(
            **payload,
            outcome_fingerprint=_outcome_fingerprint("phase-reported", payload),
        )

    async def _report_result(
        self, command: ReportAgentResultCommand
    ) -> SubagentResultSubmittedOutcome:
        result = await self.subagent_runtime.submit_result(
            command.subagent_run_id,
            summary=command.summary,
            output_preview=command.output_preview,
            diagnostics=command.diagnostics,
            event_context=command.owner.event_context,
            source_tool_call_id=command.owner.tool_call_id,
        )
        payload = {
            "outcome_kind": "result_submitted",
            "subagent_run_id": command.subagent_run_id,
            "result_id": result.result_id,
            "summary": result.summary,
        }
        return SubagentResultSubmittedOutcome(
            **payload,
            outcome_fingerprint=_outcome_fingerprint("result-submitted", payload),
        )


def _rejected(
    command: SubagentToolCommand,
    *,
    code: SubagentToolRejectCode,
    message: str,
    batch_id: str | None = None,
    failed_stage: str | None = None,
    failed_task_keys: tuple[str, ...] = (),
    diagnostics: tuple[FrozenJsonObjectFact, ...] = (),
) -> SubagentToolRejectedOutcome:
    payload = {
        "outcome_kind": "rejected",
        "command_kind": command.command_kind,
        "reject_code": code.value,
        "sanitized_message": message,
        "batch_id": batch_id,
        "failed_stage": failed_stage,
        "failed_task_keys": failed_task_keys,
        "diagnostics": diagnostics,
    }
    return SubagentToolRejectedOutcome(
        outcome_kind="rejected",
        command_kind=command.command_kind,
        reject_code=code,
        sanitized_message=message,
        batch_id=batch_id,
        failed_stage=failed_stage,  # type: ignore[arg-type]
        failed_task_keys=failed_task_keys,
        diagnostics=diagnostics,
        outcome_fingerprint=_outcome_fingerprint("rejected", payload),
    )


def _collected_result_view(result) -> SubagentCollectedResultView:
    diagnostics = tuple(_freeze_object(item) for item in result.diagnostics)
    payload = {
        "subagent_run_id": result.subagent_run_id,
        "task_id": result.task_id,
        "status": result.status,
        "result_id": result.result_id,
        "summary": result.summary,
        "output_preview": result.output_preview,
        "result_artifact_id": result.final_message_artifact_id,
        "artifact_ids": result.artifact_ids,
        "result_source": result.result_source,
        "diagnostics": diagnostics,
    }
    return SubagentCollectedResultView(
        **payload,
        view_fingerprint=process_local_subagent_fingerprint(
            "subagent-collected-result-view:v1", **payload
        ),
    )


def _task_projection_view(task, *, graph) -> SubagentTaskProjectionView:
    payload = {
        "item_kind": "task",
        "task_id": task.task_id,
        "subagent_run_id": task.current_run_id,
        "child_runtime_session_id": _child_runtime_session_id(
            graph, task.current_run_id
        ),
        "status": task.status,
        "pending_state": task.pending_state,
        "label": task.label,
        "task_key": task.task_key,
        "profile_id": task.profile_id,
        "display_role": task.display_role,
        "objective_preview": task.objective_preview,
        "depends_on": task.depends_on,
        "has_child_run": task.has_child_run,
        "run_index": task.run_index,
        "phase": task.phase,
        "result_id": task.result_id,
        "result_artifact_id": task.primary_result_artifact_id,
        "delivered": task.delivered,
        "consumed_by_wait": task.consumed_by_wait,
    }
    return SubagentTaskProjectionView(
        **payload,
        item_fingerprint=process_local_subagent_fingerprint(
            "subagent-task-projection-view:v1", **payload
        ),
    )


def _run_projection_view(node) -> SubagentRunProjectionView:
    payload = {
        "item_kind": "run",
        "subagent_run_id": node.subagent_run_id,
        "child_runtime_session_id": node.child_runtime_session_id,
        "status": node.status,
        "label": node.label,
        "role": node.role,
        "phase": node.phase,
        "result_id": node.result_id,
        "result_artifact_id": node.result_artifact_id,
        "delivered": node.delivered,
        "consumed_by_wait": node.consumed_by_wait,
    }
    return SubagentRunProjectionView(
        **payload,
        item_fingerprint=process_local_subagent_fingerprint(
            "subagent-run-projection-view:v1", **payload
        ),
    )


def _edge_view(edge) -> SubagentGraphEdgeView:
    created_at = _iso(edge.created_at)
    payload = {
        "edge_id": edge.edge_id,
        "edge_kind": edge.edge_kind,
        "subagent_run_id": edge.subagent_run_id,
        "source_tool_call_id": edge.source_tool_call_id,
        "source_tool_name": edge.source_tool_name,
        "result_id": edge.result_id,
        "returned_to_tool_call_id": edge.returned_to_tool_call_id,
        "created_at": created_at,
    }
    return SubagentGraphEdgeView(
        **payload,
        edge_fingerprint=process_local_subagent_fingerprint(
            "subagent-graph-edge-view:v1", **payload
        ),
    )


def _task_started_view(task, *, graph) -> SubagentTaskStartedView:
    payload = {
        "task_id": task.task_id,
        "task_key": task.task_key,
        "label": task.label,
        "profile": task.profile_id,
        "status": task.status,
        "subagent_run_id": task.current_run_id,
        "child_runtime_session_id": _child_runtime_session_id(
            graph, task.current_run_id
        ),
    }
    return SubagentTaskStartedView(
        **payload,
        view_fingerprint=process_local_subagent_fingerprint(
            "subagent-task-started-view:v1", **payload
        ),
    )


def _task_wait_result_view(item: Mapping[str, object]) -> SubagentTaskWaitResultView:
    result_source = item.get("result_source") or "none"
    if result_source not in {"explicit", "inferred", "none"}:
        raise SubagentRuntimeError("subagent task result source is invalid")
    status = item.get("status")
    if status not in {
        "completed",
        "failed",
        "cancelled",
        "blocked_dependency_failed",
    }:
        raise SubagentRuntimeError("subagent task wait status is invalid")
    artifact_ids = item.get("artifact_ids") or ()
    if not isinstance(artifact_ids, (tuple, list)):
        raise SubagentRuntimeError("subagent task artifact IDs are invalid")
    payload = {
        "task_id": str(item.get("task_id") or ""),
        "task_key": _optional_string_value(item.get("task_key")),
        "status": status,
        "subagent_run_id": _optional_string_value(item.get("subagent_run_id")),
        "child_runtime_session_id": _optional_string_value(
            item.get("child_runtime_session_id")
        ),
        "result_id": _optional_string_value(item.get("result_id")),
        "summary": _optional_string_value(item.get("summary")),
        "output_preview": _optional_string_value(item.get("output_preview")),
        "result_artifact_id": _optional_string_value(item.get("result_artifact_id")),
        "artifact_ids": tuple(str(value) for value in artifact_ids),
        "result_source": result_source,
        "consumed": bool(item.get("consumed", False)),
    }
    if not payload["task_id"]:
        raise SubagentRuntimeError("subagent task wait result lacks task identity")
    return SubagentTaskWaitResultView(
        **payload,  # type: ignore[arg-type]
        view_fingerprint=process_local_subagent_fingerprint(
            "subagent-task-wait-result-view:v1", **payload
        ),
    )


def _task_spec_key(spec: CreateAgentTaskSpec, *, index: int) -> str:
    return spec.task_key or str(index)


def _resolve_dependency_map(
    task_specs: tuple[CreateAgentTaskSpec, ...],
    *,
    planned_task_ids: dict[str, str],
    existing_task_ids: set[str],
) -> dict[str, tuple[str, ...]]:
    local_keys = {spec.task_key for spec in task_specs if spec.task_key is not None}
    local_edges: dict[str, set[str]] = {
        _task_spec_key(spec, index=index): set()
        for index, spec in enumerate(task_specs)
    }
    dependency_map: dict[str, tuple[str, ...]] = {}
    for index, spec in enumerate(task_specs):
        key = _task_spec_key(spec, index=index)
        resolved: list[str] = []
        for dependency_ref in spec.depends_on:
            if dependency_ref.startswith("task:"):
                dependency_task_id = dependency_ref.removeprefix("task:")
                if dependency_task_id not in existing_task_ids:
                    raise ValueError(
                        f"unknown dependency task_id: {dependency_task_id}"
                    )
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


def _initial_task_statuses(
    task_specs: tuple[CreateAgentTaskSpec, ...],
    *,
    planned_task_ids: dict[str, str],
    dependency_map: dict[str, tuple[str, ...]],
    existing_tasks: tuple[object, ...],
) -> dict[str, str]:
    task_by_id = {getattr(task, "task_id"): task for task in existing_tasks}
    planned_key_by_id = {task_id: key for key, task_id in planned_task_ids.items()}
    statuses: dict[str, str] = {}
    for index, spec in enumerate(task_specs):
        key = _task_spec_key(spec, index=index)
        dependencies = dependency_map[key]
        statuses[key] = (
            "blocked_dependency_failed"
            if any(
                dependency_id in task_by_id
                and getattr(task_by_id[dependency_id], "status")
                in {"failed", "cancelled", "blocked_dependency_failed"}
                for dependency_id in dependencies
            )
            else "pending"
        )
    changed = True
    while changed:
        changed = False
        for index, spec in enumerate(task_specs):
            key = _task_spec_key(spec, index=index)
            if statuses[key] == "blocked_dependency_failed":
                continue
            if any(
                dependency_id in planned_key_by_id
                and statuses[planned_key_by_id[dependency_id]]
                == "blocked_dependency_failed"
                for dependency_id in dependency_map[key]
            ):
                statuses[key] = "blocked_dependency_failed"
                changed = True
    for index, spec in enumerate(task_specs):
        key = _task_spec_key(spec, index=index)
        if statuses[key] == "blocked_dependency_failed":
            continue
        statuses[key] = (
            "start"
            if all(
                dependency_id in task_by_id
                and getattr(task_by_id[dependency_id], "status") == "completed"
                for dependency_id in dependency_map[key]
            )
            else "waiting_dependency"
        )
    return statuses


def _blocked_by_task_ids(
    dependency_ids: tuple[str, ...],
    *,
    planned_task_ids: dict[str, str],
    planned_statuses: dict[str, str],
    existing_tasks: tuple[object, ...],
) -> tuple[str, ...]:
    task_by_id = {getattr(task, "task_id"): task for task in existing_tasks}
    planned_key_by_id = {task_id: key for key, task_id in planned_task_ids.items()}
    blocked: list[str] = []
    for dependency_id in dependency_ids:
        existing = task_by_id.get(dependency_id)
        if existing is not None and getattr(existing, "status") in {
            "failed",
            "cancelled",
            "blocked_dependency_failed",
        }:
            blocked.append(dependency_id)
            continue
        planned_key = planned_key_by_id.get(dependency_id)
        if (
            planned_key is not None
            and planned_statuses.get(planned_key) == "blocked_dependency_failed"
        ):
            blocked.append(dependency_id)
    return tuple(blocked)


def _child_runtime_session_id(graph, run_id: str | None) -> str | None:
    if run_id is None:
        return None
    for node in graph.nodes:
        if node.subagent_run_id == run_id:
            return node.child_runtime_session_id
    return None


def _freeze_object(value: Mapping[str, object]) -> FrozenJsonObjectFact:
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise TypeError("subagent diagnostic must be an object")
    return frozen


def _optional_string_value(value: object) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _outcome_fingerprint(kind: str, payload: Mapping[str, object]) -> str:
    return process_local_subagent_fingerprint(
        f"subagent-{kind}-outcome:v1", **dict(payload)
    )


__all__ = ["RuntimeSubagentControlPort"]
