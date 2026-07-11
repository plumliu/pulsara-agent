"""Deterministic read-only Pulsara Inspector service."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from pulsara_agent.event import (
    AgentEvent,
    CapabilityGateDecisionEvent,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    CustomEvent,
    MemoryReflectionCompletedEvent,
    MemoryReflectionFailedEvent,
    ModelCallEndEvent,
    ModelCallRejectedEvent,
    ModelCallStartEvent,
    ProjectionReadyEvent,
    ReplyStartEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import PostgresEventLog, dump_agent_event
from pulsara_agent.graph.oxigraph import OxigraphGraphStore
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.inspector.diagnostics import (
    outbox_diagnostics,
    permission_snapshot_diagnostics,
    run_projection_diagnostics,
    sequence_gap_diagnostics,
    tool_flow_diagnostics,
)
from pulsara_agent.inspector.store import PostgresInspectorStore
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.message import AssistantMsg, Msg
from pulsara_agent.message.blocks import (
    DataBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from pulsara_agent.message.reducer import MessageReducer
from pulsara_agent.runtime.timeline import build_run_timeline
from pulsara_agent.runtime.subagent.projection import project_subagent_graph
from pulsara_agent.runtime.subagent.reducer import fold_subagent_graph


_REQUIRED_TABLES = (
    "sessions",
    "runs",
    "turns",
    "agent_events",
    "artifacts",
    "tool_result_artifacts",
    "working_context_summaries",
    "graph_documents",
    "memory_nodes",
    "memory_search_index",
    "memory_vector_index",
    "memory_write_outbox",
    "memory_candidates",
    "memory_governance_decisions",
    "recall_traces",
    "recall_usages",
)


@dataclass(slots=True)
class InspectorService:
    store: PostgresInspectorStore
    oxigraph_url: str | None = None

    def inspect_session(
        self,
        session_id: str,
        *,
        limit_events: int = 200,
        include_payload: bool = False,
    ) -> dict[str, Any]:
        session = self.store.session(session_id)
        if session is None:
            raise KeyError(session_id)
        events = self.store.events_for_session(session_id)
        runs = self.store.session_runs(session_id)
        diagnostics = sequence_gap_diagnostics(events)
        diagnostics.extend(_compaction_diagnostics(events, self.store))
        model_contracts = _model_contract_projection(events)
        diagnostics.extend(model_contracts["diagnostics"])
        return {
            "inspect_kind": "session",
            "session": _json_safe(session),
            "runs": [_json_safe(run) for run in runs],
            "event_counts": self.store.event_counts_for_session(session_id),
            "current_working_context_summaries": [
                _summarize_working_context(row)
                for row in self.store.working_context_for_session(session_id)
            ],
            "capability_surface_as_seen": _capability_surface_projection(events),
            "context_compilations": _context_compilation_projection(events),
            "model_targets": model_contracts["model_targets"],
            "model_calls": model_contracts["model_calls"],
            "model_usage_by_run": model_contracts["usage_by_run"],
            "compaction_model_contracts": model_contracts["compaction_model_contracts"],
            "reflection_model_contracts": model_contracts["reflection_model_contracts"],
            "compaction_windows": _compaction_windows(events, self.store),
            "subagent_graph": _subagent_graph_projection(
                session_id,
                events,
                self.store,
            ),
            "events": _event_summaries(
                events[:limit_events], include_payload=include_payload
            ),
            "event_count": len(events),
            "events_truncated": len(events) > limit_events,
            "diagnostics": diagnostics,
        }

    def inspect_run(
        self,
        run_id: str,
        *,
        limit_events: int = 200,
        include_payload: bool = False,
    ) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run is None:
            raise KeyError(run_id)
        session_id = str(run["session_id"])
        session = self.store.session(session_id)
        session_events = self.store.events_for_session(session_id)
        run_events = [event for event in session_events if event.run_id == run_id]
        if not run_events:
            raise KeyError(run_id)

        run_start = next(
            (event for event in run_events if isinstance(event, RunStartEvent)), None
        )
        prior_boundary = (
            run_start.sequence if run_start is not None else run_events[0].sequence
        )
        prior_events = [
            event
            for event in session_events
            if event.sequence is not None
            and prior_boundary is not None
            and event.sequence < prior_boundary
        ]
        archive = PostgresArtifactStore(self.store.dsn)
        prior_messages = rebuild_prior_messages(
            _BoundedEventLog(prior_events),
            archive=archive,
            session_id=session_id,
        )
        compaction_boundary = _latest_compaction_window(prior_events, self.store)
        timeline = build_run_timeline(
            run_events, runtime_session_id=session_id, run_id=run_id
        )
        tool_artifacts = self.store.tool_result_artifacts_for_run(run_id)
        indexed_artifact_ids = {str(row["artifact_id"]) for row in tool_artifacts}
        diagnostics = []
        diagnostics.extend(sequence_gap_diagnostics(session_events))
        diagnostics.extend(run_projection_diagnostics(run, run_events))
        diagnostics.extend(permission_snapshot_diagnostics(run_events))
        diagnostics.extend(
            tool_flow_diagnostics(run_events, known_artifact_ids=indexed_artifact_ids)
        )
        diagnostics.extend(outbox_diagnostics(self.store.outbox_for_run(run_id)))
        model_contracts = _model_contract_projection(run_events)
        diagnostics.extend(model_contracts["diagnostics"])

        return {
            "inspect_kind": "run",
            "session": _json_safe(session),
            "run": _json_safe(run),
            "canonical": {
                "event_count": len(run_events),
                "start_sequence": _min_sequence(run_events),
                "end_sequence": _max_sequence(run_events),
                "current_user_input": _run_user_input(run_start),
                "permission_snapshot": _run_permission_snapshot(run_start),
            },
            "timeline": timeline.to_dict(),
            "compaction_boundary_as_seen": compaction_boundary,
            "prior_messages_as_seen": [
                _message_to_dict(message) for message in prior_messages
            ],
            "projections_as_seen": [
                _projection_to_dict(event)
                for event in run_events
                if isinstance(event, ProjectionReadyEvent)
            ],
            "capability_surface_as_seen": _capability_surface_projection(run_events),
            "contexts_as_seen": _context_compilation_projection(run_events),
            "model_targets": model_contracts["model_targets"],
            "model_calls": model_contracts["model_calls"],
            "model_usage_by_run": model_contracts["usage_by_run"],
            "compaction_model_contracts": model_contracts["compaction_model_contracts"],
            "reflection_model_contracts": model_contracts["reflection_model_contracts"],
            "subagent_graph": _subagent_graph_projection(
                session_id,
                session_events,
                self.store,
                parent_run_id=run_id,
            ),
            "assistant_replies": _assistant_replies(run_events),
            "tool_result_artifacts": [_json_safe(row) for row in tool_artifacts],
            "recall_traces": [
                _json_safe(row) for row in self.store.recall_traces_for_run(run_id)
            ],
            "outbox": [_json_safe(row) for row in self.store.outbox_for_run(run_id)],
            "events": _event_summaries(
                run_events[:limit_events], include_payload=include_payload
            ),
            "events_truncated": len(run_events) > limit_events,
            "diagnostics": diagnostics,
        }

    def inspect_artifact(
        self,
        artifact_id: str,
        *,
        include_payload: bool = False,
        max_chars: int = 2_000,
    ) -> dict[str, Any]:
        row = self.store.artifact(artifact_id)
        if row is None:
            raise KeyError(artifact_id)
        text_body = row.get("text_body")
        binary_body = row.get("binary_body")
        payload: dict[str, Any] = {}
        if isinstance(text_body, str):
            payload["text_preview"] = text_body[:max_chars]
            payload["text_truncated"] = len(text_body) > max_chars
            if include_payload:
                payload["text"] = text_body
        elif binary_body is not None:
            payload["binary_preview"] = "<binary>"
        metadata = dict(row)
        metadata.pop("text_body", None)
        metadata.pop("binary_body", None)
        return {
            "inspect_kind": "artifact",
            "artifact": _json_safe(metadata),
            "payload": payload,
            "tool_refs": [
                _json_safe(ref) for ref in self.store.artifact_tool_refs(artifact_id)
            ],
            "diagnostics": _artifact_diagnostics(metadata),
        }

    def inspect_memory(self, memory_id: str) -> dict[str, Any]:
        graph_documents = self.store.graph_documents_by_id(memory_id)
        memory_nodes = self.store.memory_nodes_by_id(memory_id)
        graph_ids = set(self.store.memory_graph_ids_by_id(memory_id))
        search_rows = []
        vector_rows = []
        usage_rows = []
        for graph_id in sorted(graph_ids):
            search = self.store.memory_search_index(str(graph_id), memory_id)
            if search is not None:
                search_rows.append(search)
            vector_rows.extend(self.store.memory_vector_index(str(graph_id), memory_id))
            usage_rows.extend(
                self.store.recall_usages_for_memory(str(graph_id), memory_id)
            )
        if not any(
            (graph_documents, memory_nodes, search_rows, vector_rows, usage_rows)
        ):
            raise KeyError(memory_id)
        return {
            "inspect_kind": "memory",
            "memory_id": memory_id,
            "graph_documents": [_json_safe(row) for row in graph_documents],
            "memory_nodes": [_json_safe(row) for row in memory_nodes],
            "search_index": [_json_safe(row) for row in search_rows],
            "vector_index": [_json_safe(row) for row in vector_rows],
            "recall_usages": [_json_safe(row) for row in usage_rows],
            "diagnostics": [],
        }

    def inspect_health(self) -> dict[str, Any]:
        table_presence = self.store.required_table_presence(_REQUIRED_TABLES)
        recent_session_ids = self.store.recent_session_ids()
        sequence_diagnostics = []
        for session_id in recent_session_ids:
            events = self.store.events_for_session(session_id)
            sequence_diagnostics.extend(sequence_gap_diagnostics(events))
            sequence_diagnostics.extend(_compaction_diagnostics(events, self.store))
        outbox_counts = self.store.outbox_status_counts()
        oxigraph = self._inspect_oxigraph()
        diagnostics = []
        diagnostics.extend(sequence_diagnostics)
        diagnostics.extend(
            {
                "code": "missing_table",
                "severity": "error",
                "message": "Required Postgres table is missing.",
                "details": {"table": table},
            }
            for table, present in table_presence.items()
            if not present
        )
        stale_count = self.store.run_projection_stale_count()
        if stale_count:
            diagnostics.append(
                {
                    "code": "run_projection_stale",
                    "severity": "warning",
                    "message": "One or more runs summary rows do not match canonical RUN_END events.",
                    "details": {"count": stale_count},
                }
            )
        missing_artifact_count = self.store.tool_result_index_missing_artifact_count()
        if missing_artifact_count:
            diagnostics.append(
                {
                    "code": "tool_result_index_missing_artifact",
                    "severity": "error",
                    "message": "Tool result artifact index rows reference missing artifacts.",
                    "details": {"count": missing_artifact_count},
                }
            )
        return {
            "inspect_kind": "health",
            "postgres": {
                "connected": True,
                "tables": table_presence,
                "recent_session_count": len(recent_session_ids),
                "run_projection_stale_count": stale_count,
                "tool_result_index_missing_artifact_count": missing_artifact_count,
            },
            "outbox": [_json_safe(row) for row in outbox_counts],
            "oxigraph": oxigraph,
            "diagnostics": diagnostics,
        }

    def _inspect_oxigraph(self) -> dict[str, Any]:
        if not self.oxigraph_url:
            return {"configured": False, "connected": False, "error": None}
        try:
            graph = OxigraphGraphStore(base_url=self.oxigraph_url, timeout_seconds=2.0)
            rows = graph.query(
                """
                SELECT ?g (COUNT(*) AS ?count) WHERE {
                  GRAPH ?g { ?s ?p ?o }
                }
                GROUP BY ?g
                ORDER BY DESC(?count)
                LIMIT 20
                """
            )
        except Exception as exc:
            return {
                "configured": True,
                "connected": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {"configured": True, "connected": True, "graphs": rows}


class _BoundedEventLog:
    def __init__(self, events: Iterable[AgentEvent]) -> None:
        self._events = sorted(
            list(events),
            key=lambda event: event.sequence if event.sequence is not None else 0,
        )

    def append(self, event: AgentEvent) -> AgentEvent:
        raise RuntimeError("Inspector bounded event log is read-only")

    def extend(self, events: Iterable[AgentEvent]) -> list[AgentEvent]:
        raise RuntimeError("Inspector bounded event log is read-only")

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
        after_sequence: int | None = None,
    ) -> list[AgentEvent]:
        events = self._events
        if run_id is not None:
            events = [event for event in events if event.run_id == run_id]
        if turn_id is not None:
            events = [event for event in events if event.turn_id == turn_id]
        if reply_id is not None:
            events = [event for event in events if event.reply_id == reply_id]
        if after_sequence is not None:
            events = [
                event
                for event in events
                if event.sequence is not None and event.sequence > after_sequence
            ]
        return list(events)

    def replay(self, reply_id: str) -> Msg:
        events = self.iter(reply_id=reply_id)
        start = next(
            (event for event in events if isinstance(event, ReplyStartEvent)), None
        )
        message = AssistantMsg(
            id=reply_id,
            name=start.name if start else "assistant",
            content=[],
            created_at=start.created_at if start else None,
        )
        reducer = MessageReducer(message)
        for event in events:
            reducer.append(event)
        return reducer.message


@dataclass(frozen=True, slots=True)
class _InspectorEventLogLocator:
    store: PostgresInspectorStore

    def event_log_for_runtime_session(self, runtime_session_id: str):
        session = self.store.session(runtime_session_id)
        workspace_root = session.get("workspace_root") if session is not None else None
        return PostgresEventLog(
            dsn=self.store.dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=str(workspace_root) if workspace_root is not None else None,
        )


def _subagent_graph_projection(
    session_id: str,
    events: Iterable[AgentEvent],
    store: PostgresInspectorStore,
    *,
    parent_run_id: str | None = None,
) -> dict[str, Any]:
    state = fold_subagent_graph(events)
    projection = project_subagent_graph(
        session_id,
        state,
        locator=_InspectorEventLogLocator(store),
    )
    edges = list(projection.edges)
    nodes = list(projection.nodes)
    tasks = list(projection.tasks)
    if parent_run_id is not None:
        subagent_ids = {
            edge.subagent_run_id
            for edge in edges
            if edge.parent_run_id == parent_run_id
        }
        edges = [edge for edge in edges if edge.subagent_run_id in subagent_ids]
        nodes = [node for node in nodes if node.subagent_run_id in subagent_ids]
        task_ids = {
            task.task_id
            for task in tasks
            if task.parent_run_id == parent_run_id
            or (task.current_run_id is not None and task.current_run_id in subagent_ids)
        }
        tasks = [task for task in tasks if task.task_id in task_ids]
    return {
        "parent_runtime_session_id": projection.parent_runtime_session_id,
        "nodes": [_json_safe(asdict(node)) for node in nodes],
        "edges": [_json_safe(asdict(edge)) for edge in edges],
        "tasks": [_json_safe(asdict(task)) for task in tasks],
        "diagnostics": [
            _json_safe(diagnostic) for diagnostic in projection.diagnostics
        ],
    }


def _event_summaries(
    events: Iterable[AgentEvent], *, include_payload: bool
) -> list[dict[str, Any]]:
    return [_event_summary(event, include_payload=include_payload) for event in events]


def _event_summary(event: AgentEvent, *, include_payload: bool) -> dict[str, Any]:
    payload = dump_agent_event(event)
    summary: dict[str, Any] = {
        "sequence": event.sequence,
        "type": str(event.type),
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "reply_id": event.reply_id,
        "created_at": event.created_at,
    }
    for key in (
        "status",
        "stop_reason",
        "tool_call_id",
        "tool_call_name",
        "state",
        "provider",
        "model_name",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "question_id",
        "exit_request_id",
        "decision",
        "compaction_id",
        "trigger",
        "reason",
        "window_number",
        "window_id",
        "summary_artifact_id",
        "through_sequence",
        "keep_after_sequence",
        "threshold_tokens",
        "source_event_id",
        "source_event_sequence",
        "candidate_entry_ids",
        "attempted_count",
        "proposed_count",
        "skipped_count",
        "duplicate_count",
        "error_count",
        "extractor_version",
        "context_id",
        "model_call_index",
        "tools_estimated_tokens",
        "name",
    ):
        if key in payload:
            summary[key] = payload[key]
    if "delta" in payload:
        summary["delta_preview"] = _truncate(str(payload["delta"]), 250)
    if include_payload:
        summary["payload"] = payload
    return summary


def _capability_surface_projection(events: Iterable[AgentEvent]) -> dict[str, Any]:
    exposures: list[dict[str, Any]] = []
    gate_decisions: list[dict[str, Any]] = []
    for event in events:
        if (
            isinstance(event, CustomEvent)
            and event.name == "capability_exposure_resolved"
        ):
            value = dict(event.value)
            value["sequence"] = event.sequence
            value["run_id"] = event.run_id
            value["turn_id"] = event.turn_id
            value["reply_id"] = event.reply_id
            exposures.append(_json_safe(value))
            continue
        gate_decision = _capability_gate_decision_payload(event)
        if gate_decision is not None:
            gate_decisions.append(_json_safe(gate_decision))
    return {
        "latest_exposure": exposures[-1] if exposures else None,
        "exposures": exposures,
        "gate_decisions": gate_decisions,
    }


def _capability_gate_decision_payload(event: AgentEvent) -> dict[str, Any] | None:
    if isinstance(event, CapabilityGateDecisionEvent):
        payload = {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "descriptor_id": event.descriptor_id,
            "decision": event.decision,
            "reason_code": event.reason_code,
            "reason_message": event.reason_message,
            "suggested_rules": event.suggested_rules,
            "result_state": event.result_state.value
            if event.result_state is not None
            else None,
            "policy_mode": event.policy_mode,
            "permission_policy": event.permission_policy,
            "exposure_generation": event.exposure_generation,
            "availability": event.availability,
            "permission_category": event.permission_category,
            "effective_permission_category": event.effective_permission_category,
            "effective_read_only": event.effective_read_only,
            "capability_context": event.capability_context,
        }
    else:
        return None
    payload["sequence"] = event.sequence
    payload["run_id"] = event.run_id
    payload["turn_id"] = event.turn_id
    payload["reply_id"] = event.reply_id
    payload.setdefault("reason_code", None)
    payload.setdefault("reason_message", None)
    payload.setdefault("suggested_rules", [])
    payload.setdefault("policy_mode", None)
    payload.setdefault("permission_policy", {})
    payload.setdefault("exposure_generation", None)
    payload.setdefault("availability", None)
    payload.setdefault("permission_category", None)
    payload.setdefault("effective_permission_category", None)
    payload.setdefault("effective_read_only", None)
    payload.setdefault("result_state", None)
    payload.setdefault("capability_context", {})
    return payload


def _model_contract_projection(events: Iterable[AgentEvent]) -> dict[str, Any]:
    events_list = list(events)
    targets: dict[str, dict[str, Any]] = {}
    calls: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    compaction_contracts: list[dict[str, Any]] = []
    reflection_contracts: list[dict[str, Any]] = []
    run_targets = {
        event.run_id: event.model_target
        for event in events_list
        if isinstance(event, RunStartEvent)
    }

    def register_target(target, *, sequence: int | None, source: str) -> None:
        value = target.model_dump(mode="json")
        fingerprint = target.target_fingerprint
        existing = targets.get(fingerprint)
        if existing is not None and existing["fact"] != value:
            diagnostics.append(
                _model_contract_diagnostic(
                    "model_target_fingerprint_mismatch",
                    "The same target fingerprint carried different target facts.",
                    {"target_fingerprint": fingerprint, "sequence": sequence},
                )
            )
            return
        entry = targets.setdefault(
            fingerprint,
            {"target_fingerprint": fingerprint, "fact": value, "sources": []},
        )
        entry["sources"].append({"source": source, "sequence": sequence})

    def register_call(fact, *, sequence: int | None, source: str) -> dict[str, Any]:
        call_id = fact.resolved_model_call_id
        value = fact.model_dump(mode="json")
        register_target(fact.target, sequence=sequence, source=source)
        entry = calls.setdefault(
            call_id,
            {
                "resolved_model_call_id": call_id,
                "fact": value,
                "purpose": fact.purpose.value,
                "context_mode": fact.context_mode.value,
                "target_fingerprint": fact.target.target_fingerprint,
                "model_id": fact.target.model_id,
                "requested_model_id": fact.target.model_id,
                "model_identity_policy": fact.target.model_identity_policy,
                "reported_model_id": None,
                "model_identity_relation": None,
                "provider": fact.target.provider,
                "api": fact.target.api,
                "effective_output_tokens": fact.target.context_budget.effective_output_tokens,
                "input_budget_tokens": fact.target.context_budget.input_budget_tokens,
                "estimator": fact.target.token_estimator.model_dump(mode="json"),
                "compile_context_ids": [],
                "compile_sequences": [],
                "start_sequence": None,
                "end_sequence": None,
                "rejected_sequence": None,
                "subsystem_terminal_sequence": None,
                "run_id": None,
                "usage_status": None,
                "usage": None,
                "estimated_input_tokens": None,
                "fact_mismatch": False,
                "sources": [],
            },
        )
        if entry["fact"] != value:
            entry["fact_mismatch"] = True
            diagnostics.append(
                _model_contract_diagnostic(
                    "resolved_model_call_fact_mismatch",
                    "The same resolved model call id carried different facts.",
                    {"resolved_model_call_id": call_id, "sequence": sequence},
                )
            )
        entry["sources"].append({"source": source, "sequence": sequence})
        return entry

    for event in events_list:
        if isinstance(event, RunStartEvent):
            register_target(
                event.model_target, sequence=event.sequence, source="run_start"
            )
        elif isinstance(event, ContextCompiledEvent):
            entry = register_call(
                event.resolved_call, sequence=event.sequence, source="context_compiled"
            )
            entry["compile_context_ids"].append(event.context_id)
            entry["compile_sequences"].append(event.sequence)
            entry["run_id"] = event.run_id
            run_target = run_targets.get(event.run_id)
            if (
                run_target is not None
                and run_target.target_fingerprint
                != event.resolved_call.target.target_fingerprint
            ):
                diagnostics.append(
                    _model_contract_diagnostic(
                        "run_model_target_mismatch",
                        "A compiled call does not use its run's frozen model target.",
                        {
                            "run_id": event.run_id,
                            "resolved_model_call_id": (
                                event.resolved_call.resolved_model_call_id
                            ),
                        },
                    )
                )
        elif isinstance(event, ModelCallStartEvent):
            entry = register_call(
                event.resolved_call, sequence=event.sequence, source="model_call_start"
            )
            if entry["start_sequence"] is not None:
                diagnostics.append(
                    _model_contract_diagnostic(
                        "duplicate_model_call_start",
                        "A resolved model call has more than one start event.",
                        {
                            "resolved_model_call_id": event.resolved_call.resolved_model_call_id
                        },
                    )
                )
            entry["start_sequence"] = event.sequence
            entry["run_id"] = event.run_id
            if event.context_id not in entry["compile_context_ids"]:
                entry["compile_context_ids"].append(event.context_id)
        elif isinstance(event, ModelCallEndEvent):
            entry = calls.setdefault(
                event.resolved_model_call_id,
                {
                    "resolved_model_call_id": event.resolved_model_call_id,
                    "fact": None,
                    "purpose": None,
                    "context_mode": None,
                    "target_fingerprint": event.target_fingerprint,
                    "model_id": None,
                    "requested_model_id": None,
                    "model_identity_policy": None,
                    "reported_model_id": None,
                    "model_identity_relation": None,
                    "provider": None,
                    "api": None,
                    "effective_output_tokens": None,
                    "input_budget_tokens": None,
                    "estimator": None,
                    "compile_context_ids": [],
                    "compile_sequences": [],
                    "start_sequence": None,
                    "end_sequence": None,
                    "rejected_sequence": None,
                    "subsystem_terminal_sequence": None,
                    "run_id": event.run_id,
                    "usage_status": None,
                    "usage": None,
                    "estimated_input_tokens": None,
                    "fact_mismatch": False,
                    "sources": [],
                },
            )
            if (
                entry.get("target_fingerprint") is not None
                and entry["target_fingerprint"] != event.target_fingerprint
            ):
                entry["fact_mismatch"] = True
                diagnostics.append(
                    _model_contract_diagnostic(
                        "model_target_fingerprint_mismatch",
                        "A model call end target fingerprint differs from its call fact.",
                        {"resolved_model_call_id": event.resolved_model_call_id},
                    )
                )
            entry["end_sequence"] = event.sequence
            entry["reported_model_id"] = event.reported_model_id
            requested_model_id = entry.get("requested_model_id")
            entry["model_identity_relation"] = (
                "missing"
                if event.reported_model_id is None
                else "exact"
                if requested_model_id == event.reported_model_id
                else "different"
            )
            if (
                entry.get("model_identity_policy") == "exact"
                and entry["model_identity_relation"] == "different"
            ):
                entry["fact_mismatch"] = True
                diagnostics.append(
                    _model_contract_diagnostic(
                        "reported_model_identity_policy_violation",
                        "The provider-reported model id violates the exact identity policy.",
                        {
                            "resolved_model_call_id": event.resolved_model_call_id,
                            "requested_model_id": requested_model_id,
                            "reported_model_id": event.reported_model_id,
                        },
                    )
                )
            entry["usage_status"] = event.usage_status
            entry["usage"] = (
                event.usage.model_dump(mode="json") if event.usage else None
            )
            entry["estimated_input_tokens"] = event.estimated_input_tokens
            entry["sources"].append(
                {"source": "model_call_end", "sequence": event.sequence}
            )
        elif isinstance(event, ModelCallRejectedEvent):
            entry = register_call(
                event.resolved_call,
                sequence=event.sequence,
                source="model_call_rejected",
            )
            entry["rejected_sequence"] = event.sequence
            entry["run_id"] = event.run_id
            entry["estimated_input_tokens"] = event.estimated_input_tokens
            entry["rejection_reason_code"] = event.reason_code
        elif isinstance(event, ContextCompactionStartedEvent):
            register_target(
                event.target_model_target,
                sequence=event.sequence,
                source="compaction_target",
            )
            entry = register_call(
                event.summarizer_call,
                sequence=event.sequence,
                source="compaction_started",
            )
            entry["run_id"] = event.run_id
            compaction_contracts.append(
                {
                    "sequence": event.sequence,
                    "compaction_id": event.compaction_id,
                    "status": "started",
                    "resolved_model_call_id": event.summarizer_call.resolved_model_call_id,
                    "target_fingerprint": event.target_model_target.target_fingerprint,
                    "target_estimate": event.target_estimate.model_dump(mode="json"),
                }
            )
        elif isinstance(event, ContextCompactionCompletedEvent):
            register_target(
                event.target_model_target,
                sequence=event.sequence,
                source="compaction_target",
            )
            entry = register_call(
                event.summarizer_call,
                sequence=event.sequence,
                source="compaction_completed",
            )
            _apply_outer_usage(
                entry,
                sequence=event.sequence,
                run_id=event.run_id,
                usage_status=event.summarizer_usage_status,
                usage=event.summarizer_usage,
                estimated_input_tokens=event.summarizer_estimated_input_tokens,
                reported_model_id=event.summarizer_reported_model_id,
            )
            compaction_contracts.append(
                {
                    "sequence": event.sequence,
                    "compaction_id": event.compaction_id,
                    "status": "completed",
                    "resolved_model_call_id": event.summarizer_call.resolved_model_call_id,
                    "target_fingerprint": event.target_model_target.target_fingerprint,
                    "target_estimate": event.target_estimate.model_dump(mode="json"),
                }
            )
        elif isinstance(event, ContextCompactionFailedEvent):
            register_target(
                event.target_model_target,
                sequence=event.sequence,
                source="compaction_target",
            )
            call_id = None
            if event.summarizer_call is not None:
                entry = register_call(
                    event.summarizer_call,
                    sequence=event.sequence,
                    source="compaction_failed",
                )
                _apply_outer_usage(
                    entry,
                    sequence=event.sequence,
                    run_id=event.run_id,
                    usage_status=event.summarizer_usage_status,
                    usage=event.summarizer_usage,
                    estimated_input_tokens=event.summarizer_estimated_input_tokens,
                    reported_model_id=event.summarizer_reported_model_id,
                )
                entry["direct_rejected"] = event.failure_stage in {
                    "summarizer_input_build",
                    "started_append",
                    "model_validation",
                }
                call_id = event.summarizer_call.resolved_model_call_id
            compaction_contracts.append(
                {
                    "sequence": event.sequence,
                    "compaction_id": event.compaction_id,
                    "status": "failed",
                    "failure_stage": event.failure_stage,
                    "resolved_model_call_id": call_id,
                    "target_estimate": (
                        event.target_estimate.model_dump(mode="json")
                        if event.target_estimate is not None
                        else None
                    ),
                    "observed_after_measurement": (
                        event.observed_after_measurement.model_dump(mode="json")
                        if event.observed_after_measurement is not None
                        else None
                    ),
                }
            )
        elif isinstance(event, MemoryReflectionCompletedEvent):
            entry = register_call(
                event.resolved_call,
                sequence=event.sequence,
                source="reflection_completed",
            )
            _apply_outer_usage(
                entry,
                sequence=event.sequence,
                run_id=event.run_id,
                usage_status=event.usage_status,
                usage=event.usage,
                estimated_input_tokens=event.estimated_input_tokens,
                reported_model_id=event.reported_model_id,
            )
            reflection_contracts.append(
                {
                    "sequence": event.sequence,
                    "reflection_id": event.reflection_id,
                    "status": "completed",
                    "resolved_model_call_id": event.resolved_call.resolved_model_call_id,
                }
            )
        elif isinstance(event, MemoryReflectionFailedEvent):
            call_id = None
            if event.resolved_call is not None:
                entry = register_call(
                    event.resolved_call,
                    sequence=event.sequence,
                    source="reflection_failed",
                )
                _apply_outer_usage(
                    entry,
                    sequence=event.sequence,
                    run_id=event.run_id,
                    usage_status=event.usage_status,
                    usage=event.usage,
                    estimated_input_tokens=event.estimated_input_tokens,
                    reported_model_id=event.reported_model_id,
                )
                entry["direct_rejected"] = event.failure_stage == "model_validation"
                call_id = event.resolved_call.resolved_model_call_id
            reflection_contracts.append(
                {
                    "sequence": event.sequence,
                    "reflection_id": event.reflection_id,
                    "status": "failed",
                    "failure_stage": event.failure_stage,
                    "resolved_model_call_id": call_id,
                }
            )

    for entry in calls.values():
        entry["join_status"] = _model_call_join_status(entry)
        if (
            entry["end_sequence"] is not None
            and entry["start_sequence"] is None
            and not entry.get("subsystem_terminal_sequence")
        ):
            diagnostics.append(
                _model_contract_diagnostic(
                    "model_call_end_missing_start",
                    "A model call end event has no matching start event.",
                    {"resolved_model_call_id": entry["resolved_model_call_id"]},
                )
            )
        if (
            entry["rejected_sequence"] is not None
            and entry["start_sequence"] is not None
        ):
            diagnostics.append(
                _model_contract_diagnostic(
                    "model_call_rejected_after_start",
                    "A rejected call also has a provider start event.",
                    {"resolved_model_call_id": entry["resolved_model_call_id"]},
                )
            )

    usage_by_run = _model_usage_projection(calls.values())
    return {
        "model_targets": list(targets.values()),
        "model_calls": list(calls.values()),
        "usage_by_run": usage_by_run,
        "compaction_model_contracts": compaction_contracts,
        "reflection_model_contracts": reflection_contracts,
        "diagnostics": diagnostics,
    }


def _apply_outer_usage(
    entry: dict[str, Any],
    *,
    sequence: int | None,
    run_id: str,
    usage_status: str,
    usage,
    estimated_input_tokens: int | None,
    reported_model_id: str | None,
) -> None:
    entry["subsystem_terminal_sequence"] = sequence
    entry["run_id"] = run_id
    entry["usage_status"] = usage_status
    entry["usage"] = usage.model_dump(mode="json") if usage is not None else None
    entry["estimated_input_tokens"] = estimated_input_tokens
    entry["reported_model_id"] = reported_model_id
    entry["model_identity_relation"] = (
        "missing"
        if reported_model_id is None
        else "exact"
        if entry.get("requested_model_id") == reported_model_id
        else "different"
    )


def _model_call_join_status(entry: dict[str, Any]) -> str:
    if entry.get("fact_mismatch"):
        return "fact_mismatch"
    if entry.get("fact") is None:
        return "end_missing_start"
    if entry.get("direct_rejected"):
        return "direct_rejected"
    if entry["context_mode"] == "direct":
        if entry["subsystem_terminal_sequence"] is not None:
            return "direct_started_completed"
        if entry["start_sequence"] is not None and entry["end_sequence"] is None:
            return "started_missing_end"
        return "fact_mismatch"
    if entry["rejected_sequence"] is not None:
        return "compiled_rejected"
    if entry["start_sequence"] is not None and entry["end_sequence"] is not None:
        return "compiled_started_completed"
    if entry["start_sequence"] is not None:
        return "started_missing_end"
    if entry["compile_sequences"]:
        return "compiled_pressure_only"
    return "fact_mismatch"


def _model_usage_projection(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_run: dict[str, dict[str, Any]] = {}
    for entry in entries:
        run_id = entry.get("run_id")
        if not isinstance(run_id, str):
            continue
        aggregate = by_run.setdefault(
            run_id,
            {
                "run_id": run_id,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "reasoning_output_tokens": 0,
                "cached_input_tokens_complete": True,
                "reasoning_output_tokens_complete": True,
                "reported_call_count": 0,
                "missing_usage_call_count": 0,
                "by_purpose": {},
            },
        )
        purpose = entry.get("purpose") or "unknown"
        purpose_aggregate = aggregate["by_purpose"].setdefault(
            purpose,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "reported_call_count": 0,
                "missing_usage_call_count": 0,
            },
        )
        usage = entry.get("usage")
        if entry.get("usage_status") != "reported" or not isinstance(usage, dict):
            aggregate["missing_usage_call_count"] += 1
            purpose_aggregate["missing_usage_call_count"] += 1
            continue
        aggregate["reported_call_count"] += 1
        purpose_aggregate["reported_call_count"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            aggregate[key] += int(usage[key])
            purpose_aggregate[key] += int(usage[key])
        if usage.get("cached_input_tokens") is None:
            aggregate["cached_input_tokens_complete"] = False
        else:
            aggregate["cached_input_tokens"] += int(usage["cached_input_tokens"])
        if usage.get("reasoning_output_tokens") is None:
            aggregate["reasoning_output_tokens_complete"] = False
        else:
            aggregate["reasoning_output_tokens"] += int(
                usage["reasoning_output_tokens"]
            )
    for aggregate in by_run.values():
        if not aggregate["cached_input_tokens_complete"]:
            aggregate["cached_input_tokens"] = None
        if not aggregate["reasoning_output_tokens_complete"]:
            aggregate["reasoning_output_tokens"] = None
        aggregate["by_purpose"] = [
            {"purpose": purpose, **values}
            for purpose, values in sorted(aggregate["by_purpose"].items())
        ]
    return list(by_run.values())


def _model_contract_diagnostic(
    code: str,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {"code": code, "severity": "error", "message": message, "details": details}


def _context_compilation_projection(events: Iterable[AgentEvent]) -> dict[str, Any]:
    context_events: list[ContextCompiledEvent] = []
    model_starts: list[ModelCallStartEvent] = []
    for event in events:
        if isinstance(event, ContextCompiledEvent):
            context_events.append(event)
        elif isinstance(event, ModelCallStartEvent):
            model_starts.append(event)
    contexts_by_id = {event.context_id: event for event in context_events}
    joins: list[dict[str, Any]] = []
    for start in model_starts:
        if start.resolved_call.context_mode == "direct":
            joins.append(
                {
                    "context_id": start.context_id,
                    "model_call_index": start.model_call_index,
                    "resolved_model_call_id": start.resolved_call.resolved_model_call_id,
                    "target_fingerprint": start.resolved_call.target.target_fingerprint,
                    "model_call_sequence": start.sequence,
                    "context_compiled_sequence": None,
                    "join_status": "direct_context_not_applicable",
                    "model_name": start.resolved_call.target.model_id,
                    "model_role": start.resolved_call.target.model_role,
                    "provider": start.resolved_call.target.provider,
                }
            )
            continue
        compiled = contexts_by_id.get(start.context_id)
        join_status = "matched"
        if compiled is None:
            join_status = "missing_context_compiled"
        elif (
            compiled.resolved_call.resolved_model_call_id
            != start.resolved_call.resolved_model_call_id
            or compiled.resolved_call != start.resolved_call
            or compiled.model_call_index != start.model_call_index
        ):
            join_status = "model_call_fact_mismatch"
        joins.append(
            {
                "context_id": start.context_id,
                "model_call_index": start.model_call_index,
                "resolved_model_call_id": start.resolved_call.resolved_model_call_id,
                "target_fingerprint": start.resolved_call.target.target_fingerprint,
                "model_call_sequence": start.sequence,
                "context_compiled_sequence": compiled.sequence
                if compiled is not None
                else None,
                "join_status": join_status,
                "model_name": start.resolved_call.target.model_id,
                "model_role": start.resolved_call.target.model_role,
                "provider": start.resolved_call.target.provider,
            }
        )
    diagnostics: list[dict[str, Any]] = []
    for join in joins:
        if join["join_status"] == "missing_context_compiled":
            diagnostics.append(
                {
                    "code": "model_call_start_missing_compiled_context",
                    "severity": "warning",
                    "message": "A model call start event did not have a matching ContextCompiledEvent.",
                    "details": {
                        "context_id": join["context_id"],
                        "model_call_sequence": join["model_call_sequence"],
                    },
                }
            )
        elif join["join_status"] == "model_call_fact_mismatch":
            diagnostics.append(
                {
                    "code": "context_model_call_identity_mismatch",
                    "severity": "error",
                    "message": "Compiled context and model call start facts do not match.",
                    "details": {
                        "context_id": join["context_id"],
                        "resolved_model_call_id": join["resolved_model_call_id"],
                    },
                }
            )
    return {
        "latest": _context_compiled_to_dict(context_events[-1])
        if context_events
        else None,
        "contexts": [_context_compiled_to_dict(event) for event in context_events],
        "model_call_joins": joins,
        "diagnostics": diagnostics,
    }


def _context_compiled_to_dict(event: ContextCompiledEvent) -> dict[str, Any]:
    sections = [_json_safe(section) for section in event.sections]
    omitted = [
        section
        for section in sections
        if isinstance(section, dict) and section.get("included") is False
    ]
    return {
        "sequence": event.sequence,
        "context_id": event.context_id,
        "status": event.status,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "reply_id": event.reply_id,
        "resolved_call": event.resolved_call.model_dump(mode="json"),
        "model_role": event.resolved_call.target.model_role,
        "model_call_index": event.model_call_index,
        "compile_attempt_index": event.compile_attempt_index,
        "context_retry_index": event.context_retry_index,
        "final_payload_estimated_tokens": event.budget.final_payload_estimated_tokens,
        "total_context_tokens": event.budget.total_context_tokens,
        "effective_output_tokens": event.budget.effective_output_tokens,
        "tools_estimated_tokens": event.budget.tools_estimated_tokens,
        "budget": event.budget.model_dump(mode="json"),
        "section_count": len(event.sections),
        "included_section_count": len(event.sections) - len(omitted),
        "omitted_section_count": len(omitted),
        "sections": sections,
        "section_timings": _section_timing_projection(sections),
        "tool_specs": [_json_safe(tool) for tool in event.tool_specs],
        "diagnostics": [_json_safe(diagnostic) for diagnostic in event.diagnostics],
        "lifecycle_decisions": [
            _json_safe(decision) for decision in event.lifecycle_decisions
        ],
        "tool_result_render_decisions": [
            _json_safe(decision) for decision in event.tool_result_render_decisions
        ],
        "tool_result_timings": _tool_result_timing_projection(
            event.tool_result_render_decisions
        ),
        "tool_result_budget_report": _json_safe(event.tool_result_budget_report),
    }


def _section_timing_projection(sections: list[Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            projected.append(
                {
                    "section_index": index,
                    "section_id": None,
                    "status": "missing",
                    "freshness": "unknown",
                    "timing": None,
                }
            )
            continue
        metadata = section.get("metadata")
        timing = metadata.get("timing") if isinstance(metadata, dict) else None
        if isinstance(timing, dict):
            source = (
                timing.get("source") if isinstance(timing.get("source"), dict) else {}
            )
            freshness = source.get("freshness") if isinstance(source, dict) else None
            status = "present"
        else:
            source = {}
            freshness = None
            status = "missing"
        observed_at = (
            source.get("observed_at")
            or source.get("source_ended_at")
            or source.get("source_started_at")
            if isinstance(source, dict)
            else None
        )
        projected.append(
            {
                "section_index": index,
                "section_id": section.get("id"),
                "source_id": section.get("source_id"),
                "channel": section.get("channel"),
                "status": status,
                "freshness": freshness or "unknown",
                "compiled_at_utc": timing.get("compiled_at_utc")
                if isinstance(timing, dict)
                else None,
                "observed_at": observed_at,
                "source_started_at": source.get("source_started_at")
                if isinstance(source, dict)
                else None,
                "source_ended_at": source.get("source_ended_at")
                if isinstance(source, dict)
                else None,
                "age_seconds": timing.get("age_seconds")
                if isinstance(timing, dict)
                else None,
                "timing": _json_safe(timing) if isinstance(timing, dict) else None,
            }
        )
    return projected


def _tool_result_timing_projection(
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for index, decision in enumerate(decisions):
        timing = decision.get("tool_observation_timing")
        if not isinstance(timing, dict):
            timing = decision.get("tool_timing")
        timing_policy = decision.get("timing_policy")
        if isinstance(timing, dict):
            status = "present"
            freshness = timing.get("freshness") or "unknown"
            observed_at = timing.get("observed_at")
        elif timing_policy == "not_applicable":
            status = "not_applicable"
            freshness = "unknown"
            observed_at = None
        else:
            status = "missing"
            freshness = "unknown"
            observed_at = None
        projected.append(
            {
                "decision_index": index,
                "tool_call_id": decision.get("tool_call_id"),
                "tool_name": decision.get("tool_name"),
                "model_tool_name": decision.get("model_tool_name"),
                "tool_origin": timing.get("tool_origin")
                if isinstance(timing, dict)
                else None,
                "status": status,
                "observed_at": observed_at,
                "source_started_at": timing.get("source_started_at")
                if isinstance(timing, dict)
                else None,
                "source_ended_at": timing.get("source_ended_at")
                if isinstance(timing, dict)
                else None,
                "observation_duration_seconds": (
                    timing.get("observation_duration_seconds")
                    if isinstance(timing, dict)
                    else None
                ),
                "tool_reported_duration_seconds": (
                    timing.get("tool_reported_duration_seconds")
                    if isinstance(timing, dict)
                    else None
                ),
                "freshness": freshness,
                "clock_source": timing.get("clock_source")
                if isinstance(timing, dict)
                else None,
                "timing_policy": timing_policy or "unknown",
                "rendered_timing_chars": decision.get("rendered_timing_chars", 0),
                "diagnostics": _json_safe(decision.get("diagnostics", [])),
                "timing": _json_safe(timing) if isinstance(timing, dict) else None,
            }
        )
    return projected


def _assistant_replies(events: Iterable[AgentEvent]) -> list[dict[str, Any]]:
    reply_ids: list[str] = []
    by_reply: dict[str, list[AgentEvent]] = {}
    for event in events:
        by_reply.setdefault(event.reply_id, []).append(event)
        if isinstance(event, ReplyStartEvent) and event.reply_id not in reply_ids:
            reply_ids.append(event.reply_id)
    replies: list[dict[str, Any]] = []
    bounded = _BoundedEventLog(events)
    for reply_id in reply_ids:
        message = bounded.replay(reply_id)
        replies.append(_message_to_dict(message))
    return replies


def _projection_to_dict(event: ProjectionReadyEvent) -> dict[str, Any]:
    return {
        "projection_id": event.projection_id,
        "sequence": event.sequence,
        "role": event.role,
        "scope": event.scope,
        "token_budget": event.token_budget,
        "included_memory_ids": list(event.included_memory_ids),
        "filtered_memory_ids": list(event.filtered_memory_ids),
        "summary": event.summary,
        "created_at": event.created_at,
    }


def _compaction_windows(
    events: Iterable[AgentEvent], store: PostgresInspectorStore
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    events_list = list(events)
    proposals_by_compaction: dict[
        str, list[ContextCompactionMemoryCandidatesProposedEvent]
    ] = {}
    for event in events_list:
        if isinstance(event, ContextCompactionMemoryCandidatesProposedEvent):
            proposals_by_compaction.setdefault(event.compaction_id, []).append(event)
    for event in events_list:
        if not isinstance(event, ContextCompactionCompletedEvent):
            continue
        artifact = store.artifact(event.summary_artifact_id)
        candidates = [
            _memory_candidate_projection(row, store)
            for row in store.memory_candidates_for_compaction(event.compaction_id)
        ]
        windows.append(
            {
                "sequence": event.sequence,
                "compaction_id": event.compaction_id,
                "trigger": event.trigger,
                "reason": event.reason,
                "phase": event.metadata.get("phase", _phase_from_reason(event.reason)),
                "safe_point": event.metadata.get("safe_point"),
                "current_run_id": event.metadata.get("current_run_id"),
                "max_compactable_sequence": event.metadata.get(
                    "max_compactable_sequence"
                ),
                "tail_message_count": event.metadata.get("tail_message_count"),
                "window_number": event.window_number,
                "window_id": event.window_id,
                "summary_artifact_id": event.summary_artifact_id,
                "summary_artifact_present": artifact is not None,
                "through_sequence": event.through_sequence,
                "keep_after_sequence": event.keep_after_sequence,
                "target_model_target": event.target_model_target.model_dump(
                    mode="json"
                ),
                "target_estimate": event.target_estimate.model_dump(mode="json"),
                "estimated_tokens_before": event.target_estimate.estimated_tokens_before,
                "estimated_tokens_after": event.target_estimate.estimated_tokens_after,
                "threshold_tokens": event.threshold_tokens,
                "post_compaction_target_tokens": event.post_compaction_target_tokens,
                "target_input_budget_tokens": event.target_input_budget_tokens,
                "summarizer_call": event.summarizer_call.model_dump(mode="json"),
                "summarizer_context_id": event.summarizer_context_id,
                "summarizer_input_estimated_tokens": event.summarizer_input_estimated_tokens,
                "summarizer_input_budget_tokens": event.summarizer_input_budget_tokens,
                "summarizer_usage_status": event.summarizer_usage_status,
                "summarizer_usage": (
                    event.summarizer_usage.model_dump(mode="json")
                    if event.summarizer_usage is not None
                    else None
                ),
                "predicted_post_target_reached": event.predicted_post_target_reached,
                "included_run_ids": list(event.included_run_ids),
                "included_artifact_ids": list(event.included_artifact_ids),
                "candidate_proposals": [
                    _compaction_candidate_proposal_projection(proposal)
                    for proposal in proposals_by_compaction.get(event.compaction_id, [])
                ],
                "memory_candidates": candidates,
            }
        )
    return windows


def _compaction_candidate_proposal_projection(
    event: ContextCompactionMemoryCandidatesProposedEvent,
) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "source_event_id": event.source_event_id,
        "source_event_sequence": event.source_event_sequence,
        "summary_artifact_id": event.summary_artifact_id,
        "candidate_entry_ids": list(event.candidate_entry_ids),
        "attempted_count": event.attempted_count,
        "proposed_count": event.proposed_count,
        "skipped_count": event.skipped_count,
        "duplicate_count": event.duplicate_count,
        "error_count": event.error_count,
        "extractor_version": event.extractor_version,
        "diagnostics": [
            diagnostic.model_dump(mode="json") for diagnostic in event.diagnostics
        ],
    }


def _memory_candidate_projection(
    row: dict[str, Any], store: PostgresInspectorStore
) -> dict[str, Any]:
    entry_id = str(row["entry_id"])
    return {
        "entry_id": entry_id,
        "origin": row.get("origin"),
        "payload": _json_safe(row.get("payload")),
        "source_run_id": row.get("source_run_id"),
        "source_turn_id": row.get("source_turn_id"),
        "source_reply_id": row.get("source_reply_id"),
        "source_event_id": row.get("source_event_id"),
        "source_artifact_id": row.get("source_artifact_id"),
        "intent_fingerprint": row.get("intent_fingerprint"),
        "metadata": _json_safe(row.get("metadata") or {}),
        "created_at": str(row.get("created_at")),
        "governance_decisions": [
            _json_safe(decision)
            for decision in store.governance_decisions_for_candidate(entry_id)
        ],
    }


def _phase_from_reason(reason: str) -> str:
    if reason.startswith("mid_turn_"):
        return "mid_turn"
    if reason.startswith("preflight_") or reason == "context_threshold":
        return "preflight"
    if reason.startswith("run_end_"):
        return "run_end"
    return "unknown"


def _latest_compaction_window(
    events: Iterable[AgentEvent], store: PostgresInspectorStore
) -> dict[str, Any] | None:
    windows = _compaction_windows(events, store)
    for window in reversed(windows):
        if window["summary_artifact_present"]:
            return window
    return None


def _compaction_diagnostics(
    events: Iterable[AgentEvent], store: PostgresInspectorStore
) -> list[dict[str, Any]]:
    events_list = list(events)
    completed_ids = {
        event.compaction_id
        for event in events_list
        if isinstance(event, ContextCompactionCompletedEvent)
    }
    failed_ids = {
        event.compaction_id
        for event in events_list
        if isinstance(event, ContextCompactionFailedEvent)
    }
    diagnostics: list[dict[str, Any]] = []
    for event in events_list:
        if (
            isinstance(event, ContextCompactionStartedEvent)
            and event.compaction_id not in completed_ids | failed_ids
        ):
            diagnostics.append(
                {
                    "code": "context_compaction_dangling_started",
                    "severity": "warning",
                    "message": "Context compaction attempt started but has no completed/failed terminal event.",
                    "details": {
                        "compaction_id": event.compaction_id,
                        "sequence": event.sequence,
                        "trigger": event.trigger,
                        "reason": event.reason,
                    },
                }
            )
        if (
            isinstance(event, ContextCompactionCompletedEvent)
            and store.artifact(event.summary_artifact_id) is None
        ):
            diagnostics.append(
                {
                    "code": "context_compaction_missing_summary_artifact",
                    "severity": "error",
                    "message": "Completed context compaction references a missing summary artifact.",
                    "details": {
                        "compaction_id": event.compaction_id,
                        "summary_artifact_id": event.summary_artifact_id,
                        "sequence": event.sequence,
                    },
                }
            )
    return diagnostics


def _artifact_diagnostics(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    artifact_metadata = metadata.get("metadata")
    if not isinstance(artifact_metadata, dict):
        return []
    if artifact_metadata.get("kind") != "context_compaction_summary":
        return []
    diagnostics = []
    if artifact_metadata.get("do_not_write_back") is not True:
        diagnostics.append(
            {
                "code": "context_compaction_summary_missing_no_writeback",
                "severity": "error",
                "message": "Context compaction summary artifact is missing do_not_write_back=true.",
                "details": {"artifact_id": metadata.get("id")},
            }
        )
    return diagnostics


def _message_to_dict(message: Msg) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "name": message.name,
        "created_at": message.created_at,
        "finished_at": message.finished_at,
        "metadata": message.metadata,
        "usage": message.usage.model_dump(mode="json")
        if message.usage is not None
        else None,
        "content": [_block_to_dict(block) for block in message.content],
    }


def _block_to_dict(block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": _truncate(block.text, 2_000), "id": block.id}
    if isinstance(block, ToolCallBlock):
        return {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "input": _truncate(block.input, 2_000),
            "state": str(block.state),
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "id": block.id,
            "name": block.name,
            "state": str(block.state),
            "text": _truncate(
                "".join(
                    part.text for part in block.output if isinstance(part, TextBlock)
                ),
                2_000,
            ),
            "artifacts": [
                artifact.model_dump(mode="json") for artifact in block.artifacts
            ],
        }
    if isinstance(block, DataBlock):
        return {
            "type": "data",
            "id": block.id,
            "name": block.name,
            "source_type": block.source.type,
        }
    return {"type": getattr(block, "type", "unknown"), "id": getattr(block, "id", None)}


def _summarize_working_context(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    if isinstance(payload.get("summary"), str):
        payload["summary"] = _truncate(payload["summary"], 2_000)
    return _json_safe(payload)


def _run_user_input(event: RunStartEvent | None) -> str | None:
    if event is None:
        return None
    value = event.metadata.get("user_input")
    return value if isinstance(value, str) else None


def _run_permission_snapshot(event: RunStartEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "permission_snapshot_id": event.permission_snapshot_id,
        "permission_mode": event.permission_mode,
        "permission_policy": _json_safe(event.permission_policy),
        "permission_snapshot_source": event.permission_snapshot_source,
    }


def _min_sequence(events: Iterable[AgentEvent]) -> int | None:
    values = [event.sequence for event in events if event.sequence is not None]
    return min(values) if values else None


def _max_sequence(events: Iterable[AgentEvent]) -> int | None:
    values = [event.sequence for event in events if event.sequence is not None]
    return max(values) if values else None


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"... <+{len(value) - limit} chars>"
