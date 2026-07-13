"""Pulsara-owned subagent runtime skeleton.

This module implements the PR0/PR1 substrate: typed parent graph events,
child-runtime identity creation, fake child completion, consumption edges, and
basic lifecycle/cap enforcement.  Real child ``AgentRuntime`` wiring lives above
this boundary and should call the same methods rather than writing graph facts
directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any, Awaitable, Mapping
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallStartEvent,
    ModelCallEndEvent,
    RunEndEvent,
    RunStartEvent,
    SubagentEdgeRecordedEvent,
    SubagentMessageSentEvent,
    SubagentPhaseReportedEvent,
    SubagentResultConsumedEvent,
    SubagentResultDeliveredEvent,
    SubagentResultSubmittedEvent,
    SubagentRunCancelledEvent,
    SubagentRunCompletedEvent,
    SubagentRunFailedEvent,
    SubagentRunSuspendedEvent,
    SubagentRunStartedEvent,
    SubagentTaskBlockedEvent,
    SubagentTaskCancelledEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskCreatedEvent,
    SubagentTaskFailedEvent,
    SubagentTaskScheduledEvent,
    SubagentTaskStartedEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.model_call import ModelTokenUsageFact
from pulsara_agent.primitives.subagent import (
    ChildExplicitResultEvidenceFact,
    ChildNativeTerminalReferenceFact,
    ChildResultHandoffFact,
    build_child_result_handoff,
    build_child_result_render_policy,
    deterministic_child_result_artifact_id,
    deterministic_child_result_id,
    deterministic_parent_subagent_terminal_event_id,
    validate_child_render_policy_against_budget,
)
from pulsara_agent.message import TextBlock
from pulsara_agent.message.assembler import BlockAssembler
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.mcp.types import McpBindingIdentity
from pulsara_agent.runtime.session import EventWriteConflict, RuntimeSession
from pulsara_agent.runtime.execution_handles import BoundaryExecutionHandles
from pulsara_agent.runtime.subagent.projection import (
    EventLogLocator,
    InMemoryEventLogLocator,
    project_subagent_graph,
)
from pulsara_agent.runtime.subagent.hydration import (
    HydratedSubagentRunView,
    HydratedSubagentTaskView,
    SubagentGraphHydrator,
)
from pulsara_agent.runtime.subagent.immutable import thaw_json_mapping
from pulsara_agent.runtime.subagent.reducer import pending_subagent_result_ids
from pulsara_agent.runtime.subagent.facts import (
    SubagentGraphState,
    SubagentResultFact,
    SubagentRunFact,
    SubagentTaskFact,
    subagent_dependency_generation,
)
from pulsara_agent.runtime.subagent.execution import ChildExecutionRegistry
from pulsara_agent.runtime.subagent.commands import (
    PlannedChildReservation,
    PlannedSubagentWrite,
    SubagentCommandPlanner,
)
from pulsara_agent.runtime.subagent.store import SubagentGraphStateStore
from pulsara_agent.runtime.subagent.run_entry import (
    SubagentRunEntryCommitUntrusted,
)
from pulsara_agent.runtime.subagent.types import (
    SubagentBudget,
    SubagentCapabilityProfile,
    SubagentContextPolicy,
    SubagentGraphProjection,
    SubagentResult,
    SubagentRole,
    SubagentRunTerminalOutcome,
    SubagentStatus,
)
from pulsara_agent.primitives.run_lifecycle import (
    RunStopReason,
    RunTerminalizationKind,
)


_ACTIVE_STATUSES: set[SubagentStatus] = {"running", "suspended"}
_TERMINAL_STATUSES: set[SubagentStatus] = {"completed", "failed", "cancelled"}
_CHILD_REPORT_TOOL_NAMES = ("report_agent_phase", "report_agent_result")
_CHILD_REPORT_DESCRIPTOR_IDS = (
    "workflow:report_agent_phase",
    "workflow:report_agent_result",
)
_READ_ONLY_WORKER_TOOL_NAMES = frozenset({"read_file", "search_files", "artifact_read"})
_VERIFICATION_WORKER_TOOL_NAMES = frozenset(
    {"read_file", "search_files", "artifact_read", "terminal", "terminal_process"}
)
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
SubagentChildRunner = Callable[
    ["SubagentRuntime", HydratedSubagentRunView], Awaitable[None]
]


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
        if hasattr(self.event_log_locator, "register"):
            self.event_log_locator.register(  # type: ignore[attr-defined]
                parent_runtime_session.runtime_session_id,
                parent_runtime_session.event_log,
            )
        self.default_budget = default_budget or SubagentBudget()
        self._child_runner = child_runner
        self._execution_registry = ChildExecutionRegistry()
        self._command_planner = SubagentCommandPlanner()
        self._parent_capability_snapshot: SubagentCapabilityProfile | None = None
        initial_events = self.parent_runtime_session.event_log.iter()
        self._graph_store = SubagentGraphStateStore(initial_events)
        self._graph_reducer_id = (
            f"subagent_graph:{self.parent_runtime_session.runtime_session_id}"
        )
        self.parent_runtime_session.register_committed_reducer(
            reducer_id=self._graph_reducer_id,
            through_sequence=self._graph_store.through_sequence,
            apply_committed=self._graph_store.apply_committed,
            rebuild_committed=self._graph_store.rebuild,
        )
        self._hydrator = SubagentGraphHydrator(
            archive=self.parent_runtime_session.archive,
            parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
            event_log_locator=self.event_log_locator,
        )

    def bind_child_runner(self, child_runner: SubagentChildRunner | None) -> None:
        """Bind the runner used for future child starts.

        A ``RuntimeSession`` owns one durable subagent graph runtime.  A new
        parent ``AgentRuntime`` may be constructed for a later turn against the
        same session, so the graph owner is reused while its execution adapter
        is rebound.  Already-running child coroutines retain the runner they
        started with.
        """

        self._child_runner = child_runner

    def detach_from_parent_session(self) -> None:
        """Detach the live reducer registration during parent session teardown."""

        self.parent_runtime_session.unregister_committed_reducer(self._graph_reducer_id)
        if self.parent_runtime_session.subagent_runtime is self:
            self.parent_runtime_session.subagent_runtime = None

    @property
    def runs(self) -> tuple[SubagentRunFact, ...]:
        return tuple(self._graph_store.state.runs.values())

    @property
    def tasks(self) -> tuple[SubagentTaskFact, ...]:
        return tuple(self._graph_store.state.tasks.values())

    def result_for_run(self, subagent_run_id: str) -> SubagentResult | None:
        """Return the canonical completed result derived from reducer facts."""

        return _completed_result_for_run(self._graph_store.state, subagent_run_id)

    def terminal_outcome_for_run(
        self,
        subagent_run_id: str,
    ) -> SubagentRunTerminalOutcome | None:
        fact = self._graph_store.state.runs.get(subagent_run_id)
        if fact is None:
            raise SubagentNotFound(subagent_run_id)
        if fact.status not in {"failed", "cancelled"}:
            return None
        terminal_event_id = fact.provenance.terminal_event_id
        reason_code = (
            fact.failure_reason_code
            if fact.status == "failed"
            else fact.cancellation_reason_code
        )
        if terminal_event_id is None or reason_code is None:
            raise SubagentRuntimeError(
                f"Terminal subagent run is missing provenance: {subagent_run_id}"
            )
        return SubagentRunTerminalOutcome(
            subagent_run_id=subagent_run_id,
            status=fact.status,
            reason_code=reason_code,
            terminal_event_id=terminal_event_id,
            task_id=fact.task_id,
        )

    @property
    def child_sessions(self) -> tuple[RuntimeSession, ...]:
        return tuple(
            handle.child_session
            for handle in self._execution_registry.handles()
            if handle.child_session is not None
        )

    def attach_child_execution_handles(
        self,
        subagent_run_id: str,
        execution_handles: BoundaryExecutionHandles,
    ) -> None:
        self._execution_registry.attach_execution_handles(
            subagent_run_id,
            execution_handles,
        )

    async def _commit_plan(self, plan: PlannedSubagentWrite) -> tuple[AgentEvent, ...]:
        plan = self._command_planner.validate(plan, state=self._graph_store.state)
        result = await self.parent_runtime_session.write_events(
            plan.events,
            expected_last_sequence=plan.expected_through_sequence,
        )
        result.require_reduced(self._graph_reducer_id)
        return result.committed_events

    def _commit_plan_from_thread(
        self, plan: PlannedSubagentWrite
    ) -> tuple[AgentEvent, ...]:
        plan = self._command_planner.validate(plan, state=self._graph_store.state)
        result = self.parent_runtime_session.write_events_from_thread(
            plan.events,
            expected_last_sequence=plan.expected_through_sequence,
        )
        result.require_reduced(self._graph_reducer_id)
        return result.committed_events

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
            provider_kind = getattr(
                getattr(descriptor, "provider_kind", None), "value", None
            )
            if (
                provider_kind == "mcp"
                and isinstance(metadata, dict)
                and isinstance(metadata.get("server_id"), str)
            ):
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
            computed_from_parent_exposure_generation=getattr(
                exposure, "registry_generation", None
            ),
            diagnostics=(
                {
                    "code": "subagent_parent_snapshot_mcp_tools",
                    "tool_names": list(dict.fromkeys(mcp_tool_names)),
                },
            )
            if mcp_tool_names
            else (),
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
    ) -> SubagentTaskFact:
        if not objective.strip():
            raise ValueError("objective is required")
        task_id = task_id or f"subagent_task:{uuid4().hex}"
        state = self._graph_store.state
        if task_id in state.tasks:
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
        event = SubagentTaskCreatedEvent(
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
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="create_task",
                expected_through_sequence=state.through_sequence,
                events=(event,),
                batch_id=batch_id,
                create_tool_call_id=create_tool_call_id,
            )
        )
        return self._require_task(task_id)

    async def block_task(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        status: str,
        blocked_reason: str,
        blocked_by_task_ids: tuple[str, ...],
        dependency_terminal_event_ids: Mapping[str, str] | None = None,
    ) -> SubagentTaskFact:
        self._require_task(task_id)
        state = self._graph_store.state
        if status not in {"waiting_dependency", "blocked_dependency_failed"}:
            raise ValueError(
                "status must be waiting_dependency or blocked_dependency_failed"
            )
        terminal_event_ids = self._dependency_terminal_event_ids(
            blocked_by_task_ids,
            overrides=dependency_terminal_event_ids,
        )
        event = SubagentTaskBlockedEvent(
            **event_context.event_fields(),
            task_id=task_id,
            status=status,  # type: ignore[arg-type]
            blocked_reason=blocked_reason,  # type: ignore[arg-type]
            blocked_by_task_ids=list(blocked_by_task_ids),
            dependency_status_snapshot={
                dependency_id: state.tasks[dependency_id].status
                for dependency_id in blocked_by_task_ids
                if dependency_id in state.tasks
            },
            dependency_terminal_event_ids=terminal_event_ids,
            dependency_generation=subagent_dependency_generation(terminal_event_ids),
        )
        events: tuple[AgentEvent, ...] = (event,)
        if status == "blocked_dependency_failed":
            events += _plan_dependency_failure_cascade(
                state,
                root_task_id=task_id,
                root_status="blocked_dependency_failed",
                root_terminal_event_id=event.id,
                event_context=event_context,
            )
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="block_task",
                expected_through_sequence=state.through_sequence,
                events=events,
            )
        )
        return self._require_task(task_id)

    async def materialize_task_batch(
        self,
        plans: tuple[Mapping[str, object], ...],
        *,
        event_context: EventContext,
        parent_context_id: str | None = None,
        parent_model_call_index: int | None = None,
        spawn_initiator_id: str | None = None,
    ) -> tuple[tuple[SubagentTaskFact, ...], tuple[SubagentRunFact, ...]]:
        """Atomically write task batch facts, then start runnable child attempts."""

        if not plans:
            raise ValueError("plans must be non-empty")

        state = self._graph_store.state

        events: list[Any] = []
        task_infos: list[dict[str, Any]] = []
        run_infos: list[dict[str, Any]] = []
        planned_status_by_task_id = {
            str(plan["task_id"]): str(plan.get("initial_status") or "start")
            for plan in plans
        }
        planned_block_terminal_refs = {
            str(plan["task_id"]): f"event:subagent_task_blocked:{uuid4().hex}"
            for plan in plans
            if str(plan.get("initial_status") or "") == "blocked_dependency_failed"
        }

        for plan in plans:
            objective = str(plan["objective"])
            if not objective.strip():
                raise ValueError("objective is required")
            task_id = str(plan["task_id"])
            if task_id in state.tasks:
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
                blocked_event_kwargs: dict[str, object] = {}
                if initial_status == "blocked_dependency_failed":
                    blocked_event_kwargs["id"] = planned_block_terminal_refs[task_id]
                events.append(
                    SubagentTaskBlockedEvent(
                        **event_context.event_fields(),
                        **blocked_event_kwargs,
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
                        dependency_generation=subagent_dependency_generation(
                            terminal_event_ids
                        ),
                    )
                )
                continue

            capability_profile = self._capability_profile_for_name(profile_id)
            context_policy = SubagentContextPolicy()
            budget = self.default_budget
            subagent_run_id = f"subagent_run:{uuid4().hex}"
            child_runtime_session_id = f"runtime:subagent:{uuid4().hex}"
            edge_id = f"subagent_edge:{subagent_run_id}:spawn"
            # A task-backed run reuses the task objective artifact.  Writing a
            # second run-owned copy would create two competing full-text facts
            # for the same logical objective.
            task_artifact_id = objective_artifact_id
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
                    "spawn_initiator_id": spawn_initiator_id
                    or create_tool_call_id
                    or task_id,
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
                    spawning_tool_name="create_agent_tasks",
                    spawn_initiator_kind="tool_call",
                    spawn_initiator_id=spawn_initiator_id
                    or create_tool_call_id
                    or task_id,
                    child_runtime_session_id=child_runtime_session_id,
                    label=label,
                    role="worker",
                    profile_id=profile_id,
                    task_preview=objective_preview,
                    context_policy=context_policy.to_event_value(),
                    capability_profile=capability_profile.to_event_value(),
                    budget_snapshot=budget.to_event_value(),
                )
            )
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
            events.append(
                SubagentTaskStartedEvent(
                    **event_context.event_fields(),
                    task_id=task_id,
                    subagent_run_id=subagent_run_id,
                    batch_id=batch_id,
                    create_tool_call_id=create_tool_call_id,
                    run_index=1,
                    spawn_initiator_kind="tool_call",
                    spawn_initiator_id=spawn_initiator_id
                    or create_tool_call_id
                    or task_id,
                )
            )

        reservation = None
        if run_infos:
            self.validate_can_start_batch(event_context.run_id, count=len(run_infos))
            reservation = self._execution_registry.reserve(
                parent_run_id=event_context.run_id,
                count=len(run_infos),
            )
        try:
            await self._commit_plan(
                PlannedSubagentWrite(
                    operation="materialize_task_batch",
                    expected_through_sequence=state.through_sequence,
                    events=tuple(events),
                    batch_id=_single_optional_value(task_infos, "batch_id"),
                    create_tool_call_id=_single_optional_value(
                        task_infos,
                        "create_tool_call_id",
                    ),
                    required_reservations=(
                        (
                            PlannedChildReservation(
                                reservation_id=reservation.reservation_id,
                                parent_run_id=reservation.parent_run_id,
                                count=reservation.count,
                            ),
                        )
                        if reservation is not None
                        else ()
                    ),
                )
            )
        except Exception:
            if reservation is not None:
                self._execution_registry.release_reservation(reservation)
            raise

        runs: list[SubagentRunFact] = []
        run_views: list[HydratedSubagentRunView] = []
        try:
            for info in run_infos:
                subagent_run_id = str(info["subagent_run_id"])
                child_runtime_session_id = str(info["child_runtime_session_id"])
                child_runtime = self._create_child_runtime_session(
                    child_runtime_session_id=child_runtime_session_id,
                    subagent_run_id=subagent_run_id,
                    parent_run_id=event_context.run_id,
                    capability_profile_id=info["capability_profile"].profile_id,
                )
                child_runtime.subagent_runtime = self
                self._execution_registry.register_prepared(
                    subagent_run_id=subagent_run_id,
                    child_runtime_session_id=child_runtime_session_id,
                    child_session=child_runtime,
                    reservation=reservation,
                    mcp_binding_identities=_mcp_binding_identities(
                        child_runtime,
                        allowed_tool_names=info[
                            "capability_profile"
                        ].allowed_tool_names,
                    ),
                )
                run = self._require_run(subagent_run_id)
                run_view = HydratedSubagentRunView(
                    fact=run,
                    task_text=str(info["objective"]),
                    task_text_complete=True,
                    child_run_id=run.reported_child_run_id,
                    child_terminal_status=None,
                )
                runs.append(run)
                run_views.append(run_view)
            if self._child_runner is not None:
                for run_view in run_views:
                    self._execution_registry.attach_coroutine(
                        run_view.fact.subagent_run_id,
                        asyncio.create_task(self._run_child(run_view)),
                    )
        except Exception:
            if reservation is not None:
                # Release only slots that never attached. Attached closing
                # handles keep their physical capacity until coroutine exit.
                self._execution_registry.release_reservation(reservation)
            await self.repair_materialized_batch(
                _required_single_batch_id(task_infos),
                event_context=event_context,
                repair_id=f"subagent_repair:{uuid4().hex}",
                reason_code="subagent_task_batch_start_failed",
                reason_message="A post-commit child start step failed; the materialized batch was cancelled.",
            )
            raise

        return (
            tuple(self._require_task(str(info["task_id"])) for info in task_infos),
            tuple(runs),
        )

    async def start_task(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        parent_context_id: str | None = None,
        parent_model_call_index: int | None = None,
        spawn_initiator_kind: str = "tool_call",
        spawn_initiator_id: str | None = None,
    ) -> SubagentRunFact:
        task = self._require_task(task_id)
        task_view = await self._hydrate_task_objective(task)
        if task.has_child_run:
            raise SubagentRuntimeError(f"Task already has a child run: {task_id}")
        if task.status not in {"created", "waiting_dependency"}:
            raise SubagentRuntimeError(
                f"Task cannot be started from status {task.status}: {task_id}"
            )
        state = self._graph_store.state
        self.validate_can_start_batch(event_context.run_id, count=1)
        reservation = self._execution_registry.reserve(
            parent_run_id=event_context.run_id,
            count=1,
        )
        capability_profile = self._capability_profile_for_name(task.profile_id)
        context_policy = SubagentContextPolicy()
        budget = self.default_budget
        subagent_run_id = f"subagent_run:{uuid4().hex}"
        child_runtime_session_id = f"runtime:subagent:{uuid4().hex}"
        edge_id = f"subagent_edge:{subagent_run_id}:spawn"
        initiator_id = spawn_initiator_id or task.create_tool_call_id or task.task_id
        objective_artifact_id = task.objective_artifact_id
        events: tuple[AgentEvent, ...] = (
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
            ),
            SubagentRunStartedEvent(
                **event_context.event_fields(),
                subagent_run_id=subagent_run_id,
                task_id=task.task_id,
                batch_id=task.batch_id,
                create_tool_call_id=task.create_tool_call_id,
                run_index=1,
                edge_id=edge_id,
                parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
                parent_run_id=event_context.run_id,
                parent_turn_id=event_context.turn_id,
                parent_reply_id=event_context.reply_id,
                parent_context_id=parent_context_id,
                parent_model_call_index=parent_model_call_index,
                spawning_tool_name=(
                    "create_agent_tasks"
                    if spawn_initiator_kind == "tool_call"
                    else None
                ),
                spawn_initiator_kind=spawn_initiator_kind,  # type: ignore[arg-type]
                spawn_initiator_id=initiator_id,
                child_runtime_session_id=child_runtime_session_id,
                label=task.label,
                role="worker",
                profile_id=task.profile_id,
                task_preview=task.objective_preview,
                context_policy=context_policy.to_event_value(),
                capability_profile=capability_profile.to_event_value(),
                budget_snapshot=budget.to_event_value(),
            ),
            SubagentMessageSentEvent(
                **event_context.event_fields(),
                edge_id=edge_id,
                subagent_run_id=subagent_run_id,
                parent_runtime_session_id=self.parent_runtime_session.runtime_session_id,
                parent_run_id=event_context.run_id,
                child_runtime_session_id=child_runtime_session_id,
                message_artifact_id=objective_artifact_id,
                message_preview=task.objective_preview,
                delivery_kind="spawn_task",
            ),
            SubagentTaskStartedEvent(
                **event_context.event_fields(),
                task_id=task_id,
                subagent_run_id=subagent_run_id,
                batch_id=task.batch_id,
                create_tool_call_id=task.create_tool_call_id,
                run_index=1,
                spawn_initiator_kind=spawn_initiator_kind,  # type: ignore[arg-type]
                spawn_initiator_id=initiator_id,
            ),
        )
        try:
            await self._commit_plan(
                PlannedSubagentWrite(
                    operation="start_task",
                    expected_through_sequence=state.through_sequence,
                    events=events,
                    batch_id=task.batch_id,
                    create_tool_call_id=task.create_tool_call_id,
                    required_reservations=(
                        PlannedChildReservation(
                            reservation_id=reservation.reservation_id,
                            parent_run_id=reservation.parent_run_id,
                            count=reservation.count,
                        ),
                    ),
                )
            )
        except Exception:
            self._execution_registry.release_reservation(reservation)
            raise
        try:
            child_runtime = self._create_child_runtime_session(
                child_runtime_session_id=child_runtime_session_id,
                subagent_run_id=subagent_run_id,
                parent_run_id=event_context.run_id,
                capability_profile_id=capability_profile.profile_id,
            )
            child_runtime.subagent_runtime = self
            self._execution_registry.register_prepared(
                subagent_run_id=subagent_run_id,
                child_runtime_session_id=child_runtime_session_id,
                child_session=child_runtime,
                reservation=reservation,
                mcp_binding_identities=_mcp_binding_identities(
                    child_runtime,
                    allowed_tool_names=capability_profile.allowed_tool_names,
                ),
            )
            run = self._require_run(subagent_run_id)
            run_view = HydratedSubagentRunView(
                fact=run,
                task_text=task_view.objective_text,
                task_text_complete=True,
                child_run_id=run.reported_child_run_id,
                child_terminal_status=None,
            )
            if self._child_runner is not None:
                self._execution_registry.attach_coroutine(
                    subagent_run_id,
                    asyncio.create_task(self._run_child(run_view)),
                )
        except Exception as exc:
            self._execution_registry.release_reservation(reservation)
            await self.fail(
                subagent_run_id,
                event_context=event_context,
                reason_code="subagent_child_start_failed",
                reason_message="The committed child run could not be started in this process.",
                diagnostics=[{"error_type": type(exc).__name__}],
                repair_id=f"subagent_repair:{uuid4().hex}",
            )
            raise
        return run

    async def _hydrate_task_objective(
        self,
        task: SubagentTaskFact,
    ) -> HydratedSubagentTaskView:
        view = await self._hydrator.hydrate_task(task, max_chars=200_000)
        if not view.objective_text_complete or view.objective_text is None:
            codes = ",".join(item.code for item in view.diagnostics) or "unknown"
            raise SubagentRuntimeError(
                f"Task objective artifact is unavailable or incomplete: {task.task_id} ({codes})"
            )
        return view

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
        spawning_tool_name: str | None = None,
        task_id: str | None = None,
        batch_id: str | None = None,
        create_tool_call_id: str | None = None,
        run_index: int | None = None,
        spawn_initiator_kind: str | None = None,
        spawn_initiator_id: str | None = None,
        profile_id: str | None = None,
        task_artifact_id: str | None = None,
    ) -> SubagentRunFact:
        budget = budget or self.default_budget
        capability_profile = capability_profile or self._default_capability_profile(
            budget
        )
        context_policy = context_policy or SubagentContextPolicy()
        if (
            context_policy.mode == "fork"
            and context_policy.fork_source_context_id is None
        ):
            if parent_context_id is None:
                raise SubagentRuntimeError(
                    "fork context policy requires parent_context_id attribution"
                )
            context_policy = replace(
                context_policy,
                fork_source_context_id=parent_context_id,
            )
        state = self._graph_store.state
        self._enforce_spawn_limits(event_context.run_id, budget)
        reservation = self._execution_registry.reserve(
            parent_run_id=event_context.run_id,
            count=1,
        )

        subagent_run_id = f"subagent_run:{uuid4().hex}"
        child_runtime_session_id = f"runtime:subagent:{uuid4().hex}"
        edge_id = f"subagent_edge:{subagent_run_id}:spawn"
        try:
            if task_artifact_id is None:
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
        except Exception:
            self._execution_registry.release_reservation(reservation)
            raise
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
            budget_snapshot=budget.to_event_value(),
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
        try:
            await self._commit_plan(
                PlannedSubagentWrite(
                    operation="spawn_agent",
                    expected_through_sequence=state.through_sequence,
                    events=(started, message),
                    batch_id=batch_id,
                    create_tool_call_id=create_tool_call_id,
                    required_reservations=(
                        PlannedChildReservation(
                            reservation_id=reservation.reservation_id,
                            parent_run_id=reservation.parent_run_id,
                            count=reservation.count,
                        ),
                    ),
                )
            )
        except Exception:
            self._execution_registry.release_reservation(reservation)
            raise
        try:
            child_runtime = self._create_child_runtime_session(
                child_runtime_session_id=child_runtime_session_id,
                subagent_run_id=subagent_run_id,
                parent_run_id=event_context.run_id,
                capability_profile_id=capability_profile.profile_id,
            )
            child_runtime.subagent_runtime = self
            self._execution_registry.register_prepared(
                subagent_run_id=subagent_run_id,
                child_runtime_session_id=child_runtime_session_id,
                child_session=child_runtime,
                reservation=reservation,
                mcp_binding_identities=_mcp_binding_identities(
                    child_runtime,
                    allowed_tool_names=capability_profile.allowed_tool_names,
                ),
            )
        except Exception as exc:
            self._execution_registry.release_reservation(reservation)
            await self.fail(
                subagent_run_id,
                event_context=event_context,
                reason_code="subagent_child_start_failed",
                reason_message="The committed child run could not be started in this process.",
                diagnostics=[{"error_type": type(exc).__name__}],
                repair_id=f"subagent_repair:{uuid4().hex}",
            )
            raise
        return self._require_run(subagent_run_id)

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
        spawning_tool_name: str | None = None,
        task_id: str | None = None,
        batch_id: str | None = None,
        create_tool_call_id: str | None = None,
        run_index: int | None = None,
        spawn_initiator_kind: str | None = None,
        spawn_initiator_id: str | None = None,
        profile_id: str | None = None,
        task_artifact_id: str | None = None,
    ) -> SubagentRunFact:
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
            spawning_tool_name=spawning_tool_name,
            task_id=task_id,
            batch_id=batch_id,
            create_tool_call_id=create_tool_call_id,
            run_index=run_index,
            spawn_initiator_kind=spawn_initiator_kind,
            spawn_initiator_id=spawn_initiator_id,
            profile_id=profile_id,
            task_artifact_id=task_artifact_id,
        )
        if self._child_runner is not None:
            run_view = HydratedSubagentRunView(
                fact=run,
                task_text=task,
                task_text_complete=True,
                child_run_id=run.reported_child_run_id,
                child_terminal_status=None,
            )
            self._execution_registry.attach_coroutine(
                run.subagent_run_id,
                asyncio.create_task(self._run_child(run_view)),
            )
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
        child_run_id: str | None = None,
    ) -> SubagentResult:
        state = self._graph_store.state
        run = self._require_run(subagent_run_id)
        if run.status in _TERMINAL_STATUSES:
            existing = _completed_result_for_run(state, subagent_run_id)
            if existing is not None:
                return existing
            raise SubagentRuntimeError(f"Run is already terminal: {subagent_run_id}")
        if _result_for_run(state, subagent_run_id, status="submitted") is not None:
            return await self._complete_submitted_result(
                subagent_run_id,
                event_context=event_context,
                token_usage=token_usage,
                tool_call_count=tool_call_count,
                child_run_id=child_run_id,
                allow_synthetic=True,
            )
        ctx = event_context or _spawn_event_context(run)
        summary = _clip(summary, run.budget.max_result_summary_chars_per_child)
        resolved_child_run_id = (
            child_run_id
            or run.child_run_id
            or (f"run:synthetic-child:{subagent_run_id}")
        )
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
        handoff = self._build_result_handoff(
            run=run,
            child_run_id=resolved_child_run_id,
            handoff_kind="inferred",
            result_id=result_id,
            summary=summary,
            result_artifact_id=result_artifact_id,
            artifact_ids=tuple(sorted({result_artifact_id, *artifact_ids})),
            token_usage=token_usage,
            tool_call_count=tool_call_count,
            submitted_event=None,
            allow_synthetic=True,
        )
        completed = SubagentRunCompletedEvent(
            **ctx.event_fields(),
            subagent_run_id=subagent_run_id,
            parent_runtime_session_id=run.parent_runtime_session_id,
            child_runtime_session_id=run.child_runtime_session_id,
            child_run_id=resolved_child_run_id,
            result_id=result_id,
            summary=summary,
            result_artifact_id=result_artifact_id,
            artifact_ids=list(handoff.artifact_ids),
            token_usage=token_usage,
            tool_call_count=tool_call_count,
            result_handoff=handoff,
        )
        events: list[AgentEvent] = [completed]
        if run.task_id is not None:
            events.append(
                SubagentTaskCompletedEvent(
                    **ctx.event_fields(),
                    task_id=run.task_id,
                    subagent_run_id=subagent_run_id,
                    result_id=result_id,
                    primary_result_artifact_id=result_artifact_id,
                    result_source="inferred",
                )
            )
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="complete_run",
                expected_through_sequence=state.through_sequence,
                events=tuple(events),
                batch_id=run.batch_id,
                create_tool_call_id=run.create_tool_call_id,
            )
        )
        self._execution_registry.release_handle(subagent_run_id)
        if run.task_id is not None:
            await self._schedule_dependents_after_completion(
                run.task_id, event_context=ctx
            )
        result_fact = self._graph_store.state.results[result_id]
        return _legacy_result_from_fact(result_fact)

    async def complete_native_result(
        self,
        subagent_run_id: str,
        *,
        child_run_id: str,
    ) -> SubagentResult:
        """Fold a normally terminated child ledger into deterministic parent facts.

        This is the production inferred-result path.  It intentionally ignores
        process-local ``AgentRunResult`` text and counters: normal execution and
        restart repair must derive the exact same payload from durable child
        events plus the render policy frozen in child ``RunStart``.
        """

        state = self._graph_store.state
        run = self._require_run(subagent_run_id)
        if run.status in _TERMINAL_STATUSES:
            existing = _completed_result_for_run(state, subagent_run_id)
            if existing is not None:
                return existing
            raise SubagentRuntimeError(f"Run is already terminal: {subagent_run_id}")

        child_events, child_start, child_terminal = self._require_child_terminal(
            run=run,
            child_run_id=child_run_id,
        )
        if child_terminal.terminalization_kind != "normal":
            raise SubagentRuntimeError(
                "inferred child result requires a normal child terminal"
            )
        assert child_start.subagent_run_entry is not None
        policy = child_start.subagent_run_entry.child_result_render_policy
        validate_child_render_policy_against_budget(policy, run.budget_snapshot)
        if policy.max_artifact_refs < 1:
            raise SubagentRuntimeError(
                "child result render policy must reserve one primary artifact ref"
            )

        rendered_text = _final_child_assistant_text(
            child_events,
            terminal=child_terminal,
        )
        summary = _clip(
            rendered_text.strip() or "(child agent finished without final text)",
            policy.max_summary_chars,
        )
        result_id = deterministic_child_result_id(
            subagent_run_id=subagent_run_id,
            terminal_event_id=child_terminal.id,
            policy_fingerprint=policy.policy_fingerprint,
        )
        result_artifact_id = deterministic_child_result_artifact_id(
            subagent_run_id=subagent_run_id,
            terminal_event_id=child_terminal.id,
            policy_fingerprint=policy.policy_fingerprint,
        )
        semantic_metadata = {
            "artifact_kind": "subagent_result",
            "subagent_run_id": subagent_run_id,
            "result_id": result_id,
            "child_runtime_session_id": run.child_runtime_session_id,
            "child_terminal_event_id": child_terminal.id,
            "renderer_version": policy.renderer_version,
            "render_policy_fingerprint": policy.policy_fingerprint,
            "max_summary_chars": policy.max_summary_chars,
            "max_artifact_refs": policy.max_artifact_refs,
            "result_source": "inferred",
        }
        self.parent_runtime_session.archive.put_text_if_absent_or_confirm_identical(
            result_artifact_id,
            rendered_text or summary,
            session_id=self.parent_runtime_session.runtime_session_id,
            run_id=run.parent_run_id,
            media_type="text/markdown",
            semantic_metadata=semantic_metadata,
        )
        handoff = self._build_result_handoff(
            run=run,
            child_run_id=child_run_id,
            handoff_kind="inferred",
            result_id=result_id,
            summary=summary,
            result_artifact_id=result_artifact_id,
            artifact_ids=(result_artifact_id,),
            token_usage=None,
            tool_call_count=None,
            submitted_event=None,
        )
        token_usage = (
            handoff.token_usage.model_dump(mode="json")
            if handoff.token_usage is not None
            else None
        )
        parent_terminal_event_id = deterministic_parent_subagent_terminal_event_id(
            parent_runtime_session_id=run.parent_runtime_session_id,
            subagent_run_id=subagent_run_id,
            child_terminal_event_id=child_terminal.id,
            parent_terminal_event_type="subagent_run_completed",
        )
        ctx = _spawn_event_context(run)
        completed = SubagentRunCompletedEvent(
            id=parent_terminal_event_id,
            created_at=child_terminal.created_at,
            **ctx.event_fields(),
            subagent_run_id=subagent_run_id,
            parent_runtime_session_id=run.parent_runtime_session_id,
            child_runtime_session_id=run.child_runtime_session_id,
            child_run_id=child_run_id,
            result_id=result_id,
            summary=summary,
            result_artifact_id=result_artifact_id,
            artifact_ids=list(handoff.artifact_ids),
            token_usage=token_usage,
            tool_call_count=handoff.tool_call_count,
            result_handoff=handoff,
        )
        events: list[AgentEvent] = [completed]
        if run.task_id is not None:
            events.append(
                SubagentTaskCompletedEvent(
                    id=deterministic_parent_subagent_terminal_event_id(
                        parent_runtime_session_id=run.parent_runtime_session_id,
                        subagent_run_id=subagent_run_id,
                        child_terminal_event_id=child_terminal.id,
                        parent_terminal_event_type="subagent_task_completed",
                    ),
                    created_at=child_terminal.created_at,
                    **ctx.event_fields(),
                    task_id=run.task_id,
                    subagent_run_id=subagent_run_id,
                    result_id=result_id,
                    primary_result_artifact_id=result_artifact_id,
                    result_source="inferred",
                )
            )
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="complete_native_result",
                expected_through_sequence=state.through_sequence,
                events=tuple(events),
                batch_id=run.batch_id,
                create_tool_call_id=run.create_tool_call_id,
            )
        )
        self._execution_registry.release_handle(subagent_run_id)
        if run.task_id is not None:
            await self._schedule_dependents_after_completion(
                run.task_id, event_context=ctx
            )
        return _legacy_result_from_fact(self._graph_store.state.results[result_id])

    # Compatibility/test seam.  Production child execution calls
    # ``complete_native_result`` or ``complete_submitted_result`` explicitly.
    complete = complete_fake

    def _require_child_terminal(
        self,
        *,
        run: SubagentRunFact,
        child_run_id: str,
    ) -> tuple[tuple[AgentEvent, ...], RunStartEvent, RunEndEvent]:
        child_events = tuple(
            self.event_log_locator.event_log_for_runtime_session(
                run.child_runtime_session_id
            ).iter(run_id=child_run_id)
        )
        starts = [event for event in child_events if isinstance(event, RunStartEvent)]
        terminals = [event for event in child_events if isinstance(event, RunEndEvent)]
        if len(starts) != 1 or len(terminals) != 1:
            raise SubagentRuntimeError(
                "child result handoff requires exactly one child RunStart/RunEnd"
            )
        start = starts[0]
        terminal = terminals[0]
        if start.subagent_run_entry is None or terminal.sequence is None:
            raise SubagentRuntimeError(
                "child result handoff requires typed sequenced child RunStart/RunEnd"
            )
        if start.subagent_run_entry.subagent_run_id != run.subagent_run_id:
            raise SubagentRuntimeError("child RunStart subagent attribution mismatch")
        if terminal.id != start.terminal_run_end_event_id:
            raise SubagentRuntimeError("child terminal event identity mismatch")
        return child_events, start, terminal

    def _build_result_handoff(
        self,
        *,
        run: SubagentRunFact,
        child_run_id: str,
        handoff_kind: str,
        result_id: str,
        summary: str,
        result_artifact_id: str,
        artifact_ids: tuple[str, ...],
        token_usage: dict[str, object] | None,
        tool_call_count: int | None,
        submitted_event: SubagentResultSubmittedEvent | None,
        allow_synthetic: bool = False,
    ) -> ChildResultHandoffFact:
        try:
            child_events, start, terminal = self._require_child_terminal(
                run=run,
                child_run_id=child_run_id,
            )
        except SubagentRuntimeError:
            if not allow_synthetic:
                raise
            child_events = ()
            start = None
            terminal = None
        if start is not None and terminal is not None:
            policy = start.subagent_run_entry.child_result_render_policy
            validate_child_render_policy_against_budget(policy, run.budget_snapshot)
            terminal_reference = ChildNativeTerminalReferenceFact(
                child_runtime_session_id=run.child_runtime_session_id,
                child_run_id=child_run_id,
                terminal_event_id=terminal.id,
                terminal_sequence=terminal.sequence,
                terminal_status=terminal.status,
                terminalization_kind=terminal.terminalization_kind,
                stop_reason=terminal.stop_reason,
            )
            explicit_evidence = (
                _explicit_result_evidence(
                    run=run,
                    child_run_id=child_run_id,
                    child_events=child_events,
                    submitted_event=submitted_event,
                    terminal_sequence=terminal.sequence,
                )
                if handoff_kind == "explicit"
                else None
            )
            usage, usage_status = _child_usage_fact(
                child_events,
                terminal_sequence=terminal.sequence,
            )
            resolved_tool_call_count = sum(
                1
                for event in child_events
                if isinstance(event, ToolCallStartEvent)
                and event.sequence is not None
                and event.sequence < terminal.sequence
            )
        else:
            # ``complete_fake`` is the sole explicit synthetic seam.
            policy = build_child_result_render_policy(
                renderer_version="subagent-result:v1",
                max_summary_chars=run.budget_snapshot.max_result_summary_chars_per_child,
                max_artifact_refs=run.budget_snapshot.max_result_artifact_refs_per_child,
            )
            terminal_reference = ChildNativeTerminalReferenceFact(
                child_runtime_session_id=run.child_runtime_session_id,
                child_run_id=child_run_id,
                terminal_event_id=f"run_end:synthetic:{run.subagent_run_id}",
                terminal_sequence=4,
                terminal_status="finished",
                terminalization_kind=RunTerminalizationKind.NORMAL,
                stop_reason=RunStopReason.FINAL,
            )
            explicit_evidence = (
                ChildExplicitResultEvidenceFact(
                    source_result_submitted_event_id=(
                        submitted_event.id
                        if submitted_event is not None
                        else "submitted:test"
                    ),
                    source_result_submitted_event_sequence=(
                        submitted_event.sequence
                        if submitted_event is not None
                        and submitted_event.sequence is not None
                        else 1
                    ),
                    child_runtime_session_id=run.child_runtime_session_id,
                    child_run_id=child_run_id,
                    source_tool_call_id=(
                        submitted_event.source_tool_call_id
                        if submitted_event is not None
                        else "call:report-result"
                    ),
                    tool_call_start_event_id="tool-call-start:test",
                    tool_call_start_sequence=1,
                    tool_result_end_event_id="tool-result-end:test",
                    tool_result_end_sequence=3,
                )
                if handoff_kind == "explicit"
                else None
            )
            usage = (
                ModelTokenUsageFact.model_validate(token_usage)
                if token_usage is not None
                else None
            )
            usage_status = "complete" if usage is not None else "missing"
            resolved_tool_call_count = tool_call_count or 0
        return build_child_result_handoff(
            handoff_kind=handoff_kind,  # type: ignore[arg-type]
            policy=policy,
            child_terminal_reference=terminal_reference,
            explicit_evidence=explicit_evidence,
            result_id=result_id,
            summary=summary,
            result_artifact_id=result_artifact_id,
            artifact_ids=artifact_ids,
            token_usage=usage,
            usage_status=usage_status,
            tool_call_count=resolved_tool_call_count,
        )

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
        state = self._graph_store.state
        run = self._require_run(subagent_run_id)
        phase = phase.strip()
        if not phase:
            raise ValueError("phase is required")
        # This is a parent-graph fact even when the child tool invocation
        # supplies a child-native EventContext. Parent graph events retain the
        # owning spawn context; child attribution remains in source_tool_call_id
        # and the child runtime's own raw event stream.
        parent_context = _spawn_event_context(run)
        event = SubagentPhaseReportedEvent(
            **parent_context.event_fields(),
            subagent_run_id=subagent_run_id,
            task_id=run.task_id,
            phase=_clip(phase, 120),
            message=_clip(message, 1_000) if message else None,
            progress=dict(progress or {}),
            source_tool_call_id=source_tool_call_id,
        )
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="report_phase",
                expected_through_sequence=state.through_sequence,
                events=(event,),
            )
        )

    async def submit_result(
        self,
        subagent_run_id: str,
        *,
        summary: str,
        event_context: EventContext,
        output_preview: str | None = None,
        artifact_ids: tuple[str, ...] = (),
        diagnostics: tuple[Mapping[str, object], ...] = (),
        source_tool_call_id: str,
    ) -> SubagentResult:
        state = self._graph_store.state
        run = self._require_run(subagent_run_id)
        if run.status not in _ACTIVE_STATUSES:
            raise SubagentRuntimeError(
                f"Subagent run is already terminal: {subagent_run_id}"
            )
        existing = _result_for_run(state, subagent_run_id, status="submitted")
        if existing is not None:
            return _legacy_result_from_fact(existing)
        summary = _clip(
            summary.strip() or "(child agent submitted an empty result)",
            run.budget.max_result_summary_chars_per_child,
        )
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
        # Explicit result submission is durable parent-graph state. Never copy
        # the child-native run/turn/reply ids into the parent EventLog: real
        # PostgreSQL ownership constraints correctly reject that cross-session
        # identity reuse.
        parent_context = _spawn_event_context(run)
        event = SubagentResultSubmittedEvent(
            **parent_context.event_fields(),
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
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="submit_result",
                expected_through_sequence=state.through_sequence,
                events=(event,),
            )
        )
        return _legacy_result_from_fact(self._graph_store.state.results[result_id])

    def submitted_result(self, subagent_run_id: str) -> SubagentResult | None:
        result = _result_for_run(
            self._graph_store.state,
            subagent_run_id,
            status="submitted",
        )
        return _legacy_result_from_fact(result) if result is not None else None

    async def complete_submitted_result(
        self,
        subagent_run_id: str,
        *,
        event_context: EventContext | None = None,
        token_usage: dict[str, object] | None = None,
        tool_call_count: int | None = None,
        child_run_id: str | None = None,
    ) -> SubagentResult:
        return await self._complete_submitted_result(
            subagent_run_id,
            event_context=event_context,
            token_usage=token_usage,
            tool_call_count=tool_call_count,
            child_run_id=child_run_id,
            allow_synthetic=False,
        )

    async def _complete_submitted_result(
        self,
        subagent_run_id: str,
        *,
        event_context: EventContext | None,
        token_usage: dict[str, object] | None,
        tool_call_count: int | None,
        child_run_id: str | None,
        allow_synthetic: bool,
    ) -> SubagentResult:
        state = self._graph_store.state
        run = self._require_run(subagent_run_id)
        result_fact = _result_for_run(state, subagent_run_id, status="submitted")
        if result_fact is None:
            completed = _result_for_run(state, subagent_run_id, status="completed")
            if completed is not None:
                return _legacy_result_from_fact(completed)
            raise SubagentNotReady(subagent_run_id)
        ctx = event_context or _spawn_event_context(run)
        submitted_events = [
            event
            for event in self.parent_runtime_session.event_log.iter()
            if isinstance(event, SubagentResultSubmittedEvent)
            and event.result_id == result_fact.result_id
        ]
        if len(submitted_events) != 1:
            raise SubagentRuntimeError(
                "explicit child completion requires one durable result submission"
            )
        submitted_event = submitted_events[0]
        resolved_child_run_id = (
            child_run_id
            or run.child_run_id
            or (f"run:synthetic-child:{subagent_run_id}")
        )
        handoff = self._build_result_handoff(
            run=run,
            child_run_id=resolved_child_run_id,
            handoff_kind="explicit",
            result_id=result_fact.result_id,
            summary=result_fact.summary,
            result_artifact_id=result_fact.final_message_artifact_id,
            artifact_ids=tuple(sorted(result_fact.artifact_ids)),
            token_usage=token_usage,
            tool_call_count=tool_call_count,
            submitted_event=submitted_event,
            allow_synthetic=allow_synthetic,
        )
        committed_token_usage = (
            handoff.token_usage.model_dump(mode="json")
            if handoff.token_usage is not None
            else None
        )
        child_terminal_event_id = handoff.child_terminal_reference.terminal_event_id
        child_terminal_created_at = next(
            (
                event.created_at
                for event in self.event_log_locator.event_log_for_runtime_session(
                    run.child_runtime_session_id
                ).iter(run_id=resolved_child_run_id)
                if isinstance(event, RunEndEvent)
                and event.id == child_terminal_event_id
            ),
            submitted_event.created_at,
        )
        events: list[AgentEvent] = [
            SubagentRunCompletedEvent(
                id=deterministic_parent_subagent_terminal_event_id(
                    parent_runtime_session_id=run.parent_runtime_session_id,
                    subagent_run_id=subagent_run_id,
                    child_terminal_event_id=child_terminal_event_id,
                    parent_terminal_event_type="subagent_run_completed",
                ),
                created_at=child_terminal_created_at,
                **ctx.event_fields(),
                subagent_run_id=subagent_run_id,
                parent_runtime_session_id=run.parent_runtime_session_id,
                child_runtime_session_id=run.child_runtime_session_id,
                child_run_id=resolved_child_run_id,
                result_id=result_fact.result_id,
                summary=result_fact.summary,
                result_artifact_id=result_fact.final_message_artifact_id,
                artifact_ids=list(handoff.artifact_ids),
                token_usage=committed_token_usage,
                tool_call_count=handoff.tool_call_count,
                result_handoff=handoff,
            )
        ]
        if run.task_id is not None:
            events.append(
                SubagentTaskCompletedEvent(
                    id=deterministic_parent_subagent_terminal_event_id(
                        parent_runtime_session_id=run.parent_runtime_session_id,
                        subagent_run_id=subagent_run_id,
                        child_terminal_event_id=child_terminal_event_id,
                        parent_terminal_event_type="subagent_task_completed",
                    ),
                    created_at=child_terminal_created_at,
                    **ctx.event_fields(),
                    task_id=run.task_id,
                    subagent_run_id=subagent_run_id,
                    result_id=result_fact.result_id,
                    primary_result_artifact_id=result_fact.final_message_artifact_id,
                    result_source="explicit",
                )
            )
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="complete_submitted_result",
                expected_through_sequence=state.through_sequence,
                events=tuple(events),
                batch_id=run.batch_id,
                create_tool_call_id=run.create_tool_call_id,
            )
        )
        self._execution_registry.release_handle(subagent_run_id)
        if run.task_id is not None:
            await self._schedule_dependents_after_completion(
                run.task_id, event_context=ctx
            )
        return _legacy_result_from_fact(
            self._graph_store.state.results[result_fact.result_id]
        )

    async def fail(
        self,
        subagent_run_id: str,
        *,
        reason_code: str,
        reason_message: str | None = None,
        event_context: EventContext | None = None,
        diagnostics: list[dict[str, object]] | None = None,
        repair_id: str | None = None,
        child_terminal_reference: ChildNativeTerminalReferenceFact | None = None,
        terminal_event_id: str | None = None,
        terminal_created_at: str | None = None,
    ) -> None:
        state = self._graph_store.state
        run = self._require_run(subagent_run_id)
        if run.status in _TERMINAL_STATUSES:
            return
        ctx = event_context or _spawn_event_context(run)
        run_failed = SubagentRunFailedEvent(
            id=terminal_event_id or uuid4().hex,
            **(
                {"created_at": terminal_created_at}
                if terminal_created_at is not None
                else {}
            ),
            **ctx.event_fields(),
            subagent_run_id=subagent_run_id,
            parent_runtime_session_id=run.parent_runtime_session_id,
            child_runtime_session_id=run.child_runtime_session_id,
            batch_id=run.batch_id,
            create_tool_call_id=run.create_tool_call_id,
            repair_id=repair_id,
            reason_code=reason_code,
            reason_message=reason_message,
            diagnostics=list(diagnostics or []),
            child_terminal_reference=child_terminal_reference,
        )
        events: list[AgentEvent] = [run_failed]
        task_failed: SubagentTaskFailedEvent | None = None
        if run.task_id is not None:
            task_failed = SubagentTaskFailedEvent(
                id=(
                    deterministic_parent_subagent_terminal_event_id(
                        parent_runtime_session_id=run.parent_runtime_session_id,
                        subagent_run_id=subagent_run_id,
                        child_terminal_event_id=(
                            child_terminal_reference.terminal_event_id
                            if child_terminal_reference is not None
                            else run_failed.id
                        ),
                        parent_terminal_event_type="subagent_task_failed",
                    )
                    if terminal_event_id is not None
                    else uuid4().hex
                ),
                **(
                    {"created_at": terminal_created_at}
                    if terminal_created_at is not None
                    else {}
                ),
                **ctx.event_fields(),
                task_id=run.task_id,
                subagent_run_id=subagent_run_id,
                batch_id=run.batch_id,
                create_tool_call_id=run.create_tool_call_id,
                repair_id=repair_id,
                reason_code=reason_code,
                reason_message=reason_message,
                diagnostics=list(diagnostics or []),
            )
            events.append(task_failed)
            events.extend(
                _plan_dependency_failure_cascade(
                    state,
                    root_task_id=run.task_id,
                    root_status="failed",
                    root_terminal_event_id=task_failed.id,
                    event_context=ctx,
                )
            )
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="fail_run",
                expected_through_sequence=state.through_sequence,
                events=tuple(events),
                batch_id=run.batch_id,
                create_tool_call_id=run.create_tool_call_id,
                repair_id=repair_id,
            )
        )
        handle = self._execution_registry.get(subagent_run_id)
        child_task = handle.coroutine if handle is not None else None
        if child_task is asyncio.current_task():
            self._execution_registry.release_handle(subagent_run_id)
        else:
            await self._execution_registry.cancel(
                subagent_run_id,
                timeout_seconds=5.0,
            )

    async def fail_from_native_child_terminal(
        self,
        subagent_run_id: str,
        *,
        child_run_id: str,
        reason_code: str,
        reason_message: str,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> None:
        run = self._require_run(subagent_run_id)
        _events, _start, terminal = self._require_child_terminal(
            run=run,
            child_run_id=child_run_id,
        )
        if terminal.terminalization_kind == "normal":
            raise SubagentRuntimeError(
                "parent child failure cannot reference a normal child terminal"
            )
        terminal_reference = _child_terminal_reference(
            run=run,
            child_run_id=child_run_id,
            terminal=terminal,
        )
        await self.fail(
            subagent_run_id,
            reason_code=reason_code,
            reason_message=reason_message,
            diagnostics=diagnostics,
            child_terminal_reference=terminal_reference,
            terminal_event_id=deterministic_parent_subagent_terminal_event_id(
                parent_runtime_session_id=run.parent_runtime_session_id,
                subagent_run_id=subagent_run_id,
                child_terminal_event_id=terminal.id,
                parent_terminal_event_type="subagent_run_failed",
            ),
            terminal_created_at=terminal.created_at,
        )

    async def cancel(
        self,
        subagent_run_id: str,
        *,
        event_context: EventContext | None = None,
        reason_code: str = "subagent_cancelled",
        reason_message: str | None = None,
        cancelled_by: str = "parent_agent",
        repair_id: str | None = None,
        drain_timeout_seconds: float | None = 5.0,
        _request_only: bool = False,
        child_terminal_reference: ChildNativeTerminalReferenceFact | None = None,
        terminal_event_id: str | None = None,
        terminal_created_at: str | None = None,
    ) -> SubagentRunFact:
        state = self._graph_store.state
        run = self._require_run(subagent_run_id)
        if run.status in _TERMINAL_STATUSES:
            return run
        ctx = event_context or _spawn_event_context(run)
        await self._commit_plan(
            _plan_run_cancellation(
                state,
                run=run,
                event_context=ctx,
                reason_code=reason_code,
                reason_message=reason_message,
                cancelled_by=cancelled_by,
                repair_id=repair_id,
                operation="cancel_run",
                child_terminal_reference=child_terminal_reference,
                terminal_event_id=terminal_event_id,
                terminal_created_at=terminal_created_at,
            )
        )
        if _request_only:
            self._execution_registry.request_cancel(subagent_run_id)
        else:
            await self._execution_registry.cancel(
                subagent_run_id,
                timeout_seconds=drain_timeout_seconds,
            )
        return self._require_run(subagent_run_id)

    async def cancel_active_children(
        self,
        *,
        reason_code: str = "subagent_host_shutdown",
        reason_message: str | None = None,
        cancelled_by: str = "host_shutdown",
        timeout_seconds: float | None = 5.0,
    ) -> tuple[SubagentRunFact, ...]:
        """Cancel all live child tasks owned by this runtime.

        This is the HostSession/HostCore close hook.  It is deliberately
        parent-graph-first: every child that was active gets a durable
        cancellation fact before we wait for cooperative task teardown.
        """

        active_runs = [run for run in self.runs if run.status in _ACTIVE_STATUSES]
        cancelled: list[SubagentRunFact] = []
        for run in active_runs:
            cancelled.append(
                await self.cancel(
                    run.subagent_run_id,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by=cancelled_by,
                    _request_only=True,
                )
            )
        # Also drain handles whose durable graph was terminalized earlier by a
        # synchronous safety callback. Graph status alone is not proof that the
        # child coroutine and its finally blocks have exited.
        await self._execution_registry.drain(timeout_seconds=timeout_seconds)
        return tuple(cancelled)

    async def repair_dangling_children(
        self,
        *,
        reason_code: str = "subagent_dangling_repaired",
        reason_message: str = (
            "Child runtime was recorded as active but has no live task handle in this host process; "
            "marking it failed during resume/repair."
        ),
    ) -> tuple[SubagentRunFact, ...]:
        """Reconcile ownerless parent graph nodes from their child ledgers.

        A live matching child owner wins and is left alone.  On session reopen,
        an ownerless child receives a deterministic recovered terminal if one is
        missing; a pre-existing child terminal is then folded into the parent by
        the same completion/failure builder used on the normal path.
        """

        repaired: list[SubagentRunFact] = []
        for run in self.runs:
            if run.status not in _ACTIVE_STATUSES:
                continue
            handle = self._execution_registry.get(run.subagent_run_id)
            task = handle.coroutine if handle is not None else None
            if task is not None and not task.done():
                continue
            child_log = self.event_log_locator.event_log_for_runtime_session(
                run.child_runtime_session_id
            )
            child_events = tuple(child_log.iter())
            starts = [
                event
                for event in child_events
                if isinstance(event, RunStartEvent)
                and event.subagent_run_entry is not None
                and event.subagent_run_entry.subagent_run_id == run.subagent_run_id
            ]
            if not starts:
                await self.fail(
                    run.subagent_run_id,
                    reason_code="child_run_start_not_committed",
                    reason_message=reason_message,
                    diagnostics=[
                        {
                            "child_runtime_session_id": run.child_runtime_session_id,
                            "parent_run_id": run.parent_run_id,
                            "repair": "child_run_start_not_committed",
                        }
                    ],
                )
                repaired.append(self._require_run(run.subagent_run_id))
                continue
            if len(starts) != 1:
                raise SubagentRuntimeError(
                    "child repair requires exactly one typed child RunStart"
                )
            start = starts[0]
            terminals = [
                event
                for event in child_events
                if isinstance(event, RunEndEvent) and event.run_id == start.run_id
            ]
            if not terminals:
                recovered = RunEndEvent(
                    id=start.terminal_run_end_event_id,
                    created_at=start.created_at,
                    run_id=start.run_id,
                    turn_id=start.turn_id,
                    reply_id=start.reply_id,
                    status="aborted",
                    stop_reason=RunStopReason.ABORTED,
                    terminalization_kind=RunTerminalizationKind.RECOVERED_INTERRUPTED,
                    abort_kind="host_teardown",
                )
                try:
                    stored = child_log.extend(
                        (recovered,),
                        expected_last_sequence=child_log.next_sequence() - 1,
                    )
                    terminal = stored[0]
                except BaseException:
                    confirmation = child_log.confirm_batch((recovered,))
                    if (
                        confirmation.missing_event_ids
                        or len(confirmation.committed_events) != 1
                    ):
                        raise
                    terminal = confirmation.committed_events[0]
                if not isinstance(terminal, RunEndEvent):
                    raise SubagentRuntimeError("recovered child terminal type mismatch")
            elif len(terminals) == 1:
                terminal = terminals[0]
            else:
                raise SubagentRuntimeError(
                    "child repair found multiple child terminal facts"
                )

            if terminal.terminalization_kind == "normal":
                submitted = [
                    event
                    for event in self.parent_runtime_session.event_log.iter()
                    if isinstance(event, SubagentResultSubmittedEvent)
                    and event.subagent_run_id == run.subagent_run_id
                ]
                if len(submitted) > 1:
                    raise SubagentRuntimeError(
                        "child repair found multiple explicit result submissions"
                    )
                if submitted:
                    await self.complete_submitted_result(
                        run.subagent_run_id,
                        child_run_id=start.run_id,
                    )
                else:
                    await self.complete_native_result(
                        run.subagent_run_id,
                        child_run_id=start.run_id,
                    )
                repaired.append(self._require_run(run.subagent_run_id))
                continue

            if terminal.sequence is None:
                raise SubagentRuntimeError("child terminal repair requires sequence")
            terminal_reference = _child_terminal_reference(
                run=run,
                child_run_id=start.run_id,
                terminal=terminal,
            )
            if terminal.terminalization_kind in {"user_stop", "host_teardown"}:
                cancelled_by = (
                    "user"
                    if terminal.terminalization_kind == "user_stop"
                    else "host_shutdown"
                )
                parent_event_id = deterministic_parent_subagent_terminal_event_id(
                    parent_runtime_session_id=run.parent_runtime_session_id,
                    subagent_run_id=run.subagent_run_id,
                    child_terminal_event_id=terminal.id,
                    parent_terminal_event_type="subagent_run_cancelled",
                )
                await self.cancel(
                    run.subagent_run_id,
                    reason_code=f"child_{terminal.terminalization_kind}",
                    reason_message=reason_message,
                    cancelled_by=cancelled_by,
                    repair_id=f"subagent_repair:{run.subagent_run_id}",
                    child_terminal_reference=terminal_reference,
                    terminal_event_id=parent_event_id,
                    terminal_created_at=terminal.created_at,
                )
            else:
                parent_event_id = deterministic_parent_subagent_terminal_event_id(
                    parent_runtime_session_id=run.parent_runtime_session_id,
                    subagent_run_id=run.subagent_run_id,
                    child_terminal_event_id=terminal.id,
                    parent_terminal_event_type="subagent_run_failed",
                )
                await self.fail(
                    run.subagent_run_id,
                    reason_code=(
                        "child_recovered_interrupted"
                        if terminal.terminalization_kind == "recovered_interrupted"
                        else f"child_{terminal.stop_reason}"
                    ),
                    reason_message=reason_message,
                    diagnostics=[
                        {
                            "child_runtime_session_id": run.child_runtime_session_id,
                            "parent_run_id": run.parent_run_id,
                            "repair": reason_code,
                        }
                    ],
                    repair_id=f"subagent_repair:{run.subagent_run_id}",
                    child_terminal_reference=terminal_reference,
                    terminal_event_id=parent_event_id,
                    terminal_created_at=terminal.created_at,
                )
            repaired.append(self._require_run(run.subagent_run_id))
        return tuple(repaired)

    async def fail_active_children_for_safety_narrowing(
        self,
        *,
        reason_code: str,
        reason_message: str | None = None,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> tuple[SubagentRunFact, ...]:
        """Cancel active children after a parent-side safety/capability narrowing.

        This is the conservative V1 observer endpoint used by HostSession safe
        points.  A future implementation can refresh child snapshots, but V1
        never lets an active child continue silently with revoked capability.
        """

        cancelled: list[SubagentRunFact] = []
        for run in self.runs:
            if run.status not in _ACTIVE_STATUSES:
                continue
            await self.cancel(
                run.subagent_run_id,
                reason_code=reason_code,
                reason_message=reason_message,
                cancelled_by="runtime",
            )
            cancelled.append(self._require_run(run.subagent_run_id))
        return tuple(cancelled)

    async def fail_children_for_mcp_binding_change(
        self,
        identities: frozenset[McpBindingIdentity],
        *,
        reason_message: str,
    ) -> tuple[SubagentRunFact, ...]:
        run_ids = self._execution_registry.child_ids_for_mcp_bindings(identities)
        cancelled: list[SubagentRunFact] = []
        for subagent_run_id in sorted(run_ids):
            run = self._graph_store.state.runs.get(subagent_run_id)
            if run is None or run.status not in _ACTIVE_STATUSES:
                continue
            await self.cancel(
                subagent_run_id,
                reason_code="subagent_mcp_binding_generation_changed",
                reason_message=reason_message,
                cancelled_by="runtime",
            )
            cancelled.append(self._require_run(subagent_run_id))
        return tuple(cancelled)

    def fail_active_children_for_safety_narrowing_now(
        self,
        *,
        reason_code: str,
        reason_message: str | None = None,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> tuple[SubagentRunFact, ...]:
        """Synchronous cancel-closed variant for host permission-mode switches."""

        del diagnostics
        cancelled: list[SubagentRunFact] = []
        for run in self.runs:
            if run.status not in _ACTIVE_STATUSES:
                continue
            state = self._graph_store.state
            self._commit_plan_from_thread(
                _plan_run_cancellation(
                    state,
                    run=run,
                    event_context=_spawn_event_context(run),
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by="runtime",
                    repair_id=None,
                    operation="cancel_run_sync",
                )
            )
            self._execution_registry.cancel_now(run.subagent_run_id)
            cancelled.append(self._require_run(run.subagent_run_id))
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
        state = self._graph_store.state
        run = self._require_run(subagent_run_id)
        result_fact = _result_for_run(state, subagent_run_id, status="completed")
        if result_fact is None:
            handle = self._execution_registry.get(subagent_run_id)
            task = handle.coroutine if handle is not None else None
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0)
                except TimeoutError:
                    pass
                result_fact = _result_for_run(
                    self._graph_store.state,
                    subagent_run_id,
                    status="completed",
                )
        if result_fact is None:
            raise SubagentNotReady(subagent_run_id)
        result = _legacy_result_from_fact(result_fact)
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
        state = self._graph_store.state
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="wait_run",
                expected_through_sequence=state.through_sequence,
                events=(edge,),
            )
        )
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
            handle = self._execution_registry.get(subagent_run_id)
            task = handle.coroutine if handle is not None else None
            if (
                task is not None
                and not task.done()
                and _result_for_run(
                    self._graph_store.state,
                    subagent_run_id,
                    status="completed",
                )
                is None
            ):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=max(0.0, timeout_seconds)
                    )
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

        deadline = (
            None
            if timeout_seconds is None
            else asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
        )
        while True:
            settled = [
                self._task_wait_payload(task_id, include_consumed=include_consumed)
                for task_id in task_ids
            ]
            settled = [item for item in settled if item is not None]
            if (settle == "first" and settled) or (
                settle == "all" and len(settled) == len(task_ids)
            ):
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
            await asyncio.sleep(
                min(0.01, max(0.0, deadline - asyncio.get_running_loop().time()))
            )

    async def cancel_task(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        reason_code: str = "subagent_task_cancelled",
        reason_message: str | None = None,
        cancelled_by: str = "parent_agent",
    ) -> SubagentTaskFact:
        task = self._require_task(task_id)
        if task.current_run_id:
            run = self._graph_store.state.runs.get(task.current_run_id)
            if run is not None and run.status in {"running", "suspended"}:
                await self.cancel(
                    task.current_run_id,
                    event_context=event_context,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by=cancelled_by,
                )
                return self._require_task(task_id)
        if task.status in {
            "completed",
            "failed",
            "cancelled",
            "blocked_dependency_failed",
        }:
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
        repair_id: str | None = None,
    ) -> SubagentTaskFact:
        """Terminalize a task that has no active child run or was already repaired."""

        task = self._require_task(task_id)
        if task.status in {"completed", "failed", "cancelled"}:
            return task
        if task.status == "blocked_dependency_failed" and not force:
            return task
        state = self._graph_store.state
        event = SubagentTaskCancelledEvent(
            **event_context.event_fields(),
            task_id=task_id,
            subagent_run_id=task.current_run_id,
            batch_id=task.batch_id,
            create_tool_call_id=task.create_tool_call_id,
            repair_id=repair_id,
            reason_code=reason_code,
            reason_message=reason_message,
            cancelled_by=cancelled_by,  # type: ignore[arg-type]
        )
        events: tuple[AgentEvent, ...] = (event,)
        events += _plan_dependency_failure_cascade(
            state,
            root_task_id=task_id,
            root_status="cancelled",
            root_terminal_event_id=event.id,
            event_context=event_context,
        )
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="cancel_materialized_task",
                expected_through_sequence=state.through_sequence,
                events=events,
                batch_id=task.batch_id,
                create_tool_call_id=task.create_tool_call_id,
                repair_id=repair_id,
            )
        )
        return self._require_task(task_id)

    async def fail_materialized_task(
        self,
        task_id: str,
        *,
        event_context: EventContext,
        reason_code: str,
        reason_message: str | None = None,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> SubagentTaskFact:
        """Fail a materialized task that has no active child run."""

        task = self._require_task(task_id)
        if task.current_run_id is not None:
            run = self._require_run(task.current_run_id)
            if run.status in _ACTIVE_STATUSES:
                await self.fail(
                    run.subagent_run_id,
                    event_context=event_context,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    diagnostics=diagnostics,
                )
                return self._require_task(task_id)
        if task.status in {
            "completed",
            "failed",
            "cancelled",
            "blocked_dependency_failed",
        }:
            return task
        state = self._graph_store.state
        failed = SubagentTaskFailedEvent(
            **event_context.event_fields(),
            task_id=task.task_id,
            subagent_run_id=task.current_run_id,
            batch_id=task.batch_id,
            create_tool_call_id=task.create_tool_call_id,
            reason_code=reason_code,
            reason_message=reason_message,
            diagnostics=list(diagnostics or []),
        )
        events: tuple[AgentEvent, ...] = (failed,)
        events += _plan_dependency_failure_cascade(
            state,
            root_task_id=task.task_id,
            root_status="failed",
            root_terminal_event_id=failed.id,
            event_context=event_context,
        )
        await self._commit_plan(
            PlannedSubagentWrite(
                operation="fail_materialized_task",
                expected_through_sequence=state.through_sequence,
                events=events,
                batch_id=task.batch_id,
                create_tool_call_id=task.create_tool_call_id,
            )
        )
        return self._require_task(task_id)

    async def repair_materialized_batch(
        self,
        batch_id: str,
        *,
        event_context: EventContext,
        repair_id: str,
        reason_code: str,
        reason_message: str | None = None,
    ) -> tuple[tuple[SubagentTaskFact, ...], tuple[SubagentRunFact, ...]]:
        """Atomically terminalize every non-terminal fact in a failed create batch."""

        state = self._graph_store.state
        task_facts = tuple(
            sorted(
                (task for task in state.tasks.values() if task.batch_id == batch_id),
                key=lambda task: task.task_id,
            )
        )
        run_facts = tuple(
            sorted(
                (run for run in state.runs.values() if run.batch_id == batch_id),
                key=lambda run: run.subagent_run_id,
            )
        )
        create_tool_call_ids = {task.create_tool_call_id for task in task_facts} | {
            run.create_tool_call_id for run in run_facts
        }
        if len(create_tool_call_ids) > 1:
            raise SubagentRuntimeError(
                f"Materialized batch has conflicting creation attribution: {batch_id}"
            )
        create_tool_call_id = next(iter(create_tool_call_ids), None)
        events: list[AgentEvent] = []
        active_run_ids: list[str] = []
        for run in run_facts:
            if run.status in _TERMINAL_STATUSES:
                continue
            events.append(
                SubagentRunCancelledEvent(
                    **event_context.event_fields(),
                    subagent_run_id=run.subagent_run_id,
                    parent_runtime_session_id=run.parent_runtime_session_id,
                    child_runtime_session_id=run.child_runtime_session_id,
                    batch_id=run.batch_id,
                    create_tool_call_id=run.create_tool_call_id,
                    repair_id=repair_id,
                    reason_code=reason_code,
                    reason_message=reason_message,
                    cancelled_by="runtime",
                )
            )
            active_run_ids.append(run.subagent_run_id)

        task_terminal_refs: dict[str, tuple[str, str]] = {}
        batch_task_ids = {task.task_id for task in task_facts}
        for task in task_facts:
            if task.status in {"completed", "failed", "cancelled"}:
                continue
            cancelled = SubagentTaskCancelledEvent(
                **event_context.event_fields(),
                task_id=task.task_id,
                subagent_run_id=task.current_run_id,
                batch_id=task.batch_id,
                create_tool_call_id=task.create_tool_call_id,
                repair_id=repair_id,
                reason_code=reason_code,
                reason_message=reason_message,
                cancelled_by="runtime",
            )
            events.append(cancelled)
            task_terminal_refs[task.task_id] = ("cancelled", cancelled.id)

        events.extend(
            _plan_dependency_failure_cascade_many(
                state,
                roots=task_terminal_refs,
                event_context=event_context,
                excluded_task_ids=batch_task_ids,
            )
        )
        if events:
            await self._commit_plan(
                PlannedSubagentWrite(
                    operation="repair_materialized_batch",
                    expected_through_sequence=state.through_sequence,
                    events=tuple(events),
                    batch_id=batch_id,
                    create_tool_call_id=create_tool_call_id,
                    repair_id=repair_id,
                )
            )
        await self._execution_registry.drain_run_ids(
            tuple(active_run_ids),
            timeout_seconds=5.0,
        )
        return (
            tuple(self._require_task(task.task_id) for task in task_facts),
            tuple(self._require_run(run.subagent_run_id) for run in run_facts),
        )

    async def _schedule_dependents_after_completion(
        self,
        task_id: str,
        *,
        event_context: EventContext,
    ) -> None:
        for task in self.tasks:
            if task.status != "waiting_dependency":
                continue
            if task_id not in task.depends_on:
                continue
            if self._dependency_failed(task):
                failed_ids = tuple(
                    dependency_id
                    for dependency_id in task.depends_on
                    if self._graph_store.state.tasks.get(dependency_id) is not None
                    and self._graph_store.state.tasks[dependency_id].status
                    in {"failed", "cancelled", "blocked_dependency_failed"}
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
                try:
                    await self.start_task(
                        task.task_id,
                        event_context=event_context,
                        spawn_initiator_kind="dependency_satisfied",
                        spawn_initiator_id=task_id,
                    )
                except EventWriteConflict:
                    refreshed = self._require_task(task.task_id)
                    if (
                        refreshed.status == "running"
                        or refreshed.current_run_id is not None
                    ):
                        continue
                    raise
                except (SubagentLimitExceeded, SubagentRuntimeError) as exc:
                    await self.fail_materialized_task(
                        task.task_id,
                        event_context=event_context,
                        reason_code="subagent_dependency_start_unavailable",
                        reason_message=(
                            "A dependency became satisfied, but the child run could not pass "
                            "the immediate-start preflight."
                        ),
                        diagnostics=[{"error_type": type(exc).__name__}],
                    )
                except Exception as exc:
                    # start_task() terminalizes a post-commit child-start
                    # failure before re-raising the adapter exception. Preserve
                    # that durable outcome and do not turn the upstream
                    # completion into a second failure.
                    refreshed = self._require_task(task.task_id)
                    if refreshed.status in {
                        "failed",
                        "cancelled",
                        "blocked_dependency_failed",
                    }:
                        continue
                    await self.fail_materialized_task(
                        task.task_id,
                        event_context=event_context,
                        reason_code="subagent_dependency_start_unavailable",
                        reason_message=(
                            "A dependency became satisfied, but the child runtime could not start."
                        ),
                        diagnostics=[{"error_type": type(exc).__name__}],
                    )

    def _dependency_terminal_event_ids(
        self,
        task_ids: tuple[str, ...],
        *,
        overrides: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        terminal_event_ids: dict[str, str] = {}
        state = self._graph_store.state
        for task_id in task_ids:
            task = state.tasks.get(task_id)
            terminal_event_id = (
                task.provenance.terminal_event_id if task is not None else None
            )
            if isinstance(terminal_event_id, str) and terminal_event_id:
                terminal_event_ids[task_id] = terminal_event_id
        if overrides:
            terminal_event_ids.update(
                {key: value for key, value in overrides.items() if value}
            )
        return terminal_event_ids

    def _dependency_status_snapshot(
        self,
        task_ids: tuple[str, ...],
        *,
        planned_status_by_task_id: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        planned = planned_status_by_task_id or {}
        state = self._graph_store.state
        for task_id in task_ids:
            if task_id in state.tasks:
                snapshot[task_id] = state.tasks[task_id].status
                continue
            planned_status = planned.get(task_id)
            if planned_status is None:
                continue
            snapshot[task_id] = (
                "running" if planned_status == "start" else planned_status
            )
        return snapshot

    def _dependencies_satisfied(self, task: SubagentTaskFact) -> bool:
        state = self._graph_store.state
        return all(
            state.tasks.get(dependency_id) is not None
            and state.tasks[dependency_id].status == "completed"
            for dependency_id in task.depends_on
        )

    def _dependency_failed(self, task: SubagentTaskFact) -> bool:
        state = self._graph_store.state
        return any(
            state.tasks.get(dependency_id) is not None
            and state.tasks[dependency_id].status
            in {"failed", "cancelled", "blocked_dependency_failed"}
            for dependency_id in task.depends_on
        )

    def _task_wait_payload(
        self,
        task_id: str,
        *,
        include_consumed: bool,
    ) -> dict[str, object] | None:
        task = self._require_task(task_id)
        if task.status not in {
            "completed",
            "failed",
            "cancelled",
            "blocked_dependency_failed",
        }:
            return None
        state = self._graph_store.state
        run_fact = state.runs.get(task.current_run_id or "")
        result_fact = (
            _result_for_run(state, task.current_run_id, status="completed")
            if task.current_run_id
            else None
        )
        run = run_fact
        result = (
            _legacy_result_from_fact(result_fact) if result_fact is not None else None
        )
        consumed_task_ids, consumed_result_ids = _consumed_ids(state)
        consumed = bool(
            task_id in consumed_task_ids
            or (result is not None and result.result_id in consumed_result_ids)
        )
        if consumed and not include_consumed:
            return None
        return {
            "task_id": task.task_id,
            "task_key": task.task_key,
            "status": task.status,
            "subagent_run_id": task.current_run_id,
            "child_runtime_session_id": run.child_runtime_session_id
            if run is not None
            else None,
            "result_id": result.result_id if result is not None else None,
            "summary": result.summary if result is not None else None,
            "output_preview": result.output_preview if result is not None else None,
            "result_artifact_id": result.final_message_artifact_id
            if result is not None
            else None,
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
        state = self._graph_store.state
        consumed_task_ids, consumed_result_ids = _consumed_ids(state)
        events: list[AgentEvent] = []
        for payload in payloads:
            task_id = payload.get("task_id")
            if not isinstance(task_id, str):
                continue
            result_id = payload.get("result_id")
            already_consumed = task_id in consumed_task_ids or (
                isinstance(result_id, str) and result_id in consumed_result_ids
            )
            if already_consumed and not include_consumed:
                continue
            consumed_status = payload.get("status")
            task_fact = state.tasks.get(task_id)
            terminal_event_id = (
                None
                if isinstance(result_id, str)
                else task_fact.provenance.terminal_event_id
                if task_fact is not None
                else None
            )
            events.append(
                SubagentResultConsumedEvent(
                    **event_context.event_fields(),
                    consumption_id=f"subagent_consumption:{uuid4().hex}",
                    consumer_tool_call_id=consumer_tool_call_id,
                    kind="wait_task",
                    task_id=task_id,
                    subagent_run_id=payload.get("subagent_run_id")
                    if isinstance(payload.get("subagent_run_id"), str)
                    else None,
                    result_id=result_id if isinstance(result_id, str) else None,
                    consumed_status=(
                        consumed_status
                        if consumed_status
                        in {
                            "completed",
                            "failed",
                            "cancelled",
                            "blocked_dependency_failed",
                        }
                        else "failed"
                    ),  # type: ignore[arg-type]
                    terminal_event_id=terminal_event_id,
                    diagnostics=[],
                )
            )
        if events:
            await self._commit_plan(
                PlannedSubagentWrite(
                    operation="consume_task_results",
                    expected_through_sequence=state.through_sequence,
                    events=tuple(events),
                )
            )

    def graph(self) -> SubagentGraphProjection:
        return project_subagent_graph(
            self.parent_runtime_session.runtime_session_id,
            self._graph_store.state,
            locator=self.event_log_locator,
        )

    def child_runtime_session(self, subagent_run_id: str) -> RuntimeSession:
        self._require_run(subagent_run_id)
        handle = self._execution_registry.get(subagent_run_id)
        if handle is None or handle.child_session is None:
            raise SubagentNotReady(
                f"Child runtime session is not attached in this process: {subagent_run_id}"
            )
        return handle.child_session

    def child_event_log(self, subagent_run_id: str) -> EventLog:
        """Open the durable child ledger without depending on a live handle."""

        run = self._require_run(subagent_run_id)
        return self.event_log_locator.event_log_for_runtime_session(
            run.child_runtime_session_id
        )

    def pending_results_for_delivery(
        self, *, max_results: int = 8
    ) -> tuple[SubagentResult, ...]:
        if max_results <= 0:
            return ()
        state = self._graph_store.state
        return tuple(
            _legacy_result_from_fact(state.results[result_id])
            for result_id in pending_subagent_result_ids(state)[:max_results]
        )

    def materialize_result_selection(
        self, result_ids: tuple[str, ...]
    ) -> tuple[SubagentResult, ...]:
        """Materialize an already-frozen canonical selection without reselecting."""

        state = self._graph_store.state
        facts = tuple(state.results.get(result_id) for result_id in result_ids)
        if any(fact is None or fact.status != "completed" for fact in facts):
            raise SubagentRuntimeError(
                "frozen subagent result selection is unavailable in graph state"
            )
        return tuple(
            _legacy_result_from_fact(fact)
            for fact in facts
            if fact is not None
        )

    def render_pending_results_section(
        self, *, max_results: int = 8
    ) -> tuple[str | None, tuple[SubagentResult, ...]]:
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

    def pending_result_delivery_count(self) -> int:
        state = self._graph_store.state
        _consumed_task_ids, consumed_result_ids = _consumed_ids(state)
        return sum(
            1
            for result in state.results.values()
            if result.status == "completed"
            and result.result_id not in consumed_result_ids
            and result.result_id not in state.deliveries
        )

    async def mark_results_delivered(
        self,
        results: tuple[SubagentResult, ...],
        *,
        event_context: EventContext,
        context_id: str,
        model_call_index: int,
        section_id: str,
    ) -> list[SubagentResultDeliveredEvent]:
        matching_model_start = any(
            isinstance(event, ModelCallStartEvent)
            and event.run_id == event_context.run_id
            and event.turn_id == event_context.turn_id
            and event.reply_id == event_context.reply_id
            and event.context_id == context_id
            and event.model_call_index == model_call_index
            for event in self.parent_runtime_session.event_log.iter(
                run_id=event_context.run_id
            )
        )
        if not matching_model_start:
            raise SubagentRuntimeError(
                "Subagent result delivery requires a matching durable ModelCallStartEvent"
            )
        state = self._graph_store.state
        _consumed_task_ids, consumed_result_ids = _consumed_ids(state)
        events: list[SubagentResultDeliveredEvent] = []
        for result in results:
            if (
                result.result_id in state.deliveries
                or result.result_id in consumed_result_ids
            ):
                continue
            run = self._require_run(result.subagent_run_id)
            events.append(
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
        if not events:
            return []
        committed = await self._commit_plan(
            PlannedSubagentWrite(
                operation="deliver_results",
                expected_through_sequence=state.through_sequence,
                events=tuple(events),
            )
        )
        return [
            event
            for event in committed
            if isinstance(event, SubagentResultDeliveredEvent)
        ]

    def _create_child_runtime_session(
        self,
        *,
        child_runtime_session_id: str,
        subagent_run_id: str,
        parent_run_id: str,
        capability_profile_id: str,
    ) -> RuntimeSession:
        event_log = self._child_event_log_factory(child_runtime_session_id)
        # Capability exposure artifacts are frozen before the child RunStart
        # batch is committed.  PostgreSQL artifact ownership therefore needs
        # the child session row to exist before the first event append.
        event_log.ensure_runtime_session_owner()
        if hasattr(self.event_log_locator, "register"):
            self.event_log_locator.register(child_runtime_session_id, event_log)  # type: ignore[attr-defined]
        child = RuntimeSession(
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
            context_event_log_locator=self.event_log_locator,
        )
        child.mcp_supervisor = self.parent_runtime_session.mcp_supervisor
        child.set_mcp_installation_contract(
            installation_id=self.parent_runtime_session.mcp_installation_id,
            owner_runtime_session_id=self.parent_runtime_session.runtime_session_id,
        )
        return child

    async def _run_child(self, run_view: HydratedSubagentRunView) -> None:
        assert self._child_runner is not None
        run = run_view.fact
        retain_handle_for_reconciliation = False
        try:
            if run.budget.child_timeout_seconds is None:
                await self._child_runner(self, run_view)
            else:
                await asyncio.wait_for(
                    self._child_runner(self, run_view),
                    timeout=max(0.0, run.budget.child_timeout_seconds),
                )
        except TimeoutError:
            current = self._graph_store.state.runs.get(run.subagent_run_id)
            if current is not None and current.status in {"running", "suspended"}:
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
        except SubagentRunEntryCommitUntrusted as exc:
            retain_handle_for_reconciliation = True
            event = SubagentRunSuspendedEvent(
                run_id=run.parent_run_id,
                turn_id=run.parent_turn_id
                or run.parent_run_id.replace("run:", "turn:", 1),
                reply_id=run.parent_reply_id
                or run.parent_run_id.replace("run:", "reply:", 1),
                subagent_run_id=run.subagent_run_id,
                parent_runtime_session_id=run.parent_runtime_session_id,
                child_runtime_session_id=run.child_runtime_session_id,
                pending_kind="child_ledger_reconciliation",
                reason_code=(
                    "child_run_start_commit_" + exc.durable_run_existence.value
                ),
                reason_message=(
                    "Child RunStart commit existence is untrusted; the execution "
                    "handle and capacity remain owned until close/reconciliation."
                ),
                resumable=False,
            )
            await self._commit_plan(
                PlannedSubagentWrite(
                    events=(event,),
                    expected_through_sequence=self._graph_store.through_sequence,
                )
            )
        except Exception as exc:
            current = self._graph_store.state.runs.get(run.subagent_run_id)
            if current is not None and current.status in {"running", "suspended"}:
                child_log = self.event_log_locator.event_log_for_runtime_session(
                    run.child_runtime_session_id
                )
                child_terminals = [
                    event
                    for event in child_log.iter()
                    if isinstance(event, RunEndEvent)
                ]
                if child_terminals:
                    child_terminal = child_terminals[-1]
                    if (
                        child_terminal.terminalization_kind
                        is RunTerminalizationKind.NORMAL
                    ):
                        await self.complete_native_result(
                            run.subagent_run_id,
                            child_run_id=child_terminal.run_id,
                        )
                    else:
                        await self.fail_from_native_child_terminal(
                            run.subagent_run_id,
                            child_run_id=child_terminal.run_id,
                            reason_code="subagent_child_runner_error",
                            reason_message=(
                                "The child runtime stopped because its runner raised "
                                "an error after committing its native terminal fact."
                            ),
                            diagnostics=[{"error_type": type(exc).__name__}],
                        )
                else:
                    await self.fail(
                        run.subagent_run_id,
                        reason_code="subagent_child_runner_error",
                        reason_message=(
                            "The child runtime stopped because its runner raised "
                            "an error before a native terminal fact was committed."
                        ),
                        diagnostics=[{"error_type": type(exc).__name__}],
                    )
        finally:
            if not retain_handle_for_reconciliation:
                self._execution_registry.release_handle(run.subagent_run_id)

    def _require_run(self, subagent_run_id: str) -> SubagentRunFact:
        try:
            return self._graph_store.state.runs[subagent_run_id]
        except KeyError as exc:
            raise SubagentNotFound(subagent_run_id) from exc

    def _require_task(self, task_id: str) -> SubagentTaskFact:
        try:
            return self._graph_store.state.tasks[task_id]
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
        runs = tuple(self._graph_store.state.runs.values())
        active_for_run = {
            run.subagent_run_id
            for run in runs
            if run.parent_run_id == parent_run_id and run.status in _ACTIVE_STATUSES
        }
        active_for_session = {
            run.subagent_run_id for run in runs if run.status in _ACTIVE_STATUSES
        }
        # Durable terminal status does not release physical capacity. A child
        # whose cancellation cleanup is still running remains in the union via
        # its attached execution handle until the done callback releases it.
        active_for_run.update(
            self._execution_registry.occupied_run_ids(parent_run_id=parent_run_id)
        )
        active_for_session.update(self._execution_registry.occupied_run_ids())
        total_for_run = [run for run in runs if run.parent_run_id == parent_run_id]
        reserved_for_run = self._execution_registry.uncommitted_reservation_count(
            parent_run_id=parent_run_id
        )
        reserved_for_session = self._execution_registry.uncommitted_reservation_count()
        if count < 1:
            raise ValueError("count must be positive")
        if (
            len(active_for_run) + reserved_for_run + count
            > budget.max_concurrent_children_per_parent_run
        ):
            raise SubagentLimitExceeded(
                "max_concurrent_children_per_parent_run exceeded"
            )
        if (
            len(active_for_session) + reserved_for_session + count
            > budget.max_concurrent_children_per_host_session
        ):
            raise SubagentLimitExceeded(
                "max_concurrent_children_per_host_session exceeded"
            )
        if len(total_for_run) + count > budget.max_total_child_runs_per_parent_run:
            raise SubagentLimitExceeded("max_total_child_runs_per_parent_run exceeded")
        if budget.max_spawn_depth_from_root < 0:
            raise SubagentLimitExceeded("max_spawn_depth_from_root exceeded")

    def _default_capability_profile(
        self, budget: SubagentBudget
    ) -> SubagentCapabilityProfile:
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
        if profile_name not in {
            "research_worker",
            "review_worker",
            "verification_worker",
        }:
            return self._default_capability_profile(budget)
        base = self._parent_capability_snapshot or _default_capability_profile(budget)
        inherited_names = set(base.allowed_tool_names)
        inherited_descriptor_ids = set(base.allowed_descriptor_ids)
        if profile_name in {"research_worker", "review_worker"}:
            allowed_names = _profile_tool_subset(
                inherited_names, _READ_ONLY_WORKER_TOOL_NAMES
            )
            if profile_name == "research_worker":
                allowed_names.update(_mcp_tool_names_from_profile(base))
            profile_summary = "read-only investigation/review profile"
        else:
            allowed_names = _profile_tool_subset(
                inherited_names, _VERIFICATION_WORKER_TOOL_NAMES
            )
            profile_summary = (
                "verification profile with terminal access but no file writes"
            )
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


def _final_child_assistant_text(
    child_events: tuple[AgentEvent, ...],
    *,
    terminal: RunEndEvent,
) -> str:
    """Reconstruct the final child assistant text from committed block events."""

    assembler = BlockAssembler()
    completed: list[tuple[int, str]] = []
    terminal_sequence = terminal.sequence
    assert terminal_sequence is not None
    for event in child_events:
        if event.sequence is None or event.sequence > terminal_sequence:
            continue
        update = assembler.append(event)
        for item in update.completed:
            if item.reply_id != terminal.reply_id or not isinstance(
                item.block, TextBlock
            ):
                continue
            completed.append((item.end_sequence or 0, item.block.text))
    completed.sort(key=lambda item: item[0])
    return "\n".join(text for _, text in completed)


def _child_terminal_reference(
    *,
    run: SubagentRunFact,
    child_run_id: str,
    terminal: RunEndEvent,
) -> ChildNativeTerminalReferenceFact:
    if terminal.sequence is None:
        raise SubagentRuntimeError("child terminal reference requires sequence")
    return ChildNativeTerminalReferenceFact(
        child_runtime_session_id=run.child_runtime_session_id,
        child_run_id=child_run_id,
        terminal_event_id=terminal.id,
        terminal_sequence=terminal.sequence,
        terminal_status=terminal.status,
        terminalization_kind=terminal.terminalization_kind,
        stop_reason=terminal.stop_reason,
    )


def _child_usage_fact(
    child_events: tuple[AgentEvent, ...],
    *,
    terminal_sequence: int,
) -> tuple[ModelTokenUsageFact | None, str]:
    model_ends = [
        event
        for event in child_events
        if isinstance(event, ModelCallEndEvent)
        and event.sequence is not None
        and event.sequence < terminal_sequence
    ]
    reported = [event.usage for event in model_ends if event.usage is not None]
    if not reported:
        return None, "missing"
    cached_values = [item.cached_input_tokens for item in reported]
    reasoning_values = [item.reasoning_output_tokens for item in reported]
    usage = ModelTokenUsageFact(
        input_tokens=sum(item.input_tokens for item in reported),
        cached_input_tokens=(
            sum(value for value in cached_values if value is not None)
            if all(value is not None for value in cached_values)
            else None
        ),
        output_tokens=sum(item.output_tokens for item in reported),
        reasoning_output_tokens=(
            sum(value for value in reasoning_values if value is not None)
            if all(value is not None for value in reasoning_values)
            else None
        ),
        total_tokens=sum(item.total_tokens for item in reported),
    )
    status = "complete" if len(reported) == len(model_ends) else "partial"
    return usage, status


def _explicit_result_evidence(
    *,
    run: SubagentRunFact,
    child_run_id: str,
    child_events: tuple[AgentEvent, ...],
    submitted_event: SubagentResultSubmittedEvent | None,
    terminal_sequence: int,
) -> ChildExplicitResultEvidenceFact:
    if submitted_event is None or submitted_event.sequence is None:
        raise SubagentRuntimeError(
            "explicit child result requires a sequenced parent submission"
        )
    tool_call_id = submitted_event.source_tool_call_id
    starts = [
        event
        for event in child_events
        if isinstance(event, ToolCallStartEvent)
        and event.tool_call_id == tool_call_id
        and event.tool_call_name == "report_agent_result"
    ]
    results = [
        event
        for event in child_events
        if isinstance(event, ToolResultEndEvent) and event.tool_call_id == tool_call_id
    ]
    if len(starts) != 1 or len(results) != 1:
        raise SubagentRuntimeError(
            "explicit result must originate from one report_agent_result call/result"
        )
    start = starts[0]
    result = results[0]
    if start.sequence is None or result.sequence is None:
        raise SubagentRuntimeError("explicit result evidence is unsequenced")
    if not (start.sequence <= result.sequence < terminal_sequence):
        raise SubagentRuntimeError(
            "explicit result tool evidence must precede child terminal"
        )
    if start.run_id != child_run_id or result.run_id != child_run_id:
        raise SubagentRuntimeError("explicit result child run attribution mismatch")
    return ChildExplicitResultEvidenceFact(
        source_result_submitted_event_id=submitted_event.id,
        source_result_submitted_event_sequence=submitted_event.sequence,
        child_runtime_session_id=run.child_runtime_session_id,
        child_run_id=child_run_id,
        source_tool_call_id=tool_call_id,
        tool_call_start_event_id=start.id,
        tool_call_start_sequence=start.sequence,
        tool_result_end_event_id=result.id,
        tool_result_end_sequence=result.sequence,
    )


def _legacy_result_from_fact(fact: SubagentResultFact) -> SubagentResult:
    return SubagentResult(
        subagent_run_id=fact.subagent_run_id,
        result_id=fact.result_id,
        status="completed",
        summary=fact.summary,
        output_preview=fact.output_preview,
        final_message_artifact_id=fact.final_message_artifact_id,
        artifact_ids=fact.artifact_ids,
        diagnostics=tuple(thaw_json_mapping(item) for item in fact.diagnostics),
        token_usage=(
            thaw_json_mapping(fact.token_usage)
            if fact.token_usage is not None
            else None
        ),
        tool_call_count=fact.tool_call_count,
        completed_at=fact.provenance.updated_at,
        task_id=fact.task_id,
        result_source=fact.result_source,
    )


def _single_optional_value(
    rows: list[dict[str, Any]],
    key: str,
) -> str | None:
    values = {str(row[key]) for row in rows if row.get(key) is not None}
    if len(values) > 1:
        raise ValueError(f"Batch contains conflicting {key} values")
    return next(iter(values), None)


def _required_single_batch_id(rows: list[dict[str, Any]]) -> str:
    batch_id = _single_optional_value(rows, "batch_id")
    if batch_id is None:
        raise SubagentRuntimeError("Materialized task batch is missing batch_id")
    return batch_id


def _result_for_run(
    state: SubagentGraphState,
    subagent_run_id: str,
    *,
    status: str,
) -> SubagentResultFact | None:
    return next(
        (
            result
            for result in state.results.values()
            if result.subagent_run_id == subagent_run_id and result.status == status
        ),
        None,
    )


def _completed_result_for_run(
    state: SubagentGraphState,
    subagent_run_id: str,
) -> SubagentResult | None:
    result = _result_for_run(state, subagent_run_id, status="completed")
    return _legacy_result_from_fact(result) if result is not None else None


def _consumed_ids(state: SubagentGraphState) -> tuple[set[str], set[str]]:
    return (
        {
            item.task_id
            for item in state.consumptions.values()
            if item.task_id is not None
        },
        {
            item.result_id
            for item in state.consumptions.values()
            if item.result_id is not None
        },
    )


def _plan_dependency_failure_cascade(
    state: SubagentGraphState,
    *,
    root_task_id: str,
    root_status: str,
    root_terminal_event_id: str,
    event_context: EventContext,
) -> tuple[SubagentTaskBlockedEvent, ...]:
    return _plan_dependency_failure_cascade_many(
        state,
        roots={root_task_id: (root_status, root_terminal_event_id)},
        event_context=event_context,
    )


def _plan_run_cancellation(
    state: SubagentGraphState,
    *,
    run: SubagentRunFact,
    event_context: EventContext,
    reason_code: str,
    reason_message: str | None,
    cancelled_by: str,
    repair_id: str | None,
    operation: str,
    child_terminal_reference: ChildNativeTerminalReferenceFact | None = None,
    terminal_event_id: str | None = None,
    terminal_created_at: str | None = None,
) -> PlannedSubagentWrite:
    run_cancelled = SubagentRunCancelledEvent(
        id=terminal_event_id or uuid4().hex,
        **(
            {"created_at": terminal_created_at}
            if terminal_created_at is not None
            else {}
        ),
        **event_context.event_fields(),
        subagent_run_id=run.subagent_run_id,
        parent_runtime_session_id=run.parent_runtime_session_id,
        child_runtime_session_id=run.child_runtime_session_id,
        batch_id=run.batch_id,
        create_tool_call_id=run.create_tool_call_id,
        repair_id=repair_id,
        reason_code=reason_code,
        reason_message=reason_message,
        cancelled_by=cancelled_by,  # type: ignore[arg-type]
        child_terminal_reference=child_terminal_reference,
    )
    events: list[AgentEvent] = [run_cancelled]
    if run.task_id is not None:
        task_cancelled = SubagentTaskCancelledEvent(
            id=(
                deterministic_parent_subagent_terminal_event_id(
                    parent_runtime_session_id=run.parent_runtime_session_id,
                    subagent_run_id=run.subagent_run_id,
                    child_terminal_event_id=(
                        child_terminal_reference.terminal_event_id
                        if child_terminal_reference is not None
                        else run_cancelled.id
                    ),
                    parent_terminal_event_type="subagent_task_cancelled",
                )
                if terminal_event_id is not None
                else uuid4().hex
            ),
            **(
                {"created_at": terminal_created_at}
                if terminal_created_at is not None
                else {}
            ),
            **event_context.event_fields(),
            task_id=run.task_id,
            subagent_run_id=run.subagent_run_id,
            batch_id=run.batch_id,
            create_tool_call_id=run.create_tool_call_id,
            repair_id=repair_id,
            reason_code=reason_code,
            reason_message=reason_message,
            cancelled_by=cancelled_by,  # type: ignore[arg-type]
        )
        events.append(task_cancelled)
        events.extend(
            _plan_dependency_failure_cascade(
                state,
                root_task_id=run.task_id,
                root_status="cancelled",
                root_terminal_event_id=task_cancelled.id,
                event_context=event_context,
            )
        )
    return PlannedSubagentWrite(
        operation=operation,
        expected_through_sequence=state.through_sequence,
        events=tuple(events),
        batch_id=run.batch_id,
        create_tool_call_id=run.create_tool_call_id,
        repair_id=repair_id,
    )


def _plan_dependency_failure_cascade_many(
    state: SubagentGraphState,
    *,
    roots: Mapping[str, tuple[str, str]],
    event_context: EventContext,
    excluded_task_ids: set[str] | None = None,
) -> tuple[SubagentTaskBlockedEvent, ...]:
    planned_statuses = {
        task_id: status for task_id, (status, _event_id) in roots.items()
    }
    terminal_event_ids = {
        task_id: event_id for task_id, (_status, event_id) in roots.items()
    }
    excluded = excluded_task_ids or set()
    queue = sorted(roots)
    events: list[SubagentTaskBlockedEvent] = []
    while queue:
        failed_dependency_id = queue.pop(0)
        for task in sorted(state.tasks.values(), key=lambda item: item.task_id):
            if task.task_id in excluded:
                continue
            status = planned_statuses.get(task.task_id, task.status)
            if status not in {"created", "waiting_dependency"}:
                continue
            if failed_dependency_id not in task.depends_on:
                continue
            dependency_status = planned_statuses.get(
                failed_dependency_id,
                state.tasks[failed_dependency_id].status,
            )
            refs = {
                failed_dependency_id: terminal_event_ids[failed_dependency_id],
            }
            event = SubagentTaskBlockedEvent(
                **event_context.event_fields(),
                task_id=task.task_id,
                status="blocked_dependency_failed",
                blocked_reason="dependency_failed",
                blocked_by_task_ids=[failed_dependency_id],
                dependency_status_snapshot={
                    failed_dependency_id: dependency_status,
                },
                dependency_terminal_event_ids=refs,
                dependency_generation=subagent_dependency_generation(refs),
            )
            events.append(event)
            planned_statuses[task.task_id] = "blocked_dependency_failed"
            terminal_event_ids[task.task_id] = event.id
            queue.append(task.task_id)
    return tuple(events)


def _default_capability_profile(budget: SubagentBudget) -> SubagentCapabilityProfile:
    permission_mode = PermissionMode.READ_ONLY
    return SubagentCapabilityProfile(
        profile_id=f"subagent_capability_profile:{uuid4().hex}",
        permission_mode=permission_mode.value,
        permission_policy=preset_to_policy(permission_mode).to_dict(),
        max_spawn_depth_from_root=budget.max_spawn_depth_from_root,
        allowed_tool_names=_CHILD_REPORT_TOOL_NAMES,
        allowed_descriptor_ids=_CHILD_REPORT_DESCRIPTOR_IDS,
    )


def _profile_tool_subset(
    inherited_names: set[str], allowed_core_names: frozenset[str]
) -> set[str]:
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


def _mcp_binding_identities(
    runtime_session: RuntimeSession,
    *,
    allowed_tool_names: frozenset[str],
) -> frozenset[McpBindingIdentity]:
    identities = {
        identity
        for tool in runtime_session.extra_tool_bindings
        if getattr(tool, "name", None) in allowed_tool_names
        if isinstance(
            (identity := getattr(tool, "binding_identity", None)),
            McpBindingIdentity,
        )
    }
    return frozenset(identities)


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


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _optional_str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _spawn_event_context(run: SubagentRunFact) -> EventContext:
    return EventContext(
        run_id=run.parent_run_id,
        turn_id=run.parent_turn_id
        or f"turn:subagent-maintenance:{run.subagent_run_id}",
        reply_id=run.parent_reply_id
        or f"reply:subagent-maintenance:{run.subagent_run_id}",
    )


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _event_ref(event: Any) -> str:
    event_id = getattr(event, "id", None)
    if not isinstance(event_id, str) or not event_id:
        raise SubagentRuntimeError(
            "Subagent dependency terminal event requires a durable event id"
        )
    return event_id


def _clip(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 1)] + "…"
