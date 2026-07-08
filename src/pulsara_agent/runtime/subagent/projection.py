"""Subagent graph projection over parent and child runtime-session event logs."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol

from pulsara_agent.event import (
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
from pulsara_agent.runtime.subagent.types import (
    SubagentEdge,
    SubagentGraphNode,
    SubagentGraphProjection,
    SubagentStatus,
    SubagentTaskProjection,
)


class EventLogLocator(Protocol):
    """Resolve runtime-session ids to EventLog instances for cross-session inspect."""

    def event_log_for_runtime_session(self, runtime_session_id: str) -> EventLog: ...


class InMemoryEventLogLocator:
    """Simple locator for tests and in-memory host wiring."""

    def __init__(self, logs: dict[str, EventLog] | None = None) -> None:
        self._logs: dict[str, EventLog] = dict(logs or {})

    def register(self, runtime_session_id: str, event_log: EventLog) -> None:
        self._logs[runtime_session_id] = event_log

    def event_log_for_runtime_session(self, runtime_session_id: str) -> EventLog:
        try:
            return self._logs[runtime_session_id]
        except KeyError as exc:
            raise KeyError(f"Unknown runtime session event log: {runtime_session_id}") from exc


def project_subagent_graph(
    parent_runtime_session_id: str,
    parent_event_log: EventLog,
    *,
    locator: EventLogLocator | None = None,
) -> SubagentGraphProjection:
    parent_events = parent_event_log.iter()
    starts: dict[str, SubagentRunStartedEvent] = {}
    nodes: dict[str, SubagentGraphNode] = {}
    tasks: dict[str, SubagentTaskProjection] = {}
    edges: list[SubagentEdge] = []
    delivered: set[str] = set()
    consumed: set[str] = set()
    diagnostics: list[dict[str, object]] = []

    for event in parent_events:
        if isinstance(event, SubagentRunStartedEvent):
            starts[event.subagent_run_id] = event
            nodes[event.subagent_run_id] = SubagentGraphNode(
                subagent_run_id=event.subagent_run_id,
                child_runtime_session_id=event.child_runtime_session_id,
                status="running",
                label=event.label,
                role=event.role,
            )
            _upsert_edge(edges, _spawn_edge(event))
        elif isinstance(event, SubagentMessageSentEvent):
            _upsert_edge(edges, _message_edge(event))
        elif isinstance(event, SubagentEdgeRecordedEvent):
            edge = _recorded_edge(event)
            edges.append(edge)
            if event.edge_kind == "wait" and event.result_id:
                consumed.add(event.result_id)
        elif isinstance(event, SubagentResultConsumedEvent):
            if event.result_id:
                consumed.add(event.result_id)
            if event.kind == "wait_task" and event.task_id:
                _update_task(
                    tasks,
                    event.task_id,
                    consumed_by_wait=True,
                )
        elif isinstance(event, SubagentRunCompletedEvent):
            _update_node(
                nodes,
                event.subagent_run_id,
                status="completed",
                result_id=event.result_id,
                result_artifact_id=event.result_artifact_id,
            )
        elif isinstance(event, SubagentRunFailedEvent):
            _update_node(nodes, event.subagent_run_id, status="failed")
        elif isinstance(event, SubagentRunCancelledEvent):
            _update_node(nodes, event.subagent_run_id, status="cancelled")
        elif isinstance(event, SubagentRunSuspendedEvent):
            _update_node(nodes, event.subagent_run_id, status="suspended")
        elif isinstance(event, SubagentPhaseReportedEvent):
            _update_node(nodes, event.subagent_run_id, phase=event.phase)
            if event.task_id:
                _update_task(tasks, event.task_id, phase=event.phase)
        elif isinstance(event, SubagentResultSubmittedEvent):
            _update_node(
                nodes,
                event.subagent_run_id,
                result_id=event.result_id,
                result_artifact_id=event.result_artifact_id,
            )
        elif isinstance(event, SubagentResultDeliveredEvent):
            delivered.add(event.result_id)
            _update_node(
                nodes,
                event.subagent_run_id,
                delivered=True,
                result_id=event.result_id,
                result_artifact_id=event.result_artifact_id,
            )
        elif isinstance(event, SubagentTaskCreatedEvent):
            tasks[event.task_id] = SubagentTaskProjection(
                task_id=event.task_id,
                batch_id=event.batch_id,
                create_tool_call_id=event.create_tool_call_id,
                parent_run_id=event.run_id,
                parent_turn_id=event.turn_id,
                parent_reply_id=event.reply_id,
                task_key=event.task_key,
                label=event.label,
                profile_id=event.profile_id,
                display_role=event.display_role,
                objective_preview=event.objective_preview,
                status="created",
                depends_on=tuple(event.depends_on),
            )
        elif isinstance(event, SubagentTaskScheduledEvent):
            _update_task(tasks, event.task_id, status="created")
        elif isinstance(event, SubagentTaskStartedEvent):
            _update_task(
                tasks,
                event.task_id,
                status="running",
                current_run_id=event.subagent_run_id,
                has_child_run=True,
                run_index=event.run_index,
            )
        elif isinstance(event, SubagentTaskBlockedEvent):
            _update_task(
                tasks,
                event.task_id,
                status=event.status,
                pending_state=event.status,
                blocked_reason=event.blocked_reason,
                blocked_by_task_ids=tuple(event.blocked_by_task_ids),
                dependency_status_snapshot=dict(event.dependency_status_snapshot),
                dependency_terminal_event_ids=dict(event.dependency_terminal_event_ids),
                dependency_generation=event.dependency_generation,
            )
        elif isinstance(event, SubagentTaskCompletedEvent):
            _update_task(
                tasks,
                event.task_id,
                status="completed",
                current_run_id=event.subagent_run_id,
                has_child_run=event.subagent_run_id is not None,
                result_id=event.result_id,
                primary_result_artifact_id=event.primary_result_artifact_id,
            )
        elif isinstance(event, SubagentTaskFailedEvent):
            _update_task(
                tasks,
                event.task_id,
                status="failed",
                current_run_id=event.subagent_run_id,
                has_child_run=event.subagent_run_id is not None,
            )
        elif isinstance(event, SubagentTaskCancelledEvent):
            _update_task(
                tasks,
                event.task_id,
                status="cancelled",
                current_run_id=event.subagent_run_id,
                has_child_run=event.subagent_run_id is not None,
            )

    if locator is not None:
        for start in starts.values():
            try:
                locator.event_log_for_runtime_session(start.child_runtime_session_id)
            except KeyError:
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "subagent_child_event_log_missing",
                        "message": "Child runtime session event log could not be opened.",
                        "subagent_run_id": start.subagent_run_id,
                        "child_runtime_session_id": start.child_runtime_session_id,
                    }
                )

    final_nodes: list[SubagentGraphNode] = []
    for node in nodes.values():
        final_nodes.append(
            replace(
                node,
                delivered=bool(node.result_id and node.result_id in delivered) or node.delivered,
                consumed_by_wait=bool(node.result_id and node.result_id in consumed),
            )
        )

    final_tasks: list[SubagentTaskProjection] = []
    for task in tasks.values():
        final_tasks.append(
            replace(
                task,
                delivered=bool(task.result_id and task.result_id in delivered) or task.delivered,
                consumed_by_wait=bool(task.result_id and task.result_id in consumed) or task.consumed_by_wait,
            )
        )

    return SubagentGraphProjection(
        parent_runtime_session_id=parent_runtime_session_id,
        nodes=tuple(sorted(final_nodes, key=lambda node: node.subagent_run_id)),
        edges=tuple(edges),
        tasks=tuple(sorted(final_tasks, key=lambda task: task.task_id)),
        diagnostics=tuple(diagnostics),
    )


def _spawn_edge(event: SubagentRunStartedEvent) -> SubagentEdge:
    return SubagentEdge(
        edge_id=event.edge_id,
        edge_kind="spawn",
        parent_runtime_session_id=event.parent_runtime_session_id,
        parent_run_id=event.parent_run_id,
        parent_turn_id=event.parent_turn_id,
        parent_reply_id=event.parent_reply_id,
        subagent_run_id=event.subagent_run_id,
        child_runtime_session_id=event.child_runtime_session_id,
        source_context_id=event.parent_context_id,
        source_model_call_index=event.parent_model_call_index,
        source_tool_call_id=event.spawning_tool_call_id,
        source_tool_name=event.spawning_tool_name,
        created_at=_parse_dt(event.created_at),
    )


def _message_edge(event: SubagentMessageSentEvent) -> SubagentEdge:
    edge_kind = "spawn" if event.delivery_kind == "spawn_task" else event.delivery_kind
    return SubagentEdge(
        edge_id=event.edge_id,
        edge_kind=edge_kind,
        parent_runtime_session_id=event.parent_runtime_session_id,
        parent_run_id=event.parent_run_id,
        parent_turn_id=event.turn_id,
        parent_reply_id=event.reply_id,
        subagent_run_id=event.subagent_run_id,
        child_runtime_session_id=event.child_runtime_session_id,
        payload_artifact_id=event.message_artifact_id,
        created_at=_parse_dt(event.created_at),
    )


def _upsert_edge(edges: list[SubagentEdge], edge: SubagentEdge) -> None:
    for index, existing in enumerate(edges):
        if existing.edge_id != edge.edge_id:
            continue
        edges[index] = replace(
            existing,
            payload_artifact_id=edge.payload_artifact_id or existing.payload_artifact_id,
            result_id=edge.result_id or existing.result_id,
            result_artifact_id=edge.result_artifact_id or existing.result_artifact_id,
            returned_to_tool_call_id=edge.returned_to_tool_call_id or existing.returned_to_tool_call_id,
            metadata=edge.metadata or existing.metadata,
        )
        return
    edges.append(edge)


def _recorded_edge(event: SubagentEdgeRecordedEvent) -> SubagentEdge:
    return SubagentEdge(
        edge_id=event.edge_id,
        edge_kind=event.edge_kind,
        parent_runtime_session_id=event.parent_runtime_session_id,
        parent_run_id=event.parent_run_id,
        parent_turn_id=event.parent_turn_id,
        parent_reply_id=event.parent_reply_id,
        subagent_run_id=event.subagent_run_id,
        child_runtime_session_id=event.child_runtime_session_id,
        child_run_id=event.child_run_id,
        source_context_id=event.source_context_id,
        source_model_call_index=event.source_model_call_index,
        source_tool_call_id=event.source_tool_call_id,
        source_tool_name=event.source_tool_name,
        target_context_id=event.target_context_id,
        created_at=_parse_dt(event.created_at),
        payload_artifact_id=event.payload_artifact_id,
        result_id=event.result_id,
        result_artifact_id=event.result_artifact_id,
        returned_to_tool_call_id=event.returned_to_tool_call_id,
    )


def _update_node(
    nodes: dict[str, SubagentGraphNode],
    subagent_run_id: str,
    *,
    status: SubagentStatus | None = None,
    result_id: str | None = None,
    result_artifact_id: str | None = None,
    delivered: bool | None = None,
    phase: str | None = None,
) -> None:
    node = nodes.get(subagent_run_id)
    if node is None:
        return
    nodes[subagent_run_id] = replace(
        node,
        status=status or node.status,
        result_id=result_id or node.result_id,
        result_artifact_id=result_artifact_id or node.result_artifact_id,
        delivered=node.delivered if delivered is None else delivered,
        phase=phase if phase is not None else node.phase,
    )


def _update_task(
    tasks: dict[str, SubagentTaskProjection],
    task_id: str,
    *,
    status: str | None = None,
    current_run_id: str | None = None,
    has_child_run: bool | None = None,
    run_index: int | None = None,
    phase: str | None = None,
    result_id: str | None = None,
    primary_result_artifact_id: str | None = None,
    delivered: bool | None = None,
    consumed_by_wait: bool | None = None,
    pending_state: str | None = None,
    blocked_reason: str | None = None,
    blocked_by_task_ids: tuple[str, ...] | None = None,
    dependency_status_snapshot: dict[str, str] | None = None,
    dependency_terminal_event_ids: dict[str, str] | None = None,
    dependency_generation: int | None = None,
) -> None:
    task = tasks.get(task_id)
    if task is None:
        return
    tasks[task_id] = replace(
        task,
        status=status or task.status,
        current_run_id=current_run_id or task.current_run_id,
        has_child_run=task.has_child_run if has_child_run is None else has_child_run,
        run_index=run_index if run_index is not None else task.run_index,
        phase=phase if phase is not None else task.phase,
        result_id=result_id or task.result_id,
        primary_result_artifact_id=primary_result_artifact_id or task.primary_result_artifact_id,
        delivered=task.delivered if delivered is None else delivered,
        consumed_by_wait=task.consumed_by_wait if consumed_by_wait is None else consumed_by_wait,
        pending_state=pending_state if pending_state is not None else task.pending_state,
        blocked_reason=blocked_reason if blocked_reason is not None else task.blocked_reason,
        blocked_by_task_ids=(
            blocked_by_task_ids if blocked_by_task_ids is not None else task.blocked_by_task_ids
        ),
        dependency_status_snapshot=(
            dependency_status_snapshot
            if dependency_status_snapshot is not None
            else task.dependency_status_snapshot
        ),
        dependency_terminal_event_ids=(
            dependency_terminal_event_ids
            if dependency_terminal_event_ids is not None
            else task.dependency_terminal_event_ids
        ),
        dependency_generation=(
            dependency_generation if dependency_generation is not None else task.dependency_generation
        ),
    )


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
