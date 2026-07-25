"""Read-only durable store access for Pulsara Inspector."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any

from psycopg.rows import dict_row

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationAccountStateFact,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


def _bounded_limit(limit: int) -> int:
    if not 1 <= limit <= 256:
        raise ValueError("inspector page limit must be between 1 and 256")
    return limit


@dataclass(slots=True)
class PostgresInspectorStore:
    """Small read-only query facade over Pulsara's durable Postgres tables."""

    connection_provider: VerifiedPostgresConnectionProviderProtocol

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
        return PostgresEventLog(
            connection_provider=self.connection_provider,
            runtime_session_id=session_id,
        ).iter()

    def events_for_run(self, run_id: str) -> list[AgentEvent]:
        owner = self._fetchone(
            """
            select session_id
            from runs
            where id = %s
            """,
            (run_id,),
        )
        if owner is None:
            return []
        return PostgresEventLog(
            connection_provider=self.connection_provider,
            runtime_session_id=str(owner["session_id"]),
        ).iter(run_id=run_id)

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

    def materialization_account(
        self, session_id: str
    ) -> LedgerMaterializationAccountStateFact | None:
        row = self._fetchone(
            """
            select state_payload
            from ledger_materialization_accounts
            where session_id = %s
            """,
            (session_id,),
        )
        if row is None:
            return None
        return LedgerMaterializationAccountStateFact.model_validate(
            row["state_payload"]
        )

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

    def memory_candidates_for_compaction(
        self, compaction_id: str
    ) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select *
            from memory_candidates
            where metadata->>'compaction_id' = %s
            order by created_at asc, entry_id asc
            """,
            (compaction_id,),
        )

    def governance_decisions_for_candidate(self, entry_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select *
            from memory_governance_decisions
            where decision->>'target_entry_id' = %s
               or exists (
                   select 1
                   from jsonb_array_elements_text(coalesce(decision->'target_entry_ids', '[]'::jsonb)) as target(id)
                   where target.id = %s
               )
            order by created_at asc, decision_id asc
            """,
            (entry_id, entry_id),
        )

    def governance_batches_for_session(
        self,
        session_id: str,
        *,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select runtime_session_id, governance_batch_id,
                   batch_input_reference, preparing_claims_fingerprint,
                   source_ledger_through_sequence, resolved_model_call_id,
                   status, prepared_event_id, terminal_event_id,
                   record_fingerprint, created_at, updated_at
            from memory_governance_batch_inputs
            where runtime_session_id = %s
            order by created_at desc, governance_batch_id
            limit %s
            """,
            (session_id, limit),
        )

    def governance_claims_for_session(
        self,
        session_id: str,
        *,
        limit: int = 512,
    ) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select runtime_session_id, candidate_entry_id,
                   candidate_row_fingerprint, governance_batch_id,
                   claim_generation, status, prepared_event_id,
                   terminal_record_id, previous_claim_fingerprint,
                   claim_fingerprint, created_at, updated_at
            from memory_governance_candidate_claims
            where runtime_session_id = %s
            order by created_at desc, candidate_entry_id
            limit %s
            """,
            (session_id, limit),
        )

    def governance_evidence_rejections_for_session(
        self,
        session_id: str,
        *,
        limit: int = 256,
    ) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select runtime_session_id, candidate_entry_id, claim_generation,
                   governance_batch_id, rejection_event_id,
                   rejection_payload, created_at
            from memory_candidate_evidence_rejections
            where runtime_session_id = %s
            order by created_at desc, candidate_entry_id
            limit %s
            """,
            (session_id, limit),
        )

    def candidate_projection_outbox_for_session(
        self,
        session_id: str,
        *,
        limit: int = 256,
    ) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select runtime_session_id, producer_kind, producer_event_id,
                   candidate_entry_id, candidate_index,
                   outbox_item_fingerprint, producer_payload_fingerprint,
                   producer_event_identity, candidate_payload_fingerprint,
                   candidate_attribution_fingerprint, candidate_payload,
                   status, last_stable_failure_code,
                   created_at, updated_at
            from memory_candidate_projection_outbox
            where runtime_session_id = %s
            order by created_at desc, producer_event_id, candidate_index
            limit %s
            """,
            (session_id, limit),
        )

    def durable_projection_jobs(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        after_job_id: str | None = None,
        limit: int = 128,
        terminal_only: bool = False,
        statuses: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        resolved_limit = _bounded_limit(limit)
        where: list[str] = []
        params: list[object] = []
        if session_id is not None:
            where.append("runtime_session_id = %s")
            params.append(session_id)
        if run_id is not None:
            where.append("run_id = %s")
            params.append(run_id)
        if after_job_id is not None:
            where.append("job_id > %s")
            params.append(after_job_id)
        if terminal_only:
            where.append("status IN ('succeeded', 'superseded', 'dead_letter')")
        if statuses is not None:
            if not statuses:
                return []
            where.append("status = any(%s)")
            params.append(list(statuses))
        predicate = " AND ".join(where) if where else "TRUE"
        params.append(resolved_limit + 1)
        return self._fetchall(
            f"""
            select job_id, projection_kind, target_key, runtime_session_id,
                   run_id, source_event_id, source_sequence, source_event_type,
                   source_reference, trigger_horizon, handler_contract,
                   handler_contract_fingerprint, activation_fingerprint,
                   seed_contract_fingerprint, delivery_policy,
                   delivery_policy_fingerprint,
                   canonical_mutation_surface_plan,
                   canonical_mutation_surface_plan_fingerprint,
                   job_semantic_fingerprint, job_candidate_fingerprint,
                   status, state_revision, repair_generation, attempt_count,
                   lease_generation, lease_owner_id, lease_expires_at,
                   next_attempt_at, last_failure, result_receipt_reference,
                   state_fingerprint, created_at, updated_at
            from durable_projection_jobs
            where {predicate}
            order by job_id
            limit %s
            """,
            tuple(params),
        )

    def durable_projection_receipts_for_jobs(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        limit: int = 256,
    ) -> list[dict[str, Any]]:
        resolved_limit = _bounded_limit(limit)
        where = ["job.result_receipt_reference IS NOT NULL"]
        params: list[object] = []
        if session_id is not None:
            where.append("job.runtime_session_id = %s")
            params.append(session_id)
        if run_id is not None:
            where.append("job.run_id = %s")
            params.append(run_id)
        params.append(resolved_limit + 1)
        return self._fetchall(
            f"""
            select distinct receipt.receipt_id, receipt.receipt_kind,
                   receipt.projection_kind, receipt.target_key,
                   receipt.candidate_source_sequence,
                   receipt.effective_source_sequence,
                   receipt.result_semantic_fingerprint,
                   receipt.receipt_payload, receipt.receipt_fingerprint,
                   receipt.created_at
            from durable_projection_jobs as job
            join durable_projection_result_receipts as receipt
              on receipt.receipt_id =
                 job.result_receipt_reference->>'receipt_id'
            where {" AND ".join(where)}
            order by receipt.receipt_id
            limit %s
            """,
            tuple(params),
        )

    def durable_projection_target_heads(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        limit: int = 256,
    ) -> list[dict[str, Any]]:
        resolved_limit = _bounded_limit(limit)
        where: list[str] = []
        params: list[object] = []
        if session_id is not None:
            where.append("job.runtime_session_id = %s")
            params.append(session_id)
        if run_id is not None:
            where.append("job.run_id = %s")
            params.append(run_id)
        predicate = " AND ".join(where) if where else "TRUE"
        params.append(resolved_limit + 1)
        return self._fetchall(
            f"""
            select distinct head.projection_kind, head.target_key,
                   head.source_sequence, head.head_payload,
                   head.head_fingerprint, head.updated_at
            from durable_projection_target_heads as head
            join durable_projection_jobs as job
              on job.projection_kind = head.projection_kind
             and job.target_key = head.target_key
            where {predicate}
            order by head.projection_kind, head.target_key
            limit %s
            """,
            tuple(params),
        )

    def durable_projection_conflicts(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        resolved_limit = _bounded_limit(limit)
        where: list[str] = []
        params: list[object] = []
        if session_id is not None:
            where.append("job.runtime_session_id = %s")
            params.append(session_id)
        if run_id is not None:
            where.append("job.run_id = %s")
            params.append(run_id)
        predicate = " AND ".join(where) if where else "TRUE"
        params.append(resolved_limit + 1)
        return self._fetchall(
            f"""
            select distinct conflict.conflict_id, conflict.projection_kind,
                   conflict.target_key, conflict.candidate_source_sequence,
                   conflict.existing_target_head_fingerprint,
                   conflict.conflict_payload, conflict.conflict_fingerprint,
                   conflict.created_at
            from durable_projection_target_authority_conflicts as conflict
            join durable_projection_jobs as job
              on job.projection_kind = conflict.projection_kind
             and job.target_key = conflict.target_key
            where {predicate}
            order by conflict.conflict_id
            limit %s
            """,
            tuple(params),
        )

    def durable_projection_cutovers(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select 'active' as cutover_state, projection_kind,
                   cutover_payload, cutover_fingerprint,
                   cutover_through_sequence
            from durable_projection_session_cutovers
            where runtime_session_id = %s
            union all
            select 'pre_activation' as cutover_state, projection_kind,
                   cutover_payload, cutover_fingerprint,
                   0 as cutover_through_sequence
            from durable_projection_pre_activation_session_cutovers
            where runtime_session_id = %s
            order by projection_kind, cutover_state
            """,
            (session_id, session_id),
        )

    def durable_projection_coverage_receipts(
        self,
        session_id: str,
        *,
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        resolved_limit = min(_bounded_limit(limit), 64)
        return self._fetchall(
            """
            select coverage_receipt_id, projection_kind,
                   frozen_through_sequence, receipt_payload,
                   receipt_fingerprint, created_at
            from durable_projection_pre_activation_coverage_receipts
            where runtime_session_id = %s
            order by projection_kind, coverage_receipt_id
            limit %s
            """,
            (session_id, resolved_limit + 1),
        )

    def durable_projection_repair_actions(
        self,
        *,
        job_ids: tuple[str, ...],
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        if not job_ids:
            return []
        resolved_limit = _bounded_limit(limit)
        return self._fetchall(
            """
            select repair_action_id, owner_kind, owner_id,
                   repair_generation, action_payload, action_fingerprint,
                   created_at
            from durable_projection_repair_actions
            where owner_kind = 'projection_job'
              and owner_id = any(%s)
            order by owner_id, repair_generation, repair_action_id
            limit %s
            """,
            (list(job_ids[:resolved_limit]), resolved_limit + 1),
        )

    def durable_surface_deliveries(
        self,
        *,
        mutation_ids: tuple[str, ...] | None = None,
        after_key: tuple[str, str] | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        resolved_limit = _bounded_limit(limit)
        where_parts: list[str] = []
        params: list[object] = []
        if mutation_ids is not None:
            if not mutation_ids:
                return []
            where_parts.append("delivery.mutation_id = any(%s)")
            params.append(list(mutation_ids[:256]))
        if after_key is not None:
            where_parts.append("(delivery.mutation_id, delivery.surface) > (%s, %s)")
            params.extend(after_key)
        where = " AND ".join(where_parts) if where_parts else "TRUE"
        params.append(resolved_limit + 1)
        return self._fetchall(
            f"""
            select delivery.mutation_id, delivery.surface,
                   delivery.sequence_key, delivery.surface_sequence_number,
                   delivery.delivery_identity,
                   delivery.delivery_identity_fingerprint,
                   delivery.delivery_policy, delivery.status,
                   delivery.state_revision, delivery.repair_generation,
                   delivery.attempt_count, delivery.lease_generation,
                   delivery.lease_owner_id, delivery.lease_expires_at,
                   delivery.next_attempt_at, delivery.terminal_receipt,
                   delivery.last_failure, delivery.state_fingerprint,
                   delivery.created_at, delivery.updated_at,
                   mutation.mutation_kind, mutation.graph_id,
                   mutation.mutation_sequence_number,
                   mutation.mutation_semantic_fingerprint,
                   mutation.mutation_fact_fingerprint
            from canonical_mutation_surface_deliveries as delivery
            join canonical_mutations_v2 as mutation
              on mutation.mutation_id = delivery.mutation_id
            where {where}
            order by delivery.mutation_id, delivery.surface
            limit %s
            """,
            tuple(params),
        )

    def durable_projection_status_counts(self) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            select 'projection_job' as owner_kind, projection_kind as lane,
                   status, count(*) as count, max(updated_at) as latest_updated_at
            from durable_projection_jobs
            group by projection_kind, status
            union all
            select 'canonical_surface' as owner_kind, surface as lane,
                   status, count(*) as count, max(updated_at) as latest_updated_at
            from canonical_mutation_surface_deliveries
            group by surface, status
            order by owner_kind, lane, status
            """
        )

    def runtime_write_admission_epoch(self) -> dict[str, Any] | None:
        return self._fetchone(
            """
            select epoch_number, mode, authorized_runtime_role,
                   active_migration_registry_prefix_fingerprint,
                   protected_relation_registry_fingerprint,
                   maintenance_operation_id, target_migration_version,
                   state_revision, epoch_payload, epoch_fingerprint, updated_at
            from runtime_write_admission_epochs
            where singleton
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

    def memory_search_index(
        self, graph_id: str, memory_id: str
    ) -> dict[str, Any] | None:
        return self._fetchone(
            """
            select graph_id, memory_id, memory_type, scope, status, aliases, updated_at
            from memory_search_index
            where graph_id = %s and memory_id = %s
            """,
            (graph_id, memory_id),
        )

    def memory_vector_index(
        self, graph_id: str, memory_id: str
    ) -> list[dict[str, Any]]:
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

    def recall_usages_for_memory(
        self, graph_id: str, memory_id: str
    ) -> list[dict[str, Any]]:
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

    def _fetchone(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchone()

    def _fetchall(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return list(cursor.fetchall())
