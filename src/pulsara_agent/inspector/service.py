"""Deterministic read-only Pulsara Inspector service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from pulsara_agent.event import (
    AgentEvent,
    CapabilityGateDecisionEvent,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
    CustomEvent,
    ModelCallStartEvent,
    ProjectionReadyEvent,
    ReplyStartEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import dump_agent_event
from pulsara_agent.graph.oxigraph import OxigraphGraphStore
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.inspector.diagnostics import (
    outbox_diagnostics,
    run_projection_diagnostics,
    sequence_gap_diagnostics,
    tool_flow_diagnostics,
)
from pulsara_agent.inspector.store import PostgresInspectorStore
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.message import AssistantMsg, Msg
from pulsara_agent.message.blocks import DataBlock, TextBlock, ToolCallBlock, ToolResultBlock
from pulsara_agent.message.reducer import MessageReducer
from pulsara_agent.runtime.timeline import build_run_timeline


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
            "compaction_windows": _compaction_windows(events, self.store),
            "events": _event_summaries(events[:limit_events], include_payload=include_payload),
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

        run_start = next((event for event in run_events if isinstance(event, RunStartEvent)), None)
        prior_boundary = run_start.sequence if run_start is not None else run_events[0].sequence
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
        timeline = build_run_timeline(run_events, runtime_session_id=session_id, run_id=run_id)
        tool_artifacts = self.store.tool_result_artifacts_for_run(run_id)
        indexed_artifact_ids = {str(row["artifact_id"]) for row in tool_artifacts}
        diagnostics = []
        diagnostics.extend(sequence_gap_diagnostics(session_events))
        diagnostics.extend(run_projection_diagnostics(run, run_events))
        diagnostics.extend(tool_flow_diagnostics(run_events, known_artifact_ids=indexed_artifact_ids))
        diagnostics.extend(outbox_diagnostics(self.store.outbox_for_run(run_id)))

        return {
            "inspect_kind": "run",
            "session": _json_safe(session),
            "run": _json_safe(run),
            "canonical": {
                "event_count": len(run_events),
                "start_sequence": _min_sequence(run_events),
                "end_sequence": _max_sequence(run_events),
                "current_user_input": _run_user_input(run_start),
            },
            "timeline": timeline.to_dict(),
            "compaction_boundary_as_seen": compaction_boundary,
            "prior_messages_as_seen": [_message_to_dict(message) for message in prior_messages],
            "projections_as_seen": [_projection_to_dict(event) for event in run_events if isinstance(event, ProjectionReadyEvent)],
            "capability_surface_as_seen": _capability_surface_projection(run_events),
            "contexts_as_seen": _context_compilation_projection(run_events),
            "assistant_replies": _assistant_replies(run_events),
            "tool_result_artifacts": [_json_safe(row) for row in tool_artifacts],
            "recall_traces": [_json_safe(row) for row in self.store.recall_traces_for_run(run_id)],
            "outbox": [_json_safe(row) for row in self.store.outbox_for_run(run_id)],
            "events": _event_summaries(run_events[:limit_events], include_payload=include_payload),
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
            "tool_refs": [_json_safe(ref) for ref in self.store.artifact_tool_refs(artifact_id)],
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
            usage_rows.extend(self.store.recall_usages_for_memory(str(graph_id), memory_id))
        if not any((graph_documents, memory_nodes, search_rows, vector_rows, usage_rows)):
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
            return {"configured": True, "connected": False, "error": f"{type(exc).__name__}: {exc}"}
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
            events = [event for event in events if event.sequence is not None and event.sequence > after_sequence]
        return list(events)

    def replay(self, reply_id: str) -> Msg:
        events = self.iter(reply_id=reply_id)
        start = next((event for event in events if isinstance(event, ReplyStartEvent)), None)
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


def _event_summaries(events: Iterable[AgentEvent], *, include_payload: bool) -> list[dict[str, Any]]:
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
        "estimated_tokens_before",
        "estimated_tokens_after",
        "threshold_tokens",
        "context_window_tokens",
        "context_id",
        "model_call_index",
        "estimated_tokens",
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
        if isinstance(event, CustomEvent) and event.name == "capability_exposure_resolved":
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
            "result_state": event.result_state.value if event.result_state is not None else None,
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
        compiled = contexts_by_id.get(start.context_id or "")
        joins.append(
            {
                "context_id": start.context_id,
                "model_call_index": start.model_call_index,
                "model_call_sequence": start.sequence,
                "context_compiled_sequence": compiled.sequence if compiled is not None else None,
                "join_status": "matched" if compiled is not None else "missing_context_compiled",
                "model_name": start.model_name,
                "model_role": start.model_role,
                "provider": start.provider,
            }
        )
    diagnostics: list[dict[str, Any]] = []
    for join in joins:
        if join["join_status"] != "matched":
            diagnostics.append(
                {
                    "code": "context_compiled_missing_for_model_call",
                    "severity": "warning",
                    "message": "A model call start event did not have a matching ContextCompiledEvent.",
                    "details": {
                        "context_id": join["context_id"],
                        "model_call_sequence": join["model_call_sequence"],
                    },
                }
            )
    return {
        "latest": _context_compiled_to_dict(context_events[-1]) if context_events else None,
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
        "model_role": event.model_role,
        "model_call_index": event.model_call_index,
        "estimated_tokens": event.estimated_tokens,
        "context_window_tokens": event.context_window_tokens,
        "reserved_output_tokens": event.reserved_output_tokens,
        "tools_estimated_tokens": event.tools_estimated_tokens,
        "section_count": len(event.sections),
        "included_section_count": len(event.sections) - len(omitted),
        "omitted_section_count": len(omitted),
        "sections": sections,
        "tool_specs": [_json_safe(tool) for tool in event.tool_specs],
        "diagnostics": [_json_safe(diagnostic) for diagnostic in event.diagnostics],
        "lifecycle_decisions": [
            _json_safe(decision) for decision in event.lifecycle_decisions
        ],
        "tool_result_render_decisions": [
            _json_safe(decision) for decision in event.tool_result_render_decisions
        ],
        "tool_result_budget_report": _json_safe(event.tool_result_budget_report),
    }


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


def _compaction_windows(events: Iterable[AgentEvent], store: PostgresInspectorStore) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, ContextCompactionCompletedEvent):
            continue
        artifact = store.artifact(event.summary_artifact_id)
        windows.append(
            {
                "sequence": event.sequence,
                "compaction_id": event.compaction_id,
                "trigger": event.trigger,
                "reason": event.reason,
                "phase": event.metadata.get("phase", _phase_from_reason(event.reason)),
                "safe_point": event.metadata.get("safe_point"),
                "current_run_id": event.metadata.get("current_run_id"),
                "max_compactable_sequence": event.metadata.get("max_compactable_sequence"),
                "tail_message_count": event.metadata.get("tail_message_count"),
                "window_number": event.window_number,
                "window_id": event.window_id,
                "summary_artifact_id": event.summary_artifact_id,
                "summary_artifact_present": artifact is not None,
                "through_sequence": event.through_sequence,
                "keep_after_sequence": event.keep_after_sequence,
                "estimated_tokens_before": event.estimated_tokens_before,
                "estimated_tokens_after": event.estimated_tokens_after,
                "threshold_tokens": event.threshold_tokens,
                "context_window_tokens": event.context_window_tokens,
                "included_run_ids": list(event.included_run_ids),
                "included_artifact_ids": list(event.included_artifact_ids),
            }
        )
    return windows


def _phase_from_reason(reason: str) -> str:
    if reason.startswith("mid_turn_"):
        return "mid_turn"
    if reason.startswith("preflight_") or reason == "context_threshold":
        return "preflight"
    if reason.startswith("run_end_"):
        return "run_end"
    return "unknown"


def _latest_compaction_window(events: Iterable[AgentEvent], store: PostgresInspectorStore) -> dict[str, Any] | None:
    windows = _compaction_windows(events, store)
    for window in reversed(windows):
        if window["summary_artifact_present"]:
            return window
    return None


def _compaction_diagnostics(events: Iterable[AgentEvent], store: PostgresInspectorStore) -> list[dict[str, Any]]:
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
        if isinstance(event, ContextCompactionStartedEvent) and event.compaction_id not in completed_ids | failed_ids:
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
        if isinstance(event, ContextCompactionCompletedEvent) and store.artifact(event.summary_artifact_id) is None:
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
        "usage": message.usage.model_dump(mode="json") if message.usage is not None else None,
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
            "text": _truncate("".join(part.text for part in block.output if isinstance(part, TextBlock)), 2_000),
            "artifacts": [artifact.model_dump(mode="json") for artifact in block.artifacts],
        }
    if isinstance(block, DataBlock):
        return {"type": "data", "id": block.id, "name": block.name, "source_type": block.source.type}
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
