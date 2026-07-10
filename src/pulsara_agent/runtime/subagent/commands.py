"""Pure planning and pre-commit validation for subagent graph commands."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
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
from pulsara_agent.runtime.subagent.facts import SubagentGraphState
from pulsara_agent.runtime.subagent.invariants import (
    creation_attribution_matches,
    run_attribution_error,
)
from pulsara_agent.runtime.subagent.reducer import apply_subagent_event


@dataclass(frozen=True, slots=True)
class PlannedChildReservation:
    reservation_id: str
    parent_run_id: str
    count: int

    def __post_init__(self) -> None:
        if not self.reservation_id:
            raise ValueError("reservation_id is required")
        if not self.parent_run_id:
            raise ValueError("parent_run_id is required")
        if self.count < 1:
            raise ValueError("reservation count must be >= 1")


@dataclass(frozen=True, slots=True)
class SubagentCommandDiagnostic:
    code: str
    severity: Literal["warning", "error"]
    entity_kind: Literal["task", "run", "result", "command"] = "command"
    entity_id: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedSubagentWrite:
    operation: str
    expected_through_sequence: int
    events: tuple[AgentEvent, ...]
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None
    command_id: str = field(default_factory=lambda: f"subagent_command:{uuid4().hex}")
    affected_task_ids: tuple[str, ...] = ()
    affected_run_ids: tuple[str, ...] = ()
    required_reservations: tuple[PlannedChildReservation, ...] = ()
    diagnostics: tuple[SubagentCommandDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not self.operation:
            raise ValueError("operation is required")
        if not self.command_id:
            raise ValueError("command_id is required")
        if self.expected_through_sequence < 0:
            raise ValueError("expected_through_sequence must be >= 0")
        if not self.events:
            raise ValueError("PlannedSubagentWrite requires at least one event")
        event_ids = [event.id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("PlannedSubagentWrite event ids must be unique")
        if any(event.sequence is not None for event in self.events):
            raise ValueError("PlannedSubagentWrite only accepts uncommitted events")
        derived_tasks, derived_runs = _affected_ids(self.events)
        if self.affected_task_ids and self.affected_task_ids != derived_tasks:
            raise ValueError("affected_task_ids do not match planned events")
        if self.affected_run_ids and self.affected_run_ids != derived_runs:
            raise ValueError("affected_run_ids do not match planned events")
        object.__setattr__(self, "affected_task_ids", derived_tasks)
        object.__setattr__(self, "affected_run_ids", derived_runs)


class SubagentCommandPlanError(RuntimeError):
    """A command does not describe a legal transition from its reducer snapshot."""


class SubagentCommandPlanner:
    """Validate plans without creating canonical reducer provenance.

    The planner only reads immutable reducer facts.  It never reads artifacts,
    creates sessions, allocates canonical sequence numbers, or publishes.
    """

    def validate(
        self,
        plan: PlannedSubagentWrite,
        *,
        state: SubagentGraphState,
    ) -> PlannedSubagentWrite:
        if plan.expected_through_sequence != state.through_sequence:
            # The EventLog CAS is the authority for a stale plan.  Do not
            # reinterpret it against a newer in-process snapshot.
            return plan
        if not state.consistent:
            raise SubagentCommandPlanError("subagent graph is inconsistent")
        validate_planned_transitions(state, plan.events)
        return replace(plan)


def validate_planned_transitions(
    state: SubagentGraphState,
    events: tuple[AgentEvent, ...],
) -> None:
    """Perform logical preflight without feeding uncommitted events to reducer."""

    task_status = {task_id: fact.status for task_id, fact in state.tasks.items()}
    task_run = {task_id: fact.current_run_id for task_id, fact in state.tasks.items()}
    task_batch = {task_id: fact.batch_id for task_id, fact in state.tasks.items()}
    task_create_tool_call = {
        task_id: fact.create_tool_call_id for task_id, fact in state.tasks.items()
    }
    task_result = {task_id: fact.result_id for task_id, fact in state.tasks.items()}
    task_terminal_event = {
        task_id: fact.provenance.terminal_event_id
        for task_id, fact in state.tasks.items()
    }
    run_status = {run_id: fact.status for run_id, fact in state.runs.items()}
    run_task = {run_id: fact.task_id for run_id, fact in state.runs.items()}
    run_result = {run_id: fact.result_id for run_id, fact in state.runs.items()}
    run_parent_runtime = {
        run_id: fact.parent_runtime_session_id for run_id, fact in state.runs.items()
    }
    run_child_runtime = {
        run_id: fact.child_runtime_session_id for run_id, fact in state.runs.items()
    }
    run_reported_child = {
        run_id: fact.reported_child_run_id for run_id, fact in state.runs.items()
    }
    result_run = {result_id: fact.subagent_run_id for result_id, fact in state.results.items()}
    result_status = {result_id: fact.status for result_id, fact in state.results.items()}
    result_summary = {result_id: fact.summary for result_id, fact in state.results.items()}
    result_artifact = {
        result_id: fact.final_message_artifact_id
        for result_id, fact in state.results.items()
    }
    result_artifact_ids = {
        result_id: fact.artifact_ids for result_id, fact in state.results.items()
    }
    result_source = {
        result_id: fact.result_source for result_id, fact in state.results.items()
    }
    edge_identity = {
        edge_id: (
            fact.edge_kind,
            fact.parent_runtime_session_id,
            fact.parent_run_id,
            fact.parent_turn_id,
            fact.parent_reply_id,
            fact.subagent_run_id,
            fact.child_runtime_session_id,
        )
        for edge_id, fact in state.edges.items()
    }
    edge_payload = {
        edge_id: fact.payload_artifact_id for edge_id, fact in state.edges.items()
    }
    consumption_ids = set(state.consumptions)
    delivery_result_ids = set(state.deliveries)

    for event in events:
        if isinstance(event, SubagentTaskCreatedEvent):
            _require_absent(task_status, event.task_id, "task")
            task_status[event.task_id] = "created"
            task_run[event.task_id] = None
            task_batch[event.task_id] = event.batch_id
            task_create_tool_call[event.task_id] = event.create_tool_call_id
            task_result[event.task_id] = None
            task_terminal_event[event.task_id] = None
            continue
        if isinstance(event, SubagentTaskScheduledEvent):
            _require_status(task_status, event.task_id, {"created", "waiting_dependency"}, "task")
            _require_task_creation_attribution(
                event.task_id,
                batch_id=event.batch_id,
                create_tool_call_id=event.create_tool_call_id,
                task_batch=task_batch,
                task_create_tool_call=task_create_tool_call,
            )
            continue
        if isinstance(event, SubagentRunStartedEvent):
            _require_absent(run_status, event.subagent_run_id, "run")
            if event.task_id is not None:
                _require_status(
                    task_status,
                    event.task_id,
                    {"created", "waiting_dependency"},
                    "task",
                )
                if task_run[event.task_id] is not None:
                    raise SubagentCommandPlanError(
                        f"task already owns a run: {event.task_id}"
                    )
            run_status[event.subagent_run_id] = "running"
            run_task[event.subagent_run_id] = event.task_id
            run_result[event.subagent_run_id] = None
            run_parent_runtime[event.subagent_run_id] = event.parent_runtime_session_id
            run_child_runtime[event.subagent_run_id] = event.child_runtime_session_id
            run_reported_child[event.subagent_run_id] = None
            _require_absent(edge_identity, event.edge_id, "edge")
            edge_identity[event.edge_id] = (
                "spawn",
                event.parent_runtime_session_id,
                event.parent_run_id,
                event.parent_turn_id or event.turn_id,
                event.parent_reply_id or event.reply_id,
                event.subagent_run_id,
                event.child_runtime_session_id,
            )
            edge_payload[event.edge_id] = None
            continue
        if isinstance(event, SubagentMessageSentEvent):
            _require_present(run_status, event.subagent_run_id, "run")
            _require_run_session_attribution(
                event.subagent_run_id,
                parent_runtime_session_id=event.parent_runtime_session_id,
                child_runtime_session_id=event.child_runtime_session_id,
                reported_child_run_id=None,
                run_parent_runtime=run_parent_runtime,
                run_child_runtime=run_child_runtime,
                run_reported_child=run_reported_child,
            )
            kind = "spawn" if event.delivery_kind == "spawn_task" else event.delivery_kind
            identity = (
                kind,
                event.parent_runtime_session_id,
                event.parent_run_id,
                event.turn_id,
                event.reply_id,
                event.subagent_run_id,
                event.child_runtime_session_id,
            )
            existing_identity = edge_identity.get(event.edge_id)
            if existing_identity is not None and existing_identity != identity:
                raise SubagentCommandPlanError("subagent edge identity conflict")
            existing_payload = edge_payload.get(event.edge_id)
            if existing_payload is not None and existing_payload != event.message_artifact_id:
                raise SubagentCommandPlanError("subagent edge payload conflict")
            edge_identity[event.edge_id] = identity
            edge_payload[event.edge_id] = event.message_artifact_id
            continue
        if isinstance(event, SubagentTaskStartedEvent):
            _require_status(
                task_status,
                event.task_id,
                {"created", "waiting_dependency"},
                "task",
            )
            _require_status(run_status, event.subagent_run_id, {"running"}, "run")
            if run_task[event.subagent_run_id] != event.task_id:
                raise SubagentCommandPlanError("task/run attribution mismatch")
            task_status[event.task_id] = "running"
            task_run[event.task_id] = event.subagent_run_id
            continue
        if isinstance(event, SubagentTaskBlockedEvent):
            _require_status(
                task_status,
                event.task_id,
                {"created", "waiting_dependency"},
                "task",
            )
            task_status[event.task_id] = event.status
            continue
        if isinstance(event, SubagentResultSubmittedEvent):
            _require_status(run_status, event.subagent_run_id, {"running", "suspended"}, "run")
            if event.task_id is not None and run_task[event.subagent_run_id] != event.task_id:
                raise SubagentCommandPlanError("result task/run attribution mismatch")
            if run_result[event.subagent_run_id] is not None:
                raise SubagentCommandPlanError("run already owns a result")
            _require_absent(result_run, event.result_id, "result")
            result_run[event.result_id] = event.subagent_run_id
            result_status[event.result_id] = "submitted"
            result_summary[event.result_id] = event.summary
            result_artifact[event.result_id] = event.result_artifact_id
            result_artifact_ids[event.result_id] = tuple(event.artifact_ids)
            result_source[event.result_id] = "explicit"
            run_result[event.subagent_run_id] = event.result_id
            continue
        if isinstance(event, SubagentRunSuspendedEvent):
            _require_status(run_status, event.subagent_run_id, {"running"}, "run")
            _require_run_session_attribution(
                event.subagent_run_id,
                parent_runtime_session_id=event.parent_runtime_session_id,
                child_runtime_session_id=event.child_runtime_session_id,
                reported_child_run_id=None,
                run_parent_runtime=run_parent_runtime,
                run_child_runtime=run_child_runtime,
                run_reported_child=run_reported_child,
            )
            run_status[event.subagent_run_id] = "suspended"
            continue
        if isinstance(event, SubagentRunCompletedEvent):
            _require_status(run_status, event.subagent_run_id, {"running", "suspended"}, "run")
            _require_run_session_attribution(
                event.subagent_run_id,
                parent_runtime_session_id=event.parent_runtime_session_id,
                child_runtime_session_id=event.child_runtime_session_id,
                reported_child_run_id=event.child_run_id,
                run_parent_runtime=run_parent_runtime,
                run_child_runtime=run_child_runtime,
                run_reported_child=run_reported_child,
            )
            existing_run_result = run_result[event.subagent_run_id]
            if existing_run_result is not None and existing_run_result != event.result_id:
                raise SubagentCommandPlanError(
                    "completion cannot replace explicit result identity"
                )
            owner = result_run.get(event.result_id)
            if owner not in {None, event.subagent_run_id}:
                raise SubagentCommandPlanError("result/run attribution mismatch")
            if owner is not None and existing_run_result is None:
                raise SubagentCommandPlanError("completion result already exists")
            if existing_run_result is not None:
                if (
                    result_status.get(event.result_id) != "submitted"
                    or result_source.get(event.result_id) != "explicit"
                    or result_summary.get(event.result_id) != event.summary
                    or result_artifact.get(event.result_id) != event.result_artifact_id
                    or result_artifact_ids.get(event.result_id)
                    != tuple(event.artifact_ids)
                ):
                    raise SubagentCommandPlanError(
                        "completion cannot replace explicit result body"
                    )
            else:
                result_summary[event.result_id] = event.summary
                result_artifact[event.result_id] = event.result_artifact_id
                result_artifact_ids[event.result_id] = tuple(event.artifact_ids)
                result_source[event.result_id] = "inferred"
            result_run[event.result_id] = event.subagent_run_id
            result_status[event.result_id] = "completed"
            run_result[event.subagent_run_id] = event.result_id
            run_status[event.subagent_run_id] = "completed"
            if event.child_run_id is not None:
                run_reported_child[event.subagent_run_id] = event.child_run_id
            continue
        if isinstance(event, (SubagentRunFailedEvent, SubagentRunCancelledEvent)):
            _require_status(run_status, event.subagent_run_id, {"running", "suspended"}, "run")
            _require_run_session_attribution(
                event.subagent_run_id,
                parent_runtime_session_id=event.parent_runtime_session_id,
                child_runtime_session_id=event.child_runtime_session_id,
                reported_child_run_id=None,
                run_parent_runtime=run_parent_runtime,
                run_child_runtime=run_child_runtime,
                run_reported_child=run_reported_child,
            )
            run_status[event.subagent_run_id] = (
                "failed" if isinstance(event, SubagentRunFailedEvent) else "cancelled"
            )
            continue
        if isinstance(event, SubagentTaskCompletedEvent):
            _require_status(task_status, event.task_id, {"running"}, "task")
            _require_status(run_status, event.subagent_run_id, {"completed"}, "run")
            if task_run[event.task_id] != event.subagent_run_id:
                raise SubagentCommandPlanError("task/run attribution mismatch")
            if result_run.get(event.result_id) != event.subagent_run_id:
                raise SubagentCommandPlanError("task completion result attribution mismatch")
            if run_result[event.subagent_run_id] != event.result_id:
                raise SubagentCommandPlanError("run completion result attribution mismatch")
            if result_artifact.get(event.result_id) != event.primary_result_artifact_id:
                raise SubagentCommandPlanError("task completion artifact mismatch")
            task_status[event.task_id] = "completed"
            task_result[event.task_id] = event.result_id
            task_terminal_event[event.task_id] = event.id
            continue
        if isinstance(event, (SubagentTaskFailedEvent, SubagentTaskCancelledEvent)):
            allowed = {"created", "waiting_dependency", "running"}
            if isinstance(event, SubagentTaskCancelledEvent) and event.repair_id is not None:
                allowed.add("blocked_dependency_failed")
            _require_status(task_status, event.task_id, allowed, "task")
            terminal_status = (
                "failed" if isinstance(event, SubagentTaskFailedEvent) else "cancelled"
            )
            owning_run_id = task_run[event.task_id]
            if owning_run_id is None:
                if event.subagent_run_id is not None:
                    raise SubagentCommandPlanError("task terminal run attribution mismatch")
            else:
                if event.subagent_run_id != owning_run_id:
                    raise SubagentCommandPlanError("task terminal run attribution mismatch")
                _require_status(run_status, owning_run_id, {terminal_status}, "run")
            task_status[event.task_id] = terminal_status
            task_terminal_event[event.task_id] = event.id
            continue
        if isinstance(event, SubagentPhaseReportedEvent):
            _require_status(run_status, event.subagent_run_id, {"running", "suspended"}, "run")
            continue
        if isinstance(event, SubagentEdgeRecordedEvent):
            _require_present(run_status, event.subagent_run_id, "run")
            _require_run_session_attribution(
                event.subagent_run_id,
                parent_runtime_session_id=event.parent_runtime_session_id,
                child_runtime_session_id=event.child_runtime_session_id,
                reported_child_run_id=event.child_run_id,
                run_parent_runtime=run_parent_runtime,
                run_child_runtime=run_child_runtime,
                run_reported_child=run_reported_child,
            )
            _require_absent(edge_identity, event.edge_id, "edge")
            if event.result_id is not None:
                _require_present(result_run, event.result_id, "result")
                if result_run[event.result_id] != event.subagent_run_id:
                    raise SubagentCommandPlanError("edge result/run attribution mismatch")
                if result_status.get(event.result_id) != "completed":
                    raise SubagentCommandPlanError("edge result is not completed")
                if result_artifact.get(event.result_id) != event.result_artifact_id:
                    raise SubagentCommandPlanError("edge result artifact mismatch")
            edge_identity[event.edge_id] = (
                event.edge_kind,
                event.parent_runtime_session_id,
                event.parent_run_id,
                event.parent_turn_id,
                event.parent_reply_id,
                event.subagent_run_id,
                event.child_runtime_session_id,
            )
            edge_payload[event.edge_id] = event.payload_artifact_id
            if event.child_run_id is not None:
                run_reported_child[event.subagent_run_id] = event.child_run_id
            continue
        if isinstance(event, SubagentResultConsumedEvent):
            _require_absent(consumption_ids, event.consumption_id, "consumption")
            if event.task_id is not None:
                _require_present(task_status, event.task_id, "task")
                if task_status[event.task_id] != event.consumed_status:
                    raise SubagentCommandPlanError("consumption task status mismatch")
            if event.subagent_run_id is not None:
                _require_present(run_status, event.subagent_run_id, "run")
                if run_status[event.subagent_run_id] != event.consumed_status:
                    raise SubagentCommandPlanError("consumption run status mismatch")
            if event.result_id is not None:
                _require_present(result_run, event.result_id, "result")
                if (
                    event.subagent_run_id is not None
                    and result_run[event.result_id] != event.subagent_run_id
                ):
                    raise SubagentCommandPlanError(
                        "consumption result/run attribution mismatch"
                    )
                if result_status.get(event.result_id) != "completed":
                    raise SubagentCommandPlanError("consumption result is not completed")
            if event.task_id is not None:
                if (
                    event.subagent_run_id is not None
                    and task_run[event.task_id] != event.subagent_run_id
                ):
                    raise SubagentCommandPlanError("consumption task/run attribution mismatch")
                if (
                    event.result_id is not None
                    and task_result[event.task_id] != event.result_id
                ):
                    raise SubagentCommandPlanError("consumption task/result attribution mismatch")
                if (
                    event.result_id is None
                    and task_terminal_event[event.task_id] != event.terminal_event_id
                ):
                    raise SubagentCommandPlanError("consumption terminal event mismatch")
            consumption_ids.add(event.consumption_id)
            continue
        if isinstance(event, SubagentResultDeliveredEvent):
            _require_present(result_run, event.result_id, "result")
            if result_run[event.result_id] != event.subagent_run_id:
                raise SubagentCommandPlanError("delivery result/run attribution mismatch")
            _require_run_session_attribution(
                event.subagent_run_id,
                parent_runtime_session_id=event.parent_runtime_session_id,
                child_runtime_session_id=None,
                reported_child_run_id=None,
                run_parent_runtime=run_parent_runtime,
                run_child_runtime=run_child_runtime,
                run_reported_child=run_reported_child,
            )
            if result_artifact.get(event.result_id) != event.result_artifact_id:
                raise SubagentCommandPlanError("delivery result artifact mismatch")
            if event.result_id in delivery_result_ids:
                raise SubagentCommandPlanError(
                    f"result already delivered: {event.result_id}"
                )
            delivery_result_ids.add(event.result_id)

    _require_reducer_acceptance(state, events)


def _affected_ids(events: tuple[AgentEvent, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    task_ids: set[str] = set()
    run_ids: set[str] = set()
    for event in events:
        task_id = getattr(event, "task_id", None)
        run_id = getattr(event, "subagent_run_id", None)
        if isinstance(task_id, str) and task_id:
            task_ids.add(task_id)
        if isinstance(run_id, str) and run_id:
            run_ids.add(run_id)
    return tuple(sorted(task_ids)), tuple(sorted(run_ids))


def _require_present(values: object, key: str, kind: str) -> None:
    if key not in values:  # type: ignore[operator]
        raise SubagentCommandPlanError(f"unknown {kind}: {key}")


def _require_absent(values: object, key: str, kind: str) -> None:
    if key in values:  # type: ignore[operator]
        raise SubagentCommandPlanError(f"duplicate {kind}: {key}")


def _require_status(
    values: dict[str, str],
    key: str,
    allowed: set[str],
    kind: str,
) -> None:
    _require_present(values, key, kind)
    status = values[key]
    if status not in allowed:
        raise SubagentCommandPlanError(
            f"invalid {kind} transition for {key}: {status} not in {sorted(allowed)}"
        )


def _require_run_session_attribution(
    run_id: str,
    *,
    parent_runtime_session_id: str,
    child_runtime_session_id: str | None,
    reported_child_run_id: str | None,
    run_parent_runtime: dict[str, str],
    run_child_runtime: dict[str, str],
    run_reported_child: dict[str, str | None],
) -> None:
    code = run_attribution_error(
        expected_parent_runtime_session_id=run_parent_runtime[run_id],
        expected_child_runtime_session_id=run_child_runtime[run_id],
        expected_reported_child_run_id=run_reported_child[run_id],
        parent_runtime_session_id=parent_runtime_session_id,
        child_runtime_session_id=child_runtime_session_id,
        reported_child_run_id=reported_child_run_id,
    )
    if code == "child_run_attribution_mismatch":
        raise SubagentCommandPlanError("reported child run attribution mismatch")
    if code is not None:
        raise SubagentCommandPlanError("run runtime-session attribution mismatch")


def _require_task_creation_attribution(
    task_id: str,
    *,
    batch_id: str | None,
    create_tool_call_id: str | None,
    task_batch: dict[str, str | None],
    task_create_tool_call: dict[str, str | None],
) -> None:
    if not creation_attribution_matches(
        expected_batch_id=task_batch[task_id],
        expected_create_tool_call_id=task_create_tool_call[task_id],
        batch_id=batch_id,
        create_tool_call_id=create_tool_call_id,
    ):
        raise SubagentCommandPlanError("task creation attribution mismatch")


def _require_reducer_acceptance(
    state: SubagentGraphState,
    events: tuple[AgentEvent, ...],
) -> None:
    """Differential guard: every accepted plan must also satisfy the reducer."""

    working = state
    for event in events:
        simulated = event.model_copy(
            update={"sequence": working.through_sequence + 1}
        )
        next_state = apply_subagent_event(working, simulated)
        if working.consistent and not next_state.consistent:
            diagnostic = next_state.diagnostics[-1]
            raise SubagentCommandPlanError(
                f"reducer rejected planned event: {diagnostic.code}"
            )
        working = next_state
