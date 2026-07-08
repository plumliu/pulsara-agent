"""Pulsara-owned subagent runtime skeleton.

This module implements the PR0/PR1 substrate: typed parent graph events,
child-runtime identity creation, fake child completion, consumption edges, and
basic lifecycle/cap enforcement.  Real child ``AgentRuntime`` wiring lives above
this boundary and should call the same methods rather than writing graph facts
directly.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any, Awaitable, Mapping
from uuid import uuid4

from pulsara_agent.event import (
    EventContext,
    SubagentEdgeRecordedEvent,
    SubagentMessageSentEvent,
    SubagentPhaseReportedEvent,
    SubagentResultConsumedEvent,
    SubagentResultDeliveredEvent,
    SubagentResultSubmittedEvent,
    SubagentRunCancelledEvent,
    SubagentRunCompletedEvent,
    SubagentRunFailedEvent,
    SubagentRunStartedEvent,
    SubagentRunSuspendedEvent,
    SubagentTaskBlockedEvent,
    SubagentTaskCancelledEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskCreatedEvent,
    SubagentTaskFailedEvent,
    SubagentTaskScheduledEvent,
    SubagentTaskStartedEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.subagent.projection import (
    EventLogLocator,
    InMemoryEventLogLocator,
    project_subagent_graph,
)
from pulsara_agent.runtime.subagent.types import (
    SubagentBudget,
    SubagentCapabilityProfile,
    SubagentContextPolicy,
    SubagentGraphProjection,
    SubagentResult,
    SubagentRole,
    SubagentRun,
    SubagentStatus,
    SubagentTask,
)


_ACTIVE_STATUSES: set[SubagentStatus] = {"starting", "running", "suspended"}
_TERMINAL_STATUSES: set[SubagentStatus] = {"completed", "failed", "cancelled"}
_CHILD_REPORT_TOOL_NAMES = ("report_agent_phase", "report_agent_result")
_CHILD_REPORT_DESCRIPTOR_IDS = ("workflow:report_agent_phase", "workflow:report_agent_result")
_READ_ONLY_WORKER_TOOL_NAMES = frozenset({"read_file", "search_files", "artifact_read"})
_VERIFICATION_WORKER_TOOL_NAMES = frozenset({"read_file", "search_files", "artifact_read", "terminal", "terminal_process"})
_WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file"})
_TERMINAL_TOOL_NAMES = frozenset({"terminal", "terminal_process"})


class SubagentRuntimeError(RuntimeError):
    """Base error for subagent runtime contract violations."""


class SubagentLimitExceeded(SubagentRuntimeError):
    """Raised when a spawn would exceed the runtime hard caps."""


class SubagentNotFound(SubagentRuntimeError):
    """Raised when a subagent id is unknown to this runtime."""


class SubagentNotReady(SubagentRuntimeError):
    """Raised when a result is requested before it is available."""


ChildEventLogFactory = Callable[[str], EventLog]
SubagentChildRunner = Callable[["SubagentRuntime", SubagentRun], Awaitable[None]]


class SubagentRuntime:
    def __init__(
        self,
        *,
        parent_runtime_session: RuntimeSession,
        child_event_log_factory: ChildEventLogFactory,
        event_log_locator: EventLogLocator | None = None,
        default_budget: SubagentBudget | None = None,
        child_runner: SubagentChildRunner | None = None,
    ) -> None:
        self.parent_runtime_session = parent_runtime_session
        self._child_event_log_factory = child_event_log_factory
        self.event_log_locator = event_log_locator or InMemoryEventLogLocator()
        self.default_budget = default_budget or SubagentBudget()
        self._child_runner = child_runner
        self._runs: dict[str, SubagentRun] = {}
        self._tasks: dict[str, SubagentTask] = {}
        self._results: dict[str, SubagentResult] = {}
        self._submitted_results: dict[str, SubagentResult] = {}
        self._child_sessions: dict[str, RuntimeSession] = {}
        self._child_tasks: dict[str, asyncio.Task[None]] = {}
        self._consumed_result_ids: set[str] = set()
        self._consumed_task_ids: set[str] = set()
        self._delivered_result_ids: set[str] = set()
        self._parent_capability_snapshot: SubagentCapabilityProfile | None = None
        self._bootstrap_from_parent_event_log()

    @property
    def runs(self) -> tuple[SubagentRun, ...]:
        return tuple(self._runs.values())

    @property
    def tasks(self) -> tuple[SubagentTask, ...]:
        return tuple(self._tasks.values())

    @property
    def child_sessions(self) -> tuple[RuntimeSession, ...]:
        return tuple(self._child_sessions.values())

    def refresh_parent_capability_snapshot(
        self,
        *,
        exposure: Any,
        permission_mode: str | None,
        permission_policy: Mapping[str, object],
    ) -> None:
        """Freeze the current parent exposure subset used by future child spawns."""

        allowed_tool_names: list[str] = []
        allowed_descriptor_ids: list[str] = []
        allowed_mcp_server_ids: list[str] = []
        mcp_tool_names: list[str] = []
        descriptors_by_name = getattr(exposure, "descriptors_by_name", {})
        callable_names = getattr(exposure, "callable_names", frozenset())
        for name in sorted(callable_names):
            descriptor = descriptors_by_name.get(name)
            if descriptor is None or not _descriptor_allowed_for_child(descriptor):
                continue
            allowed_tool_names.append(name)
            allowed_descriptor_ids.append(str(getattr(descriptor, "id", name)))
            metadata = getattr(descriptor, "metadata", {}) or {}
            provider_kind = getattr(getattr(descriptor, "provider_kind", None), "value", None)
            if provider_kind == "mcp" and isinstance(metadata, dict) and isinstance(metadata.get("server_id"), str):
                allowed_mcp_server_ids.append(metadata["server_id"])
                mcp_tool_names.append(name)
        for name in _CHILD_REPORT_TOOL_NAMES:
            if name not in allowed_tool_names:
                allowed_tool_names.append(name)
        for descriptor_id in _CHILD_REPORT_DESCRIPTOR_IDS:
            if descriptor_id not in allowed_descriptor_ids:
                allowed_descriptor_ids.append(descriptor_id)
        self._parent_capability_snapshot = SubagentCapabilityProfile(
            profile_id=f"subagent_capability_profile:{uuid4().hex}",
            profile_name="general_worker",
            inherited_from_parent_context_id=getattr(exposure, "context_id", None),
            permission_mode=permission_mode,
            permission_policy=dict(permission_policy),
            allowed_tool_names=tuple(allowed_tool_names),
            allowed_descriptor_ids=tuple(allowed_descriptor_ids),
            allowed_mcp_server_ids=tuple(dict.fromkeys(allowed_mcp_server_ids)),
            can_spawn_subagents=False,
            memory_enabled=False,
            computed_from_parent_exposure_generation=getattr(exposure, "registry_generation", None),
            diagnostics=(
                {
                    "code": "subagent_parent_snapshot_mcp_tools",
                    "tool_names": list(dict.fromkeys(mcp_tool_names)),
                },
            ) if mcp_tool_names else (),
        )

    async def create_task(
        self,
        *,
        objective: str,
        event_context: EventContext,
        task_id: str | None = None,
        profile_id: str = "general_worker",
        batch_id: str | None = None,
        create_tool_call_id: str | None = None,
        task_key: str | None = None,
        label: str | None = None,
        display_role: str | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> SubagentTask:
        if not objective.strip():
            raise ValueError("objective is required")
        task_id = task_id or f"subagent_task:{uuid4().hex}"
        if task_id in self._tasks:
            raise SubagentRuntimeError(f"Task already exists: {task_id}")
        objective_preview = _clip(objective, 500)
        objective_artifact_id = f"{task_id}:objective"
        self.parent_runtime_session.archive.put_text(
            objective_artifact_id,
            objective,
            session_id=self.parent_runtime_session.runtime_session_id,
            run_id=event_context.run_id,
            media_type="text/markdown",
            metadata={
                "artifact_kind": "subagent_task_objective",
                "task_id": task_id,
                "batch_id": batch_id,
            },
        )
        stored = await self.parent_runtime_session.emit(
            SubagentTaskCreatedEvent(
                **event_context.event_fields(),
                task_id=task_id,
                batch_id=batch_id,
                create_tool_call_id=create_tool_call_id,
                task_key=task_key,
                label=label,
                profile_id=profile_id,
                display_role=display_role,
                objective_preview=objective_preview,
                objective_artifact_id=objective_artifact_id,
                depends_on=list(depends_on),
            )
        )
        created_at = _parse_dt(stored.created_at)
        task = SubagentTask(
            task_id=task_id,
            batch_id=batch_id,
            create_tool_call_id=create_tool_call_id,
            task_key=task_key,
            label=label,
            profile_id=profile_id,
            display_role=display_role,
            objective=objective,
            objective_preview=objective_preview,
            status="created",
            depends_on=depends_on,
            current_run_id=None,
            has_child_run=False,
            phase=None,
            result_id=None,
            primary_result_artifact_id=None,
            created_at=created_at,
            updated_at=created_at,
            completed_at=None,
            metadata={"objective_artifact_id": objective_artifact_id},
        )
        self._tasks[task_id] = task
        return task

    async def block_task(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        status: str,
        blocked_reason: str,
        blocked_by_task_ids: tuple[str, ...],
        dependency_terminal_event_ids: Mapping[str, str] | None = None,
    ) -> SubagentTask:
        task = self._require_task(task_id)
        if status not in {"waiting_dependency", "blocked_dependency_failed"}:
            raise ValueError("status must be waiting_dependency or blocked_dependency_failed")
        terminal_event_ids = self._dependency_terminal_event_ids(
            blocked_by_task_ids,
            overrides=dependency_terminal_event_ids,
        )
        stored = await self.parent_runtime_session.emit(
            SubagentTaskBlockedEvent(
                **event_context.event_fields(),
                task_id=task_id,
                status=status,  # type: ignore[arg-type]
                blocked_reason=blocked_reason,  # type: ignore[arg-type]
                blocked_by_task_ids=list(blocked_by_task_ids),
                dependency_status_snapshot={
                    dependency_id: self._tasks[dependency_id].status
                    for dependency_id in blocked_by_task_ids
                    if dependency_id in self._tasks
                },
                dependency_terminal_event_ids=terminal_event_ids,
                dependency_generation=_dependency_generation(terminal_event_ids),
            )
        )
        metadata = dict(task.metadata)
        if status == "blocked_dependency_failed":
            metadata["terminal_event_id"] = _event_ref(stored)
        blocked = replace(task, status=status, updated_at=_parse_dt(stored.created_at), metadata=metadata)  # type: ignore[arg-type]
        self._tasks[task_id] = blocked
        if status == "blocked_dependency_failed":
            await self._block_dependents_after_dependency_failed(
                task_id,
                event_context=event_context,
                dependency_terminal_event_ids={task_id: _event_ref(stored)},
            )
        return blocked

    async def materialize_task_batch(
        self,
        plans: tuple[Mapping[str, object], ...],
        *,
        event_context: EventContext,
        parent_context_id: str | None = None,
        parent_model_call_index: int | None = None,
        spawn_initiator_id: str | None = None,
    ) -> tuple[tuple[SubagentTask, ...], tuple[SubagentRun, ...]]:
        """Atomically write task batch facts, then start runnable child attempts."""

        if not plans:
            raise ValueError("plans must be non-empty")

        events: list[Any] = []
        labels: list[tuple[str, str]] = []
        task_infos: list[dict[str, Any]] = []
        run_infos: list[dict[str, Any]] = []
        planned_status_by_task_id = {
            str(plan["task_id"]): str(plan.get("initial_status") or "start")
            for plan in plans
        }
        planned_block_terminal_refs = {
            str(plan["task_id"]): f"subagent_task_terminal:{plan['task_id']}:blocked_dependency_failed"
            for plan in plans
            if str(plan.get("initial_status") or "") == "blocked_dependency_failed"
        }

        for plan in plans:
            objective = str(plan["objective"])
            if not objective.strip():
                raise ValueError("objective is required")
            task_id = str(plan["task_id"])
            if task_id in self._tasks:
                raise SubagentRuntimeError(f"Task already exists: {task_id}")
            profile_id = str(plan.get("profile_id") or "general_worker")
            batch_id = _optional_str_value(plan.get("batch_id"))
            create_tool_call_id = _optional_str_value(plan.get("create_tool_call_id"))
            task_key = _optional_str_value(plan.get("task_key"))
            label = _optional_str_value(plan.get("label"))
            display_role = _optional_str_value(plan.get("display_role"))
            depends_on = _str_tuple(plan.get("depends_on"))
            initial_status = str(plan.get("initial_status") or "start")
            blocked_by_task_ids = _str_tuple(plan.get("blocked_by_task_ids"))

            objective_preview = _clip(objective, 500)
            objective_artifact_id = f"{task_id}:objective"
            self.parent_runtime_session.archive.put_text(
                objective_artifact_id,
                objective,
                session_id=self.parent_runtime_session.runtime_session_id,
                run_id=event_context.run_id,
                media_type="text/markdown",
                metadata={
                    "artifact_kind": "subagent_task_objective",
                    "task_id": task_id,
                    "batch_id": batch_id,
                },
            )
            task_infos.append(
                {
                    "task_id": task_id,
                    "batch_id": batch_id,
                    "create_tool_call_id": create_tool_call_id,
                    "task_key": task_key,
                    "label": label,
                    "profile_id": profile_id,
                    "display_role": display_role,
                    "objective": objective,
                    "objective_preview": objective_preview,
                    "objective_artifact_id": objective_artifact_id,
                    "depends_on": depends_on,
                    "initial_status": initial_status,
                }
            )
            events.append(
                SubagentTaskCreatedEvent(
                    **event_context.event_fields(),
                    task_id=task_id,
                    batch_id=batch_id,
                    create_tool_call_id=create_tool_call_id,
                    task_key=task_key,
                    label=label,
                    profile_id=profile_id,
                    display_role=display_role,
                    objective_preview=objective_preview,
                    objective_artifact_id=objective_artifact_id,
                    depends_on=list(depends_on),
                )
            )
            labels.append(("task_created", task_id))

            if initial_status in {"waiting_dependency", "blocked_dependency_failed"}:
                planned_overrides = {
                    dependency_id: planned_block_terminal_refs[dependency_id]
                    for dependency_id in blocked_by_task_ids
                    if dependency_id in planned_block_terminal_refs
                }
                terminal_event_ids = (
                    self._dependency_terminal_event_ids(
                        blocked_by_task_ids,
                        overrides=planned_overrides,
                    )
                    if initial_status == "blocked_dependency_failed"
                    else {}
                )
                dependency_status_snapshot = self._dependency_status_snapshot(
                    blocked_by_task_ids,
                    planned_status_by_task_id=planned_status_by_task_id,
                )
                events.append(
                    SubagentTaskBlockedEvent(
                        **event_context.event_fields(),
                        task_id=task_id,
                        status=initial_status,  # type: ignore[arg-type]
                        blocked_reason=(
                            "dependency_failed"
                            if initial_status == "blocked_dependency_failed"
                            else "waiting_dependency"
                        ),
                        blocked_by_task_ids=list(blocked_by_task_ids),
                        dependency_status_snapshot=dependency_status_snapshot,
                        dependency_terminal_event_ids=terminal_event_ids,
                        dependency_generation=_dependency_generation(terminal_event_ids),
                    )
                )
                labels.append(("task_blocked", task_id))
                continue

            capability_profile = self._capability_profile_for_name(profile_id)
            context_policy = SubagentContextPolicy()
            budget = self.default_budget
            subagent_run_id = f"subagent_run:{uuid4().hex}"
            child_runtime_session_id = f"runtime:subagent:{uuid4().hex}"
            edge_id = f"subagent_edge:{subagent_run_id}:spawn"
            task_artifact_id = f"{subagent_run_id}:task"
            self.parent_runtime_session.archive.put_text(
                task_artifact_id,
                objective,
                session_id=self.parent_runtime_session.runtime_session_id,
                run_id=event_context.run_id,
                media_type="text/markdown",
                metadata={
                    "artifact_kind": "subagent_task",
                    "subagent_run_id": subagent_run_id,
                    "child_runtime_session_id": child_runtime_session_id,
                },
            )
            run_infos.append(
                {
                    "task_id": task_id,
                    "subagent_run_id": subagent_run_id,
                    "child_runtime_session_id": child_runtime_session_id,
                    "edge_id": edge_id,
                    "task_artifact_id": task_artifact_id,
                    "task_preview": objective_preview,
                    "objective": objective,
                    "label": label,
                    "capability_profile": capability_profile,
                    "context_policy": context_policy,
                    "budget": budget,
                    "profile_id": profile_id,
                    "batch_id": batch_id,
                    "create_tool_call_id": create_tool_call_id,
                    "spawn_initiator_id": spawn_initiator_id or create_tool_call_id or task_id,
                    "parent_context_id": parent_context_id,
                    "parent_model_call_index": parent_model_call_index,
                }
            )
            events.append(
                SubagentTaskScheduledEvent(
                    **event_context.event_fields(),
                    task_id=task_id,
                    batch_id=batch_id,
                    create_tool_call_id=create_tool_call_id,
                    schedule_reason="immediate",
                )
            )
            labels.append(("task_scheduled", task_id))
            events.append(
                SubagentRunStartedEvent(
                    **event_context.event_fields(),
                    subagent_run_id=subagent_run_id,
                    task_id=task_id,
                    batch_id=batch_id,
                    create_tool_call_id=create_tool_call_id,
                    run_index=1,
                    edge_id=edge_id,
                    parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
                    parent_run_id=event_context.run_id,
                    parent_turn_id=event_context.turn_id,
                    parent_reply_id=event_context.reply_id,
                    parent_context_id=parent_context_id,
                    parent_model_call_index=parent_model_call_index,
                    spawning_tool_call_id=spawn_initiator_id,
                    spawning_tool_name="create_agent_tasks",
                    spawn_initiator_kind="tool_call",
                    spawn_initiator_id=spawn_initiator_id or create_tool_call_id or task_id,
                    child_runtime_session_id=child_runtime_session_id,
                    label=label,
                    role="worker",
                    profile_id=profile_id,
                    task_preview=objective_preview,
                    context_policy=context_policy.to_event_value(),
                    capability_profile=capability_profile.to_event_value(),
                )
            )
            labels.append(("run_started", subagent_run_id))
            events.append(
                SubagentMessageSentEvent(
                    **event_context.event_fields(),
                    edge_id=edge_id,
                    subagent_run_id=subagent_run_id,
                    parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
                    parent_run_id=event_context.run_id,
                    child_runtime_session_id=child_runtime_session_id,
                    message_artifact_id=task_artifact_id,
                    message_preview=objective_preview,
                    delivery_kind="spawn_task",
                )
            )
            labels.append(("run_message", subagent_run_id))
            events.append(
                SubagentTaskStartedEvent(
                    **event_context.event_fields(),
                    task_id=task_id,
                    subagent_run_id=subagent_run_id,
                    batch_id=batch_id,
                    create_tool_call_id=create_tool_call_id,
                    run_index=1,
                    spawn_initiator_kind="tool_call",
                    spawn_initiator_id=spawn_initiator_id or create_tool_call_id or task_id,
                )
            )
            labels.append(("task_started", task_id))

        stored_events = await self.parent_runtime_session.emit_many(events)
        stored_by_label = {
            label: stored
            for label, stored in zip(labels, stored_events, strict=True)
        }

        for info in task_infos:
            task_id = str(info["task_id"])
            created_event = stored_by_label[("task_created", task_id)]
            created_at = _parse_dt(created_event.created_at)
            status = str(info["initial_status"])
            updated_at = created_at
            completed_at = None
            current_run_id = None
            has_child_run = False
            metadata = {"objective_artifact_id": info["objective_artifact_id"]}
            if status in {"waiting_dependency", "blocked_dependency_failed"}:
                blocked_event = stored_by_label[("task_blocked", task_id)]
                updated_at = _parse_dt(blocked_event.created_at)
                if status == "blocked_dependency_failed":
                    completed_at = updated_at
                    metadata["terminal_event_id"] = planned_block_terminal_refs.get(
                        task_id,
                        _event_ref(blocked_event),
                    )
            elif status == "start":
                status = "running"
                started_event = stored_by_label[("task_started", task_id)]
                updated_at = _parse_dt(started_event.created_at)
                run_info = next(item for item in run_infos if item["task_id"] == task_id)
                current_run_id = str(run_info["subagent_run_id"])
                has_child_run = True
            self._tasks[task_id] = SubagentTask(
                task_id=task_id,
                batch_id=info["batch_id"],  # type: ignore[arg-type]
                create_tool_call_id=info["create_tool_call_id"],  # type: ignore[arg-type]
                task_key=info["task_key"],  # type: ignore[arg-type]
                label=info["label"],  # type: ignore[arg-type]
                profile_id=str(info["profile_id"]),
                display_role=info["display_role"],  # type: ignore[arg-type]
                objective=str(info["objective"]),
                objective_preview=str(info["objective_preview"]),
                status=status,  # type: ignore[arg-type]
                depends_on=info["depends_on"],  # type: ignore[arg-type]
                current_run_id=current_run_id,
                has_child_run=has_child_run,
                phase=None,
                result_id=None,
                primary_result_artifact_id=None,
                created_at=created_at,
                updated_at=updated_at,
                completed_at=completed_at,
                metadata=metadata,
            )

        runs: list[SubagentRun] = []
        for info in run_infos:
            subagent_run_id = str(info["subagent_run_id"])
            child_runtime_session_id = str(info["child_runtime_session_id"])
            child_runtime = self._create_child_runtime_session(
                child_runtime_session_id=child_runtime_session_id,
                subagent_run_id=subagent_run_id,
                parent_run_id=event_context.run_id,
                capability_profile_id=info["capability_profile"].profile_id,
            )
            started_event = stored_by_label[("run_started", subagent_run_id)]
            created_at = _parse_dt(started_event.created_at)
            run = SubagentRun(
                subagent_run_id=subagent_run_id,
                parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
                parent_run_id=event_context.run_id,
                parent_turn_id=event_context.turn_id,
                parent_reply_id=event_context.reply_id,
                parent_context_id=parent_context_id,
                parent_model_call_index=parent_model_call_index,
                spawning_tool_call_id=spawn_initiator_id,
                spawning_tool_name="create_agent_tasks",
                child_runtime_session_id=child_runtime_session_id,
                child_run_id=None,
                label=info["label"],  # type: ignore[arg-type]
                role="worker",
                status="running",
                task=str(info["objective"]),
                created_at=created_at,
                updated_at=created_at,
                context_policy=info["context_policy"],  # type: ignore[arg-type]
                capability_profile=info["capability_profile"],  # type: ignore[arg-type]
                budget=info["budget"],  # type: ignore[arg-type]
                task_id=info["task_id"],  # type: ignore[arg-type]
                batch_id=info["batch_id"],  # type: ignore[arg-type]
                create_tool_call_id=info["create_tool_call_id"],  # type: ignore[arg-type]
                run_index=1,
                spawn_initiator_kind="tool_call",
                spawn_initiator_id=str(info["spawn_initiator_id"]),
                profile_id=str(info["profile_id"]),
                metadata={},
            )
            self._runs[subagent_run_id] = run
            child_runtime.subagent_runtime = self
            self._child_sessions[child_runtime_session_id] = child_runtime
            runs.append(run)

        if self._child_runner is not None:
            for run in runs:
                self._child_tasks[run.subagent_run_id] = asyncio.create_task(self._run_child(run))

        return tuple(self._tasks[str(info["task_id"])] for info in task_infos), tuple(runs)

    async def start_task(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        parent_context_id: str | None = None,
        parent_model_call_index: int | None = None,
        spawn_initiator_kind: str = "tool_call",
        spawn_initiator_id: str | None = None,
    ) -> SubagentRun:
        task = self._require_task(task_id)
        if task.has_child_run:
            raise SubagentRuntimeError(f"Task already has a child run: {task_id}")
        if task.status not in {"created", "waiting_dependency"}:
            raise SubagentRuntimeError(f"Task cannot be started from status {task.status}: {task_id}")
        await self.parent_runtime_session.emit(
            SubagentTaskScheduledEvent(
                **event_context.event_fields(),
                task_id=task_id,
                batch_id=task.batch_id,
                create_tool_call_id=task.create_tool_call_id,
                schedule_reason=(
                    "dependency_satisfied"
                    if spawn_initiator_kind == "dependency_satisfied"
                    else "immediate"
                ),
            )
        )
        capability_profile = self._capability_profile_for_name(task.profile_id)
        run = await self.spawn_agent(
            task=task.objective,
            event_context=event_context,
            label=task.label,
            role="worker",
            capability_profile=capability_profile,
            parent_context_id=parent_context_id,
            parent_model_call_index=parent_model_call_index,
            spawning_tool_call_id=(
                spawn_initiator_id if spawn_initiator_kind == "tool_call" else None
            ),
            spawning_tool_name="create_agent_tasks" if spawn_initiator_kind == "tool_call" else None,
            task_id=task.task_id,
            batch_id=task.batch_id,
            create_tool_call_id=task.create_tool_call_id,
            run_index=1,
            spawn_initiator_kind=spawn_initiator_kind,  # type: ignore[arg-type]
            spawn_initiator_id=spawn_initiator_id or task.create_tool_call_id or task.task_id,
            profile_id=task.profile_id,
        )
        stored = await self.parent_runtime_session.emit(
            SubagentTaskStartedEvent(
                **event_context.event_fields(),
                task_id=task_id,
                subagent_run_id=run.subagent_run_id,
                batch_id=task.batch_id,
                create_tool_call_id=task.create_tool_call_id,
                run_index=1,
                spawn_initiator_kind=run.spawn_initiator_kind or "tool_call",
                spawn_initiator_id=run.spawn_initiator_id or task.task_id,
            )
        )
        updated_at = _parse_dt(stored.created_at)
        self._tasks[task_id] = replace(
            task,
            status="running",
            current_run_id=run.subagent_run_id,
            has_child_run=True,
            updated_at=updated_at,
        )
        return run

    async def spawn_fake(
        self,
        *,
        task: str,
        event_context: EventContext,
        label: str | None = None,
        role: SubagentRole = "worker",
        context_policy: SubagentContextPolicy | None = None,
        capability_profile: SubagentCapabilityProfile | None = None,
        budget: SubagentBudget | None = None,
        parent_context_id: str | None = None,
        parent_model_call_index: int | None = None,
        spawning_tool_call_id: str | None = None,
        spawning_tool_name: str | None = None,
        task_id: str | None = None,
        batch_id: str | None = None,
        create_tool_call_id: str | None = None,
        run_index: int | None = None,
        spawn_initiator_kind: str | None = None,
        spawn_initiator_id: str | None = None,
        profile_id: str | None = None,
    ) -> SubagentRun:
        budget = budget or self.default_budget
        capability_profile = capability_profile or self._default_capability_profile(budget)
        context_policy = context_policy or SubagentContextPolicy()
        self._enforce_spawn_limits(event_context.run_id, budget)

        subagent_run_id = f"subagent_run:{uuid4().hex}"
        child_runtime_session_id = f"runtime:subagent:{uuid4().hex}"
        child_runtime = self._create_child_runtime_session(
            child_runtime_session_id=child_runtime_session_id,
            subagent_run_id=subagent_run_id,
            parent_run_id=event_context.run_id,
            capability_profile_id=capability_profile.profile_id,
        )
        edge_id = f"subagent_edge:{subagent_run_id}:spawn"
        task_artifact_id = f"{subagent_run_id}:task"
        self.parent_runtime_session.archive.put_text(
            task_artifact_id,
            task,
            session_id=self.parent_runtime_session.runtime_session_id,
            run_id=event_context.run_id,
            media_type="text/markdown",
            metadata={
                "artifact_kind": "subagent_task",
                "subagent_run_id": subagent_run_id,
                "child_runtime_session_id": child_runtime_session_id,
            },
        )
        task_preview = _clip(task, 500)
        started = SubagentRunStartedEvent(
            **event_context.event_fields(),
            subagent_run_id=subagent_run_id,
            task_id=task_id,
            batch_id=batch_id,
            create_tool_call_id=create_tool_call_id,
            run_index=run_index,
            edge_id=edge_id,
            parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
            parent_run_id=event_context.run_id,
            parent_turn_id=event_context.turn_id,
            parent_reply_id=event_context.reply_id,
            parent_context_id=parent_context_id,
            parent_model_call_index=parent_model_call_index,
            spawning_tool_call_id=spawning_tool_call_id,
            spawning_tool_name=spawning_tool_name,
            spawn_initiator_kind=spawn_initiator_kind,  # type: ignore[arg-type]
            spawn_initiator_id=spawn_initiator_id,
            child_runtime_session_id=child_runtime_session_id,
            label=label,
            role=role,
            profile_id=profile_id or capability_profile.profile_id,
            task_preview=task_preview,
            context_policy=context_policy.to_event_value(),
            capability_profile=capability_profile.to_event_value(),
        )
        message = SubagentMessageSentEvent(
            **event_context.event_fields(),
            edge_id=edge_id,
            subagent_run_id=subagent_run_id,
            parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
            parent_run_id=event_context.run_id,
            child_runtime_session_id=child_runtime_session_id,
            message_artifact_id=task_artifact_id,
            message_preview=task_preview,
            delivery_kind="spawn_task",
        )
        stored_started, _stored_message = await self.parent_runtime_session.emit_many([started, message])
        created_at = _parse_dt(stored_started.created_at)
        run = SubagentRun(
            subagent_run_id=subagent_run_id,
            parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
            parent_run_id=event_context.run_id,
            parent_turn_id=event_context.turn_id,
            parent_reply_id=event_context.reply_id,
            parent_context_id=parent_context_id,
            parent_model_call_index=parent_model_call_index,
            spawning_tool_call_id=spawning_tool_call_id,
            spawning_tool_name=spawning_tool_name,
            child_runtime_session_id=child_runtime_session_id,
            child_run_id=None,
            label=label,
            role=role,
            status="running",
            task=task,
            created_at=created_at,
            updated_at=created_at,
            context_policy=context_policy,
            capability_profile=capability_profile,
            budget=budget,
            task_id=task_id,
            batch_id=batch_id,
            create_tool_call_id=create_tool_call_id,
            run_index=run_index,
            spawn_initiator_kind=spawn_initiator_kind,  # type: ignore[arg-type]
            spawn_initiator_id=spawn_initiator_id,
            profile_id=profile_id or capability_profile.profile_id,
            metadata={},
        )
        self._runs[subagent_run_id] = run
        child_runtime.subagent_runtime = self
        self._child_sessions[child_runtime_session_id] = child_runtime
        return run

    async def spawn_agent(
        self,
        *,
        task: str,
        event_context: EventContext,
        label: str | None = None,
        role: SubagentRole = "worker",
        context_policy: SubagentContextPolicy | None = None,
        capability_profile: SubagentCapabilityProfile | None = None,
        budget: SubagentBudget | None = None,
        parent_context_id: str | None = None,
        parent_model_call_index: int | None = None,
        spawning_tool_call_id: str | None = None,
        spawning_tool_name: str | None = None,
        task_id: str | None = None,
        batch_id: str | None = None,
        create_tool_call_id: str | None = None,
        run_index: int | None = None,
        spawn_initiator_kind: str | None = None,
        spawn_initiator_id: str | None = None,
        profile_id: str | None = None,
    ) -> SubagentRun:
        run = await self.spawn_fake(
            task=task,
            event_context=event_context,
            label=label,
            role=role,
            context_policy=context_policy,
            capability_profile=capability_profile,
            budget=budget,
            parent_context_id=parent_context_id,
            parent_model_call_index=parent_model_call_index,
            spawning_tool_call_id=spawning_tool_call_id,
            spawning_tool_name=spawning_tool_name,
            task_id=task_id,
            batch_id=batch_id,
            create_tool_call_id=create_tool_call_id,
            run_index=run_index,
            spawn_initiator_kind=spawn_initiator_kind,
            spawn_initiator_id=spawn_initiator_id,
            profile_id=profile_id,
        )
        if self._child_runner is not None:
            self._child_tasks[run.subagent_run_id] = asyncio.create_task(self._run_child(run))
        return run

    async def complete_fake(
        self,
        subagent_run_id: str,
        *,
        summary: str,
        event_context: EventContext | None = None,
        output_preview: str | None = None,
        artifact_ids: tuple[str, ...] = (),
        token_usage: dict[str, object] | None = None,
        tool_call_count: int | None = None,
    ) -> SubagentResult:
        run = self._require_run(subagent_run_id)
        ctx = event_context or _spawn_event_context(run)
        summary = _clip(summary, run.budget.max_result_summary_chars_per_child)
        result_id = f"subagent_result:{uuid4().hex}"
        result_artifact_id = f"{subagent_run_id}:result:{uuid4().hex}"
        self.parent_runtime_session.archive.put_text(
            result_artifact_id,
            output_preview or summary,
            session_id=self.parent_runtime_session.runtime_session_id,
            run_id=run.parent_run_id,
            media_type="text/markdown",
            metadata={
                "artifact_kind": "subagent_result",
                "subagent_run_id": subagent_run_id,
                "result_id": result_id,
                "child_runtime_session_id": run.child_runtime_session_id,
            },
        )
        completed = SubagentRunCompletedEvent(
            **ctx.event_fields(),
            subagent_run_id=subagent_run_id,
            parent_runtime_session_id=run.parent_runtime_session_id,
            child_runtime_session_id=run.child_runtime_session_id,
            child_run_id=run.child_run_id,
            result_id=result_id,
            summary=summary,
            result_artifact_id=result_artifact_id,
            artifact_ids=[result_artifact_id, *artifact_ids],
            token_usage=token_usage,
            tool_call_count=tool_call_count,
        )
        stored = await self.parent_runtime_session.emit(completed)
        completed_at = _parse_dt(stored.created_at)
        result = SubagentResult(
            subagent_run_id=subagent_run_id,
            result_id=result_id,
            status="completed",
            summary=summary,
            output_preview=output_preview,
            final_message_artifact_id=result_artifact_id,
            artifact_ids=(result_artifact_id, *artifact_ids),
            diagnostics=(),
            token_usage=token_usage,
            tool_call_count=tool_call_count,
            completed_at=completed_at,
            task_id=run.task_id,
            result_source="inferred",
        )
        self._results[subagent_run_id] = result
        self._runs[subagent_run_id] = replace(
            run,
            status="completed",
            updated_at=completed_at,
            result_id=result_id,
            result_source="inferred",
        )
        if run.task_id is not None:
            task_completed = await self.parent_runtime_session.emit(
                SubagentTaskCompletedEvent(
                    **ctx.event_fields(),
                    task_id=run.task_id,
                    subagent_run_id=subagent_run_id,
                    result_id=result_id,
                    primary_result_artifact_id=result_artifact_id,
                    result_source="inferred",
                )
            )
            task = self._tasks.get(run.task_id)
            if task is not None:
                self._tasks[run.task_id] = replace(
                    task,
                    status="completed",
                    result_id=result_id,
                    primary_result_artifact_id=result_artifact_id,
                    updated_at=_parse_dt(task_completed.created_at),
                    completed_at=_parse_dt(task_completed.created_at),
                )
            await self._schedule_dependents_after_completion(run.task_id, event_context=ctx)
        return result

    complete = complete_fake

    async def report_phase(
        self,
        subagent_run_id: str,
        *,
        phase: str,
        event_context: EventContext,
        message: str | None = None,
        progress: Mapping[str, object] | None = None,
        source_tool_call_id: str | None = None,
    ) -> None:
        run = self._require_run(subagent_run_id)
        phase = phase.strip()
        if not phase:
            raise ValueError("phase is required")
        stored = await self.parent_runtime_session.emit(
            SubagentPhaseReportedEvent(
                **event_context.event_fields(),
                subagent_run_id=subagent_run_id,
                task_id=run.task_id,
                phase=_clip(phase, 120),
                message=_clip(message, 1_000) if message else None,
                progress=dict(progress or {}),
                source_tool_call_id=source_tool_call_id,
            )
        )
        updated_at = _parse_dt(stored.created_at)
        self._runs[subagent_run_id] = replace(run, phase=_clip(phase, 120), updated_at=updated_at)
        if run.task_id is not None:
            task = self._tasks.get(run.task_id)
            if task is not None:
                self._tasks[run.task_id] = replace(task, phase=_clip(phase, 120), updated_at=updated_at)

    async def submit_result(
        self,
        subagent_run_id: str,
        *,
        summary: str,
        event_context: EventContext,
        output_preview: str | None = None,
        artifact_ids: tuple[str, ...] = (),
        diagnostics: tuple[Mapping[str, object], ...] = (),
        source_tool_call_id: str | None = None,
    ) -> SubagentResult:
        run = self._require_run(subagent_run_id)
        if run.status not in _ACTIVE_STATUSES:
            raise SubagentRuntimeError(f"Subagent run is already terminal: {subagent_run_id}")
        existing = self._submitted_results.get(subagent_run_id)
        if existing is not None:
            return existing
        summary = _clip(summary.strip() or "(child agent submitted an empty result)", run.budget.max_result_summary_chars_per_child)
        result_id = f"subagent_result:{uuid4().hex}"
        result_artifact_id = f"{subagent_run_id}:result:{uuid4().hex}"
        self.parent_runtime_session.archive.put_text(
            result_artifact_id,
            output_preview or summary,
            session_id=self.parent_runtime_session.runtime_session_id,
            run_id=run.parent_run_id,
            media_type="text/markdown",
            metadata={
                "artifact_kind": "subagent_result",
                "subagent_run_id": subagent_run_id,
                "result_id": result_id,
                "child_runtime_session_id": run.child_runtime_session_id,
                "result_source": "explicit",
            },
        )
        stored = await self.parent_runtime_session.emit(
            SubagentResultSubmittedEvent(
                **event_context.event_fields(),
                subagent_run_id=subagent_run_id,
                task_id=run.task_id,
                result_id=result_id,
                summary=summary,
                output_preview=output_preview,
                result_artifact_id=result_artifact_id,
                artifact_ids=[result_artifact_id, *artifact_ids],
                source_tool_call_id=source_tool_call_id,
                diagnostics=[dict(item) for item in diagnostics],
            )
        )
        submitted_at = _parse_dt(stored.created_at)
        result = SubagentResult(
            subagent_run_id=subagent_run_id,
            result_id=result_id,
            status="completed",
            summary=summary,
            output_preview=output_preview,
            final_message_artifact_id=result_artifact_id,
            artifact_ids=(result_artifact_id, *artifact_ids),
            diagnostics=diagnostics,
            token_usage=None,
            tool_call_count=None,
            completed_at=submitted_at,
            task_id=run.task_id,
            result_source="explicit",
        )
        self._submitted_results[subagent_run_id] = result
        return result

    def submitted_result(self, subagent_run_id: str) -> SubagentResult | None:
        return self._submitted_results.get(subagent_run_id)

    async def complete_submitted_result(
        self,
        subagent_run_id: str,
        *,
        event_context: EventContext | None = None,
        token_usage: dict[str, object] | None = None,
        tool_call_count: int | None = None,
    ) -> SubagentResult:
        run = self._require_run(subagent_run_id)
        result = self._submitted_results.get(subagent_run_id)
        if result is None:
            raise SubagentNotReady(subagent_run_id)
        if run.status == "completed" and self._results.get(subagent_run_id) is not None:
            return self._results[subagent_run_id]
        ctx = event_context or _spawn_event_context(run)
        stored = await self.parent_runtime_session.emit(
            SubagentRunCompletedEvent(
                **ctx.event_fields(),
                subagent_run_id=subagent_run_id,
                parent_runtime_session_id=run.parent_runtime_session_id,
                child_runtime_session_id=run.child_runtime_session_id,
                child_run_id=run.child_run_id,
                result_id=result.result_id,
                summary=result.summary,
                result_artifact_id=result.final_message_artifact_id,
                artifact_ids=list(result.artifact_ids),
                token_usage=token_usage,
                tool_call_count=tool_call_count,
            )
        )
        completed_at = _parse_dt(stored.created_at)
        result = replace(result, token_usage=token_usage, tool_call_count=tool_call_count, completed_at=completed_at)
        self._results[subagent_run_id] = result
        self._runs[subagent_run_id] = replace(
            run,
            status="completed",
            updated_at=completed_at,
            result_id=result.result_id,
            result_source="explicit",
        )
        if run.task_id is not None:
            task_completed = await self.parent_runtime_session.emit(
                SubagentTaskCompletedEvent(
                    **ctx.event_fields(),
                    task_id=run.task_id,
                    subagent_run_id=subagent_run_id,
                    result_id=result.result_id,
                    primary_result_artifact_id=result.final_message_artifact_id,
                    result_source="explicit",
                )
            )
            task = self._tasks.get(run.task_id)
            if task is not None:
                task_completed_at = _parse_dt(task_completed.created_at)
                self._tasks[run.task_id] = replace(
                    task,
                    status="completed",
                    result_id=result.result_id,
                    primary_result_artifact_id=result.final_message_artifact_id,
                    updated_at=task_completed_at,
                    completed_at=task_completed_at,
                )
            await self._schedule_dependents_after_completion(run.task_id, event_context=ctx)
        return result

    def set_child_run_id(self, subagent_run_id: str, child_run_id: str) -> None:
        run = self._require_run(subagent_run_id)
        self._runs[subagent_run_id] = replace(run, child_run_id=child_run_id)

    async def fail(
        self,
        subagent_run_id: str,
        *,
        reason_code: str,
        reason_message: str | None = None,
        event_context: EventContext | None = None,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> None:
        run = self._require_run(subagent_run_id)
        ctx = event_context or _spawn_event_context(run)
        task = self._child_tasks.get(subagent_run_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        stored = await self.parent_runtime_session.emit(
            SubagentRunFailedEvent(
                **ctx.event_fields(),
                subagent_run_id=subagent_run_id,
                parent_runtime_session_id=run.parent_runtime_session_id,
                child_runtime_session_id=run.child_runtime_session_id,
                reason_code=reason_code,
                reason_message=reason_message,
                diagnostics=list(diagnostics or []),
            )
        )
        self._runs[subagent_run_id] = replace(run, status="failed", updated_at=_parse_dt(stored.created_at))
        if run.task_id is not None:
            task_failed = await self.parent_runtime_session.emit(
                SubagentTaskFailedEvent(
                    **ctx.event_fields(),
                    task_id=run.task_id,
                    subagent_run_id=subagent_run_id,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    diagnostics=list(diagnostics or []),
                )
            )
            task = self._tasks.get(run.task_id)
            if task is not None:
                metadata = dict(task.metadata)
                metadata["terminal_event_id"] = _event_ref(task_failed)
                self._tasks[run.task_id] = replace(
                    task,
                    status="failed",
                    updated_at=_parse_dt(task_failed.created_at),
                    completed_at=_parse_dt(task_failed.created_at),
                    metadata=metadata,
                )
            await self._block_dependents_after_dependency_failed(
                run.task_id,
                event_context=ctx,
                dependency_terminal_event_ids={run.task_id: _event_ref(task_failed)},
            )

    async def cancel(
        self,
        subagent_run_id: str,
        *,
        event_context: EventContext | None = None,
        reason_code: str = "subagent_cancelled",
        reason_message: str | None = None,
        cancelled_by: str = "parent_agent",
    ) -> SubagentRun:
        run = self._require_run(subagent_run_id)
        if run.status in _TERMINAL_STATUSES:
            return run
        ctx = event_context or _spawn_event_context(run)
        task = self._child_tasks.get(subagent_run_id)
        if task is not None and not task.done():
            task.cancel()
        stored = await self.parent_runtime_session.emit(
            SubagentRunCancelledEvent(
                **ctx.event_fields(),
                subagent_run_id=subagent_run_id,
                parent_runtime_session_id=run.parent_runtime_session_id,
                child_runtime_session_id=run.child_runtime_session_id,
                reason_code=reason_code,
                reason_message=reason_message,
                cancelled_by=cancelled_by,  # type: ignore[arg-type]
            )
        )
        updated = replace(run, status="cancelled", updated_at=_parse_dt(stored.created_at))
        self._runs[subagent_run_id] = updated
        if run.task_id is not None:
            task_cancelled = await self.parent_runtime_session.emit(
                SubagentTaskCancelledEvent(
                    **ctx.event_fields(),
                    task_id=run.task_id,
                    subagent_run_id=subagent_run_id,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by=cancelled_by,  # type: ignore[arg-type]
                )
            )
            task = self._tasks.get(run.task_id)
            if task is not None:
                metadata = dict(task.metadata)
                metadata["terminal_event_id"] = _event_ref(task_cancelled)
                self._tasks[run.task_id] = replace(
                    task,
                    status="cancelled",
                    updated_at=_parse_dt(task_cancelled.created_at),
                    completed_at=_parse_dt(task_cancelled.created_at),
                    metadata=metadata,
                )
            await self._block_dependents_after_dependency_failed(
                run.task_id,
                event_context=ctx,
                dependency_terminal_event_ids={run.task_id: _event_ref(task_cancelled)},
            )
        return updated

    async def cancel_active_children(
        self,
        *,
        reason_code: str = "subagent_host_shutdown",
        reason_message: str | None = None,
        cancelled_by: str = "host_shutdown",
        timeout_seconds: float | None = 5.0,
    ) -> tuple[SubagentRun, ...]:
        """Cancel all live child tasks owned by this runtime.

        This is the HostSession/HostCore close hook.  It is deliberately
        parent-graph-first: every child that was active gets a durable
        cancellation fact before we wait for cooperative task teardown.
        """

        active_runs = [run for run in self._runs.values() if run.status in _ACTIVE_STATUSES]
        cancelled: list[SubagentRun] = []
        for run in active_runs:
            cancelled.append(
                await self.cancel(
                    run.subagent_run_id,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by=cancelled_by,
                )
            )
        tasks = [
            task for subagent_run_id, task in self._child_tasks.items()
            if any(run.subagent_run_id == subagent_run_id for run in active_runs) and not task.done()
        ]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_seconds)
            except TimeoutError:
                pass
        return tuple(cancelled)

    async def repair_dangling_children(
        self,
        *,
        reason_code: str = "subagent_dangling_repaired",
        reason_message: str = (
            "Child runtime was recorded as active but has no live task handle in this host process; "
            "marking it failed during resume/repair."
        ),
    ) -> tuple[SubagentRun, ...]:
        """Fail active durable graph nodes that cannot be resumed in-process.

        V1 child runtimes do not survive host-process death.  On resume, the
        parent graph may still contain running/suspended children, but there is
        no coroutine or terminal ownership to safely continue.  Fail closed with
        typed parent graph events instead of leaving phantom running children.
        """

        repaired: list[SubagentRun] = []
        self._bootstrap_from_parent_event_log()
        for run in list(self._runs.values()):
            if run.status not in _ACTIVE_STATUSES:
                continue
            task = self._child_tasks.get(run.subagent_run_id)
            if task is not None and not task.done():
                continue
            await self.fail(
                run.subagent_run_id,
                reason_code=reason_code,
                reason_message=reason_message,
                diagnostics=[
                    {
                        "child_runtime_session_id": run.child_runtime_session_id,
                        "parent_run_id": run.parent_run_id,
                        "repair": "dangling_child_without_live_task",
                    }
                ],
            )
            repaired.append(self._runs[run.subagent_run_id])
        return tuple(repaired)

    async def fail_active_children_for_safety_narrowing(
        self,
        *,
        reason_code: str,
        reason_message: str | None = None,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> tuple[SubagentRun, ...]:
        """Cancel active children after a parent-side safety/capability narrowing.

        This is the conservative V1 observer endpoint used by HostSession safe
        points.  A future implementation can refresh child snapshots, but V1
        never lets an active child continue silently with revoked capability.
        """

        cancelled: list[SubagentRun] = []
        for run in list(self._runs.values()):
            if run.status not in _ACTIVE_STATUSES:
                continue
            await self.cancel(
                run.subagent_run_id,
                reason_code=reason_code,
                reason_message=reason_message,
                cancelled_by="runtime",
            )
            cancelled.append(self._runs[run.subagent_run_id])
        return tuple(cancelled)

    def fail_active_children_for_safety_narrowing_now(
        self,
        *,
        reason_code: str,
        reason_message: str | None = None,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> tuple[SubagentRun, ...]:
        """Synchronous cancel-closed variant for host permission-mode switches."""

        del diagnostics
        cancelled: list[SubagentRun] = []
        for run in list(self._runs.values()):
            if run.status not in _ACTIVE_STATUSES:
                continue
            task = self._child_tasks.get(run.subagent_run_id)
            if task is not None and not task.done():
                task.cancel()
            event = self.parent_runtime_session.emit_from_thread(
                SubagentRunCancelledEvent(
                    **_spawn_event_context(run).event_fields(),
                    subagent_run_id=run.subagent_run_id,
                    parent_runtime_session_id=run.parent_runtime_session_id,
                    child_runtime_session_id=run.child_runtime_session_id,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by="runtime",
                )
            )
            updated = replace(run, status="cancelled", updated_at=_parse_dt(event.created_at))
            self._runs[run.subagent_run_id] = updated
            if run.task_id is not None:
                self._cancel_task_for_run_now(
                    run=updated,
                    event_context=_spawn_event_context(run),
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by="runtime",
                )
            cancelled.append(updated)
        return tuple(cancelled)

    async def wait_result(
        self,
        subagent_run_id: str,
        *,
        event_context: EventContext,
        returned_to_tool_call_id: str,
        source_context_id: str | None = None,
        source_model_call_index: int | None = None,
        source_tool_name: str | None = "wait_agent",
    ) -> SubagentResult:
        run = self._require_run(subagent_run_id)
        result = self._results.get(subagent_run_id)
        if result is None:
            task = self._child_tasks.get(subagent_run_id)
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0)
                except TimeoutError:
                    pass
                result = self._results.get(subagent_run_id)
        if result is None:
            raise SubagentNotReady(subagent_run_id)
        edge = SubagentEdgeRecordedEvent(
            **event_context.event_fields(),
            edge_id=f"subagent_edge:{subagent_run_id}:wait:{uuid4().hex}",
            edge_kind="wait",
            parent_runtime_session_id=run.parent_runtime_session_id,
            parent_run_id=event_context.run_id,
            parent_turn_id=event_context.turn_id,
            parent_reply_id=event_context.reply_id,
            subagent_run_id=subagent_run_id,
            child_runtime_session_id=run.child_runtime_session_id,
            child_run_id=run.child_run_id,
            source_context_id=source_context_id,
            source_model_call_index=source_model_call_index,
            source_tool_call_id=returned_to_tool_call_id,
            source_tool_name=source_tool_name,
            result_id=result.result_id,
            result_artifact_id=result.final_message_artifact_id,
            returned_to_tool_call_id=returned_to_tool_call_id,
        )
        await self.parent_runtime_session.emit(edge)
        self._consumed_result_ids.add(result.result_id)
        return result

    async def wait_for_result(
        self,
        subagent_run_id: str,
        *,
        event_context: EventContext,
        returned_to_tool_call_id: str,
        source_context_id: str | None = None,
        source_model_call_index: int | None = None,
        source_tool_name: str | None = "wait_agent",
        timeout_seconds: float | None = None,
    ) -> SubagentResult:
        if timeout_seconds is not None:
            task = self._child_tasks.get(subagent_run_id)
            if task is not None and not task.done() and subagent_run_id not in self._results:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, timeout_seconds))
                except TimeoutError:
                    pass
        return await self.wait_result(
            subagent_run_id,
            event_context=event_context,
            returned_to_tool_call_id=returned_to_tool_call_id,
            source_context_id=source_context_id,
            source_model_call_index=source_model_call_index,
            source_tool_name=source_tool_name,
        )

    async def wait_tasks(
        self,
        task_ids: tuple[str, ...],
        *,
        event_context: EventContext,
        consumer_tool_call_id: str,
        settle: str = "all",
        timeout_seconds: float | None = None,
        include_consumed: bool = False,
    ) -> tuple[dict[str, object], ...]:
        if settle not in {"all", "first"}:
            raise ValueError("settle must be all or first")
        if not task_ids:
            raise ValueError("task_ids is required")
        for task_id in task_ids:
            self._require_task(task_id)

        deadline = None if timeout_seconds is None else asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
        while True:
            settled = [
                self._task_wait_payload(task_id, include_consumed=include_consumed)
                for task_id in task_ids
            ]
            settled = [item for item in settled if item is not None]
            if (settle == "first" and settled) or (settle == "all" and len(settled) == len(task_ids)):
                chosen = settled[:1] if settle == "first" else settled
                await self._consume_task_wait_results(
                    chosen,
                    event_context=event_context,
                    consumer_tool_call_id=consumer_tool_call_id,
                    include_consumed=include_consumed,
                )
                return tuple(chosen)
            if deadline is None or asyncio.get_running_loop().time() >= deadline:
                await self._consume_task_wait_results(
                    settled,
                    event_context=event_context,
                    consumer_tool_call_id=consumer_tool_call_id,
                    include_consumed=include_consumed,
                )
                return tuple(settled)
            await asyncio.sleep(min(0.01, max(0.0, deadline - asyncio.get_running_loop().time())))

    async def cancel_task(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        reason_code: str = "subagent_task_cancelled",
        reason_message: str | None = None,
        cancelled_by: str = "parent_agent",
    ) -> SubagentTask:
        task = self._require_task(task_id)
        if task.current_run_id:
            run = self._runs.get(task.current_run_id)
            if run is not None and run.status in _ACTIVE_STATUSES:
                await self.cancel(
                    run.subagent_run_id,
                    event_context=event_context,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by=cancelled_by,
                )
                return self._tasks.get(task_id, task)
        if task.status in {"completed", "failed", "cancelled", "blocked_dependency_failed"}:
            return task
        return await self.cancel_materialized_task(
            task_id,
            event_context=event_context,
            reason_code=reason_code,
            reason_message=reason_message,
            cancelled_by=cancelled_by,
        )

    async def cancel_materialized_task(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        reason_code: str,
        reason_message: str | None = None,
        cancelled_by: str = "runtime",
        force: bool = False,
    ) -> SubagentTask:
        """Terminalize a task that has no active child run or was already repaired."""

        task = self._require_task(task_id)
        if task.status in {"completed", "failed", "cancelled"}:
            return task
        if task.status == "blocked_dependency_failed" and not force:
            return task
        stored = await self.parent_runtime_session.emit(
            SubagentTaskCancelledEvent(
                **event_context.event_fields(),
                task_id=task_id,
                subagent_run_id=task.current_run_id,
                reason_code=reason_code,
                reason_message=reason_message,
                cancelled_by=cancelled_by,  # type: ignore[arg-type]
            )
        )
        cancelled = self._apply_task_cancelled(
            task,
            stored_created_at=stored.created_at,
            terminal_event_id=_event_ref(stored),
        )
        await self._block_dependents_after_dependency_failed(
            task_id,
            event_context=event_context,
            dependency_terminal_event_ids={task_id: _event_ref(stored)},
        )
        return cancelled

    async def _schedule_dependents_after_completion(
        self,
        task_id: str,
        *,
        event_context: EventContext,
    ) -> None:
        for task in list(self._tasks.values()):
            if task.status != "waiting_dependency":
                continue
            if task_id not in task.depends_on:
                continue
            if self._dependency_failed(task):
                failed_ids = tuple(
                    dependency_id
                    for dependency_id in task.depends_on
                    if self._tasks.get(dependency_id) is not None
                    and self._tasks[dependency_id].status in {"failed", "cancelled", "blocked_dependency_failed"}
                )
                await self.block_task(
                    task.task_id,
                    event_context=event_context,
                    status="blocked_dependency_failed",
                    blocked_reason="dependency_failed",
                    blocked_by_task_ids=failed_ids,
                )
                continue
            if self._dependencies_satisfied(task):
                await self.start_task(
                    task.task_id,
                    event_context=event_context,
                    spawn_initiator_kind="dependency_satisfied",
                    spawn_initiator_id=task_id,
                )

    async def _block_dependents_after_dependency_failed(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        dependency_terminal_event_ids: Mapping[str, str] | None = None,
    ) -> None:
        for task in list(self._tasks.values()):
            if task.status not in {"created", "waiting_dependency"}:
                continue
            if task_id not in task.depends_on:
                continue
            await self.block_task(
                task.task_id,
                event_context=event_context,
                status="blocked_dependency_failed",
                blocked_reason="dependency_failed",
                blocked_by_task_ids=(task_id,),
                dependency_terminal_event_ids=dependency_terminal_event_ids,
            )

    def _cancel_task_for_run_now(
        self,
        *,
        run: SubagentRun,
        event_context: EventContext,
        reason_code: str,
        reason_message: str | None,
        cancelled_by: str,
    ) -> None:
        if run.task_id is None:
            return
        task = self._tasks.get(run.task_id)
        if task is None or task.status in {"completed", "failed", "cancelled", "blocked_dependency_failed"}:
            return
        stored = self.parent_runtime_session.emit_from_thread(
            SubagentTaskCancelledEvent(
                **event_context.event_fields(),
                task_id=run.task_id,
                subagent_run_id=run.subagent_run_id,
                reason_code=reason_code,
                reason_message=reason_message,
                cancelled_by=cancelled_by,  # type: ignore[arg-type]
            )
        )
        self._apply_task_cancelled(
            task,
            stored_created_at=stored.created_at,
            terminal_event_id=_event_ref(stored),
        )
        self._block_dependents_after_dependency_failed_now(
            run.task_id,
            event_context=event_context,
            dependency_terminal_event_ids={run.task_id: _event_ref(stored)},
        )

    def _block_dependents_after_dependency_failed_now(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        dependency_terminal_event_ids: Mapping[str, str] | None = None,
    ) -> None:
        for task in list(self._tasks.values()):
            if task.status not in {"created", "waiting_dependency"}:
                continue
            if task_id not in task.depends_on:
                continue
            self._block_task_now(
                task.task_id,
                event_context=event_context,
                status="blocked_dependency_failed",
                blocked_reason="dependency_failed",
                blocked_by_task_ids=(task_id,),
                dependency_terminal_event_ids=dependency_terminal_event_ids,
            )

    def _block_task_now(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        status: str,
        blocked_reason: str,
        blocked_by_task_ids: tuple[str, ...],
        dependency_terminal_event_ids: Mapping[str, str] | None = None,
    ) -> SubagentTask:
        task = self._require_task(task_id)
        terminal_event_ids = self._dependency_terminal_event_ids(
            blocked_by_task_ids,
            overrides=dependency_terminal_event_ids,
        )
        stored = self.parent_runtime_session.emit_from_thread(
            SubagentTaskBlockedEvent(
                **event_context.event_fields(),
                task_id=task_id,
                status=status,  # type: ignore[arg-type]
                blocked_reason=blocked_reason,  # type: ignore[arg-type]
                blocked_by_task_ids=list(blocked_by_task_ids),
                dependency_status_snapshot={
                    dependency_id: self._tasks[dependency_id].status
                    for dependency_id in blocked_by_task_ids
                    if dependency_id in self._tasks
                },
                dependency_terminal_event_ids=terminal_event_ids,
                dependency_generation=_dependency_generation(terminal_event_ids),
            )
        )
        metadata = dict(task.metadata)
        if status == "blocked_dependency_failed":
            metadata["terminal_event_id"] = _event_ref(stored)
        blocked = replace(task, status=status, updated_at=_parse_dt(stored.created_at), metadata=metadata)  # type: ignore[arg-type]
        self._tasks[task_id] = blocked
        if status == "blocked_dependency_failed":
            self._block_dependents_after_dependency_failed_now(
                task_id,
                event_context=event_context,
                dependency_terminal_event_ids={task_id: _event_ref(stored)},
            )
        return blocked

    def _apply_task_cancelled(
        self,
        task: SubagentTask,
        *,
        stored_created_at: str,
        terminal_event_id: str,
    ) -> SubagentTask:
        cancelled_at = _parse_dt(stored_created_at)
        metadata = dict(task.metadata)
        metadata["terminal_event_id"] = terminal_event_id
        cancelled = replace(
            task,
            status="cancelled",
            updated_at=cancelled_at,
            completed_at=cancelled_at,
            metadata=metadata,
        )
        self._tasks[task.task_id] = cancelled
        return cancelled

    def _dependency_terminal_event_ids(
        self,
        task_ids: tuple[str, ...],
        *,
        overrides: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        terminal_event_ids: dict[str, str] = {}
        for task_id in task_ids:
            task = self._tasks.get(task_id)
            terminal_event_id = task.metadata.get("terminal_event_id") if task is not None else None
            if isinstance(terminal_event_id, str) and terminal_event_id:
                terminal_event_ids[task_id] = terminal_event_id
        if overrides:
            terminal_event_ids.update({key: value for key, value in overrides.items() if value})
        return terminal_event_ids

    def _dependency_status_snapshot(
        self,
        task_ids: tuple[str, ...],
        *,
        planned_status_by_task_id: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        planned = planned_status_by_task_id or {}
        for task_id in task_ids:
            if task_id in self._tasks:
                snapshot[task_id] = self._tasks[task_id].status
                continue
            planned_status = planned.get(task_id)
            if planned_status is None:
                continue
            snapshot[task_id] = "running" if planned_status == "start" else planned_status
        return snapshot

    def _dependencies_satisfied(self, task: SubagentTask) -> bool:
        return all(
            self._tasks.get(dependency_id) is not None
            and self._tasks[dependency_id].status == "completed"
            for dependency_id in task.depends_on
        )

    def _dependency_failed(self, task: SubagentTask) -> bool:
        return any(
            self._tasks.get(dependency_id) is not None
            and self._tasks[dependency_id].status in {"failed", "cancelled", "blocked_dependency_failed"}
            for dependency_id in task.depends_on
        )

    def _task_wait_payload(
        self,
        task_id: str,
        *,
        include_consumed: bool,
    ) -> dict[str, object] | None:
        task = self._require_task(task_id)
        if task.status not in {"completed", "failed", "cancelled", "blocked_dependency_failed"}:
            return None
        run = self._runs.get(task.current_run_id or "")
        result = self._results.get(task.current_run_id or "") if task.current_run_id else None
        consumed = bool(
            task_id in self._consumed_task_ids
            or (result is not None and result.result_id in self._consumed_result_ids)
        )
        if consumed and not include_consumed:
            return None
        return {
            "task_id": task.task_id,
            "task_key": task.task_key,
            "status": task.status,
            "subagent_run_id": task.current_run_id,
            "child_runtime_session_id": run.child_runtime_session_id if run is not None else None,
            "result_id": result.result_id if result is not None else None,
            "summary": result.summary if result is not None else None,
            "output_preview": result.output_preview if result is not None else None,
            "result_artifact_id": result.final_message_artifact_id if result is not None else None,
            "artifact_ids": list(result.artifact_ids) if result is not None else [],
            "result_source": result.result_source if result is not None else "none",
            "consumed": consumed,
        }

    async def _consume_task_wait_results(
        self,
        payloads: list[dict[str, object]],
        *,
        event_context: EventContext,
        consumer_tool_call_id: str,
        include_consumed: bool,
    ) -> None:
        for payload in payloads:
            task_id = payload.get("task_id")
            if not isinstance(task_id, str):
                continue
            result_id = payload.get("result_id")
            already_consumed = task_id in self._consumed_task_ids or (
                isinstance(result_id, str) and result_id in self._consumed_result_ids
            )
            if already_consumed and not include_consumed:
                continue
            consumed_status = payload.get("status")
            terminal_event_id = None if isinstance(result_id, str) else f"subagent_task_terminal:{task_id}"
            await self.parent_runtime_session.emit(
                SubagentResultConsumedEvent(
                    **event_context.event_fields(),
                    consumption_id=f"subagent_consumption:{uuid4().hex}",
                    consumer_tool_call_id=consumer_tool_call_id,
                    kind="wait_task",
                    task_id=task_id,
                    subagent_run_id=payload.get("subagent_run_id") if isinstance(payload.get("subagent_run_id"), str) else None,
                    result_id=result_id if isinstance(result_id, str) else None,
                    consumed_status=(
                        consumed_status
                        if consumed_status in {"completed", "failed", "cancelled", "blocked_dependency_failed"}
                        else "failed"
                    ),  # type: ignore[arg-type]
                    terminal_event_id=terminal_event_id,
                    diagnostics=[],
                )
            )
            self._consumed_task_ids.add(task_id)
            if isinstance(result_id, str):
                self._consumed_result_ids.add(result_id)

    def graph(self) -> SubagentGraphProjection:
        return project_subagent_graph(
            self.parent_runtime_session.runtime_session_id,
            self.parent_runtime_session.event_log,
            locator=self.event_log_locator,
        )

    def child_runtime_session(self, subagent_run_id: str) -> RuntimeSession:
        run = self._require_run(subagent_run_id)
        return self._child_sessions[run.child_runtime_session_id]

    def pending_results_for_delivery(self, *, max_results: int = 8) -> tuple[SubagentResult, ...]:
        results: list[SubagentResult] = []
        for run in self._runs.values():
            result = self._results.get(run.subagent_run_id)
            if result is None:
                continue
            if result.result_id in self._consumed_result_ids:
                continue
            if result.result_id in self._delivered_result_ids:
                continue
            results.append(result)
            if len(results) >= max_results:
                break
        return tuple(results)

    def render_pending_results_section(self, *, max_results: int = 8) -> tuple[str | None, tuple[SubagentResult, ...]]:
        results = self.pending_results_for_delivery(max_results=max_results)
        if not results:
            return None, ()
        lines = [
            "Completed child agent results that have not been explicitly collected with wait_agent:",
        ]
        for result in results:
            lines.extend(
                [
                    f"- subagent_run_id: {result.subagent_run_id}",
                    f"  result_id: {result.result_id}",
                    f"  status: {result.status}",
                    f"  summary: {result.summary}",
                    f"  result_artifact_id: {result.final_message_artifact_id or 'none'}",
                ]
            )
        return "\n".join(lines), results

    async def mark_results_delivered(
        self,
        results: tuple[SubagentResult, ...],
        *,
        event_context: EventContext,
        context_id: str,
        model_call_index: int,
        section_id: str,
    ) -> list[SubagentResultDeliveredEvent]:
        events: list[SubagentResultDeliveredEvent] = []
        for result in results:
            if result.result_id in self._delivered_result_ids or result.result_id in self._consumed_result_ids:
                continue
            run = self._require_run(result.subagent_run_id)
            event = await self.parent_runtime_session.emit(
                SubagentResultDeliveredEvent(
                    **event_context.event_fields(),
                    subagent_run_id=result.subagent_run_id,
                    parent_runtime_session_id=run.parent_runtime_session_id,
                    parent_run_id=event_context.run_id,
                    parent_turn_id=event_context.turn_id,
                    parent_reply_id=event_context.reply_id,
                    context_id=context_id,
                    model_call_index=model_call_index,
                    section_id=section_id,
                    result_id=result.result_id,
                    result_artifact_id=result.final_message_artifact_id,
                    summary=result.summary,
                )
            )
            self._delivered_result_ids.add(result.result_id)
            events.append(event)
        return events

    def _create_child_runtime_session(
        self,
        *,
        child_runtime_session_id: str,
        subagent_run_id: str,
        parent_run_id: str,
        capability_profile_id: str,
    ) -> RuntimeSession:
        event_log = self._child_event_log_factory(child_runtime_session_id)
        if hasattr(self.event_log_locator, "register"):
            self.event_log_locator.register(child_runtime_session_id, event_log)  # type: ignore[attr-defined]
        return RuntimeSession(
            self.parent_runtime_session.workspace_root,
            event_log=event_log,
            archive=self.parent_runtime_session.archive,
            tool_result_artifacts=self.parent_runtime_session.tool_result_artifacts,
            runtime_session_id=child_runtime_session_id,
            terminal_binding=self.parent_runtime_session.terminal_binding,
            extra_tool_bindings=self.parent_runtime_session.extra_tool_bindings,
            default_event_metadata={
                "subagent": {
                    "subagent_run_id": subagent_run_id,
                    "parent_runtime_session_id": self.parent_runtime_session.runtime_session_id,
                    "parent_run_id": parent_run_id,
                    "capability_profile_id": capability_profile_id,
                }
            },
        )

    async def _run_child(self, run: SubagentRun) -> None:
        assert self._child_runner is not None
        try:
            if run.budget.child_timeout_seconds is None:
                await self._child_runner(self, run)
            else:
                await asyncio.wait_for(
                    self._child_runner(self, run),
                    timeout=max(0.0, run.budget.child_timeout_seconds),
                )
        except TimeoutError:
            current = self._runs.get(run.subagent_run_id)
            if current is not None and current.status in _ACTIVE_STATUSES:
                await self.fail(
                    run.subagent_run_id,
                    reason_code="subagent_timeout",
                    reason_message="Child agent exceeded its configured timeout.",
                    diagnostics=[{"timeout_seconds": run.budget.child_timeout_seconds}],
                )
        except asyncio.CancelledError:
            # Explicit stop_agent/cancel already writes the canonical parent
            # graph event. Do not convert cooperative cancellation into an
            # additional failure.
            raise
        except Exception as exc:
            current = self._runs.get(run.subagent_run_id)
            if current is not None and current.status in {"starting", "running", "suspended"}:
                await self.fail(
                    run.subagent_run_id,
                    reason_code="subagent_child_runner_error",
                    reason_message=f"{type(exc).__name__}: {exc}",
                    diagnostics=[{"error_type": type(exc).__name__, "message": str(exc)}],
                )

    def _require_run(self, subagent_run_id: str) -> SubagentRun:
        try:
            return self._runs[subagent_run_id]
        except KeyError as exc:
            raise SubagentNotFound(subagent_run_id) from exc

    def _require_task(self, task_id: str) -> SubagentTask:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise SubagentNotFound(task_id) from exc

    def _enforce_spawn_limits(self, parent_run_id: str, budget: SubagentBudget) -> None:
        self.validate_can_start_batch(parent_run_id, count=1, budget=budget)

    def validate_can_start_batch(
        self,
        parent_run_id: str,
        *,
        count: int,
        budget: SubagentBudget | None = None,
    ) -> None:
        budget = budget or self.default_budget
        active_for_run = [
            run for run in self._runs.values()
            if run.parent_run_id == parent_run_id and run.status in _ACTIVE_STATUSES
        ]
        active_for_session = [
            run for run in self._runs.values()
            if run.status in _ACTIVE_STATUSES
        ]
        total_for_run = [run for run in self._runs.values() if run.parent_run_id == parent_run_id]
        if count < 1:
            raise ValueError("count must be positive")
        if len(active_for_run) + count > budget.max_concurrent_children_per_parent_run:
            raise SubagentLimitExceeded("max_concurrent_children_per_parent_run exceeded")
        if len(active_for_session) + count > budget.max_concurrent_children_per_host_session:
            raise SubagentLimitExceeded("max_concurrent_children_per_host_session exceeded")
        if len(total_for_run) + count > budget.max_total_child_runs_per_parent_run:
            raise SubagentLimitExceeded("max_total_child_runs_per_parent_run exceeded")
        if budget.max_spawn_depth_from_root < 0:
            raise SubagentLimitExceeded("max_spawn_depth_from_root exceeded")

    def _default_capability_profile(self, budget: SubagentBudget) -> SubagentCapabilityProfile:
        if self._parent_capability_snapshot is None:
            return _default_capability_profile(budget)
        return replace(
            self._parent_capability_snapshot,
            profile_id=f"subagent_capability_profile:{uuid4().hex}",
            max_spawn_depth_from_root=budget.max_spawn_depth_from_root,
        )

    def _capability_profile_for_name(
        self,
        profile_name: str,
        *,
        budget: SubagentBudget | None = None,
    ) -> SubagentCapabilityProfile:
        budget = budget or self.default_budget
        if profile_name not in {"research_worker", "review_worker", "verification_worker"}:
            return self._default_capability_profile(budget)
        base = self._parent_capability_snapshot or _default_capability_profile(budget)
        inherited_names = set(base.allowed_tool_names)
        inherited_descriptor_ids = set(base.allowed_descriptor_ids)
        if profile_name in {"research_worker", "review_worker"}:
            allowed_names = _profile_tool_subset(inherited_names, _READ_ONLY_WORKER_TOOL_NAMES)
            if profile_name == "research_worker":
                allowed_names.update(_mcp_tool_names_from_profile(base))
            profile_summary = "read-only investigation/review profile"
        else:
            allowed_names = _profile_tool_subset(inherited_names, _VERIFICATION_WORKER_TOOL_NAMES)
            profile_summary = "verification profile with terminal access but no file writes"
        allowed_names.update(_CHILD_REPORT_TOOL_NAMES)
        allowed_descriptor_ids = {
            descriptor_id
            for descriptor_id in inherited_descriptor_ids
            if descriptor_id.rsplit(":", 1)[-1] in allowed_names
        }
        allowed_descriptor_ids.update(_CHILD_REPORT_DESCRIPTOR_IDS)
        diagnostics = [
            {
                "code": "subagent_builtin_profile_applied",
                "profile": profile_name,
                "summary": profile_summary,
            },
            {
                "code": "subagent_memory_tools_disabled",
                "memory_enabled": False,
            },
        ]
        return replace(
            base,
            profile_id=f"subagent_capability_profile:{profile_name}:{uuid4().hex}",
            profile_name=profile_name,  # type: ignore[arg-type]
            allowed_tool_names=tuple(sorted(allowed_names)),
            allowed_descriptor_ids=tuple(sorted(allowed_descriptor_ids)),
            can_spawn_subagents=False,
            max_spawn_depth_from_root=budget.max_spawn_depth_from_root,
            memory_enabled=False,
            diagnostics=tuple(diagnostics),
        )

    def _bootstrap_from_parent_event_log(self) -> None:
        for event in self.parent_runtime_session.event_log.iter():
            if isinstance(event, SubagentRunStartedEvent):
                self._bootstrap_started(event)
            elif isinstance(event, SubagentMessageSentEvent):
                self._bootstrap_message(event)
            elif isinstance(event, SubagentRunCompletedEvent):
                self._bootstrap_completed(event)
            elif isinstance(event, SubagentRunFailedEvent):
                self._update_run_status(event.subagent_run_id, "failed", _parse_dt(event.created_at))
            elif isinstance(event, SubagentRunCancelledEvent):
                self._update_run_status(event.subagent_run_id, "cancelled", _parse_dt(event.created_at))
            elif isinstance(event, SubagentRunSuspendedEvent):
                self._update_run_status(event.subagent_run_id, "suspended", _parse_dt(event.created_at))
            elif isinstance(event, SubagentPhaseReportedEvent):
                self._bootstrap_phase_reported(event)
            elif isinstance(event, SubagentResultSubmittedEvent):
                self._bootstrap_result_submitted(event)
            elif isinstance(event, SubagentEdgeRecordedEvent):
                if event.edge_kind == "wait" and event.result_id:
                    self._consumed_result_ids.add(event.result_id)
            elif isinstance(event, SubagentResultConsumedEvent):
                if event.result_id:
                    self._consumed_result_ids.add(event.result_id)
                if event.task_id:
                    self._consumed_task_ids.add(event.task_id)
            elif isinstance(event, SubagentResultDeliveredEvent):
                self._delivered_result_ids.add(event.result_id)
            elif isinstance(event, SubagentTaskCreatedEvent):
                self._bootstrap_task_created(event)
            elif isinstance(event, SubagentTaskStartedEvent):
                self._bootstrap_task_started(event)
            elif isinstance(event, SubagentTaskCompletedEvent):
                self._bootstrap_task_completed(event)
            elif isinstance(event, SubagentTaskFailedEvent):
                self._update_task_status(event.task_id, "failed", _parse_dt(event.created_at))
            elif isinstance(event, SubagentTaskCancelledEvent):
                self._update_task_status(event.task_id, "cancelled", _parse_dt(event.created_at))

    def _bootstrap_started(self, event: SubagentRunStartedEvent) -> None:
        if event.subagent_run_id in self._runs:
            return
        created_at = _parse_dt(event.created_at)
        run = SubagentRun(
            subagent_run_id=event.subagent_run_id,
            parent_runtime_session_id=event.parent_runtime_session_id,
            parent_run_id=event.parent_run_id,
            parent_turn_id=event.parent_turn_id,
            parent_reply_id=event.parent_reply_id,
            parent_context_id=event.parent_context_id,
            parent_model_call_index=event.parent_model_call_index,
            spawning_tool_call_id=event.spawning_tool_call_id,
            spawning_tool_name=event.spawning_tool_name,
            child_runtime_session_id=event.child_runtime_session_id,
            child_run_id=None,
            label=event.label,
            role=event.role,  # type: ignore[arg-type]
            status="running",
            task=event.task_preview,
            created_at=created_at,
            updated_at=created_at,
            context_policy=_context_policy_from_event(event.context_policy),
            capability_profile=_capability_profile_from_event(event.capability_profile),
            budget=self.default_budget,
            task_id=event.task_id,
            batch_id=event.batch_id,
            create_tool_call_id=event.create_tool_call_id,
            run_index=event.run_index,
            spawn_initiator_kind=event.spawn_initiator_kind,  # type: ignore[arg-type]
            spawn_initiator_id=event.spawn_initiator_id,
            profile_id=event.profile_id,
            metadata={"bootstrapped_from_event_log": True},
        )
        self._runs[event.subagent_run_id] = run

    def _bootstrap_message(self, event: SubagentMessageSentEvent) -> None:
        if event.delivery_kind != "spawn_task":
            return
        run = self._runs.get(event.subagent_run_id)
        if run is None or not event.message_artifact_id:
            return
        try:
            task = self.parent_runtime_session.archive.get_text(
                event.message_artifact_id,
                session_id=self.parent_runtime_session.runtime_session_id,
            )
        except Exception:
            task = event.message_preview
        self._runs[event.subagent_run_id] = replace(run, task=task)

    def _bootstrap_completed(self, event: SubagentRunCompletedEvent) -> None:
        completed_at = _parse_dt(event.created_at)
        self._update_run_status(event.subagent_run_id, "completed", completed_at)
        submitted = self._submitted_results.get(event.subagent_run_id)
        self._results[event.subagent_run_id] = SubagentResult(
            subagent_run_id=event.subagent_run_id,
            result_id=event.result_id,
            status="completed",
            summary=event.summary,
            output_preview=submitted.output_preview if submitted is not None else None,
            final_message_artifact_id=event.result_artifact_id,
            artifact_ids=tuple(event.artifact_ids),
            diagnostics=submitted.diagnostics if submitted is not None else (),
            token_usage=event.token_usage,
            tool_call_count=event.tool_call_count,
            completed_at=completed_at,
            task_id=self._runs.get(event.subagent_run_id).task_id if self._runs.get(event.subagent_run_id) else None,
            result_source="explicit" if submitted is not None else "inferred",
        )

    def _bootstrap_phase_reported(self, event: SubagentPhaseReportedEvent) -> None:
        updated_at = _parse_dt(event.created_at)
        run = self._runs.get(event.subagent_run_id)
        if run is not None:
            self._runs[event.subagent_run_id] = replace(run, phase=event.phase, updated_at=updated_at)
        if event.task_id:
            task = self._tasks.get(event.task_id)
            if task is not None:
                self._tasks[event.task_id] = replace(task, phase=event.phase, updated_at=updated_at)

    def _bootstrap_result_submitted(self, event: SubagentResultSubmittedEvent) -> None:
        submitted_at = _parse_dt(event.created_at)
        self._submitted_results[event.subagent_run_id] = SubagentResult(
            subagent_run_id=event.subagent_run_id,
            result_id=event.result_id,
            status="completed",
            summary=event.summary,
            output_preview=event.output_preview,
            final_message_artifact_id=event.result_artifact_id,
            artifact_ids=tuple(event.artifact_ids),
            diagnostics=tuple(event.diagnostics),
            token_usage=None,
            tool_call_count=None,
            completed_at=submitted_at,
            task_id=event.task_id,
            result_source="explicit",
        )

    def _update_run_status(
        self,
        subagent_run_id: str,
        status: SubagentStatus,
        updated_at: datetime,
    ) -> None:
        run = self._runs.get(subagent_run_id)
        if run is None:
            return
        self._runs[subagent_run_id] = replace(run, status=status, updated_at=updated_at)

    def _bootstrap_task_created(self, event: SubagentTaskCreatedEvent) -> None:
        if event.task_id in self._tasks:
            return
        created_at = _parse_dt(event.created_at)
        objective = event.objective_preview
        if event.objective_artifact_id:
            try:
                objective = self.parent_runtime_session.archive.get_text(
                    event.objective_artifact_id,
                    session_id=self.parent_runtime_session.runtime_session_id,
                )
            except Exception:
                objective = event.objective_preview
        self._tasks[event.task_id] = SubagentTask(
            task_id=event.task_id,
            batch_id=event.batch_id,
            create_tool_call_id=event.create_tool_call_id,
            task_key=event.task_key,
            label=event.label,
            profile_id=event.profile_id,
            display_role=event.display_role,
            objective=objective,
            objective_preview=event.objective_preview,
            status="created",
            depends_on=tuple(event.depends_on),
            current_run_id=None,
            has_child_run=False,
            phase=None,
            result_id=None,
            primary_result_artifact_id=None,
            created_at=created_at,
            updated_at=created_at,
            completed_at=None,
            metadata={"objective_artifact_id": event.objective_artifact_id},
        )

    def _bootstrap_task_started(self, event: SubagentTaskStartedEvent) -> None:
        task = self._tasks.get(event.task_id)
        if task is None:
            return
        self._tasks[event.task_id] = replace(
            task,
            status="running",
            current_run_id=event.subagent_run_id,
            has_child_run=True,
            updated_at=_parse_dt(event.created_at),
        )

    def _bootstrap_task_completed(self, event: SubagentTaskCompletedEvent) -> None:
        task = self._tasks.get(event.task_id)
        if task is None:
            return
        completed_at = _parse_dt(event.created_at)
        self._tasks[event.task_id] = replace(
            task,
            status="completed",
            current_run_id=event.subagent_run_id or task.current_run_id,
            has_child_run=event.subagent_run_id is not None or task.has_child_run,
            result_id=event.result_id,
            primary_result_artifact_id=event.primary_result_artifact_id,
            updated_at=completed_at,
            completed_at=completed_at,
        )

    def _update_task_status(
        self,
        task_id: str,
        status: str,
        updated_at: datetime,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        self._tasks[task_id] = replace(
            task,
            status=status,  # type: ignore[arg-type]
            updated_at=updated_at,
            completed_at=updated_at if status in {"completed", "failed", "cancelled"} else task.completed_at,
        )


def _default_capability_profile(budget: SubagentBudget) -> SubagentCapabilityProfile:
    return SubagentCapabilityProfile(
        profile_id=f"subagent_capability_profile:{uuid4().hex}",
        max_spawn_depth_from_root=budget.max_spawn_depth_from_root,
        allowed_tool_names=_CHILD_REPORT_TOOL_NAMES,
        allowed_descriptor_ids=_CHILD_REPORT_DESCRIPTOR_IDS,
    )


def _profile_tool_subset(inherited_names: set[str], allowed_core_names: frozenset[str]) -> set[str]:
    allowed = set(inherited_names) & allowed_core_names
    allowed.difference_update(_WRITE_TOOL_NAMES)
    if "terminal" not in allowed_core_names:
        allowed.difference_update(_TERMINAL_TOOL_NAMES)
    return allowed


def _mcp_tool_names_from_profile(profile: SubagentCapabilityProfile) -> set[str]:
    names: set[str] = set()
    for diagnostic in profile.diagnostics:
        if diagnostic.get("code") != "subagent_parent_snapshot_mcp_tools":
            continue
        tool_names = diagnostic.get("tool_names")
        if isinstance(tool_names, list):
            names.update(str(item) for item in tool_names if isinstance(item, str))
    return names


def _descriptor_allowed_for_child(descriptor: Any) -> bool:
    name = str(getattr(descriptor, "name", ""))
    category = str(getattr(descriptor, "permission_category", ""))
    provider_kind = getattr(getattr(descriptor, "provider_kind", None), "value", "")
    if name.startswith("memory_") or name.startswith("remember_"):
        return False
    if category.startswith("memory"):
        return False
    if category in {"subagent_runtime", "auth", "config", "admin"}:
        return False
    if provider_kind == "memory":
        return False
    return True


def _context_policy_from_event(payload: dict[str, object]) -> SubagentContextPolicy:
    mode = payload.get("mode")
    return SubagentContextPolicy(
        mode=mode if mode in {"isolated", "fork"} else "isolated",  # type: ignore[arg-type]
        include_parent_summary=bool(payload.get("include_parent_summary", False)),
        include_parent_current_task=bool(payload.get("include_parent_current_task", True)),
        include_parent_memory_projection=bool(payload.get("include_parent_memory_projection", False)),
        include_parent_artifact_refs=bool(payload.get("include_parent_artifact_refs", False)),
        max_parent_context_chars=(
            int(payload["max_parent_context_chars"])
            if isinstance(payload.get("max_parent_context_chars"), int)
            else None
        ),
        fork_source_context_id=(
            str(payload["fork_source_context_id"])
            if isinstance(payload.get("fork_source_context_id"), str)
            else None
        ),
    )


def _capability_profile_from_event(payload: dict[str, object]) -> SubagentCapabilityProfile:
    profile_id = payload.get("profile_id")
    profile_name = payload.get("profile_name")
    return SubagentCapabilityProfile(
        profile_id=str(profile_id) if isinstance(profile_id, str) and profile_id else (
            f"subagent_capability_profile:{uuid4().hex}"
        ),
        profile_name=profile_name if profile_name in {
            "general_worker",
            "research_worker",
            "review_worker",
            "verification_worker",
            "synthesizer",
            "orchestrator",
        } else "general_worker",  # type: ignore[arg-type]
        inherited_from_parent_context_id=(
            str(payload["inherited_from_parent_context_id"])
            if isinstance(payload.get("inherited_from_parent_context_id"), str)
            else None
        ),
        permission_mode=(
            str(payload["permission_mode"])
            if isinstance(payload.get("permission_mode"), str)
            else None
        ),
        permission_policy=(
            dict(payload["permission_policy"])
            if isinstance(payload.get("permission_policy"), dict)
            else {}
        ),
        allowed_tool_names=_str_tuple(payload.get("allowed_tool_names")),
        allowed_descriptor_ids=_str_tuple(payload.get("allowed_descriptor_ids")),
        allowed_skill_names=_str_tuple(payload.get("allowed_skill_names")),
        allowed_mcp_server_ids=_str_tuple(payload.get("allowed_mcp_server_ids")),
        can_spawn_subagents=bool(payload.get("can_spawn_subagents", False)),
        max_spawn_depth_from_root=(
            int(payload["max_spawn_depth_from_root"])
            if isinstance(payload.get("max_spawn_depth_from_root"), int)
            else 0
        ),
        memory_enabled=bool(payload.get("memory_enabled", False)),
        computed_from_parent_exposure_generation=(
            int(payload["computed_from_parent_exposure_generation"])
            if isinstance(payload.get("computed_from_parent_exposure_generation"), int)
            else None
        ),
        diagnostics=tuple(
            dict(item)
            for item in payload.get("diagnostics", ())
            if isinstance(item, dict)
        ) if isinstance(payload.get("diagnostics"), list | tuple) else (),
    )


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _optional_str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _spawn_event_context(run: SubagentRun) -> EventContext:
    return EventContext(
        run_id=run.parent_run_id,
        turn_id=run.parent_turn_id or f"turn:subagent-maintenance:{run.subagent_run_id}",
        reply_id=run.parent_reply_id or f"reply:subagent-maintenance:{run.subagent_run_id}",
    )


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _event_ref(event: Any) -> str:
    sequence = getattr(event, "sequence", None)
    if sequence is not None:
        return f"event_sequence:{sequence}"
    event_type = getattr(event, "type", None)
    return f"event:{event_type or 'unknown'}:{uuid4().hex}"


def _dependency_generation(terminal_event_ids: Mapping[str, str]) -> int | None:
    sequences: list[int] = []
    for event_ref in terminal_event_ids.values():
        if event_ref.startswith("event_sequence:"):
            try:
                sequences.append(int(event_ref.removeprefix("event_sequence:")))
            except ValueError:
                continue
            continue
        if event_ref.startswith("subagent_task_terminal:"):
            # Stable generation token for batch-local synthetic terminal refs.
            # This is not an event sequence and must not be used for event ordering.
            digest = hashlib.sha1(event_ref.encode("utf-8")).hexdigest()  # noqa: S324 - stable non-security id.
            sequences.append(int(digest[:12], 16))
    return max(sequences) if sequences else None


def _clip(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 1)] + "…"
