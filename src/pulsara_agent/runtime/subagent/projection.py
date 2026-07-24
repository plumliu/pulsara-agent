"""Projection adapters over the canonical subagent graph reducer state."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Protocol, cast

from pulsara_agent.event_log import EventLog, PostgresEventLog
from pulsara_agent.runtime.subagent.facts import SubagentGraphState
from pulsara_agent.runtime.subagent.immutable import thaw_json_value
from pulsara_agent.runtime.subagent.types import (
    SubagentEdge,
    SubagentGraphNode,
    SubagentGraphProjection,
    SubagentStatus,
    SubagentTaskProjection,
)
from pulsara_agent.storage.postgres_connection_provider import (
    VerifiedPostgresConnectionProviderProtocol,
)


class EventLogLocator(Protocol):
    """Resolve runtime-session ids to EventLog instances for cross-session inspect."""

    def event_log_for_runtime_session(self, runtime_session_id: str) -> EventLog: ...


class InMemoryEventLogLocator:
    """Simple test-support locator; production uses the PostgreSQL locator."""

    def __init__(self, logs: dict[str, EventLog] | None = None) -> None:
        self._logs: dict[str, EventLog] = dict(logs or {})

    def register(self, runtime_session_id: str, event_log: EventLog) -> None:
        self._logs[runtime_session_id] = event_log

    def event_log_for_runtime_session(self, runtime_session_id: str) -> EventLog:
        try:
            return self._logs[runtime_session_id]
        except KeyError as exc:
            raise KeyError(f"Unknown runtime session event log: {runtime_session_id}") from exc


class PostgresEventLogLocator:
    """Open child ledgers by runtime-session id over the shared PostgreSQL truth store."""

    def __init__(
        self,
        *,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        workspace_root: str | Path,
    ) -> None:
        self._connection_provider = connection_provider
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._lock = RLock()
        self._logs: dict[str, EventLog] = {}

    def register(self, runtime_session_id: str, event_log: EventLog) -> None:
        if getattr(event_log, "runtime_session_id", runtime_session_id) != runtime_session_id:
            raise ValueError("EventLog runtime session identity mismatch")
        with self._lock:
            self._logs[runtime_session_id] = event_log

    def event_log_for_runtime_session(self, runtime_session_id: str) -> EventLog:
        with self._lock:
            event_log = self._logs.get(runtime_session_id)
            if event_log is None:
                event_log = PostgresEventLog(
                    connection_provider=self._connection_provider,
                    runtime_session_id=runtime_session_id,
                    workspace_root=self._workspace_root,
                )
                self._logs[runtime_session_id] = event_log
            return event_log


def project_subagent_graph(
    parent_runtime_session_id: str,
    state: SubagentGraphState,
    *,
    locator: EventLogLocator | None = None,
) -> SubagentGraphProjection:
    """Render list/inspect DTOs without re-interpreting the event stream."""

    consumed_result_ids = {
        item.result_id for item in state.consumptions.values() if item.result_id is not None
    }
    consumed_task_ids = {
        item.task_id for item in state.consumptions.values() if item.task_id is not None
    }
    delivered_result_ids = set(state.deliveries)

    nodes: list[SubagentGraphNode] = []
    for run in state.runs.values():
        result = state.results.get(run.result_id or "")
        nodes.append(
            SubagentGraphNode(
                subagent_run_id=run.subagent_run_id,
                child_runtime_session_id=run.child_runtime_session_id,
                status=cast(SubagentStatus, run.status),
                label=run.label,
                role=run.role,
                phase=run.phase,
                result_id=run.result_id,
                result_artifact_id=(
                    result.final_message_artifact_id if result is not None else None
                ),
                delivered=bool(run.result_id and run.result_id in delivered_result_ids),
                consumed_by_wait=bool(run.result_id and run.result_id in consumed_result_ids),
            )
        )

    edges = tuple(
        SubagentEdge(
            edge_id=edge.edge_id,
            edge_kind=edge.edge_kind,
            parent_runtime_session_id=edge.parent_runtime_session_id,
            parent_run_id=edge.parent_run_id,
            parent_turn_id=edge.parent_turn_id,
            parent_reply_id=edge.parent_reply_id,
            subagent_run_id=edge.subagent_run_id,
            child_runtime_session_id=edge.child_runtime_session_id,
            child_run_id=edge.child_run_id,
            source_context_id=edge.source_context_id,
            source_model_call_index=edge.source_model_call_index,
            source_tool_call_id=edge.source_tool_call_id,
            source_tool_name=edge.source_tool_name,
            target_context_id=edge.target_context_id,
            created_at=edge.provenance.created_at,
            payload_artifact_id=edge.payload_artifact_id,
            result_id=edge.result_id,
            result_artifact_id=edge.result_artifact_id,
            returned_to_tool_call_id=edge.returned_to_tool_call_id,
        )
        for edge in sorted(state.edges.values(), key=lambda item: item.provenance.created_sequence)
    )

    tasks: list[SubagentTaskProjection] = []
    for task in state.tasks.values():
        result = state.results.get(task.result_id or "")
        run = state.runs.get(task.current_run_id or "")
        pending_state = (
            task.status
            if task.status in {"waiting_dependency", "blocked_dependency_failed"}
            else run.pending_kind if run is not None and run.status == "suspended" else None
        )
        tasks.append(
            SubagentTaskProjection(
                task_id=task.task_id,
                batch_id=task.batch_id,
                create_tool_call_id=task.create_tool_call_id,
                parent_run_id=task.parent_run_id,
                parent_turn_id=task.parent_turn_id,
                parent_reply_id=task.parent_reply_id,
                task_key=task.task_key,
                label=task.label,
                profile_id=task.profile_id,
                display_role=task.display_role,
                objective_preview=task.objective_preview,
                status=task.status,
                depends_on=task.depends_on,
                current_run_id=task.current_run_id,
                has_child_run=task.current_run_id is not None,
                run_index=task.run_index,
                phase=task.phase,
                result_id=task.result_id,
                primary_result_artifact_id=(
                    result.final_message_artifact_id if result is not None else None
                ),
                delivered=bool(task.result_id and task.result_id in delivered_result_ids),
                consumed_by_wait=(
                    task.task_id in consumed_task_ids
                    or bool(task.result_id and task.result_id in consumed_result_ids)
                ),
                pending_state=pending_state,
                blocked_reason=task.blocked_reason,
                blocked_by_task_ids=task.blocked_by_task_ids,
                dependency_status_snapshot=dict(task.dependency_status_snapshot),
                dependency_terminal_event_ids=dict(task.dependency_terminal_event_ids),
                dependency_generation=task.dependency_generation,
            )
        )

    diagnostics: list[dict[str, object]] = []
    for item in state.diagnostics:
        thawed = thaw_json_value(item)
        assert isinstance(thawed, dict)
        diagnostics.append(thawed)
    if locator is not None:
        for run in state.runs.values():
            try:
                locator.event_log_for_runtime_session(run.child_runtime_session_id)
            except KeyError:
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "subagent_child_event_log_missing",
                        "message": "Child runtime session event log could not be opened.",
                        "subagent_run_id": run.subagent_run_id,
                        "child_runtime_session_id": run.child_runtime_session_id,
                    }
                )

    return SubagentGraphProjection(
        parent_runtime_session_id=parent_runtime_session_id,
        nodes=tuple(sorted(nodes, key=lambda node: node.subagent_run_id)),
        edges=edges,
        tasks=tuple(sorted(tasks, key=lambda task: task.task_id)),
        diagnostics=tuple(diagnostics),
    )
