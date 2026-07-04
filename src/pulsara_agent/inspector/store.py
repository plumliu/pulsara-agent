"""Read-only durable store access for Pulsara Inspector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import load_agent_event


@dataclass(slots=True)
class PostgresInspectorStore:
    """Small read-only query facade over Pulsara's durable Postgres tables."""

    dsn: str

    def session(self, session_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            """
            select id, workspace_root, created_at, metadata
            from sessions
            where id = %s
            """,
            (session_id,),
        )

    def run(self, run_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            """
            select id, session_id, status, stop_reason, started_at, completed_at, metadata
            from runs
            where id = %s
            """,
            (run_id,),
        )

    def session_runs(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select id, session_id, status, stop_reason, started_at, completed_at, metadata
            from runs
            where session_id = %s
            order by started_at desc, id desc
            limit %s
            """,
            (session_id, limit),
        )

    def recent_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select id, workspace_root, created_at, metadata
            from sessions
            order by created_at desc
            limit %s
            """,
            (limit,),
        )

    def events_for_session(self, session_id: str) -> list[AgentEvent]:
        rows = self._fetchall(
            """
            select payload
            from agent_events
            where session_id = %s
            order by sequence asc
            """,
            (session_id,),
        )
        return [load_agent_event(row["payload"]) for row in rows]

    def events_for_run(self, run_id: str) -> list[AgentEvent]:
        rows = self._fetchall(
            """
            select payload
            from agent_events
            where run_id = %s
            order by sequence asc
            """,
            (run_id,),
        )
        return [load_agent_event(row["payload"]) for row in rows]

    def event_counts_for_session(self, session_id: str) -> dict[str, int]:
        rows = self._fetchall(
            """
            select event_type, count(*) as count
            from agent_events
            where session_id = %s
            group by event_type
            order by event_type
            """,
            (session_id,),
        )
        return {row["event_type"]: row["count"] for row in rows}

    def tool_result_artifacts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select *
            from tool_result_artifacts
            where run_id = %s
            order by tool_call_id, role, ordinal
            """,
            (run_id,),
        )

    def artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            """
            select
                id, session_id, run_id, media_type, text_body, binary_body,
                digest, size_bytes, stored_at, created_at, metadata
            from artifacts
            where id = %s
            """,
            (artifact_id,),
        )

    def artifact_tool_refs(self, artifact_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select *
            from tool_result_artifacts
            where artifact_id = %s
            order by created_at, run_id, tool_call_id, ordinal
            """,
            (artifact_id,),
        )

    def recall_traces_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select *
            from recall_traces
            where run_id = %s
            order by created_at asc
            """,
            (run_id,),
        )

    def working_context_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select *
            from working_context_summaries
            where source_session_id = %s
            order by updated_at desc
            """,
            (session_id,),
        )

    def outbox_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select distinct outbox.*
            from memory_write_outbox as outbox
            left join memory_candidates as direct_candidate
                on direct_candidate.entry_id = outbox.target_entry_key
            left join memory_governance_decisions as decision
                on decision.governance_batch_id = outbox.governance_batch_id
               and decision.decision_id = outbox.decision_id
            left join lateral (
                select decision.decision->>'target_entry_id' as entry_id
                where decision.decision ? 'target_entry_id'
                union all
                select jsonb_array_elements_text(decision.decision->'target_entry_ids') as entry_id
                where decision.decision ? 'target_entry_ids'
            ) as decision_target on true
            left join memory_candidates as decision_candidate
                on decision_candidate.entry_id = decision_target.entry_id
            where outbox.payload->>'source_run_id' = %s
               or direct_candidate.source_run_id = %s
               or decision_candidate.source_run_id = %s
            order by created_at asc
            """,
            (run_id, run_id, run_id),
        )

    def outbox_status_counts(self) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select status, mutation_lane, count(*) as count, max(created_at) as latest_created_at
            from memory_write_outbox
            group by status, mutation_lane
            order by status, mutation_lane
            """
        )

    def graph_document(self, graph_id: str, memory_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            """
            select graph_id, id, type, payload, updated_at
            from graph_documents
            where graph_id = %s and id = %s
            """,
            (graph_id, memory_id),
        )

    def graph_documents_by_id(self, memory_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select graph_id, id, type, payload, updated_at
            from graph_documents
            where id = %s
            order by updated_at desc nulls last, graph_id
            """,
            (memory_id,),
        )

    def memory_node(self, graph_id: str, memory_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            """
            select *
            from memory_nodes
            where graph_id = %s and id = %s
            """,
            (graph_id, memory_id),
        )

    def memory_nodes_by_id(self, memory_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select *
            from memory_nodes
            where id = %s
            order by updated_at desc nulls last, graph_id
            """,
            (memory_id,),
        )

    def memory_search_index(self, graph_id: str, memory_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            """
            select graph_id, memory_id, memory_type, scope, status, aliases, updated_at
            from memory_search_index
            where graph_id = %s and memory_id = %s
            """,
            (graph_id, memory_id),
        )

    def memory_vector_index(self, graph_id: str, memory_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select graph_id, memory_id, embedding_fingerprint, updated_at, embedded_text_hash
            from memory_vector_index
            where graph_id = %s and memory_id = %s
            order by updated_at desc
            """,
            (graph_id, memory_id),
        )

    def memory_graph_ids_by_id(self, memory_id: str) -> list[str]:
        rows = self._fetchall(
            """
            select graph_id
            from graph_documents
            where id = %s
            union
            select graph_id
            from memory_nodes
            where id = %s
            union
            select graph_id
            from memory_search_index
            where memory_id = %s
            union
            select graph_id
            from memory_vector_index
            where memory_id = %s
            union
            select graph_id
            from recall_usages
            where memory_id = %s
            order by graph_id
            """,
            (memory_id, memory_id, memory_id, memory_id, memory_id),
        )
        return [str(row["graph_id"]) for row in rows]

    def recall_usages_for_memory(self, graph_id: str, memory_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select usage.*, trace.session_id, trace.run_id, trace.query, trace.trigger_kind, trace.created_at
            from recall_usages as usage
            join recall_traces as trace on trace.trace_id = usage.trace_id
            where usage.graph_id = %s and usage.memory_id = %s
            order by trace.created_at desc
            limit 50
            """,
            (graph_id, memory_id),
        )

    def required_table_presence(self, table_names: tuple[str, ...]) -> dict[str, bool]:
        rows = self._fetchall(
            """
            select table_name
            from information_schema.tables
            where table_schema = 'public' and table_name = any(%s)
            """,
            (list(table_names),),
        )
        present = {row["table_name"] for row in rows}
        return {name: name in present for name in table_names}

    def run_projection_stale_count(self) -> int:
        row = self._fetchone(
            """
            with latest_end as (
                select distinct on (run_id)
                    run_id,
                    payload->>'status' as status,
                    payload->>'stop_reason' as stop_reason,
                    created_at as completed_at
                from agent_events
                where event_type = 'RUN_END'
                order by run_id, sequence desc
            )
            select count(*) as count
            from runs
            join latest_end on latest_end.run_id = runs.id
            where runs.status is distinct from latest_end.status
               or runs.stop_reason is distinct from latest_end.stop_reason
               or runs.completed_at is null
            """
        )
        return int(row["count"]) if row is not None else 0

    def tool_result_index_missing_artifact_count(self) -> int:
        row = self._fetchone(
            """
            select count(*) as count
            from tool_result_artifacts as idx
            left join artifacts as artifact on artifact.id = idx.artifact_id
            where artifact.id is null
            """
        )
        return int(row["count"]) if row is not None else 0

    def recent_session_ids(self, *, limit: int = 20) -> list[str]:
        rows = self._fetchall(
            """
            select id
            from sessions
            order by created_at desc
            limit %s
            """,
            (limit,),
        )
        return [row["id"] for row in rows]

    def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchone()

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return list(cursor.fetchall())
