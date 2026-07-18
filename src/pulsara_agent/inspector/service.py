"""Deterministic read-only Pulsara Inspector service."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Iterable

from pydantic import ValidationError

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    CapabilityGateDecisionEvent,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    ContextWindowClosedEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionFailedEvent,
    ContextWindowCompactionStartedEvent,
    ContextWindowOpenedEvent,
    MemoryReflectionCompletedEvent,
    MemoryReflectionFailedEvent,
    McpCapabilitySnapshotInstalledEvent,
    ModelCallEndEvent,
    ModelCallRejectedEvent,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    PhysicalOperationReservationCreatedEvent,
    PhysicalOperationReservationSettledEvent,
    PhysicalOperationReservationSuspendedEvent,
    ProjectionReadyEvent,
    ReplyStartEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RolloutPhaseTransitionedEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
    SubagentGraphCheckpointCommittedEvent,
    SubagentRunCompletedEvent,
    ToolResultTerminalProjectionCommittedEvent,
    TranscriptProjectionCheckpointCancelledEvent,
    TranscriptProjectionCheckpointCommittedEvent,
    TranscriptProjectionCheckpointFailedEvent,
    TranscriptProjectionCheckpointRecoveredInterruptedEvent,
)
from pulsara_agent.event_log import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    PostgresEventLog,
    dump_agent_event,
)
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
from pulsara_agent.runtime.context_input.event_slice import ContextEventSlice
from pulsara_agent.runtime.context_input.replay import (
    ContextInputReplayError,
    ContextInputReplayStatus,
    load_context_input_manifest,
    replay_compiled_context,
    replay_context_input,
)
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventSliceError,
    FrozenStoredEvent,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.transcript_projection import (
    RunTranscriptSeedArtifactFact,
)
from pulsara_agent.primitives.terminal_projection import (
    ModelCallSemanticSourceFact,
    ModelTerminalProjectionPayloadFact,
    TerminalProjectionDocumentFact,
)
from pulsara_agent.runtime.long_horizon.status import (
    derive_rollout_status_candidate,
    derive_rollout_status_shadow,
)
from pulsara_agent.runtime.long_horizon.store import LongHorizonStateStore
from pulsara_agent.runtime.subagent.projection import project_subagent_graph
from pulsara_agent.runtime.subagent.reducer import fold_subagent_graph


_REQUIRED_TABLES = (
    "sessions",
    "runs",
    "turns",
    "agent_events",
    "ledger_materialization_accounts",
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
        boundary_projections = [
            _run_boundary_projection(start, events, self.store)
            for start in events
            if isinstance(start, RunStartEvent)
        ]
        diagnostics.extend(
            diagnostic
            for projection in boundary_projections
            for diagnostic in projection.get("diagnostics", [])
        )
        rollout_status = _rollout_status_shadow_projection(
            events,
            runtime_session_id=session_id,
        )
        diagnostics.extend(rollout_status["diagnostics"])
        context_windows = _context_window_projection(events, self.store)
        diagnostics.extend(context_windows["diagnostics"])
        context_compilations = _context_compilation_projection(
            events,
            store=self.store,
        )
        long_horizon = _long_horizon_run_projection(
            events,
            runtime_session_id=session_id,
            context_compilations=context_compilations["contexts"],
        )
        diagnostics.extend(long_horizon["diagnostics"])
        authority_materialization = _authority_materialization_projection(
            events,
            account=self.store.materialization_account(session_id),
            store=self.store,
        )
        diagnostics.extend(authority_materialization["diagnostics"])
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
            "context_compilations": context_compilations,
            "model_targets": model_contracts["model_targets"],
            "model_calls": model_contracts["model_calls"],
            "model_usage_by_run": model_contracts["usage_by_run"],
            "compaction_model_contracts": model_contracts["compaction_model_contracts"],
            "reflection_model_contracts": model_contracts["reflection_model_contracts"],
            "compaction_windows": _compaction_windows(events, self.store),
            "context_windows": context_windows["windows"],
            "context_window_compactions": context_windows["compactions"],
            "subagent_graph": _subagent_graph_projection(
                session_id,
                events,
                self.store,
            ),
            "subagent_graph_checkpoints": _subagent_graph_checkpoint_projection(
                events
            ),
            "rollout_status_shadows": rollout_status["shadows"],
            "long_horizon_runs": long_horizon["runs"],
            "authority_materialization": authority_materialization,
            "transcript_projection": authority_materialization[
                "transcript_projection"
            ],
            "terminal_projections": authority_materialization[
                "terminal_projections"
            ],
            "checkpoint_accelerations": authority_materialization[
                "checkpoint_accelerations"
            ],
            "ledger_materialization_account": authority_materialization[
                "ledger_materialization_account"
            ],
            "mcp_installations": _mcp_installation_events_projection(events),
            "run_boundaries": boundary_projections,
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
        mcp_installation = _mcp_run_installation_projection(
            run_start,
            current_session_id=session_id,
            current_session_events=session_events,
            store=self.store,
        )
        if mcp_installation.get("status") == "missing":
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "mcp_installation_audit_missing",
                    "message": "RunStart references an MCP installation audit that could not be located.",
                    "details": {
                        "installation_id": mcp_installation.get("installation_id"),
                        "owner_runtime_session_id": mcp_installation.get(
                            "owner_runtime_session_id"
                        ),
                    },
                }
            )
        boundary_projection = (
            _run_boundary_projection(run_start, session_events, self.store)
            if run_start is not None
            else None
        )
        if boundary_projection is not None:
            diagnostics.extend(boundary_projection.get("diagnostics", []))
        rollout_status = _rollout_status_shadow_projection(
            session_events,
            runtime_session_id=session_id,
            root_run_id=run_id,
        )
        diagnostics.extend(rollout_status["diagnostics"])
        context_windows = _context_window_projection(run_events, self.store)
        diagnostics.extend(context_windows["diagnostics"])
        context_compilations = _context_compilation_projection(
            run_events,
            store=self.store,
        )
        long_horizon = _long_horizon_run_projection(
            session_events,
            runtime_session_id=session_id,
            root_run_id=run_id,
            context_compilations=context_compilations["contexts"],
        )
        diagnostics.extend(long_horizon["diagnostics"])

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
                "mcp_installation": mcp_installation,
            },
            "run_boundary": boundary_projection,
            "continuation_boundaries": (
                boundary_projection.get("continuation_boundaries", [])
                if boundary_projection is not None
                else []
            ),
            "child_run_entry": (
                boundary_projection.get("child_run_entry")
                if boundary_projection is not None
                else None
            ),
            "subagent_graph_checkpoints": _subagent_graph_checkpoint_projection(
                session_events
            ),
            "rollout_status_shadows": rollout_status["shadows"],
            "long_horizon": (
                long_horizon["runs"][0] if long_horizon["runs"] else None
            ),
            "timeline": timeline.to_dict(),
            "compaction_boundary_as_seen": compaction_boundary,
            "context_windows": context_windows["windows"],
            "context_window_compactions": context_windows["compactions"],
            "prior_messages_as_seen": [
                _message_to_dict(message) for message in prior_messages
            ],
            "projections_as_seen": [
                _projection_to_dict(event)
                for event in run_events
                if isinstance(event, ProjectionReadyEvent)
            ],
            "capability_surface_as_seen": _capability_surface_projection(run_events),
            "contexts_as_seen": context_compilations,
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


def _mcp_installation_events_projection(
    events: Iterable[AgentEvent],
) -> list[dict[str, Any]]:
    return [
        {
            "sequence": event.sequence,
            "installed_at_utc": event.created_at,
            "installation_id": event.installation_id,
            "previous_installation_id": event.previous_installation_id,
            "config_epoch": event.config_epoch,
            "event_safe_config_set_fingerprint": (
                event.event_safe_config_set_fingerprint
            ),
            "installation_triggers": list(event.installation_triggers),
            "coalesced_installation_count": event.coalesced_installation_count,
            "coalesced_attempt_summaries": [
                summary.model_dump(mode="json")
                for summary in event.coalesced_attempt_summaries
            ],
            "coalesced_attempt_summaries_omitted": (
                event.coalesced_attempt_summaries_omitted
            ),
            "server_snapshots": [
                snapshot.model_dump(mode="json") for snapshot in event.server_snapshots
            ],
            "total_installed_tool_count": event.total_installed_tool_count,
            "added_tool_count": event.added_tool_count,
            "revoked_tool_count": event.revoked_tool_count,
            "changed_tool_names": list(event.changed_tool_names_bounded),
            "changed_tool_names_omitted": event.changed_tool_names_omitted,
            "diagnostics": [
                diagnostic.model_dump(mode="json") for diagnostic in event.diagnostics
            ],
        }
        for event in events
        if isinstance(event, McpCapabilitySnapshotInstalledEvent)
    ]


def _mcp_run_installation_projection(
    run_start: RunStartEvent | None,
    *,
    current_session_id: str,
    current_session_events: Iterable[AgentEvent],
    store: PostgresInspectorStore,
) -> dict[str, Any]:
    if run_start is None:
        return {"status": "missing_run_start"}
    installation_id = run_start.mcp_installation_id
    owner_id = run_start.mcp_installation_owner_runtime_session_id
    if installation_id == "mcp_installation:empty":
        return {
            "status": "canonical_empty",
            "installation_id": installation_id,
            "owner_runtime_session_id": owner_id,
            "owner_is_current_session": owner_id == current_session_id,
            "audit": None,
        }
    owner_events = (
        list(current_session_events)
        if owner_id == current_session_id
        else store.events_for_session(owner_id)
    )
    audit = next(
        (
            event
            for event in owner_events
            if isinstance(event, McpCapabilitySnapshotInstalledEvent)
            and event.installation_id == installation_id
        ),
        None,
    )
    return {
        "status": "durable" if audit is not None else "missing",
        "installation_id": installation_id,
        "owner_runtime_session_id": owner_id,
        "owner_is_current_session": owner_id == current_session_id,
        "audit": (
            _mcp_installation_events_projection((audit,))[0]
            if audit is not None
            else None
        ),
    }


def _run_boundary_projection(
    run_start: RunStartEvent,
    session_events: Iterable[AgentEvent],
    store: PostgresInspectorStore,
) -> dict[str, Any]:
    """Project only durable run-entry/continuation facts; never query live Host state."""

    events = list(session_events)
    run_events = [event for event in events if event.run_id == run_start.run_id]
    exposures = sorted(
        (
            event
            for event in run_events
            if isinstance(event, CapabilityExposureResolvedEvent)
        ),
        key=lambda event: event.sequence or 0,
    )
    diagnostics: list[dict[str, Any]] = []
    initial_exposure = next(
        (event for event in exposures if event.exposure_revision == 1),
        None,
    )
    current_user = run_start.current_user_message
    transcript_seed = _run_transcript_seed_projection(run_start, store)

    continuation_events = sorted(
        (
            event
            for event in run_events
            if isinstance(event, RunInteractionResumeBoundaryEvent)
        ),
        key=lambda event: event.sequence or 0,
    )
    exposure_by_id = {event.exposure.exposure_id: event for event in exposures}
    continuations: list[dict[str, Any]] = []
    for event in continuation_events:
        boundary = event.boundary
        effective = exposure_by_id.get(boundary.effective_exposure_id)
        source = exposure_by_id.get(boundary.source_exposure_id)
        status = "committed"
        if source is None or effective is None:
            status = "contract_error"
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "resume_boundary_exposure_join_missing",
                    "message": "Resume boundary source/effective exposure cannot be joined.",
                    "details": {
                        "boundary_id": boundary.identity.boundary_id,
                        "source_exposure_id": boundary.source_exposure_id,
                        "effective_exposure_id": boundary.effective_exposure_id,
                    },
                }
            )
        continuations.append(
            {
                "status": status,
                "boundary_id": boundary.identity.boundary_id,
                "interaction_id": boundary.interaction_id,
                "interaction_kind": boundary.interaction_kind,
                "original_run_start_event_id": (boundary.original_run_start_event_id),
                "original_run_start_sequence": (boundary.original_run_start_sequence),
                "mcp_installation_id": boundary.mcp_installation_id,
                "source_exposure_id": boundary.source_exposure_id,
                "source_exposure_semantic_fingerprint": (
                    boundary.source_exposure_semantic_fingerprint
                ),
                "source_exposure_fact_fingerprint": (
                    boundary.source_exposure_fact_fingerprint
                ),
                "effective_exposure_id": boundary.effective_exposure_id,
                "effective_exposure_semantic_fingerprint": (
                    boundary.effective_exposure_semantic_fingerprint
                ),
                "effective_exposure_fact_fingerprint": (
                    boundary.effective_exposure_fact_fingerprint
                ),
                "exposure_transition": boundary.exposure_transition,
                "committed_audit_event_ids": list(
                    boundary.committed_mcp_audit_event_ids
                ),
                "sequence": event.sequence,
                "created_at": event.created_at,
            }
        )

    if run_start.new_run_boundary is not None:
        boundary = run_start.new_run_boundary
        identity = boundary.identity
        if (
            initial_exposure is None
            or initial_exposure.exposure.owner.owner_id != identity.boundary_id
        ):
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "initial_capability_exposure_missing_or_mismatched",
                    "message": "Host RunStart has no matching initial capability exposure.",
                    "details": {"boundary_id": identity.boundary_id},
                }
            )
        exposure = initial_exposure.exposure if initial_exposure is not None else None
        compaction_events = [
            event
            for event in events
            if isinstance(
                event,
                (
                    ContextCompactionStartedEvent,
                    ContextCompactionCompletedEvent,
                    ContextCompactionFailedEvent,
                ),
            )
            and event.host_boundary_id == identity.boundary_id
        ]
        return {
            "boundary_id": identity.boundary_id,
            "kind": identity.kind.value,
            "run_entry_kind": "host",
            "status": "committed",
            "durable_run_existence": "full",
            "observed_at_utc": identity.observed_at_utc,
            "current_user_message_id": current_user.message_id,
            "current_user_chars": len(current_user.text),
            "current_user_content_sha256": current_user.content_sha256,
            "transcript_seed": transcript_seed,
            "source_through_sequence": boundary.transcript.source_through_sequence,
            "preflight_compaction": (
                {
                    "compaction_id": boundary.transcript.preflight_compaction_id,
                    "terminal_event_id": (
                        boundary.transcript.preflight_compaction_terminal_event_id
                    ),
                    "terminal_sequence": (
                        boundary.transcript.preflight_compaction_terminal_sequence
                    ),
                    "events": [
                        {
                            "type": event.type.value,
                            "event_id": event.id,
                            "sequence": event.sequence,
                        }
                        for event in compaction_events
                    ],
                }
                if boundary.transcript.preflight_compaction_id is not None
                else None
            ),
            "permission_snapshot_id": boundary.permission_snapshot_id,
            "target_fingerprint": boundary.model_target_fingerprint,
            "mcp_installation_id": boundary.mcp_installation_id,
            "capability_basis_fingerprint": (
                boundary.capability_basis.basis_fingerprint
            ),
            "execution_surface_fingerprint": (
                boundary.capability_basis.execution_surface_identity.execution_surface_fingerprint
            ),
            "descriptor_set_fingerprint": (
                boundary.capability_basis.execution_surface_identity.descriptor_set_fingerprint
            ),
            "execution_binding_set_fingerprint": (
                boundary.capability_basis.execution_surface_identity.execution_binding_set_fingerprint
            ),
            "catalog_projection_fingerprint": (
                exposure.semantic.catalog_projection.projection_semantic_fingerprint
                if exposure is not None
                else None
            ),
            "active_skill_projection_fingerprint": (
                exposure.semantic.active_skill_projection.projection_semantic_fingerprint
                if exposure is not None
                else None
            ),
            "exposure_semantic_fingerprint": (
                exposure.exposure_semantic_fingerprint if exposure is not None else None
            ),
            "exposure_fact_fingerprint": (
                exposure.exposure_fact_fingerprint if exposure is not None else None
            ),
            "run_start_event_id": run_start.id,
            "run_start_sequence": run_start.sequence,
            "continuation_boundaries": continuations,
            "child_run_entry": None,
            "diagnostics": diagnostics,
        }

    entry = run_start.subagent_run_entry
    if entry is None:
        diagnostics.append(
            {
                "severity": "error",
                "code": "run_entry_fact_missing",
                "message": "RunStart has neither Host nor subagent entry fact.",
                "details": {"run_start_event_id": run_start.id},
            }
        )
        return {
            "status": "contract_error",
            "run_entry_kind": str(run_start.run_entry_kind),
            "run_start_event_id": run_start.id,
            "run_start_sequence": run_start.sequence,
            "continuation_boundaries": continuations,
            "child_run_entry": None,
            "diagnostics": diagnostics,
        }

    parent_events = store.events_for_session(entry.parent_runtime_session_id)
    parent_terminal = next(
        (
            event
            for event in parent_events
            if isinstance(event, SubagentRunCompletedEvent)
            and event.subagent_run_id == entry.subagent_run_id
            and event.child_run_id == run_start.run_id
        ),
        None,
    )
    handoff = parent_terminal.result_handoff if parent_terminal is not None else None
    terminal_ref = handoff.child_terminal_reference if handoff is not None else None
    if terminal_ref is not None:
        child_terminal = next(
            (
                event
                for event in run_events
                if event.id == terminal_ref.terminal_event_id
                and event.sequence == terminal_ref.terminal_sequence
            ),
            None,
        )
        if child_terminal is None:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "child_terminal_reference_missing",
                    "message": "Parent child-result handoff references a missing child terminal.",
                    "details": {
                        "terminal_event_id": terminal_ref.terminal_event_id,
                    },
                }
            )
    child_projection = {
        "run_entry_kind": "subagent_child",
        "subagent_run_id": entry.subagent_run_id,
        "subagent_task_id": entry.subagent_task_id,
        "entry_mode": (
            "task_backed" if entry.subagent_task_id is not None else "primitive_run"
        ),
        "parent_runtime_session_id": entry.parent_runtime_session_id,
        "parent_run_id": entry.parent_run_id,
        "task_artifact_id": entry.task_artifact_id,
        "child_result_render_policy": (
            entry.child_result_render_policy.model_dump(mode="json")
        ),
        "current_user_message_id": current_user.message_id,
        "current_user_content_sha256": current_user.content_sha256,
        "transcript_seed": transcript_seed,
        "exposure_owner_kind": (
            initial_exposure.exposure.owner.owner_kind
            if initial_exposure is not None
            else None
        ),
        "child_terminal_reference": (
            terminal_ref.model_dump(mode="json") if terminal_ref is not None else None
        ),
        "child_result_handoff": (
            {
                **handoff.model_dump(mode="json"),
                "max_summary_chars": entry.child_result_render_policy.max_summary_chars,
                "max_artifact_refs": entry.child_result_render_policy.max_artifact_refs,
                "explicit_source_tool_call_id": (
                    handoff.explicit_evidence.source_tool_call_id
                    if handoff.explicit_evidence is not None
                    else None
                ),
            }
            if handoff is not None
            else None
        ),
        "run_start_event_id": run_start.id,
        "run_start_sequence": run_start.sequence,
    }
    return {
        "status": "committed",
        "run_entry_kind": "subagent_child",
        "run_start_event_id": run_start.id,
        "run_start_sequence": run_start.sequence,
        "continuation_boundaries": continuations,
        "child_run_entry": child_projection,
        "diagnostics": diagnostics,
    }


def _run_transcript_seed_projection(
    run_start: RunStartEvent,
    store: PostgresInspectorStore,
) -> dict[str, Any]:
    semantic = run_start.run_transcript_seed_semantic
    source = semantic.prior_semantic_source
    stable = semantic.prior_stable_semantic_state
    reference = run_start.run_transcript_seed_reference
    source_state_joined = (
        source.semantic_source_event_count == stable.semantic_source_event_count
        and source.semantic_source_accumulator == stable.semantic_source_accumulator
        and source.resulting_state_fingerprint == stable.state_semantic_fingerprint
        and semantic.normalized_prior_transcript_fingerprint
        == stable.normalized_transcript_fingerprint
    )
    artifact_row = store.artifact(reference.seed_artifact_id)
    restore_outcome = "artifact_missing"
    root_manifest_contract_fingerprint = None
    total_entry_count = None
    if artifact_row is not None:
        try:
            text = artifact_row["text_body"]
            if not isinstance(text, str):
                raise ValueError("run transcript seed artifact is not text")
            payload = text.encode("utf-8")
            artifact = RunTranscriptSeedArtifactFact.model_validate_json(payload)
            if (
                len(payload) != reference.seed_artifact_bytes
                or f"sha256:{sha256(payload).hexdigest()}"
                != reference.seed_artifact_sha256
                or artifact.seed_semantic != semantic
                or artifact.artifact_contract_fingerprint
                != reference.seed_artifact_contract_fingerprint
                or artifact.root_manifest.materialization_fingerprint
                != reference.root_materialization_fingerprint
            ):
                raise ValueError("run transcript seed artifact identity drifted")
            root_manifest_contract_fingerprint = (
                artifact.root_manifest.root_manifest_contract_fingerprint
            )
            total_entry_count = artifact.root_manifest.total_entry_count
            restore_outcome = "exact"
        except (TypeError, ValueError):
            restore_outcome = "contract_mismatch"
    return {
        "seed_semantic_fingerprint": semantic.seed_semantic_fingerprint,
        "normalized_prior_transcript_fingerprint": (
            semantic.normalized_prior_transcript_fingerprint
        ),
        "semantic_source": {
            "reducer_id": source.reducer_id,
            "reducer_version": source.reducer_version,
            "reducer_contract_fingerprint": source.reducer_contract_fingerprint,
            "event_count": source.semantic_source_event_count,
            "accumulator": source.semantic_source_accumulator,
            "resulting_state_fingerprint": source.resulting_state_fingerprint,
        },
        "stable_state_fingerprint": stable.state_semantic_fingerprint,
        "prior_source_state_join_outcome": (
            "matched" if source_state_joined else "mismatch"
        ),
        "root_manifest_contract_fingerprint": (
            root_manifest_contract_fingerprint
        ),
        "total_entry_count": total_entry_count,
        "restore_outcome": restore_outcome,
        "materialization": {
            "seed_artifact_id": reference.seed_artifact_id,
            "seed_artifact_sha256": reference.seed_artifact_sha256,
            "seed_artifact_bytes": reference.seed_artifact_bytes,
            "root_materialization_fingerprint": (
                reference.root_materialization_fingerprint
            ),
            "artifact_contract_fingerprint": (
                reference.seed_artifact_contract_fingerprint
            ),
        },
        "source_ledger": {
            "runtime_session_id": reference.source_runtime_session_id,
            "through_sequence": reference.source_ledger_through_sequence,
            "continuity_accumulator": (
                reference.source_ledger_continuity_accumulator
            ),
            "checkpoint_id": reference.source_checkpoint_id,
        },
        "reference_fingerprint": reference.reference_fingerprint,
    }


def _authority_materialization_projection(
    events: Iterable[AgentEvent],
    *,
    account,
    store: PostgresInspectorStore,
) -> dict[str, Any]:
    ordered = tuple(sorted(events, key=lambda item: item.sequence or 0))
    run_starts = tuple(
        item for item in ordered if isinstance(item, RunStartEvent)
    )
    projection_events = tuple(
        item
        for item in ordered
        if isinstance(
            item,
            (
                ModelCallTerminalProjectionCommittedEvent,
                ToolResultTerminalProjectionCommittedEvent,
            ),
        )
    )
    checkpoint_events = tuple(
        item
        for item in ordered
        if isinstance(item, TranscriptProjectionCheckpointCommittedEvent)
    )
    checkpoint_terminals = {
        item.checkpoint_id: item
        for item in ordered
        if isinstance(
            item,
            (
                TranscriptProjectionCheckpointFailedEvent,
                TranscriptProjectionCheckpointCancelledEvent,
                TranscriptProjectionCheckpointRecoveredInterruptedEvent,
            ),
        )
    }
    latest_source = (
        checkpoint_events[-1].checkpoint.semantic_source
        if checkpoint_events
        else run_starts[-1].run_transcript_seed_semantic.prior_semantic_source
        if run_starts
        else None
    )
    transcript_projection = (
        None
        if latest_source is None
        else {
            "reducer_id": latest_source.reducer_id,
            "reducer_version": latest_source.reducer_version,
            "reducer_contract_fingerprint": (
                latest_source.reducer_contract_fingerprint
            ),
            "transcript_semantic_domain_contract_fingerprint": (
                latest_source.transcript_semantic_domain_contract_fingerprint
            ),
            "semantic_source_event_count": (
                latest_source.semantic_source_event_count
            ),
            "semantic_source_accumulator": (
                latest_source.semantic_source_accumulator
            ),
            "state_fingerprint": latest_source.resulting_state_fingerprint,
            "source_kind": "checkpoint" if checkpoint_events else "run_seed",
        }
    )
    terminal_projections = [
        _terminal_projection_event_projection(item, store=store)
        for item in projection_events
    ]
    checkpoints = []
    for item in checkpoint_events:
        candidate = item.checkpoint
        materialization = candidate.materialization
        terminal = checkpoint_terminals.get(item.checkpoint_id)
        checkpoints.append(
            {
                "checkpoint_id": item.checkpoint_id,
                "checkpoint_intent_event_id": (
                    item.checkpoint_intent_event_identity.event_id
                ),
                "checkpoint_committed_event_id": item.id,
                "checkpoint_committed_event_sequence": item.sequence,
                "checkpoint_terminal_outcome": (
                    str(terminal.type) if terminal is not None else "committed"
                ),
                "checkpoint_terminal_event_id": (
                    terminal.id if terminal is not None else item.id
                ),
                "checkpoint_candidate_fingerprint": (
                    item.checkpoint_candidate_fingerprint
                ),
                "root_kind": materialization.root_kind,
                "checkpoint_artifact_id": (
                    materialization.root_manifest_ref.root_artifact_id
                ),
                "checkpoint_artifact_bytes": (
                    materialization.root_manifest_ref.root_byte_count
                ),
                "ledger_materialization_generation": (
                    candidate.source_ledger_materialization_generation
                ),
                "materialization_consumer_id": (
                    candidate.materialization_consumer_id
                ),
                "previous_checkpoint_id": candidate.previous_checkpoint_id,
                "root_manifest_contract_fingerprint": (
                    materialization.root_manifest_ref.root_manifest_contract_fingerprint
                ),
                "tree_contract_fingerprint": (
                    materialization.tree_contract_fingerprint
                ),
                "tree_height": getattr(materialization, "tree_height", 0),
                "total_entry_count": materialization.total_entry_count,
                "restore_outcome": "not_hydrated",
                "candidate_ledger_through_sequence": (
                    candidate.candidate_ledger_through_sequence
                ),
                "candidate_ledger_continuity_accumulator": (
                    candidate.candidate_ledger_continuity_accumulator
                ),
                "semantic_source": candidate.semantic_source.model_dump(mode="json"),
                "stable_state": candidate.stable_semantic_state.model_dump(
                    mode="json"
                ),
                "terminal": (
                    terminal.model_dump(mode="json")
                    if terminal is not None
                    else None
                ),
            }
        )
    diagnostics: list[dict[str, Any]] = []
    if ordered and account is None:
        diagnostics.append(
            {
                "severity": "error",
                "code": "ledger_materialization_account_missing",
                "message": (
                    "Non-empty hard-cut ledger has no materialization account; "
                    "reset the database."
                ),
            }
        )
    account_projection = None
    authority_pressure = None
    if account is not None:
        generation = account.generation
        account_projection = {
            "runtime_session_id": account.runtime_session_id,
            "ledger_materialization_generation": (
                generation.ledger_materialization_generation
            ),
            "consumer_horizon_revision": generation.consumer_horizon_revision,
            "reclaimable_through_sequence": (
                generation.reclaimable_through_sequence
            ),
            "consumer_horizons": [
                item.model_dump(mode="json") for item in generation.consumer_horizons
            ],
            "ledger_through_sequence": account.ledger_through_sequence,
            "ledger_charged_payload_bytes_through": (
                account.ledger_charged_payload_bytes_through
            ),
            "active_checkpoint_barrier": (
                account.active_checkpoint_barrier.model_dump(mode="json")
                if account.active_checkpoint_barrier is not None
                else None
            ),
            "active_reservations": [
                item.model_dump(mode="json") for item in account.active_reservations
            ],
            "latest_transition_event_ids": list(
                account.latest_transition_event_ids
            ),
            "reconciliation_required": account.reconciliation_required,
            "reconciliation_reason_code": account.reconciliation_reason_code,
            "account_state_fingerprint": account.account_state_fingerprint,
            "projection_row_verified": "schema_valid_not_full_folded",
        }
        authority_pressure = {
            "ledger_materialization_generation": (
                generation.ledger_materialization_generation
            ),
            "reclaimable_through_sequence": (
                generation.reclaimable_through_sequence
            ),
            "consumer_horizons": [
                item.model_dump(mode="json") for item in generation.consumer_horizons
            ],
            "events_since_reclaimable_horizon": (
                account.used_since_reclaimable_events
            ),
            "charged_payload_bytes_since_reclaimable_horizon": (
                account.used_since_reclaimable_payload_bytes
            ),
            "active_reservation_count": len(account.active_reservations),
            "active_remaining_reserved_events": sum(
                item.remaining_events for item in account.active_reservations
            ),
            "active_remaining_reserved_bytes": sum(
                item.remaining_payload_bytes for item in account.active_reservations
            ),
            "historical_hard_limits": None,
            "current_config_not_recomputed": True,
        }
    return {
        "transcript_projection": transcript_projection,
        "terminal_projections": terminal_projections,
        "checkpoint_accelerations": checkpoints,
        "ledger_materialization_account": account_projection,
        "authority_pressure": authority_pressure,
        "reservation_lifecycle": _physical_reservation_lifecycle(ordered),
        "diagnostics": diagnostics,
    }


def _terminal_projection_event_projection(
    event,
    *,
    store: PostgresInspectorStore,
) -> dict[str, Any]:
    reference = event.projection_reference
    join = reference.semantic_join
    projection = {
        "projection_kind": reference.projection_kind,
        "semantic_identity_fingerprint": join.semantic_fingerprint,
        "document_fact_fingerprint": reference.document_fact_fingerprint,
        "reference_fingerprint": reference.reference_fingerprint,
        "document_artifact_id": reference.document_artifact_id,
        "document_byte_count": reference.document_byte_count,
        "committed_event_id": event.id,
        "committed_event_sequence": event.sequence,
        "semantic_join": join.model_dump(mode="json"),
        "restore_outcome": "artifact_missing",
        "completed_block_count": None,
        "interrupted_block_count": None,
        "projection_order_verified": None,
        "model_stream_settlement_measurement": None,
    }
    artifact = store.artifact(reference.document_artifact_id)
    if artifact is not None:
        try:
            text = artifact["text_body"]
            if not isinstance(text, str):
                raise ValueError("terminal projection artifact is not text")
            payload = text.encode("utf-8")
            document = TerminalProjectionDocumentFact.model_validate_json(payload)
            if (
                len(payload) != reference.document_byte_count
                or f"sha256:{sha256(payload).hexdigest()}" != reference.document_sha256
                or document.fact_fingerprint != reference.document_fact_fingerprint
                or document.semantic_identity.semantic_fingerprint
                != join.semantic_fingerprint
            ):
                raise ValueError("terminal projection artifact identity drifted")
            projection["restore_outcome"] = "exact"
            projection["projection_order_verified"] = True
            if isinstance(document.payload, ModelTerminalProjectionPayloadFact):
                statuses = tuple(
                    getattr(
                        item.semantic_identity,
                        "completion_status",
                        "completed",
                    )
                    for item in document.payload.items
                )
                projection["completed_block_count"] = statuses.count("completed")
                projection["interrupted_block_count"] = statuses.count(
                    "interrupted"
                )
                if isinstance(document.source_fact, ModelCallSemanticSourceFact):
                    measurement = document.source_fact.stream_settlement_measurement
                    projection["model_stream_settlement_measurement"] = {
                        "measurement_fingerprint": measurement.measurement_fingerprint,
                        "physical_accounting_mode": (
                            measurement.physical_accounting_mode
                        ),
                        "adapter_source_item_count": (
                            measurement.adapter_source_item_count
                        ),
                        "adapter_source_payload_bytes": (
                            measurement.adapter_source_payload_bytes
                        ),
                        "synthetic_source_item_count": (
                            measurement.synthetic_source_item_count
                        ),
                        "synthetic_source_payload_bytes": (
                            measurement.synthetic_source_payload_bytes
                        ),
                        "singleton_event_count": measurement.singleton_event_count,
                        "segment_event_count": measurement.segment_event_count,
                        "durable_semantic_event_count": (
                            measurement.durable_semantic_event_count
                        ),
                        "segment_content_utf8_bytes": (
                            measurement.segment_content_utf8_bytes
                        ),
                        "durable_candidate_payload_bytes": (
                            measurement.durable_candidate_payload_bytes
                        ),
                        "actual_semantic_commit_batch_count": (
                            measurement.actual_semantic_commit_batch_count
                        ),
                    }
            else:
                projection["completed_block_count"] = 1
                projection["interrupted_block_count"] = 0
        except (KeyError, TypeError, ValueError, ValidationError):
            projection["restore_outcome"] = "contract_mismatch"
            projection["projection_order_verified"] = False
    if isinstance(event, ModelCallTerminalProjectionCommittedEvent):
        projection["resolved_model_call_id"] = event.resolved_model_call_id
        projection["source_event_id"] = (
            event.model_call_start_event_identity.event_id
        )
    else:
        projection["tool_call_id"] = event.tool_call_id
        projection["source_kind"] = event.source_kind
        projection["source_event_id"] = event.source_event_identity.event_id
    return projection


def _physical_reservation_lifecycle(
    events: tuple[AgentEvent, ...],
) -> list[dict[str, Any]]:
    lifecycle = []
    for event in events:
        if isinstance(event, PhysicalOperationReservationCreatedEvent):
            lifecycle.append(
                {
                    "event_id": event.id,
                    "sequence": event.sequence,
                    "status": "active",
                    "reservation": event.reservation.model_dump(mode="json"),
                    "resulting_account_state_fingerprint": (
                        event.resulting_account_state_fingerprint
                    ),
                }
            )
        elif isinstance(event, PhysicalOperationReservationSuspendedEvent):
            lifecycle.append(
                {
                    "event_id": event.id,
                    "sequence": event.sequence,
                    "status": "suspended_tail",
                    "suspension": event.suspension.model_dump(mode="json"),
                    "resulting_account_state_fingerprint": (
                        event.resulting_account_state_fingerprint
                    ),
                }
            )
        elif isinstance(event, PhysicalOperationReservationSettledEvent):
            lifecycle.append(
                {
                    "event_id": event.id,
                    "sequence": event.sequence,
                    "status": "settled",
                    "settlement": event.settlement.model_dump(mode="json"),
                    "resulting_account_state_fingerprint": (
                        event.resulting_account_state_fingerprint
                    ),
                }
            )
    return lifecycle


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
        if isinstance(event, CapabilityExposureResolvedEvent):
            value = event.exposure.model_dump(mode="json")
            value["direct_names"] = sorted(
                entry.capability_name
                for entry in event.exposure.authorization_entries
                if entry.disposition == "direct"
            )
            value["callable_names"] = sorted(
                entry.capability_name
                for entry in event.exposure.authorization_entries
                if entry.callable
            )
            value["exposure_revision"] = event.exposure_revision
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


def _context_compilation_projection(
    events: Iterable[AgentEvent],
    *,
    store: PostgresInspectorStore | None = None,
) -> dict[str, Any]:
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
    projected = [
        _context_compiled_to_dict(
            event,
            input_replay=(
                _context_input_replay_projection(event, store)
                if store is not None
                else None
            ),
        )
        for event in context_events
    ]
    return {
        "latest": projected[-1] if context_events else None,
        "contexts": projected,
        "model_call_joins": joins,
        "diagnostics": diagnostics,
    }


def _context_compiled_to_dict(
    event: ContextCompiledEvent,
    *,
    input_replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        "canonical_tool_result_render_decisions": [
            decision.model_dump(mode="json")
            for decision in event.tool_result_render_decision_facts
        ],
        "tool_result_render_operational": [
            item.model_dump(mode="json")
            for item in event.tool_result_render_operational_facts
        ],
        "tool_result_timings": _tool_result_timing_projection(
            event.tool_result_render_decisions
        ),
        "tool_result_budget_report": _json_safe(event.tool_result_budget_report),
        "long_horizon_context_budget_decision": (
            event.long_horizon_context_budget_decision.model_dump(mode="json")
            if event.long_horizon_context_budget_decision is not None
            else None
        ),
        "long_horizon_projection_pressure_shadow": (
            event.long_horizon_projection_pressure_shadow.model_dump(mode="json")
            if event.long_horizon_projection_pressure_shadow is not None
            else None
        ),
        "input_status": (
            "audited" if event.input_audit is not None else "input_failed"
        ),
        "input_audit": (
            event.input_audit.model_dump(mode="json")
            if event.input_audit is not None
            else None
        ),
        "input_failure": (
            event.input_failure.model_dump(mode="json")
            if event.input_failure is not None
            else None
        ),
        "provider_neutral_payload_fingerprint": (
            event.provider_neutral_payload_fingerprint
        ),
        "canonical_render_decisions_fingerprint": (
            event.canonical_render_decisions_fingerprint
        ),
        "input_replay": input_replay,
    }


def _context_input_replay_projection(
    event: ContextCompiledEvent,
    store: PostgresInspectorStore,
) -> dict[str, Any]:
    if event.input_audit is None:
        failure = event.input_failure
        reason = (
            failure.reason_code.value
            if failure is not None
            else "missing_input_carrier"
        )
        status = (
            ContextInputReplayStatus.LEDGER_UNTRUSTED
            if reason == "ledger_untrusted"
            else ContextInputReplayStatus.ARTIFACT_MISSING
            if reason == "manifest_confirmed_absent"
            else ContextInputReplayStatus.CONTRACT_MISMATCH
        )
        return {
            "status": status.value,
            "diagnostics": [
                {
                    "code": reason,
                    "message": "Context compilation did not confirm a replay manifest.",
                }
            ],
            "manifest": None,
        }

    audit = event.input_audit
    archive = PostgresArtifactStore(store.dsn)
    try:
        manifest = load_context_input_manifest(audit=audit, archive=archive)
        primary = _context_event_slice_for_range(
            store=store,
            runtime_session_id=manifest.snapshot.primary_event_range.runtime_session_id,
            first_sequence=manifest.snapshot.primary_event_range.first_sequence,
            through_sequence=manifest.snapshot.primary_event_range.through_sequence,
        )
        named = tuple(
            _context_event_slice_for_range(
                store=store,
                runtime_session_id=item.runtime_session_id,
                first_sequence=item.first_sequence,
                through_sequence=item.through_sequence,
            )
            for item in manifest.snapshot.named_event_ranges
        )
        replay_event_log = PostgresEventLog(
            dsn=store.dsn,
            runtime_session_id=audit.source_runtime_session_id,
        )
        replayed = replay_context_input(
            audit=audit,
            archive=archive,
            event_log=replay_event_log,
            event_slice=primary,
            named_slices=named,
        )
        try:
            exact = replay_compiled_context(
                event=event,
                archive=archive,
                event_log=replay_event_log,
                event_slice=primary,
                named_slices=named,
            )
        except ContextInputReplayError as exc:
            if exc.status is not ContextInputReplayStatus.FACT_REPLAY_ONLY:
                raise
            replay_status = exc.status
            replay_diagnostics = [{"code": exc.reason_code, "message": str(exc)}]
        else:
            replayed = exact.inputs
            replay_status = ContextInputReplayStatus.EXACT_REPLAY
            replay_diagnostics = []
    except ContextInputReplayError as exc:
        return {
            "status": exc.status.value,
            "diagnostics": [{"code": exc.reason_code, "message": str(exc)}],
            "manifest": {
                "artifact_id": audit.input_manifest_artifact_id,
                "fingerprint": audit.input_manifest_fingerprint,
                "write_outcome": audit.input_manifest_write_outcome,
            },
        }
    except ContextEventSliceError as exc:
        return {
            "status": ContextInputReplayStatus.LEDGER_UNTRUSTED.value,
            "diagnostics": [
                {
                    "code": "context_input_event_slice_untrusted",
                    "message": str(exc),
                }
            ],
            "manifest": {
                "artifact_id": audit.input_manifest_artifact_id,
                "fingerprint": audit.input_manifest_fingerprint,
                "write_outcome": audit.input_manifest_write_outcome,
            },
        }

    snapshot = replayed.manifest.snapshot
    transcript_provider_projection = (
        replayed.manifest.transcript_provider_projection
    )
    transcript_authority = replayed.manifest.transcript_authority
    units = replayed.normalized_transcript.tool_result_units
    candidates = replayed.prepared_candidates
    profile_counts: dict[str, int] = {}
    builder_contracts: dict[tuple[str, str, str], dict[str, str]] = {}
    for unit in units:
        variant = unit.render_profile.selected_variant.variant_code.value
        profile_counts[variant] = profile_counts.get(variant, 0) + 1
        contract = unit.render_profile.render_contract.semantics_builder_contract
        key = (
            contract.builder_id,
            contract.builder_version,
            contract.contract_fingerprint,
        )
        builder_contracts[key] = {
            "builder_id": contract.builder_id,
            "builder_version": contract.builder_version,
            "contract_fingerprint": contract.contract_fingerprint,
        }
    source_counts: dict[str, int] = {}
    lifecycle_counts: dict[str, int] = {}
    for entry in candidates.entries:
        source = entry.candidate.source_kind
        source_counts[source] = source_counts.get(source, 0) + 1
        lifecycle = entry.lifecycle.status
        lifecycle_counts[lifecycle] = lifecycle_counts.get(lifecycle, 0) + 1
    source_selections = [
        item.model_dump(mode="json")
        for item in snapshot.candidate_source_selections[:64]
    ]
    collection_decisions = [
        item.model_dump(mode="json")
        for item in candidates.collection_decisions[:128]
    ]
    window = snapshot.authority_slice_plan.transcript_window
    try:
        rollout_status_hint = _replayed_rollout_status_hint(
            event=event,
            snapshot=snapshot,
            primary=primary,
            named=named,
        )
    except ContextInputReplayError as exc:
        return {
            "status": exc.status.value,
            "diagnostics": [{"code": exc.reason_code, "message": str(exc)}],
            "manifest": {
                "artifact_id": audit.input_manifest_artifact_id,
                "fingerprint": replayed.manifest.manifest_fingerprint,
                "write_outcome": audit.input_manifest_write_outcome,
            },
        }
    return {
        "status": replay_status.value,
        "diagnostics": replay_diagnostics,
        "manifest": {
            "artifact_id": audit.input_manifest_artifact_id,
            "fingerprint": replayed.manifest.manifest_fingerprint,
            "write_outcome": audit.input_manifest_write_outcome,
            "aggregate_fingerprint": replayed.manifest.input_aggregate_fingerprint,
        },
        "subagent_graph": {
            "semantic_source": (
                replayed.manifest.subagent_graph_semantic_source.model_dump(
                    mode="json"
                )
            ),
            "preferred_checkpoint_id": (
                replayed.manifest.subagent_graph_acceleration.checkpoint_id
            ),
            "actual_checkpoint_id": (
                replayed.subagent_graph_acceleration.checkpoint_id
            ),
            "rebased": (
                replayed.manifest.subagent_graph_acceleration.checkpoint_id
                != replayed.subagent_graph_acceleration.checkpoint_id
            ),
            "checkpoint_through_sequence": (
                replayed.subagent_graph_acceleration.checkpoint_through_sequence
            ),
            "delta_from_sequence": (
                replayed.subagent_graph_acceleration.delta_from_sequence
            ),
            "delta_through_sequence": (
                replayed.subagent_graph_acceleration.delta_through_sequence
            ),
            "delta_count": replayed.subagent_graph_acceleration.delta_count,
            "delta_byte_count": (
                replayed.subagent_graph_acceleration.delta_byte_count
            ),
            "ledger_through_sequence": (
                replayed.subagent_graph_acceleration.ledger_through_sequence
            ),
            "ledger_continuity_accumulator": (
                replayed.subagent_graph_acceleration.ledger_continuity_accumulator
            ),
        },
        "snapshot": {
            "snapshot_id": snapshot.identity.snapshot_id,
            "schema_version": snapshot.identity.schema_version,
            "semantic_fingerprint": snapshot.snapshot_semantic_fingerprint,
            "fact_fingerprint": snapshot.snapshot_fact_fingerprint,
            "compiler_contract_version": snapshot.identity.compiler_contract_version,
            "run_entry_kind": snapshot.run_entry.run_entry_kind,
            "run_start": snapshot.run_entry.run_start.model_dump(mode="json"),
            "continuation": (
                snapshot.continuation.model_dump(mode="json")
                if snapshot.continuation is not None
                else None
            ),
            "primary_range": snapshot.primary_event_range.model_dump(mode="json"),
            "named_ranges": [
                item.model_dump(mode="json") for item in snapshot.named_event_ranges
            ],
            "authority_plan_fingerprint": snapshot.authority_slice_plan.plan_fingerprint,
            "transcript_window": window.model_dump(mode="json"),
            "resolved_model_call_id": snapshot.resolved_model_call.resolved_model_call_id,
            "target_fingerprint": snapshot.resolved_model_call.target.target_fingerprint,
        },
        "invocation_provider_projection": {
            "context_id": transcript_provider_projection.context_id,
            "model_call_index": transcript_provider_projection.model_call_index,
            "compile_attempt_index": (
                transcript_provider_projection.compile_attempt_index
            ),
            "stable_normalized_transcript_fingerprint": (
                transcript_provider_projection.semantic_identity.stable_normalized_transcript_fingerprint
            ),
            "provider_projection_semantic_fingerprint": (
                transcript_provider_projection.semantic_identity.semantic_fingerprint
            ),
            "rendering_contract": (
                transcript_provider_projection.rendering_contract.model_dump(
                    mode="json"
                )
            ),
            "section_count": len(transcript_provider_projection.sections),
            "rendered_timing_header_count": sum(
                item.semantic_identity.timing_semantic.rendered_timing_header
                is not None
                for item in transcript_provider_projection.sections
            ),
            "section_timing_semantics": [
                item.semantic_identity.timing_semantic.model_dump(mode="json")
                for item in transcript_provider_projection.sections
            ],
            "lowered_provider_messages_fingerprint": (
                transcript_provider_projection.semantic_identity.lowered_provider_messages_fingerprint
            ),
            "stable_root_unchanged": (
                transcript_provider_projection.semantic_identity.stable_normalized_transcript_fingerprint
                == transcript_authority.final_normalized_transcript_fingerprint
            ),
        },
        "transcript_authority": {
            "provider_semantic_identity": (
                transcript_authority.provider_semantic_identity.model_dump(
                    mode="json"
                )
            ),
            "semantic_source": transcript_authority.semantic_source.model_dump(
                mode="json"
            ),
            "projection_base_kind": transcript_authority.projection_base.base_kind,
            "projection_base_semantic_identity": (
                transcript_authority.projection_base.common.semantic_identity.model_dump(
                    mode="json"
                )
            ),
            "final_normalized_transcript_fingerprint": (
                transcript_authority.final_normalized_transcript_fingerprint
            ),
            "domain_completeness_proof": (
                transcript_authority.domain_completeness_proof.model_dump(
                    mode="json"
                )
            ),
            "named_fact_selection": (
                transcript_authority.named_fact_selection.model_dump(mode="json")
            ),
            "fact_fingerprint": transcript_authority.fact_fingerprint,
        },
        "transcript": {
            "fingerprint": replayed.normalized_transcript.transcript.transcript_fingerprint,
            "message_count": len(replayed.normalized_transcript.transcript.messages),
            "pair_count": len(replayed.normalized_transcript.transcript.tool_pairs),
            "stripped_unfinished_call_ids": list(
                replayed.normalized_transcript.transcript.stripped_unfinished_call_ids
            ),
        },
        "tool_results": {
            "unit_count": len(units),
            "profile_counts": profile_counts,
            "builder_contracts": list(builder_contracts.values()),
            "render_policy_fingerprint": (
                replayed.prepared_tool_results.resolved_policy.policy_fingerprint
            ),
            "protected_unit_ids": list(
                replayed.prepared_tool_results.resolved_policy.protected_unit_ids
            ),
        },
        "candidates": {
            "count": len(candidates.entries),
            "source_counts": source_counts,
            "lifecycle_counts": lifecycle_counts,
            "source_selections": source_selections,
            "source_selections_truncated": (
                len(snapshot.candidate_source_selections) > len(source_selections)
            ),
            "collection_decisions": collection_decisions,
            "collection_decisions_truncated": (
                len(candidates.collection_decisions) > len(collection_decisions)
            ),
            "invalidation_count": len(candidates.invalidations),
            "fingerprint": candidates.candidate_set_fingerprint,
        },
        "rollout_status_hint": rollout_status_hint,
        "current_process_diagnostics": {
            "semantics_builder_implementation_build_fingerprints": None,
            "historical_fact": False,
        },
    }


def _replayed_rollout_status_hint(
    *,
    event: ContextCompiledEvent,
    snapshot,
    primary: ContextEventSlice,
    named: tuple[ContextEventSlice, ...],
) -> dict[str, Any] | None:
    included = tuple(
        section
        for section in event.sections
        if isinstance(section, dict)
        and section.get("id") == "rollout:status"
        and section.get("included") is True
    )
    if not included:
        return None
    if len(included) != 1:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_rollout_status_section_ambiguous",
            "compiled context contains multiple included rollout status sections",
        )
    owner_id = (
        snapshot.long_horizon_attribution.rollout_account_owner_runtime_session_id
    )
    owner_slices = tuple(
        item
        for item in (primary, *named)
        if item.runtime_session_id == owner_id
    )
    if len(owner_slices) != 1:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_rollout_status_owner_slice_missing",
            "rollout status replay requires one frozen account-owner slice",
        )
    starts = tuple(
        decoded
        for frozen in primary.events
        if frozen.run_id == snapshot.identity.run_id
        if isinstance(
            (decoded := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
            RunStartEvent,
        )
    )
    if len(starts) != 1:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_rollout_status_run_start_missing",
            "rollout status replay requires one matching RunStart",
        )
    candidate = derive_rollout_status_candidate(
        event_slice=owner_slices[0],
        account_id=snapshot.long_horizon_attribution.rollout_account_id,
        policy=starts[0].long_horizon.rollout_status_hint_policy,
    )
    authorities = tuple(
        item
        for item in snapshot.candidate_authorities
        if item.source_instance_id == "rollout:status"
    )
    if (
        candidate is None
        or len(authorities) != 1
        or authorities[0].lifecycle_dependency_fingerprint
        != candidate.semantic_fingerprint
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_rollout_status_hint_mismatch",
            "included rollout status differs from the frozen ledger derivation",
        )
    return candidate.model_dump(mode="json")


def _subagent_graph_checkpoint_projection(
    events: Iterable[AgentEvent],
    *,
    limit: int = 64,
) -> dict[str, Any]:
    committed = [
        event
        for event in events
        if isinstance(event, SubagentGraphCheckpointCommittedEvent)
    ]
    selected = committed[-limit:]
    return {
        "status": "available" if committed else "missing",
        "confirmed_checkpoint_count": len(committed),
        "checkpoints": [
            {
                "event_id": event.id,
                "event_sequence": event.sequence,
                "checkpoint_id": event.checkpoint.checkpoint_id,
                "through_sequence": event.checkpoint.through_sequence,
                "artifact_id": event.artifact.artifact_id,
                "artifact_content_sha256": event.artifact.content_sha256,
                "graph_reducer_id": event.checkpoint.graph_reducer_id,
                "graph_reducer_version": event.checkpoint.graph_reducer_version,
                "graph_reducer_contract_fingerprint": (
                    event.checkpoint.graph_reducer_contract_fingerprint
                ),
                "graph_event_count": event.checkpoint.graph_event_count,
                "graph_state_semantic_fingerprint": (
                    event.checkpoint.graph_state_semantic_fingerprint
                ),
                "writer_status": "committed",
            }
            for event in selected
        ],
        "truncated": len(selected) < len(committed),
    }


def _rollout_status_shadow_projection(
    events: Iterable[AgentEvent],
    *,
    runtime_session_id: str,
    root_run_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    ordered = tuple(sorted(events, key=lambda event: event.sequence or 0))
    openings = tuple(
        event
        for event in ordered
        if isinstance(event, RolloutBudgetAccountOpenedEvent)
        and (root_run_id is None or event.account.root_run_id == root_run_id)
    )
    if not openings:
        return {"shadows": [], "diagnostics": []}
    diagnostics: list[dict[str, Any]] = []
    try:
        frozen = tuple(
            FrozenStoredEvent.from_stored_event(
                event,
                runtime_session_id=runtime_session_id,
            )
            for event in ordered
        )
        event_slice = ContextEventSlice(
            runtime_session_id=runtime_session_id,
            from_sequence=1,
            through_sequence=len(frozen),
            events=frozen,
            event_ids_fingerprint=context_fingerprint(
                "context-event-slice-ids:v1",
                tuple(event.event_id for event in frozen),
            ),
            event_payloads_fingerprint=context_fingerprint(
                "context-event-slice-payloads:v1",
                tuple(event.payload_fingerprint for event in frozen),
            ),
        )
    except Exception as exc:
        return {
            "shadows": [],
            "diagnostics": [
                {
                    "severity": "error",
                    "code": "rollout_status_source_slice_untrusted",
                    "message": "Rollout status source slice is not canonical.",
                    "details": {"error_type": type(exc).__name__},
                }
            ],
        }

    starts = {
        event.run_id: event
        for event in ordered
        if isinstance(event, RunStartEvent)
    }
    shadows: list[dict[str, Any]] = []
    for opening in openings:
        start = starts.get(opening.account.root_run_id)
        if (
            start is None
            or start.long_horizon.rollout_account_id != opening.account.account_id
        ):
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "rollout_status_run_contract_missing",
                    "message": "Rollout status cannot locate its RunStart contract.",
                    "details": {
                        "account_id": opening.account.account_id,
                        "root_run_id": opening.account.root_run_id,
                    },
                }
            )
            continue
        try:
            shadow = derive_rollout_status_shadow(
                event_slice=event_slice,
                account_id=opening.account.account_id,
                policy=start.long_horizon.rollout_status_hint_policy,
            )
        except Exception as exc:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "rollout_status_projection_failed",
                    "message": "Rollout status shadow could not be derived.",
                    "details": {
                        "account_id": opening.account.account_id,
                        "error_type": type(exc).__name__,
                    },
                }
            )
            continue
        shadows.append(shadow.model_dump(mode="json"))
    return {"shadows": shadows, "diagnostics": diagnostics}


def _long_horizon_run_projection(
    events: Iterable[AgentEvent],
    *,
    runtime_session_id: str,
    context_compilations: Iterable[dict[str, Any]] = (),
    root_run_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    ordered = tuple(sorted(events, key=lambda event: event.sequence or 0))
    openings = tuple(
        event
        for event in ordered
        if isinstance(event, RolloutBudgetAccountOpenedEvent)
        and (root_run_id is None or event.account.root_run_id == root_run_id)
    )
    if not openings:
        return {"runs": [], "diagnostics": []}
    diagnostics: list[dict[str, Any]] = []
    try:
        event_slice = _inspector_context_event_slice(
            ordered,
            runtime_session_id=runtime_session_id,
        )
        state_store = LongHorizonStateStore(ordered)
    except Exception as exc:
        return {
            "runs": [],
            "diagnostics": [
                {
                    "severity": "error",
                    "code": "long_horizon_projection_ledger_untrusted",
                    "message": "Long-horizon state could not be folded from the ledger.",
                    "details": {"error_type": type(exc).__name__},
                }
            ],
        }

    starts = {
        event.run_id: event
        for event in ordered
        if isinstance(event, RunStartEvent)
    }
    latest_contexts: dict[str, dict[str, Any]] = {}
    for context in context_compilations:
        run_id = context.get("run_id")
        if isinstance(run_id, str):
            latest_contexts[run_id] = context

    projected: list[dict[str, Any]] = []
    for opening in openings:
        account = opening.account
        state = state_store.rollout_state(account.account_id)
        chain = state_store.window_state(account.root_run_id)
        start = starts.get(account.root_run_id)
        if state is None or chain is None or start is None:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "long_horizon_projection_contract_missing",
                    "message": "Long-horizon account lacks its run contract or reducer state.",
                    "details": {
                        "account_id": account.account_id,
                        "root_run_id": account.root_run_id,
                    },
                }
            )
            continue
        try:
            shadow = derive_rollout_status_shadow(
                event_slice=event_slice,
                account_id=account.account_id,
                policy=start.long_horizon.rollout_status_hint_policy,
            )
        except Exception as exc:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "long_horizon_rollout_status_projection_failed",
                    "message": "Current rollout status could not be derived.",
                    "details": {
                        "account_id": account.account_id,
                        "error_type": type(exc).__name__,
                    },
                }
            )
            shadow_payload = None
        else:
            shadow_payload = shadow.model_dump(mode="json")

        context = latest_contexts.get(account.root_run_id)
        input_replay = (
            context.get("input_replay") if isinstance(context, dict) else None
        )
        actual_hint = (
            input_replay.get("rollout_status_hint")
            if isinstance(input_replay, dict)
            else None
        )
        replay_status = (
            input_replay.get("status")
            if isinstance(input_replay, dict)
            else None
        )
        graph = (
            input_replay.get("subagent_graph")
            if isinstance(input_replay, dict)
            else None
        )
        semantic = (
            graph.get("semantic_source") if isinstance(graph, dict) else None
        )

        active_or_final_window_id = chain.active_window_id or (
            chain.ordered_window_ids[-1] if chain.ordered_window_ids else None
        )
        projection_state = (
            state_store.projection_state(active_or_final_window_id)
            if active_or_final_window_id is not None
            else None
        )
        owner_counts = {"model_call": 0, "tool_call": 0, "subagent_run": 0}
        for reservation in state.active_reservations:
            owner_counts[reservation.owner_kind] += 1

        transition_events = tuple(
            event
            for event in ordered
            if isinstance(event, RolloutPhaseTransitionedEvent)
            and event.account_id == account.account_id
        )
        reservation_events = tuple(
            event
            for event in ordered
            if isinstance(event, RolloutBudgetReservationCreatedEvent)
            and event.reservation.account_id == account.account_id
        )
        reservation_ids = {
            event.reservation.reservation_id for event in reservation_events
        }
        settlement_events = tuple(
            event
            for event in ordered
            if isinstance(event, RolloutBudgetReservationSettledEvent)
            and event.reservation_id in reservation_ids
        )
        finalization_denials = tuple(
            event
            for event in ordered
            if isinstance(event, CapabilityGateDecisionEvent)
            and event.run_id == account.root_run_id
            and event.reason_code in {
                "rollout_phase_tool_denied",
                "rollout_emergency_hard_stop",
            }
        )

        final_agent_remaining = max(
            0,
            account.finalization_agent_reserve_milliunits
            - state.finalization_agent_charged_milliunits
            - state.finalization_agent_reserved_milliunits,
        )
        final_compaction_remaining = max(
            0,
            account.finalization_compaction_reserve_milliunits
            - state.finalization_compaction_charged_milliunits
            - state.finalization_compaction_reserved_milliunits,
        )
        final_tool_remaining = max(
            0,
            account.finalization_tool_reserve_milliunits
            - state.finalization_tool_charged_milliunits
            - state.finalization_tool_reserved_milliunits,
        )
        projected.append(
            {
                "run_id": account.root_run_id,
                "account_id": account.account_id,
                "active_or_final_window_id": active_or_final_window_id,
                "window_count": len(chain.ordered_window_ids),
                "projection_generation_count": (
                    projection_state.projection_generation
                    if projection_state is not None
                    else 0
                ),
                "rollout_phase": state.phase.value,
                "rollout_charged_milliunits": state.charged_milliunits,
                "rollout_reserved_milliunits": state.reserved_milliunits,
                "rollout_total_milliunits": account.total_budget_milliunits,
                "finalization_reserve_remaining_milliunits": (
                    final_agent_remaining
                    + final_compaction_remaining
                    + final_tool_remaining
                ),
                "finalization_agent_remaining_milliunits": final_agent_remaining,
                "finalization_compaction_remaining_milliunits": (
                    final_compaction_remaining
                ),
                "finalization_tool_remaining_milliunits": final_tool_remaining,
                "model_call_count": state.model_call_count,
                "tool_call_count": state.tool_call_count,
                "rollout_status_shadow": shadow_payload,
                "latest_rollout_status_hint": actual_hint,
                "subagent_graph_event_count": (
                    semantic.get("graph_event_count")
                    if isinstance(semantic, dict)
                    else None
                ),
                "subagent_graph_semantic_accumulator": (
                    semantic.get("graph_semantic_accumulator")
                    if isinstance(semantic, dict)
                    else None
                ),
                "subagent_graph_state_semantic_fingerprint": (
                    semantic.get("graph_state_semantic_fingerprint")
                    if isinstance(semantic, dict)
                    else None
                ),
                "graph_reducer_id": (
                    semantic.get("graph_reducer_id")
                    if isinstance(semantic, dict)
                    else None
                ),
                "graph_reducer_version": (
                    semantic.get("graph_reducer_version")
                    if isinstance(semantic, dict)
                    else None
                ),
                "graph_reducer_contract_fingerprint": (
                    semantic.get("graph_reducer_contract_fingerprint")
                    if isinstance(semantic, dict)
                    else None
                ),
                "preferred_checkpoint_id": (
                    graph.get("preferred_checkpoint_id")
                    if isinstance(graph, dict)
                    else None
                ),
                "checkpoint_id": (
                    graph.get("actual_checkpoint_id")
                    if isinstance(graph, dict)
                    else None
                ),
                "checkpoint_through_sequence": (
                    graph.get("checkpoint_through_sequence", 0)
                    if isinstance(graph, dict)
                    else 0
                ),
                "checkpoint_delta_count": (
                    graph.get("delta_count", 0) if isinstance(graph, dict) else 0
                ),
                "ledger_through_sequence": (
                    graph.get("ledger_through_sequence", state.through_sequence)
                    if isinstance(graph, dict)
                    else state.through_sequence
                ),
                "ledger_continuity_accumulator": (
                    graph.get("ledger_continuity_accumulator")
                    if isinstance(graph, dict)
                    else None
                ),
                "checkpoint_rebased": (
                    bool(graph.get("rebased")) if isinstance(graph, dict) else False
                ),
                "pending_owner_counts": {
                    **owner_counts,
                    "total": len(state.active_reservations),
                },
                "replay_status": replay_status,
                "rollout_timeline": {
                    "phase_transitions": [
                        {
                            "sequence": item.sequence,
                            "from_phase": item.from_phase.value,
                            "to_phase": item.to_phase.value,
                            "reason_code": item.reason_code.value,
                            "source_through_sequence": item.source_through_sequence,
                        }
                        for item in transition_events
                    ],
                    "reservations": [
                        {
                            "sequence": item.sequence,
                            "reservation_id": item.reservation.reservation_id,
                            "owner_kind": item.reservation.owner_kind,
                            "owner_id": item.reservation.owner_id,
                            "budget_bucket": item.reservation.budget_bucket.value,
                            "reserved_milliunits": item.reservation.reserved_milliunits,
                        }
                        for item in reservation_events
                    ],
                    "settlements": [
                        {
                            "sequence": item.sequence,
                            "reservation_id": item.reservation_id,
                            "usage_status": item.usage_status,
                            "charged_milliunits": item.charged_milliunits,
                        }
                        for item in settlement_events
                    ],
                    "finalization_denials": [
                        {
                            "sequence": item.sequence,
                            "tool_call_id": item.tool_call_id,
                            "tool_name": item.tool_name,
                            "reason_code": item.reason_code,
                            "action_classification": (
                                item.action_classification.model_dump(mode="json")
                                if item.action_classification is not None
                                else None
                            ),
                        }
                        for item in finalization_denials
                    ],
                },
            }
        )
    return {"runs": projected, "diagnostics": diagnostics}


def _inspector_context_event_slice(
    events: tuple[AgentEvent, ...],
    *,
    runtime_session_id: str,
) -> ContextEventSlice:
    frozen = tuple(
        FrozenStoredEvent.from_stored_event(
            event,
            runtime_session_id=runtime_session_id,
        )
        for event in events
    )
    sequences = tuple(event.sequence for event in frozen)
    if sequences != tuple(range(1, len(frozen) + 1)):
        raise ContextEventSliceError("Inspector event slice is not contiguous")
    return ContextEventSlice(
        runtime_session_id=runtime_session_id,
        from_sequence=1,
        through_sequence=len(frozen),
        events=frozen,
        event_ids_fingerprint=context_fingerprint(
            "context-event-slice-ids:v1",
            tuple(event.event_id for event in frozen),
        ),
        event_payloads_fingerprint=context_fingerprint(
            "context-event-slice-payloads:v1",
            tuple(event.payload_fingerprint for event in frozen),
        ),
    )


def _context_event_slice_for_range(
    *,
    store: PostgresInspectorStore,
    runtime_session_id: str,
    first_sequence: int,
    through_sequence: int,
) -> ContextEventSlice:
    snapshot = PostgresEventLog(
        dsn=store.dsn,
        runtime_session_id=runtime_session_id,
    ).read_raw_range_snapshot(
        minimum_sequence=first_sequence,
        through_sequence=through_sequence,
    )
    return ContextEventSlice.from_read_snapshot(
        runtime_session_id=runtime_session_id,
        minimum_sequence=first_sequence,
        snapshot=snapshot,
    )


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
        source_started_at = (
            source.get("source_started_at_utc") or source.get("source_started_at")
            if isinstance(source, dict)
            else None
        )
        source_ended_at = (
            source.get("source_ended_at_utc") or source.get("source_ended_at")
            if isinstance(source, dict)
            else None
        )
        observed_at = (
            source.get("observed_at_utc")
            or source.get("observed_at")
            or source_ended_at
            or source_started_at
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
                "source_started_at": source_started_at,
                "source_ended_at": source_ended_at,
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
                "host_boundary_id": event.host_boundary_id,
                "host_boundary_kind": event.host_boundary_kind,
                "started_event_id": event.started_event_id,
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


def _context_window_projection(
    events: Iterable[AgentEvent], store: PostgresInspectorStore
) -> dict[str, list[dict[str, Any]]]:
    """Project the same-run window chain and its L4 compaction attempts."""

    ordered = sorted(
        events,
        key=lambda event: (
            event.sequence if event.sequence is not None else 2**63,
            event.id,
        ),
    )
    opened_by_id = {
        event.window.window_id: event
        for event in ordered
        if isinstance(event, ContextWindowOpenedEvent)
    }
    closed_by_id = {
        event.window_id: event
        for event in ordered
        if isinstance(event, ContextWindowClosedEvent)
    }
    starts = {
        event.plan.compaction_id: event
        for event in ordered
        if isinstance(event, ContextWindowCompactionStartedEvent)
    }
    terminals: dict[
        str, ContextWindowCompactionCompletedEvent | ContextWindowCompactionFailedEvent
    ] = {}
    diagnostics: list[dict[str, Any]] = []
    for event in ordered:
        if not isinstance(
            event,
            (ContextWindowCompactionCompletedEvent, ContextWindowCompactionFailedEvent),
        ):
            continue
        prior = terminals.get(event.compaction_id)
        if prior is not None and prior.id != event.id:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "context_window_compaction_terminal_conflict",
                    "message": "Context-window compaction has multiple terminal facts.",
                    "details": {"compaction_id": event.compaction_id},
                }
            )
        terminals[event.compaction_id] = event

    windows: list[dict[str, Any]] = []
    for window_id, opened in sorted(
        opened_by_id.items(), key=lambda item: item[1].window.generation
    ):
        closed = closed_by_id.get(window_id)
        windows.append(
            {
                "window_id": window_id,
                "generation": opened.window.generation,
                "previous_window_id": opened.window.previous_window_id,
                "open_reason": opened.window.open_reason.value,
                "opened_event_id": opened.id,
                "opened_sequence": opened.sequence,
                "opening_batch_id": opened.opening_batch_id,
                "window_semantic_fingerprint": (
                    opened.window.window_semantic_fingerprint
                ),
                "window_fact_fingerprint": opened.window.window_fact_fingerprint,
                "source_compaction_id": opened.window.source_compaction_id,
                "source_summary_artifact_id": (
                    opened.window.source_summary_artifact_id
                ),
                "status": "closed" if closed is not None else "active",
                "closed_event_id": closed.id if closed is not None else None,
                "closed_sequence": closed.sequence if closed is not None else None,
                "close_reason": (
                    closed.close_reason.value if closed is not None else None
                ),
                "next_window_id": (
                    closed.next_window_id if closed is not None else None
                ),
            }
        )

    compactions: list[dict[str, Any]] = []
    all_ids = sorted(
        set(starts) | set(terminals),
        key=lambda compaction_id: (
            (
                starts.get(compaction_id) or terminals[compaction_id]
            ).sequence
            or 2**63,
            compaction_id,
        ),
    )
    for compaction_id in all_ids:
        started = starts.get(compaction_id)
        terminal = terminals.get(compaction_id)
        completed = (
            terminal
            if isinstance(terminal, ContextWindowCompactionCompletedEvent)
            else None
        )
        failed = (
            terminal
            if isinstance(terminal, ContextWindowCompactionFailedEvent)
            else None
        )
        plan = started.plan if started is not None else None
        summary_present = (
            store.artifact(completed.summary_artifact_id) is not None
            if completed is not None
            else None
        )
        compactions.append(
            {
                "compaction_id": compaction_id,
                "status": (
                    "completed"
                    if completed is not None
                    else "failed"
                    if failed is not None
                    else "started"
                ),
                "attempt_index": (
                    plan.compaction_attempt_index
                    if plan is not None
                    else failed.compaction_attempt_index
                    if failed is not None
                    else None
                ),
                "started_event_id": started.id if started is not None else None,
                "started_sequence": (
                    started.sequence if started is not None else None
                ),
                "terminal_event_id": terminal.id if terminal is not None else None,
                "terminal_sequence": (
                    terminal.sequence if terminal is not None else None
                ),
                "source_window_id": (
                    plan.source_window_id
                    if plan is not None
                    else failed.source_window_id
                    if failed is not None
                    else None
                ),
                "source_window_generation": (
                    plan.source_window_generation
                    if plan is not None
                    else failed.source_window_generation
                    if failed is not None
                    else None
                ),
                "source_projection_generation": (
                    plan.source_projection_generation if plan is not None else None
                ),
                "source_through_sequence": (
                    plan.source_through_sequence if plan is not None else None
                ),
                "target_window_id": (
                    plan.target_window_id if plan is not None else None
                ),
                "target_window_generation": (
                    plan.target_window_generation if plan is not None else None
                ),
                "plan_fingerprint": (
                    plan.plan_fingerprint
                    if plan is not None
                    else failed.plan_fingerprint
                    if failed is not None
                    else None
                ),
                "summarizer_call": (
                    plan.summarizer_call.model_dump(mode="json")
                    if plan is not None
                    else failed.summarizer_call.model_dump(mode="json")
                    if failed is not None and failed.summarizer_call is not None
                    else None
                ),
                "rollout_settlement_event_id": (
                    terminal.rollout_settlement_event_id
                    if terminal is not None
                    else None
                ),
                "summary_artifact_id": (
                    completed.summary_artifact_id
                    if completed is not None
                    else None
                ),
                "summary_artifact_present": summary_present,
                "actual_post_compaction_estimated_tokens": (
                    completed.actual_post_compaction_estimated_tokens
                    if completed is not None
                    else failed.observed_post_compaction_tokens
                    if failed is not None
                    else None
                ),
                "post_compaction_target_tokens": (
                    completed.post_compaction_target_tokens
                    if completed is not None
                    else plan.post_compaction_target_tokens
                    if plan is not None
                    else None
                ),
                "failure_stage": failed.failure_stage if failed is not None else None,
                "reason_code": failed.reason_code if failed is not None else None,
            }
        )
        if started is not None and terminal is None:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "context_window_compaction_dangling_started",
                    "message": "Context-window compaction Started has no terminal fact.",
                    "details": {"compaction_id": compaction_id},
                }
            )
        if completed is not None and not summary_present:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "context_window_compaction_summary_artifact_missing",
                    "message": "Completed context-window compaction summary artifact is missing.",
                    "details": {
                        "compaction_id": compaction_id,
                        "summary_artifact_id": completed.summary_artifact_id,
                    },
                }
            )

    return {
        "windows": windows,
        "compactions": compactions,
        "diagnostics": diagnostics,
    }


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


def _run_user_input(event: RunStartEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    current = event.current_user_message
    return {
        "message_id": current.message_id,
        "source_kind": current.source_kind,
        "chars": len(current.text),
        "content_sha256": current.content_sha256,
        "observed_at_utc": current.observed_at_utc,
        "source_artifact_id": current.source_artifact_id,
        "text_redacted": True,
    }


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
