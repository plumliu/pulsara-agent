"""Memory candidate, governance and index operations."""

from __future__ import annotations

from datetime import datetime
import json
import math
from typing import Mapping, Sequence
from psycopg.types.json import Jsonb
from pulsara_agent.conversation_kernel.contracts import CommittedEventDraft, HostWriterGuard, JobAttemptClaimGuard, canonical_digest
from pulsara_agent.conversation_kernel.job_catalog import MEMORY_GOVERNANCE, job_handler_contract
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot

from .contracts import (
    AcceptedMemoryCandidate,
    AcceptedMemoryGovernance,
    ConversationKernelConflict,
    MemoryVectorFactSource,
    MemoryVectorSource,
    StaleJobClaim,
    _utcnow,
)

from .matching import (
    _required_nonnegative_int,
)

class _MemoryOperations:
    def read_memory_candidate_for_governance(
        self,
        guard: JobAttemptClaimGuard,
        *,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT c.*, j.intent_payload, j.handler_type,
                       j.workspace_id AS job_workspace_id
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.memory_candidates AS c
                  ON c.id = j.intent_payload ->> 'candidate_id'
                WHERE j.id = %s
                """,
                (guard.job_id,),
            ).fetchone()
            if row is None or row["handler_type"] != "MEMORY_GOVERNANCE":
                raise ConversationKernelConflict("governance candidate is absent")
            intent = dict(row["intent_payload"])
            if (
                row["status"] != "PENDING"
                or row["workspace_id"] != row["job_workspace_id"]
                or intent.get("candidate_semantic_digest") != row["semantic_digest"]
            ):
                raise ConversationKernelConflict(
                    "governance candidate identity drifted"
                )
            return {
                "id": str(row["id"]),
                "workspace_id": str(row["workspace_id"]),
                "proposal_kind": str(row["proposal_kind"]),
                "proposal_payload": dict(row["proposal_payload"]),
                "semantic_digest": str(row["semantic_digest"]),
            }

    def accept_extracted_memory_bundle(
        self,
        guard: JobAttemptClaimGuard,
        *,
        candidates: Sequence[tuple[str, str, Mapping[str, object], str]],
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> tuple[AcceptedMemoryCandidate, ...]:
        if len(candidates) > 32:
            raise ValueError("memory extraction bundle exceeds 32 candidates")
        governance = job_handler_contract(MEMORY_GOVERNANCE)
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            source = connection.execute(
                """
                SELECT * FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = 'POST_COMPACTION_MEMORY_EXTRACTION'
                FOR UPDATE
                """,
                (guard.job_id,),
            ).fetchone()
            if source is None:
                raise ConversationKernelConflict("memory extraction source is absent")
            workspace_id = str(source["workspace_id"])
            origin_session_id = source["origin_session_id"]
            accepted: list[AcceptedMemoryCandidate] = []
            drafts: list[CommittedEventDraft] = []
            for (
                candidate_id,
                proposal_kind,
                proposal_payload,
                governance_job_id,
            ) in candidates:
                if proposal_kind not in {
                    "FACT",
                    "PREFERENCE",
                    "RELATION",
                    "CORRECTION",
                    "LIFECYCLE",
                }:
                    raise ValueError("memory proposal kind is not closed")
                semantic_digest = canonical_digest(
                    "pulsara:memory-candidate:v1",
                    {
                        "workspace_id": workspace_id,
                        "proposal_kind": proposal_kind,
                        "proposal_payload": dict(proposal_payload),
                        "source_entry_id": None,
                    },
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_candidates (
                        id, workspace_id, origin_session_id, source_entry_id,
                        proposal_kind, semantic_digest, proposal_payload, status
                    ) VALUES (%s, %s, %s, NULL, %s, %s, %s, 'PENDING')
                    """,
                    (
                        candidate_id,
                        workspace_id,
                        origin_session_id,
                        proposal_kind,
                        semantic_digest,
                        Jsonb(dict(proposal_payload)),
                    ),
                )
                intent = {
                    "candidate_id": candidate_id,
                    "candidate_semantic_digest": semantic_digest,
                }
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.durable_jobs (
                        id, workspace_id, origin_session_id, handler_type,
                        intent_schema_version, intent_digest, intent_payload,
                        automatic_intent_key, safety_class, status,
                        retry_policy_id, retry_policy_version, maximum_attempts,
                        attempt_timeout_ms, provider_input_token_limit_per_attempt,
                        provider_output_token_limit_per_attempt, next_eligible_at
                    ) VALUES (
                        %s, %s, %s, 'MEMORY_GOVERNANCE',
                        'memory_governance.v1', %s, %s, %s,
                        'RETRY_SAFE', 'PENDING', 'bounded-exponential', 1,
                        %s, %s, %s, %s, clock_timestamp()
                    )
                    """,
                    (
                        governance_job_id,
                        workspace_id,
                        origin_session_id,
                        canonical_digest(
                            "pulsara:job-intent:memory_governance.v1", intent
                        ),
                        Jsonb(intent),
                        f"memory-governance:{candidate_id}",
                        governance.maximum_attempts,
                        governance.attempt_timeout_ms,
                        governance.input_token_limit,
                        governance.output_token_limit,
                    ),
                )
                accepted.append(
                    AcceptedMemoryCandidate(candidate_id, governance_job_id)
                )
                if origin_session_id is not None:
                    drafts.append(
                        self._event(
                            CommittedEventType.JOB_QUEUED,
                            SubjectSlot.JOB,
                            governance_job_id,
                            occurred_at=occurred_at,
                            actor_kind="job_worker",
                            actor_id=guard.claim_owner_id,
                            payload={"handler_type": "MEMORY_GOVERNANCE"},
                        )
                    )
            if origin_session_id is not None:
                drafts.append(
                    self._event(
                        CommittedEventType.JOB_TERMINAL_ACCEPTED,
                        SubjectSlot.JOB,
                        guard.job_id,
                        occurred_at=occurred_at,
                        actor_kind="job_worker",
                        actor_id=guard.claim_owner_id,
                        payload={"status": "SUCCEEDED", "terminal_reason": None},
                    )
                )
                self._append_events(
                    connection,
                    guard,
                    workspace_id=workspace_id,
                    drafts=tuple(drafts),
                )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb({"candidate_ids": [item.candidate_id for item in accepted]}),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'CANDIDATES_ACCEPTED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return tuple(accepted)

    def accept_memory_candidate_and_governance_job(
        self,
        guard: HostWriterGuard | JobAttemptClaimGuard,
        *,
        candidate_id: str,
        source_entry_id: str | None,
        proposal_kind: str,
        proposal_payload: Mapping[str, object],
        governance_job_id: str,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryCandidate:
        if proposal_kind not in {
            "FACT",
            "PREFERENCE",
            "RELATION",
            "CORRECTION",
            "LIFECYCLE",
        }:
            raise ValueError("memory proposal kind is not closed")
        governance = job_handler_contract(MEMORY_GOVERNANCE)
        scope = (
            self._writer_transaction(guard, deadline_monotonic=deadline_monotonic)
            if isinstance(guard, HostWriterGuard)
            else self._job_transaction(guard, deadline_monotonic=deadline_monotonic)
        )
        with scope as connection:
            if isinstance(guard, HostWriterGuard):
                origin_session_id = guard.session_id
                workspace_id = self._workspace_id(connection, guard.session_id)
            else:
                origin_session_id = guard.origin_session_id
                source_job = connection.execute(
                    "SELECT workspace_id FROM pulsara_v3.durable_jobs WHERE id = %s",
                    (guard.job_id,),
                ).fetchone()
                if source_job is None:
                    raise StaleJobClaim("candidate source job is absent")
                workspace_id = str(source_job["workspace_id"])
            semantic_digest = canonical_digest(
                "pulsara:memory-candidate:v1",
                {
                    "workspace_id": workspace_id,
                    "proposal_kind": proposal_kind,
                    "proposal_payload": dict(proposal_payload),
                    "source_entry_id": source_entry_id,
                },
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.memory_candidates (
                    id, workspace_id, origin_session_id, source_entry_id,
                    proposal_kind, semantic_digest, proposal_payload, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
                """,
                (
                    candidate_id,
                    workspace_id,
                    origin_session_id,
                    source_entry_id,
                    proposal_kind,
                    semantic_digest,
                    Jsonb(dict(proposal_payload)),
                ),
            )
            intent_payload = {
                "candidate_id": candidate_id,
                "candidate_semantic_digest": semantic_digest,
            }
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_jobs (
                    id, workspace_id, origin_session_id, handler_type,
                    intent_schema_version, intent_digest, intent_payload,
                    automatic_intent_key, safety_class, status,
                    retry_policy_id, retry_policy_version, maximum_attempts,
                    attempt_timeout_ms, provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt, next_eligible_at
                ) VALUES (
                    %s, %s, %s, 'MEMORY_GOVERNANCE',
                    'memory_governance.v1', %s, %s, %s,
                    'RETRY_SAFE', 'PENDING', 'bounded-exponential', 1,
                    %s, %s, %s, %s, clock_timestamp()
                )
                """,
                (
                    governance_job_id,
                    workspace_id,
                    origin_session_id,
                    canonical_digest(
                        "pulsara:job-intent:memory_governance.v1", intent_payload
                    ),
                    Jsonb(intent_payload),
                    f"memory-governance:{candidate_id}",
                    governance.maximum_attempts,
                    governance.attempt_timeout_ms,
                    governance.input_token_limit,
                    governance.output_token_limit,
                ),
            )
            if origin_session_id is not None:
                self._append_events(
                    connection,
                    guard,
                    workspace_id=workspace_id,
                    drafts=(
                        self._event(
                            CommittedEventType.JOB_QUEUED,
                            SubjectSlot.JOB,
                            governance_job_id,
                            occurred_at=occurred_at,
                            actor_kind=(
                                "runtime"
                                if isinstance(guard, HostWriterGuard)
                                else "job_worker"
                            ),
                            actor_id=(
                                guard.writer_owner_id
                                if isinstance(guard, HostWriterGuard)
                                else guard.claim_owner_id
                            ),
                            payload={"handler_type": "MEMORY_GOVERNANCE"},
                        ),
                    ),
                )
        return AcceptedMemoryCandidate(candidate_id, governance_job_id)

    def accept_memory_governance(
        self,
        guard: JobAttemptClaimGuard,
        *,
        candidate_id: str,
        decision_id: str,
        decision: str,
        lineage_payload: Mapping[str, object],
        fact_id: str | None,
        fact_kind: str | None,
        fact_payload: Mapping[str, object] | None,
        relations: Sequence[tuple[str, str, str]],
        index_handler_contract_id: str,
        index_handler_contract_version: int,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryGovernance:
        allowed = {"SKIP", "SUBMIT", "CORRECT", "MERGE", "SUPERSEDE", "CONTRADICT"}
        if decision not in allowed:
            raise ValueError("memory governance decision is not closed")
        if (decision == "SKIP") != (fact_id is None):
            raise ValueError("memory governance fact branch is invalid")
        if fact_id is not None and (not fact_kind or fact_payload is None):
            raise ValueError("accepted memory fact payload is incomplete")
        superseded_fact_ids_value = lineage_payload.get("superseded_fact_ids", ())
        if not isinstance(superseded_fact_ids_value, (list, tuple)):
            raise ValueError("memory lineage superseded-fact carrier is invalid")
        superseded_fact_ids = tuple(str(value) for value in superseded_fact_ids_value)
        if len(superseded_fact_ids) > 32 or any(
            not value for value in superseded_fact_ids
        ):
            raise ValueError("memory lineage exceeds its hard bound")
        lifecycle_decision = decision in {
            "CORRECT",
            "MERGE",
            "SUPERSEDE",
            "CONTRADICT",
        }
        if lifecycle_decision != bool(superseded_fact_ids):
            raise ValueError("memory lifecycle decision lineage is incomplete")
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            candidate = connection.execute(
                """
                SELECT c.*, j.workspace_id AS job_workspace_id,
                       j.intent_payload, j.handler_type
                FROM pulsara_v3.memory_candidates AS c
                JOIN pulsara_v3.durable_jobs AS j ON j.id = %s
                WHERE c.id = %s
                FOR UPDATE OF c, j
                """,
                (guard.job_id, candidate_id),
            ).fetchone()
            if (
                candidate is None
                or candidate["handler_type"] != "MEMORY_GOVERNANCE"
                or candidate["workspace_id"] != candidate["job_workspace_id"]
                or dict(candidate["intent_payload"]).get("candidate_id") != candidate_id
                or candidate["status"] != "PENDING"
            ):
                raise ConversationKernelConflict(
                    "governance job does not own the pending candidate"
                )
            workspace_id = str(candidate["workspace_id"])
            connection.execute(
                """
                INSERT INTO pulsara_v3.memory_governance_decisions (
                    id, candidate_id, job_id, decision, lineage_payload
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    decision_id,
                    candidate_id,
                    guard.job_id,
                    decision,
                    Jsonb(dict(lineage_payload)),
                ),
            )
            connection.execute(
                "UPDATE pulsara_v3.memory_candidates SET status = 'DECIDED' WHERE id = %s",
                (candidate_id,),
            )
            drafts: list[CommittedEventDraft] = []
            relation_ids: list[str] = []
            for superseded_fact_id in superseded_fact_ids:
                changed = connection.execute(
                    """
                    UPDATE pulsara_v3.memory_facts
                    SET lifecycle = 'SUPERSEDED', updated_at = clock_timestamp()
                    WHERE workspace_id = %s AND id = %s AND lifecycle = 'ACTIVE'
                    RETURNING id
                    """,
                    (workspace_id, superseded_fact_id),
                ).fetchone()
                if changed is None:
                    raise ConversationKernelConflict(
                        "memory lifecycle predecessor is not active"
                    )
                drafts.append(
                    self._event(
                        CommittedEventType.MEMORY_FACT_LIFECYCLE_CHANGED,
                        SubjectSlot.MEMORY_FACT,
                        superseded_fact_id,
                        occurred_at=occurred_at,
                        actor_kind="job_worker",
                        actor_id=guard.claim_owner_id,
                        payload={
                            "lifecycle": "SUPERSEDED",
                            "successor_fact_id": fact_id,
                            "decision": decision,
                        },
                    )
                )
            if fact_id is not None:
                assert fact_payload is not None and fact_kind is not None
                semantic_digest = canonical_digest(
                    "pulsara:memory-fact:v1",
                    {
                        "workspace_id": workspace_id,
                        "fact_kind": fact_kind,
                        "fact_payload": dict(fact_payload),
                    },
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_facts (
                        id, workspace_id, governance_decision_id, lifecycle,
                        fact_kind, fact_payload, semantic_digest
                    ) VALUES (%s, %s, %s, 'ACTIVE', %s, %s, %s)
                    """,
                    (
                        fact_id,
                        workspace_id,
                        decision_id,
                        fact_kind,
                        Jsonb(dict(fact_payload)),
                        semantic_digest,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.MEMORY_FACT_ACCEPTED,
                        SubjectSlot.MEMORY_FACT,
                        fact_id,
                        occurred_at=occurred_at,
                        actor_kind="job_worker",
                        actor_id=guard.claim_owner_id,
                        payload={"fact_kind": fact_kind},
                    )
                )
                for relation_id, target_fact_id, relation_kind in relations:
                    connection.execute(
                        """
                        INSERT INTO pulsara_v3.memory_relations (
                            id, workspace_id, source_fact_id,
                            target_fact_id, relation_kind
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            relation_id,
                            workspace_id,
                            fact_id,
                            target_fact_id,
                            relation_kind,
                        ),
                    )
                    relation_ids.append(relation_id)
                    drafts.append(
                        self._event(
                            CommittedEventType.MEMORY_RELATION_ACCEPTED,
                            SubjectSlot.MEMORY_RELATION,
                            relation_id,
                            occurred_at=occurred_at,
                            actor_kind="job_worker",
                            actor_id=guard.claim_owner_id,
                            payload={"relation_kind": relation_kind},
                        )
                    )
                for channel in ("FTS", "VECTOR"):
                    connection.execute(
                        """
                        INSERT INTO pulsara_v3.memory_index_state (
                            workspace_id, channel, desired_generation,
                            desired_handler_contract_id,
                            desired_handler_contract_version,
                            applied_generation, applied_handler_contract_id,
                            applied_handler_contract_version
                        ) VALUES (%s, %s, 1, %s, %s, 0, %s, %s)
                        ON CONFLICT (workspace_id, channel) DO UPDATE
                        SET desired_generation =
                                pulsara_v3.memory_index_state.desired_generation + 1,
                            desired_handler_contract_id = EXCLUDED.desired_handler_contract_id,
                            desired_handler_contract_version = EXCLUDED.desired_handler_contract_version
                        """,
                        (
                            workspace_id,
                            channel,
                            index_handler_contract_id,
                            index_handler_contract_version,
                            index_handler_contract_id,
                            index_handler_contract_version,
                        ),
                    )
            drafts.append(
                self._event(
                    CommittedEventType.JOB_TERMINAL_ACCEPTED,
                    SubjectSlot.JOB,
                    guard.job_id,
                    occurred_at=occurred_at,
                    actor_kind="job_worker",
                    actor_id=guard.claim_owner_id,
                    payload={"status": "SUCCEEDED", "terminal_reason": None},
                )
            )
            if guard.origin_session_id is not None:
                self._append_events(
                    connection,
                    guard,
                    workspace_id=workspace_id,
                    drafts=tuple(drafts),
                )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb({"decision_id": decision_id, "fact_id": fact_id}),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'GOVERNANCE_ACCEPTED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return AcceptedMemoryGovernance(
            candidate_id,
            decision_id,
            decision,
            fact_id,
            tuple(relation_ids),
        )

    def apply_fts_memory_index(
        self,
        guard: JobAttemptClaimGuard,
        *,
        handler_contract_id: str,
        handler_contract_version: int,
        deadline_monotonic: float,
    ) -> int:
        """Apply one exact FTS target; this port cannot mutate conversation rows."""

        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT j.intent_payload, j.workspace_id
                FROM pulsara_v3.durable_jobs AS j
                WHERE j.id = %s AND j.handler_type = 'MEMORY_INDEX_REFRESH'
                FOR UPDATE
                """,
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise ConversationKernelConflict("index refresh job is absent")
            intent = dict(job["intent_payload"])
            if (
                intent.get("channel") != "FTS"
                or intent.get("workspace_id") != job["workspace_id"]
                or intent.get("handler_contract_id") != handler_contract_id
                or intent.get("handler_contract_version") != handler_contract_version
            ):
                raise ConversationKernelConflict("index refresh intent mismatch")
            target = int(intent["target_generation"])
            state = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE workspace_id = %s AND channel = 'FTS'
                FOR UPDATE
                """,
                (job["workspace_id"],),
            ).fetchone()
            if (
                state is None
                or int(state["desired_generation"]) != target
                or state["desired_handler_contract_id"] != handler_contract_id
                or int(state["desired_handler_contract_version"])
                != handler_contract_version
            ):
                raise ConversationKernelConflict("index target was superseded")
            connection.execute(
                "DELETE FROM pulsara_v3.memory_search_index WHERE workspace_id = %s",
                (job["workspace_id"],),
            )
            inserted = connection.execute(
                """
                INSERT INTO pulsara_v3.memory_search_index (
                    workspace_id, fact_id, generation, search_document
                )
                SELECT workspace_id, id, %s,
                       to_tsvector('simple', fact_kind || ' ' || fact_payload::text)
                FROM pulsara_v3.memory_facts
                WHERE workspace_id = %s AND lifecycle = 'ACTIVE'
                RETURNING fact_id
                """,
                (target, job["workspace_id"]),
            ).fetchall()
            connection.execute(
                """
                UPDATE pulsara_v3.memory_index_state
                SET applied_generation = %s,
                    applied_handler_contract_id = %s,
                    applied_handler_contract_version = %s
                WHERE workspace_id = %s AND channel = 'FTS'
                """,
                (
                    target,
                    handler_contract_id,
                    handler_contract_version,
                    job["workspace_id"],
                ),
            )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb({"indexed_fact_count": len(inserted)}),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'INDEX_APPLIED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return len(inserted)

    def snapshot_memory_vector_source(
        self,
        guard: JobAttemptClaimGuard,
        *,
        handler_contract_id: str,
        handler_contract_version: int,
        deadline_monotonic: float,
    ) -> MemoryVectorSource:
        """Freeze the immutable input of one exact VECTOR refresh attempt."""

        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT intent_payload, workspace_id
                FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = 'MEMORY_INDEX_REFRESH'
                """,
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise ConversationKernelConflict("vector refresh job is absent")
            intent = dict(job["intent_payload"])
            if (
                intent.get("channel") != "VECTOR"
                or intent.get("workspace_id") != job["workspace_id"]
                or intent.get("handler_contract_id") != handler_contract_id
                or intent.get("handler_contract_version") != handler_contract_version
            ):
                raise ConversationKernelConflict("vector refresh intent mismatch")
            target = _required_nonnegative_int(
                intent.get("target_generation"), "target_generation"
            )
            state = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE workspace_id = %s AND channel = 'VECTOR'
                """,
                (job["workspace_id"],),
            ).fetchone()
            if (
                state is None
                or int(state["desired_generation"]) != target
                or state["desired_handler_contract_id"] != handler_contract_id
                or int(state["desired_handler_contract_version"])
                != handler_contract_version
            ):
                raise ConversationKernelConflict("vector index target was superseded")
            facts = tuple(
                MemoryVectorFactSource(
                    fact_id=str(row["id"]),
                    semantic_digest=str(row["semantic_digest"]),
                    embedding_text=(
                        str(row["fact_kind"])
                        + " "
                        + json.dumps(
                            dict(row["fact_payload"]),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                )
                for row in connection.execute(
                    """
                    SELECT id, fact_kind, fact_payload, semantic_digest
                    FROM pulsara_v3.memory_facts
                    WHERE workspace_id = %s AND lifecycle = 'ACTIVE'
                    ORDER BY id
                    """,
                    (job["workspace_id"],),
                ).fetchall()
            )
            digest = canonical_digest(
                "pulsara:memory-vector-source:v1",
                {
                    "workspace_id": str(job["workspace_id"]),
                    "target_generation": target,
                    "handler_contract_id": handler_contract_id,
                    "handler_contract_version": handler_contract_version,
                    "facts": tuple(
                        {
                            "fact_id": item.fact_id,
                            "semantic_digest": item.semantic_digest,
                            "embedding_text": item.embedding_text,
                        }
                        for item in facts
                    ),
                },
            )
            return MemoryVectorSource(
                workspace_id=str(job["workspace_id"]),
                target_generation=target,
                handler_contract_id=handler_contract_id,
                handler_contract_version=handler_contract_version,
                source_digest=digest,
                facts=facts,
            )

    def apply_vector_memory_index(
        self,
        guard: JobAttemptClaimGuard,
        *,
        source: MemoryVectorSource,
        embeddings: Sequence[Sequence[float]],
        deadline_monotonic: float,
    ) -> int:
        if len(embeddings) != len(source.facts):
            raise ValueError("vector result count does not match source facts")
        normalized: list[str] = []
        dimensions: int | None = None
        for vector in embeddings:
            values = tuple(float(value) for value in vector)
            if not values or any(not math.isfinite(value) for value in values):
                raise ValueError("vector result contains invalid coordinates")
            if dimensions is None:
                dimensions = len(values)
            elif dimensions != len(values):
                raise ValueError("vector result dimensions are inconsistent")
            normalized.append(
                "[" + ",".join(format(value, ".17g") for value in values) + "]"
            )
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT intent_payload, workspace_id
                FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = 'MEMORY_INDEX_REFRESH'
                FOR UPDATE
                """,
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise ConversationKernelConflict("vector refresh job is absent")
            intent = dict(job["intent_payload"])
            if (
                source.workspace_id != job["workspace_id"]
                or intent.get("channel") != "VECTOR"
                or int(intent.get("target_generation", -1)) != source.target_generation
                or intent.get("handler_contract_id") != source.handler_contract_id
                or intent.get("handler_contract_version")
                != source.handler_contract_version
            ):
                raise ConversationKernelConflict(
                    "vector refresh source identity drifted"
                )
            state = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE workspace_id = %s AND channel = 'VECTOR'
                FOR UPDATE
                """,
                (source.workspace_id,),
            ).fetchone()
            current = tuple(
                MemoryVectorFactSource(
                    fact_id=str(row["id"]),
                    semantic_digest=str(row["semantic_digest"]),
                    embedding_text=(
                        str(row["fact_kind"])
                        + " "
                        + json.dumps(
                            dict(row["fact_payload"]),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                )
                for row in connection.execute(
                    """
                    SELECT id, fact_kind, fact_payload, semantic_digest
                    FROM pulsara_v3.memory_facts
                    WHERE workspace_id = %s AND lifecycle = 'ACTIVE'
                    ORDER BY id
                    """,
                    (source.workspace_id,),
                ).fetchall()
            )
            expected_digest = canonical_digest(
                "pulsara:memory-vector-source:v1",
                {
                    "workspace_id": source.workspace_id,
                    "target_generation": source.target_generation,
                    "handler_contract_id": source.handler_contract_id,
                    "handler_contract_version": source.handler_contract_version,
                    "facts": tuple(
                        {
                            "fact_id": item.fact_id,
                            "semantic_digest": item.semantic_digest,
                            "embedding_text": item.embedding_text,
                        }
                        for item in current
                    ),
                },
            )
            if (
                state is None
                or int(state["desired_generation"]) != source.target_generation
                or state["desired_handler_contract_id"] != source.handler_contract_id
                or int(state["desired_handler_contract_version"])
                != source.handler_contract_version
                or current != source.facts
                or expected_digest != source.source_digest
            ):
                raise ConversationKernelConflict("vector refresh source was superseded")
            connection.execute(
                "DELETE FROM pulsara_v3.memory_vector_index WHERE workspace_id = %s",
                (source.workspace_id,),
            )
            for fact, vector_literal in zip(source.facts, normalized, strict=True):
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_vector_index (
                        workspace_id, fact_id, generation, embedding
                    ) VALUES (%s, %s, %s, %s::public.vector)
                    """,
                    (
                        source.workspace_id,
                        fact.fact_id,
                        source.target_generation,
                        vector_literal,
                    ),
                )
            connection.execute(
                """
                UPDATE pulsara_v3.memory_index_state
                SET applied_generation = %s,
                    applied_handler_contract_id = %s,
                    applied_handler_contract_version = %s
                WHERE workspace_id = %s AND channel = 'VECTOR'
                """,
                (
                    source.target_generation,
                    source.handler_contract_id,
                    source.handler_contract_version,
                    source.workspace_id,
                ),
            )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb(
                        {
                            "indexed_fact_count": len(source.facts),
                            "source_digest": source.source_digest,
                            "dimensions": dimensions or 0,
                        }
                    ),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'INDEX_APPLIED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return len(source.facts)
