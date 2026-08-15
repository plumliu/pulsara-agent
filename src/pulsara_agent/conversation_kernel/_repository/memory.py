"""Canonical advisory-memory intake, governance and cache operations.

This module deliberately owns no worker, lease, retry queue or recovery graph.
The Host may abandon a PROCESSING candidate forever; only accepted relational
facts are product-visible memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Sequence

from psycopg import Connection, IsolationLevel
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.memory.contracts import (
    ExistingSourceRelationDisposition,
    FrozenMemoryCandidateForGovernance,
    FrozenMemoryFactSettlementIdentity,
    FrozenMemoryGovernanceEvidence,
    FrozenMemoryGovernanceToolEvidence,
    FrozenMemoryGovernanceTurnItem,
    FrozenMemoryPublicFactProjection,
    FrozenMemoryProposal,
    FrozenModelVisibleMemoryProvenance,
    MemoryCandidateStatus,
    MemoryCitationEvidenceKind,
    MemoryCitationVisibility,
    MemoryDecisionKind,
    MemoryDecisionReasonCode,
    MemoryFactKind,
    MemoryGovernanceConfirmation,
    MemoryKindHint,
    MemoryProducerKind,
    MemoryRelationKind,
    MemorySupersedeMode,
    ModelVisibleMemoryProvenanceDisposition,
    PreparedExistingSourceRelationSettlement,
    PreparedMemoryBasisReference,
    PreparedMemoryCandidateAcceptance,
    PreparedMemoryGovernanceAcceptance,
    PreparedMemoryToolResultReference,
    canonical_json_bytes,
    memory_relation_id,
    memory_response_preference_item_payload,
    prepare_existing_source_relation_settlement,
    prepare_memory_candidate,
    memory_public_fact_payload,
)
from pulsara_agent.conversation_kernel.memory.recall import (
    MEMORY_EMBEDDING_CONTRACT_ID,
    MEMORY_EMBEDDING_CONTRACT_VERSION,
)
from pulsara_agent.memory.scope import FrozenMemoryReadScopeBinding, MemoryScopeKind
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.retrieval.embedding.validation import (
    freeze_v1_embedding_vector,
)

from .contracts import (
    ConversationKernelConflict,
    PreparedMemoryProposalSideBranch,
    _ObservedActiveMemoryDuplicate,
)


MAXIMUM_ACTIVE_RESPONSE_PREFERENCES_PER_SCOPE = 16
MAXIMUM_RESPONSE_PREFERENCE_SCOPE_PROJECTION_BYTES = 7 * 1024
_MAXIMUM_GOVERNANCE_TURN_ITEMS = 32
_MAXIMUM_GOVERNANCE_TURN_BODY_BYTES = 24 * 1024
_MAXIMUM_GOVERNANCE_TOOL_BODY_BYTES = 56 * 1024
_PRIVATE_OR_REMOTE_URL = re.compile(r"https?://[^\s\]\[\)\(\}\{<>'\"]+", re.I)


@dataclass(frozen=True, slots=True)
class AcceptedMemoryGovernance:
    candidate_id: str
    status: MemoryCandidateStatus
    fact_id: str | None
    duplicate_winner_fact_id: str | None = None
    relation_id: str | None = None


class _MemoryOperations:
    def accept_reflection_memory_candidates(
        self,
        guard: HostWriterGuard,
        *,
        candidates: Sequence[PreparedMemoryCandidateAcceptance],
        deadline_monotonic: float,
    ) -> tuple[str, ...]:
        """Best-effort batch intake; no event or durable work is created."""

        if len(candidates) > 4:
            raise ValueError("reflection candidate batch exceeds its bound")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            for candidate in candidates:
                if (
                    candidate.origin_session_id != guard.session_id
                    or candidate.producer_kind
                    is not MemoryProducerKind.CHEAP_HINT_REFLECTION
                ):
                    raise ConversationKernelConflict(
                        "reflection candidate does not belong to this Host"
                    )
                self._insert_prepared_memory_candidate(
                    connection, PreparedMemoryProposalSideBranch(candidate)
                )
        return tuple(candidate.candidate_id for candidate in candidates)

    def confirm_memory_candidate_intake(
        self,
        *,
        candidate: PreparedMemoryCandidateAcceptance,
        deadline_monotonic: float,
    ) -> bool:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            row = connection.execute(
                """
                SELECT candidate_acceptance_digest
                FROM pulsara_v3.memory_candidates
                WHERE memory_domain_id=%s AND id=%s
                """,
                (candidate.memory_domain_id, candidate.candidate_id),
            ).fetchone()
            if row is None:
                return False
            if str(row["candidate_acceptance_digest"]) != (
                candidate.candidate_acceptance_digest
            ):
                raise ConversationKernelConflict(
                    "memory candidate identity names a different winner"
                )
            observed = self._read_prepared_memory_candidate(
                connection, candidate.candidate_id
            )
            if observed != candidate:
                raise ConversationKernelConflict(
                    "memory candidate payload or reference set drifted"
                )
            return True

    def claim_memory_candidate_for_governance(
        self,
        guard: HostWriterGuard,
        *,
        candidate_id: str | None = None,
        processing_started_at: datetime,
        deadline_monotonic: float,
    ) -> FrozenMemoryCandidateForGovernance | None:
        """Claim only candidates produced by this exact origin workspace."""

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            session = connection.execute(
                """
                SELECT workspace_id, memory_domain_id
                FROM pulsara_v3.sessions WHERE id=%s
                """,
                (guard.session_id,),
            ).fetchone()
            if session is None:
                raise ConversationKernelConflict("governor session is absent")
            row = connection.execute(
                """
                SELECT id FROM pulsara_v3.memory_candidates
                WHERE memory_domain_id=%s AND origin_workspace_id=%s
                  AND status='PENDING' AND (%s::text IS NULL OR id=%s)
                ORDER BY accepted_at, id
                LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                (
                    session["memory_domain_id"],
                    session["workspace_id"],
                    candidate_id,
                    candidate_id,
                ),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE pulsara_v3.memory_candidates
                SET status='PROCESSING', processing_started_at=%s
                WHERE id=%s AND status='PENDING'
                """,
                (processing_started_at, row["id"]),
            )
            prepared = self._read_prepared_memory_candidate(
                connection, str(row["id"])
            )
        return FrozenMemoryCandidateForGovernance(
            prepared=prepared,
            status=MemoryCandidateStatus.PROCESSING,
            processing_started_at=processing_started_at,
        )

    def read_memory_candidate_for_governance(
        self,
        guard: HostWriterGuard,
        *,
        candidate_id: str,
        deadline_monotonic: float,
    ) -> FrozenMemoryCandidateForGovernance | None:
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            session = connection.execute(
                "SELECT workspace_id, memory_domain_id FROM pulsara_v3.sessions WHERE id=%s",
                (guard.session_id,),
            ).fetchone()
            head = connection.execute(
                """
                SELECT status, processing_started_at
                FROM pulsara_v3.memory_candidates
                WHERE id=%s AND memory_domain_id=%s AND origin_workspace_id=%s
                """,
                (candidate_id, session["memory_domain_id"], session["workspace_id"]),
            ).fetchone()
            if head is None or str(head["status"]) != "PROCESSING":
                return None
            prepared = self._read_prepared_memory_candidate(connection, candidate_id)
            return FrozenMemoryCandidateForGovernance(
                prepared=prepared,
                status=MemoryCandidateStatus.PROCESSING,
                processing_started_at=head["processing_started_at"],
            )

    def read_memory_governance_evidence(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedMemoryCandidateAcceptance,
        deadline_monotonic: float,
    ) -> FrozenMemoryGovernanceEvidence:
        """Freeze the bounded same-origin evidence visible to one governor call."""

        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            session = connection.execute(
                """
                SELECT workspace_id, memory_domain_id
                FROM pulsara_v3.sessions WHERE id=%s
                """,
                (guard.session_id,),
            ).fetchone()
            if session is None or (
                candidate.origin_session_id != guard.session_id
                or candidate.origin_workspace_id != str(session["workspace_id"])
                or candidate.memory_domain_id != str(session["memory_domain_id"])
            ):
                raise ConversationKernelConflict(
                    "memory governance evidence origin does not match the Host"
                )
            source_entry_id = (
                candidate.producer_entry_id
                if candidate.producer_kind is MemoryProducerKind.MAIN_AGENT_REMEMBER
                else candidate.trigger_user_entry_id
            )
            assert source_entry_id is not None
            source = connection.execute(
                """
                SELECT turn_id, entry_kind
                FROM pulsara_v3.transcript_entries
                WHERE session_id=%s AND id=%s
                """,
                (candidate.origin_session_id, source_entry_id),
            ).fetchone()
            if source is None or (
                candidate.producer_kind is MemoryProducerKind.MAIN_AGENT_REMEMBER
                and str(source["entry_kind"])
                not in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST"}
            ) or (
                candidate.producer_kind is MemoryProducerKind.CHEAP_HINT_REFLECTION
                and str(source["entry_kind"]) not in {"USER_MESSAGE", "USER_STEER"}
            ):
                raise ConversationKernelConflict(
                    "memory governance producer entry drifted"
                )
            turn_items = self._read_memory_governance_turn_projection(
                connection,
                session_id=candidate.origin_session_id,
                turn_id=str(source["turn_id"]),
            )
            tool_evidence = self._read_memory_governance_tool_evidence(
                connection, candidate
            )
            basis_items, basis_complete = self._read_memory_public_facts(
                connection,
                candidate=candidate,
                fact_ids=tuple(item.target_fact_id for item in candidate.basis_refs),
            )
            if not basis_complete:
                raise ConversationKernelConflict("memory governance basis drifted")
            if (
                candidate.visible_memory.disposition
                is ModelVisibleMemoryProvenanceDisposition.OVERFLOW
            ):
                visible_items: tuple[FrozenMemoryPublicFactProjection, ...] = ()
                visible_complete = False
            else:
                visible_items, visible_complete = self._read_memory_public_facts(
                    connection,
                    candidate=candidate,
                    fact_ids=candidate.visible_memory.fact_ids,
                )
                if visible_complete and len(
                    canonical_json_bytes(
                        tuple(memory_public_fact_payload(item) for item in visible_items)
                    )
                ) > 64 * 1024:
                    visible_items = ()
                    visible_complete = False
        return FrozenMemoryGovernanceEvidence(
            origin_workspace_id=candidate.origin_workspace_id,
            producer_turn_items=turn_items,
            tool_result_evidence=tool_evidence,
            basis_items=basis_items,
            model_visible_items=visible_items,
            model_visible_complete=visible_complete,
        )

    @classmethod
    def _read_memory_governance_turn_projection(
        cls, connection, *, session_id: str, turn_id: str
    ) -> tuple[FrozenMemoryGovernanceTurnItem, ...]:
        rows = connection.execute(
            """
            SELECT id, entry_kind
            FROM pulsara_v3.transcript_entries
            WHERE session_id=%s AND turn_id=%s
            ORDER BY entry_sequence, id
            LIMIT %s
            """,
            (session_id, turn_id, _MAXIMUM_GOVERNANCE_TURN_ITEMS + 1),
        ).fetchall()
        output: list[FrozenMemoryGovernanceTurnItem] = []
        remaining = _MAXIMUM_GOVERNANCE_TURN_BODY_BYTES
        for row in rows[:_MAXIMUM_GOVERNANCE_TURN_ITEMS]:
            if remaining <= 0:
                break
            kind = str(row["entry_kind"])
            entry_id = str(row["id"])
            if kind in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST"}:
                body, truncated = cls._read_assistant_public_body(
                    connection,
                    session_id=session_id,
                    entry_id=entry_id,
                    maximum_bytes=min(remaining, 8 * 1024),
                )
                role = "ASSISTANT"
            else:
                body, truncated = cls._read_entry_public_body(
                    connection,
                    session_id=session_id,
                    entry_id=entry_id,
                    maximum_bytes=min(remaining, 8 * 1024),
                )
                role = (
                    "USER"
                    if kind
                    in {"USER_MESSAGE", "USER_STEER", "PLAN_CONTINUATION"}
                    else "TOOL"
                )
            body = _governance_public_text(body)
            body_bytes = len(body.encode("utf-8"))
            if not body and not truncated:
                continue
            output.append(
                FrozenMemoryGovernanceTurnItem(
                    ordinal=len(output), role=role, body=body, truncated=truncated
                )
            )
            remaining -= body_bytes
        if len(rows) > _MAXIMUM_GOVERNANCE_TURN_ITEMS and output:
            last = output[-1]
            output[-1] = FrozenMemoryGovernanceTurnItem(
                ordinal=last.ordinal,
                role=last.role,
                body=last.body,
                truncated=True,
            )
        # Contract overhead is bounded independently from retained body bytes.
        while output and len(
            canonical_json_bytes(
                tuple((item.role, item.body, item.truncated) for item in output)
            )
        ) > 32 * 1024:
            output.pop()
        return tuple(
            FrozenMemoryGovernanceTurnItem(
                ordinal=ordinal,
                role=item.role,
                body=item.body,
                truncated=item.truncated,
            )
            for ordinal, item in enumerate(output)
        )

    @classmethod
    def _read_memory_governance_tool_evidence(
        cls, connection, candidate: PreparedMemoryCandidateAcceptance
    ) -> tuple[FrozenMemoryGovernanceToolEvidence, ...]:
        output: list[FrozenMemoryGovernanceToolEvidence] = []
        remaining = _MAXIMUM_GOVERNANCE_TOOL_BODY_BYTES
        count = max(1, len(candidate.tool_result_refs))
        for reference in candidate.tool_result_refs:
            row = connection.execute(
                """
                SELECT r.result_entry_id, r.result_state, r.observed_at,
                       r.observation_duration_microseconds,
                       r.tool_reported_duration_microseconds, r.workspace_id
                FROM pulsara_v3.tool_results AS r
                WHERE r.session_id=%s AND r.id=%s
                """,
                (reference.origin_session_id, reference.tool_result_id),
            ).fetchone()
            if row is None or (
                reference.origin_session_id != candidate.origin_session_id
                or str(row["workspace_id"]) != candidate.origin_workspace_id
            ):
                raise ConversationKernelConflict(
                    "memory governance ToolResult citation drifted"
                )
            per_item = min(8 * 1024, max(0, remaining // count))
            body, truncated = cls._read_entry_public_body(
                connection,
                session_id=reference.origin_session_id,
                entry_id=str(row["result_entry_id"]),
                maximum_bytes=per_item,
            )
            body = _governance_public_text(body)
            remaining -= len(body.encode("utf-8"))
            count -= 1
            output.append(
                FrozenMemoryGovernanceToolEvidence(
                    ordinal=len(output),
                    evidence_kind=reference.evidence_kind,
                    result_state=str(row["result_state"]),
                    observed_at_iso=row["observed_at"].isoformat(),
                    observation_duration_microseconds=(
                        None
                        if row["observation_duration_microseconds"] is None
                        else int(row["observation_duration_microseconds"])
                    ),
                    tool_reported_duration_microseconds=(
                        None
                        if row["tool_reported_duration_microseconds"] is None
                        else int(row["tool_reported_duration_microseconds"])
                    ),
                    body=body,
                    truncated=truncated,
                )
            )
        return tuple(output)

    @staticmethod
    def _read_memory_public_facts(
        connection,
        *,
        candidate: PreparedMemoryCandidateAcceptance,
        fact_ids: Sequence[str],
    ) -> tuple[tuple[FrozenMemoryPublicFactProjection, ...], bool]:
        if not fact_ids:
            return (), True
        rows = connection.execute(
            """
            SELECT id, memory_domain_id, scope_kind, scope_id, fact_kind,
                   lifecycle, statement, applies_when, do_not_apply_when,
                   fact_semantic_digest
            FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND id=ANY(%s)
              AND ((scope_kind='USER' AND scope_id='ctx:user')
                   OR (scope_kind='WORKSPACE' AND scope_id=%s))
            """,
            (
                candidate.memory_domain_id,
                list(fact_ids),
                candidate.proposal.scope_id
                if candidate.proposal.scope_kind is MemoryScopeKind.WORKSPACE
                else candidate.origin_workspace_id,
            ),
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        if set(by_id) != set(fact_ids):
            return (), False
        output = tuple(
            FrozenMemoryPublicFactProjection(
                fact_id=fact_id,
                scope_kind=MemoryScopeKind(str(by_id[fact_id]["scope_kind"])),
                scope_id=str(by_id[fact_id]["scope_id"]),
                fact_kind=MemoryFactKind(str(by_id[fact_id]["fact_kind"])),
                lifecycle=str(by_id[fact_id]["lifecycle"]),
                statement=str(by_id[fact_id]["statement"]),
                applies_when=(
                    None
                    if by_id[fact_id]["applies_when"] is None
                    else str(by_id[fact_id]["applies_when"])
                ),
                do_not_apply_when=tuple(
                    str(value) for value in by_id[fact_id]["do_not_apply_when"]
                ),
                fact_semantic_digest=str(by_id[fact_id]["fact_semantic_digest"]),
            )
            for fact_id in fact_ids
        )
        return output, True

    @staticmethod
    def _read_entry_public_body(
        connection, *, session_id: str, entry_id: str, maximum_bytes: int
    ) -> tuple[str, bool]:
        if maximum_bytes <= 0:
            return "", True
        row = connection.execute(
            """
            SELECT e.content_size,
                   substring(COALESCE(e.inline_content, b.body)
                             FROM 1 FOR %s) AS body
            FROM pulsara_v3.transcript_entries AS e
            LEFT JOIN pulsara_v3.blobs AS b
              ON b.id=e.blob_id AND b.workspace_id=e.workspace_id
            WHERE e.session_id=%s AND e.id=%s
            """,
            (maximum_bytes + 4, session_id, entry_id),
        ).fetchone()
        if row is None or row["body"] is None:
            raise ConversationKernelConflict("memory governance entry content is absent")
        return _decode_governance_projection(
            bytes(row["body"]),
            maximum_bytes=maximum_bytes,
            truncated=int(row["content_size"]) > maximum_bytes,
        )

    @classmethod
    def _read_assistant_public_body(
        cls, connection, *, session_id: str, entry_id: str, maximum_bytes: int
    ) -> tuple[str, bool]:
        rows = connection.execute(
            """
            SELECT id FROM pulsara_v3.assistant_message_blocks
            WHERE session_id=%s AND assistant_entry_id=%s
              AND block_kind IN ('TEXT', 'DATA')
            ORDER BY block_ordinal, id LIMIT 65
            """,
            (session_id, entry_id),
        ).fetchall()
        parts: list[str] = []
        remaining = maximum_bytes
        truncated = len(rows) > 64
        for row in rows[:64]:
            if remaining <= 0:
                truncated = True
                break
            block = connection.execute(
                """
                SELECT m.content_size,
                       substring(COALESCE(m.inline_content, b.body)
                                 FROM 1 FOR %s) AS body
                FROM pulsara_v3.assistant_message_blocks AS m
                LEFT JOIN pulsara_v3.blobs AS b
                  ON b.id=m.blob_id AND b.workspace_id=m.workspace_id
                WHERE m.session_id=%s AND m.id=%s
                """,
                (remaining + 4, session_id, row["id"]),
            ).fetchone()
            if block is None or block["body"] is None:
                raise ConversationKernelConflict(
                    "memory governance assistant content is absent"
                )
            text, item_truncated = _decode_governance_projection(
                bytes(block["body"]),
                maximum_bytes=remaining,
                truncated=int(block["content_size"]) > remaining,
            )
            parts.append(text)
            remaining -= len(text.encode("utf-8"))
            truncated = truncated or item_truncated
        return "".join(parts), truncated

    def abandon_memory_candidate(
        self,
        guard: HostWriterGuard,
        *,
        candidate_id: str,
        reason_code: str,
        public_summary: str | None,
        decided_at: datetime,
        deadline_monotonic: float,
    ) -> bool:
        try:
            reason = MemoryDecisionReasonCode(reason_code)
        except ValueError as exc:
            raise ValueError("memory abandonment reason is outside the closed union") from exc
        if not reason.value.startswith("ABANDONED_"):
            raise ValueError("memory abandonment requires an ABANDONED reason")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                UPDATE pulsara_v3.memory_candidates AS c
                SET status='ABANDONED', decision_kind='SKIP',
                    decision_reason_code=%s, decision_public_summary=%s,
                    decided_at=%s
                FROM pulsara_v3.sessions AS s
                WHERE c.id=%s AND c.status='PROCESSING'
                  AND s.id=%s AND c.memory_domain_id=s.memory_domain_id
                  AND c.origin_workspace_id=s.workspace_id
                RETURNING c.id
                """,
                (reason_code, public_summary, decided_at, candidate_id, guard.session_id),
            ).fetchone()
            return row is not None

    def accept_memory_governance(
        self,
        guard: HostWriterGuard,
        *,
        prepared: PreparedMemoryGovernanceAcceptance,
        decided_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryGovernance | PreparedExistingSourceRelationSettlement:
        """Apply one sealed decision; duplicate routing never reruns the model."""

        try:
            return self._accept_memory_governance_once(
                guard,
                prepared=prepared,
                decided_at=decided_at,
                deadline_monotonic=deadline_monotonic,
            )
        except _ObservedActiveMemoryDuplicate:
            pass
        except UniqueViolation as exc:
            if getattr(exc.diag, "constraint_name", "") != (
                "uq_pulsara_v3_memory_active_semantic"
            ):
                raise
        return self._prepare_memory_duplicate_outcome(
            guard,
            prepared=prepared,
            decided_at=decided_at,
            deadline_monotonic=deadline_monotonic,
        )

    def settle_existing_source_memory_relation(
        self,
        guard: HostWriterGuard,
        *,
        prepared: PreparedMemoryGovernanceAcceptance,
        settlement: PreparedExistingSourceRelationSettlement,
        decided_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryGovernance | PreparedExistingSourceRelationSettlement:
        """Settle one sealed duplicate relation without reopening model judgment."""

        if settlement.parent_candidate_fingerprint != prepared.candidate_fingerprint:
            raise ConversationKernelConflict(
                "existing memory settlement does not name its parent decision"
            )
        try:
            return self._settle_existing_source_memory_relation_once(
                guard,
                prepared=prepared,
                settlement=settlement,
                decided_at=decided_at,
                deadline_monotonic=deadline_monotonic,
            )
        except UniqueViolation:
            # The APPLY candidate lost only the semantic relation race.  Freeze
            # the now-existing exact relation and its original attribution in a
            # new carrier; never adopt it inside the stale APPLY carrier.
            return self.prepare_existing_source_memory_relation_settlement(
                guard,
                prepared=prepared,
                deadline_monotonic=deadline_monotonic,
            )

    def prepare_existing_source_memory_relation_settlement(
        self,
        guard: HostWriterGuard,
        *,
        prepared: PreparedMemoryGovernanceAcceptance,
        deadline_monotonic: float,
    ) -> PreparedExistingSourceRelationSettlement:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            self._lock_processing_candidate(connection, prepared)
            return self._freeze_existing_source_relation_settlement(
                connection, prepared
            )

    def confirm_memory_governance_winner(
        self,
        *,
        prepared: PreparedMemoryGovernanceAcceptance,
        existing_settlement: PreparedExistingSourceRelationSettlement | None = None,
        deadline_monotonic: float,
    ) -> MemoryGovernanceConfirmation:
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            candidate = connection.execute(
                "SELECT * FROM pulsara_v3.memory_candidates WHERE id=%s",
                (prepared.candidate_id,),
            ).fetchone()
            if candidate is None or str(candidate["candidate_acceptance_digest"]) != (
                prepared.candidate_acceptance_digest
            ):
                return MemoryGovernanceConfirmation.CONFLICT
            try:
                observed_candidate = self._read_prepared_memory_candidate(
                    connection, prepared.candidate_id
                )
            except (ConversationKernelConflict, ValueError):
                return MemoryGovernanceConfirmation.CONFLICT
            if (
                observed_candidate.candidate_acceptance_digest
                != prepared.candidate_acceptance_digest
            ):
                return MemoryGovernanceConfirmation.CONFLICT
            status = str(candidate["status"])
            if status == "PROCESSING":
                fact = connection.execute(
                    "SELECT 1 FROM pulsara_v3.memory_facts WHERE source_candidate_id=%s",
                    (prepared.candidate_id,),
                ).fetchone()
                relation = connection.execute(
                    "SELECT 1 FROM pulsara_v3.memory_relations WHERE decision_candidate_id=%s",
                    (prepared.candidate_id,),
                ).fetchone()
                if fact is not None or relation is not None:
                    return MemoryGovernanceConfirmation.CONFLICT
                if existing_settlement is not None:
                    return self._confirm_processing_existing_source_settlement(
                        connection, prepared, existing_settlement
                    )
                return (
                    MemoryGovernanceConfirmation.NONE
                    if self._prepared_governance_inputs_still_match(
                        connection, prepared
                    )
                    else MemoryGovernanceConfirmation.CONFLICT
                )
            if prepared.decision.decision_kind is MemoryDecisionKind.SKIP:
                return (
                    MemoryGovernanceConfirmation.FULL
                    if status == "SKIPPED"
                    and candidate["decision_kind"] == "SKIP"
                    and candidate["decision_reason_code"]
                    == prepared.decision.reason_code
                    and candidate["decision_public_summary"]
                    == prepared.decision.public_summary
                    and candidate["final_kind"] is None
                    and candidate["related_target_fact_id"] is None
                    and candidate["duplicate_winner_fact_id"] is None
                    and candidate["accepted_fact_id"] is None
                    and candidate["applied_existing_fact_id"] is None
                    and self._candidate_owns_no_memory_rows(
                        connection, prepared.candidate_id
                    )
                    else MemoryGovernanceConfirmation.CONFLICT
                )
            fact = prepared.fact
            assert fact is not None
            if (
                status == "SKIPPED"
                and candidate["decision_kind"] == "SKIP"
                and candidate["decision_reason_code"]
                == "RESPONSE_PREFERENCE_CAPACITY_EXCEEDED"
                and candidate["final_kind"] is None
                and candidate["accepted_fact_id"] is None
                and candidate["applied_existing_fact_id"] is None
                and candidate["duplicate_winner_fact_id"] is None
                and candidate["related_target_fact_id"] is None
                and candidate["decision_public_summary"] is None
            ):
                return (
                    MemoryGovernanceConfirmation.FULL
                    if self._candidate_owns_no_memory_rows(
                        connection, prepared.candidate_id
                    )
                    else MemoryGovernanceConfirmation.CONFLICT
                )
            if status == "ACCEPTED":
                row = connection.execute(
                    "SELECT * FROM pulsara_v3.memory_facts WHERE id=%s",
                    (fact.fact_id,),
                ).fetchone()
                if (
                    row is None
                    or not self._memory_fact_matches(row, fact)
                    or str(candidate["accepted_fact_id"]) != fact.fact_id
                    or str(candidate["decision_kind"])
                    != prepared.decision.decision_kind.value
                    or str(candidate["final_kind"]) != fact.fact_kind.value
                    or candidate["decision_reason_code"] is not None
                    or candidate["decision_public_summary"]
                    != prepared.decision.public_summary
                    or candidate["related_target_fact_id"]
                    != prepared.decision.related_target_fact_id
                    or candidate["duplicate_winner_fact_id"] is not None
                    or candidate["applied_existing_fact_id"] is not None
                    or str(row["source_candidate_id"]) != prepared.candidate_id
                ):
                    return MemoryGovernanceConfirmation.CONFLICT
                return (
                    MemoryGovernanceConfirmation.FULL
                    if self._governance_relations_match(connection, prepared)
                    else MemoryGovernanceConfirmation.CONFLICT
                )
            if status in {"SKIPPED", "APPLIED_TO_EXISTING"}:
                winner_id = (
                    candidate["applied_existing_fact_id"]
                    if status == "APPLIED_TO_EXISTING"
                    else candidate["duplicate_winner_fact_id"]
                )
                winner = connection.execute(
                    "SELECT * FROM pulsara_v3.memory_facts WHERE id=%s",
                    (winner_id,),
                ).fetchone()
                if (
                    winner is None
                    or not self._memory_fact_matches(winner, fact)
                    or str(candidate["decision_kind"])
                    not in {
                        "SKIP",
                        prepared.decision.decision_kind.value,
                    }
                    or candidate["related_target_fact_id"]
                    != prepared.decision.related_target_fact_id
                    or candidate["decision_public_summary"]
                    != prepared.decision.public_summary
                ):
                    return MemoryGovernanceConfirmation.CONFLICT
                if prepared.decision.decision_kind is MemoryDecisionKind.ACCEPT:
                    return (
                        MemoryGovernanceConfirmation.FULL
                        if status == "SKIPPED"
                        and candidate["decision_reason_code"]
                        in {
                            "SKIPPED_DUPLICATE",
                            "SKIPPED_DUPLICATE_BASIS_UNAPPLIED",
                        }
                        and candidate["related_target_fact_id"] is None
                        and candidate["accepted_fact_id"] is None
                        and candidate["applied_existing_fact_id"] is None
                        and self._candidate_owns_no_memory_rows(
                            connection, prepared.candidate_id
                        )
                        else MemoryGovernanceConfirmation.CONFLICT
                    )
                if (
                    existing_settlement is None
                    or existing_settlement.parent_candidate_fingerprint
                    != prepared.candidate_fingerprint
                    or existing_settlement.candidate_id != prepared.candidate_id
                    or existing_settlement.existing_source.fact_id != str(winner_id)
                    or existing_settlement.target.fact_id
                    != prepared.decision.related_target_fact_id
                    or not self._confirm_existing_relation(
                        connection, existing_settlement
                    )
                ):
                    return MemoryGovernanceConfirmation.CONFLICT
                target = connection.execute(
                    "SELECT * FROM pulsara_v3.memory_facts WHERE id=%s",
                    (existing_settlement.target.fact_id,),
                ).fetchone()
                if (
                    target is None
                    or not self._memory_settlement_identity_matches(
                        winner,
                        existing_settlement.existing_source,
                        include_lifecycle=False,
                    )
                    or not self._memory_settlement_identity_matches(
                        target,
                        existing_settlement.target,
                        include_lifecycle=False,
                    )
                    or str(target["lifecycle"])
                    != existing_settlement.settled_target_lifecycle
                ):
                    return MemoryGovernanceConfirmation.CONFLICT
                prepared_fact_row = connection.execute(
                    "SELECT 1 FROM pulsara_v3.memory_facts WHERE id=%s OR source_candidate_id=%s",
                    (fact.fact_id, prepared.candidate_id),
                ).fetchone()
                if prepared_fact_row is not None:
                    return MemoryGovernanceConfirmation.CONFLICT
                if status == "APPLIED_TO_EXISTING":
                    if (
                        existing_settlement.disposition
                        is not ExistingSourceRelationDisposition.APPLY_NEW_RELATION
                        or str(candidate["decision_kind"])
                        != prepared.decision.decision_kind.value
                        or candidate["decision_reason_code"] is not None
                        or candidate["applied_existing_fact_id"]
                        != existing_settlement.existing_source.fact_id
                    ):
                        return MemoryGovernanceConfirmation.CONFLICT
                    owned = connection.execute(
                        "SELECT count(*) AS n FROM pulsara_v3.memory_relations WHERE decision_candidate_id=%s",
                        (prepared.candidate_id,),
                    ).fetchone()
                    if int(owned["n"]) != 1:
                        return MemoryGovernanceConfirmation.CONFLICT
                elif (
                    existing_settlement.disposition
                    is not ExistingSourceRelationDisposition.CONFIRM_EXISTING_RELATION
                    or candidate["decision_reason_code"]
                    != "SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT"
                    or candidate["applied_existing_fact_id"] is not None
                ):
                    return MemoryGovernanceConfirmation.CONFLICT
                return MemoryGovernanceConfirmation.FULL
            return MemoryGovernanceConfirmation.CONFLICT

    @staticmethod
    def _candidate_owns_no_memory_rows(
        connection: Connection, candidate_id: str
    ) -> bool:
        fact = connection.execute(
            "SELECT 1 FROM pulsara_v3.memory_facts WHERE source_candidate_id=%s",
            (candidate_id,),
        ).fetchone()
        relation = connection.execute(
            "SELECT 1 FROM pulsara_v3.memory_relations WHERE decision_candidate_id=%s",
            (candidate_id,),
        ).fetchone()
        return fact is None and relation is None

    def _prepared_governance_inputs_still_match(
        self,
        connection: Connection,
        prepared: PreparedMemoryGovernanceAcceptance,
    ) -> bool:
        """Confirm NONE only while all sealed canonical inputs remain writable."""

        decision = prepared.decision
        if decision.decision_kind is MemoryDecisionKind.SKIP:
            return True
        fact = prepared.fact
        assert fact is not None
        duplicate = connection.execute(
            """
            SELECT 1 FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND scope_kind=%s AND scope_id=%s
              AND fact_semantic_digest=%s AND lifecycle='ACTIVE'
            """,
            (
                fact.memory_domain_id,
                fact.scope_kind.value,
                fact.scope_id,
                fact.fact_semantic_digest,
            ),
        ).fetchone()
        if duplicate is not None:
            return False
        target = self._read_governance_target_for_confirmation(connection, prepared)
        if decision.related_target_fact_id is not None and target is None:
            return False
        basis = connection.execute(
            """
            SELECT r.ordinal, f.*
            FROM pulsara_v3.memory_candidate_basis_refs AS r
            JOIN pulsara_v3.memory_facts AS f
              ON f.memory_domain_id=r.memory_domain_id
             AND f.scope_kind=r.target_scope_kind
             AND f.scope_id=r.target_scope_id AND f.id=r.target_fact_id
            WHERE r.candidate_id=%s ORDER BY r.ordinal
            """,
            (prepared.candidate_id,),
        ).fetchall()
        if len(basis) != len(prepared.basis_targets):
            return False
        for ordinal, (row, expected) in enumerate(
            zip(basis, prepared.basis_targets, strict=True)
        ):
            if int(row["ordinal"]) != ordinal or not (
                self._memory_settlement_identity_matches(
                    row, expected, include_lifecycle=True
                )
            ):
                return False
        return fact.fact_kind is MemoryFactKind.DECISION or not basis

    def _read_governance_target_for_confirmation(
        self,
        connection: Connection,
        prepared: PreparedMemoryGovernanceAcceptance,
    ):
        target_id = prepared.decision.related_target_fact_id
        if target_id is None:
            return None
        row = connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND id=%s
            """,
            (prepared.memory_domain_id, target_id),
        ).fetchone()
        fact = prepared.fact
        assert fact is not None
        if row is None or prepared.target is None:
            return None
        if not self._memory_settlement_identity_matches(
            row, prepared.target, include_lifecycle=True
        ):
            return None
        same_kind = str(row["fact_kind"]) == fact.fact_kind.value
        decision = prepared.decision
        if decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_CONTRADICT:
            return row if same_kind else None
        if decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE:
            expected_same_kind = (
                decision.supersede_mode
                is MemorySupersedeMode.SAME_KIND_REPLACEMENT
            )
            return row if same_kind == expected_same_kind else None
        return None

    def _governance_relations_match(
        self,
        connection: Connection,
        prepared: PreparedMemoryGovernanceAcceptance,
    ) -> bool:
        rows = connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_relations
            WHERE decision_candidate_id=%s ORDER BY ordinal NULLS LAST, id
            """,
            (prepared.candidate_id,),
        ).fetchall()
        expected = tuple(
            (
                item.relation_id,
                item.target.memory_domain_id,
                item.decision_candidate_id,
                item.source_scope_kind.value,
                item.source_scope_id,
                item.source_fact_id,
                item.source_fact_kind.value,
                item.relation_kind.value,
                item.target.scope_kind.value,
                item.target.scope_id,
                item.target.fact_id,
                item.target.fact_kind.value,
                None
                if item.supersede_mode is None
                else item.supersede_mode.value,
                item.ordinal,
            )
            for item in prepared.relation_drafts
        )
        if tuple(self._relation_tuple(row) for row in rows) != expected:
            return False
        for item in prepared.relation_drafts:
            if item.expected_target_lifecycle_after != "SUPERSEDED":
                continue
            target = connection.execute(
                "SELECT lifecycle FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=%s",
                (item.target.memory_domain_id, item.target.fact_id),
            ).fetchone()
            if target is None or str(target["lifecycle"]) != "SUPERSEDED":
                return False
        return True

    @staticmethod
    def _expected_relation_tuple(
        *, candidate_id, source, target, relation_kind, supersede_mode, ordinal
    ) -> tuple[object, ...]:
        relation_id = memory_relation_id(
            memory_domain_id=str(source["memory_domain_id"]),
            source_scope_kind=MemoryScopeKind(str(source["scope_kind"])),
            source_scope_id=str(source["scope_id"]),
            source_fact_id=str(source["id"]),
            relation_kind=relation_kind,
            target_scope_kind=MemoryScopeKind(str(target["scope_kind"])),
            target_scope_id=str(target["scope_id"]),
            target_fact_id=str(target["id"]),
            supersede_mode=supersede_mode,
        )
        return (
            relation_id,
            str(source["memory_domain_id"]),
            candidate_id,
            str(source["scope_kind"]),
            str(source["scope_id"]),
            str(source["id"]),
            str(source["fact_kind"]),
            relation_kind.value,
            str(target["scope_kind"]),
            str(target["scope_id"]),
            str(target["id"]),
            str(target["fact_kind"]),
            None if supersede_mode is None else supersede_mode.value,
            ordinal,
        )

    @staticmethod
    def _relation_tuple(row) -> tuple[object, ...]:
        return (
            str(row["id"]),
            str(row["memory_domain_id"]),
            str(row["decision_candidate_id"]),
            str(row["source_scope_kind"]),
            str(row["source_scope_id"]),
            str(row["source_fact_id"]),
            str(row["source_fact_kind"]),
            str(row["relation_kind"]),
            str(row["target_scope_kind"]),
            str(row["target_scope_id"]),
            str(row["target_fact_id"]),
            str(row["target_fact_kind"]),
            None if row["supersede_mode"] is None else str(row["supersede_mode"]),
            None if row["ordinal"] is None else int(row["ordinal"]),
        )

    def _confirm_processing_existing_source_settlement(
        self,
        connection: Connection,
        prepared: PreparedMemoryGovernanceAcceptance,
        settlement: PreparedExistingSourceRelationSettlement,
    ) -> MemoryGovernanceConfirmation:
        if (
            settlement.parent_candidate_fingerprint
            != prepared.candidate_fingerprint
            or settlement.candidate_id != prepared.candidate_id
        ):
            return MemoryGovernanceConfirmation.CONFLICT
        source = connection.execute(
            "SELECT * FROM pulsara_v3.memory_facts WHERE id=%s",
            (settlement.existing_source.fact_id,),
        ).fetchone()
        target = connection.execute(
            "SELECT * FROM pulsara_v3.memory_facts WHERE id=%s",
            (settlement.target.fact_id,),
        ).fetchone()
        if source is None or target is None:
            return MemoryGovernanceConfirmation.CONFLICT
        if not self._memory_settlement_identity_matches(
            source,
            settlement.existing_source,
            include_lifecycle=(
                settlement.disposition
                is ExistingSourceRelationDisposition.APPLY_NEW_RELATION
            ),
        ):
            return MemoryGovernanceConfirmation.CONFLICT
        if settlement.disposition is ExistingSourceRelationDisposition.APPLY_NEW_RELATION:
            if (
                not self._memory_settlement_identity_matches(
                    target, settlement.target, include_lifecycle=True
                )
                or self._confirm_existing_relation(connection, settlement)
            ):
                return MemoryGovernanceConfirmation.CONFLICT
        elif (
            not self._memory_settlement_identity_matches(
                target, settlement.target, include_lifecycle=False
            )
            or str(target["lifecycle"]) != settlement.settled_target_lifecycle
            or not self._confirm_existing_relation(connection, settlement)
        ):
            return MemoryGovernanceConfirmation.CONFLICT
        return MemoryGovernanceConfirmation.NONE

    def list_unembedded_memory_facts(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        limit: int,
        deadline_monotonic: float,
    ) -> tuple[tuple[str, str, str], ...]:
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                SELECT f.id, f.fact_semantic_digest,
                       concat_ws(E'\n', f.statement, f.applies_when,
                                 array_to_string(f.do_not_apply_when, E'\n')) AS body
                FROM pulsara_v3.memory_facts AS f
                LEFT JOIN pulsara_v3.memory_embeddings AS e
                  ON e.memory_domain_id=f.memory_domain_id AND e.fact_id=f.id
                 AND e.fact_semantic_digest=f.fact_semantic_digest
                WHERE f.memory_domain_id=%s AND f.lifecycle='ACTIVE'
                  AND (f.scope_kind, f.scope_id) IN (
                    SELECT * FROM unnest(%s::text[], %s::text[])
                  )
                  AND e.fact_id IS NULL
                ORDER BY f.accepted_at, f.id LIMIT %s
                """,
                (
                    read_binding.memory_domain_id,
                    [item.kind.value for item in read_binding.readable_scopes],
                    [item.scope_id for item in read_binding.readable_scopes],
                    max(1, min(limit, 100)),
                ),
            ).fetchall()
        return tuple(
            (str(row["id"]), str(row["fact_semantic_digest"]), str(row["body"]))
            for row in rows
        )

    def upsert_memory_embedding(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        fact_id: str,
        fact_semantic_digest: str,
        vector: Sequence[float],
        embedded_at: datetime,
        deadline_monotonic: float,
    ) -> bool:
        values = freeze_v1_embedding_vector(vector)
        literal = "[" + ",".join(format(value, ".17g") for value in values) + "]"
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                INSERT INTO pulsara_v3.memory_embeddings (
                    memory_domain_id, fact_id, fact_semantic_digest,
                    embedding_contract_id, embedding_contract_version,
                    embedding, embedded_at
                )
                SELECT f.memory_domain_id, f.id, f.fact_semantic_digest,
                       %s, %s, %s::public.vector, %s
                FROM pulsara_v3.memory_facts AS f
                WHERE f.memory_domain_id=%s AND f.id=%s
                  AND (f.scope_kind, f.scope_id) IN (
                    SELECT * FROM unnest(%s::text[], %s::text[])
                  )
                  AND f.lifecycle='ACTIVE' AND f.fact_semantic_digest=%s
                ON CONFLICT (memory_domain_id, fact_id) DO UPDATE
                SET fact_semantic_digest=EXCLUDED.fact_semantic_digest,
                    embedding_contract_id=EXCLUDED.embedding_contract_id,
                    embedding_contract_version=EXCLUDED.embedding_contract_version,
                    embedding=EXCLUDED.embedding, embedded_at=EXCLUDED.embedded_at
                RETURNING fact_id
                """,
                (
                    MEMORY_EMBEDDING_CONTRACT_ID,
                    MEMORY_EMBEDDING_CONTRACT_VERSION,
                    literal,
                    embedded_at,
                    read_binding.memory_domain_id,
                    fact_id,
                    [item.kind.value for item in read_binding.readable_scopes],
                    [item.scope_id for item in read_binding.readable_scopes],
                    fact_semantic_digest,
                ),
            ).fetchone()
            return row is not None

    def _accept_memory_governance_once(
        self,
        guard: HostWriterGuard,
        *,
        prepared: PreparedMemoryGovernanceAcceptance,
        decided_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryGovernance:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            self._lock_processing_candidate(connection, prepared)
            decision = prepared.decision
            if decision.decision_kind is MemoryDecisionKind.SKIP:
                connection.execute(
                    """
                    UPDATE pulsara_v3.memory_candidates
                    SET status='SKIPPED', decision_kind='SKIP',
                        decision_reason_code=%s, decision_public_summary=%s,
                        decided_at=%s WHERE id=%s
                    """,
                    (
                        decision.reason_code,
                        decision.public_summary,
                        decided_at,
                        prepared.candidate_id,
                    ),
                )
                return AcceptedMemoryGovernance(
                    prepared.candidate_id, MemoryCandidateStatus.SKIPPED, None
                )
            fact = prepared.fact
            assert fact is not None
            if self._active_semantic_winner(connection, fact) is not None:
                raise _ObservedActiveMemoryDuplicate
            target = self._lock_governance_target(connection, prepared)
            basis = self._lock_basis_targets(connection, prepared)
            if fact.fact_kind is MemoryFactKind.RESPONSE_PREFERENCE or (
                target is not None
                and str(target["fact_kind"])
                == MemoryFactKind.RESPONSE_PREFERENCE.value
            ):
                self._lock_response_preference_scope(connection, fact)
                if not self._response_preference_capacity_allows(
                    connection,
                    fact,
                    superseded_target=(
                        target
                        if decision.decision_kind
                        is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE
                        else None
                    ),
                    include_new=(
                        fact.fact_kind is MemoryFactKind.RESPONSE_PREFERENCE
                    ),
                ):
                    connection.execute(
                        """
                        UPDATE pulsara_v3.memory_candidates
                        SET status='SKIPPED', decision_kind='SKIP', final_kind=NULL,
                            decision_reason_code='RESPONSE_PREFERENCE_CAPACITY_EXCEEDED',
                            decision_public_summary=NULL, decided_at=%s
                        WHERE id=%s
                        """,
                        (decided_at, prepared.candidate_id),
                    )
                    return AcceptedMemoryGovernance(
                        prepared.candidate_id, MemoryCandidateStatus.SKIPPED, None
                    )
            self._insert_memory_fact(connection, fact, decided_at)
            relation_ids = self._insert_governance_relations(
                connection,
                prepared=prepared,
                source_fact=fact,
                target=target,
                basis=basis,
                decided_at=decided_at,
            )
            connection.execute(
                """
                UPDATE pulsara_v3.memory_candidates
                SET status='ACCEPTED', decision_kind=%s, final_kind=%s,
                    decision_public_summary=%s, related_target_fact_id=%s,
                    accepted_fact_id=%s, accepted_fact_at=%s, decided_at=%s
                WHERE id=%s
                """,
                (
                    decision.decision_kind.value,
                    fact.fact_kind.value,
                    decision.public_summary,
                    decision.related_target_fact_id,
                    fact.fact_id,
                    decided_at,
                    decided_at,
                    prepared.candidate_id,
                ),
            )
            return AcceptedMemoryGovernance(
                prepared.candidate_id,
                MemoryCandidateStatus.ACCEPTED,
                fact.fact_id,
                relation_id=relation_ids[0] if len(relation_ids) == 1 else None,
            )

    def _prepare_memory_duplicate_outcome(
        self,
        guard: HostWriterGuard,
        *,
        prepared: PreparedMemoryGovernanceAcceptance,
        decided_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryGovernance | PreparedExistingSourceRelationSettlement:
        fact = prepared.fact
        if fact is None:
            raise ConversationKernelConflict("SKIP has no duplicate settlement")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            self._lock_processing_candidate(connection, prepared)
            winner = self._active_semantic_winner(connection, fact)
            if winner is None or not self._memory_fact_matches(winner, fact):
                raise ConversationKernelConflict("memory duplicate winner drifted")
            winner_id = str(winner["id"])
            decision = prepared.decision
            if decision.decision_kind is MemoryDecisionKind.ACCEPT:
                prepared_basis = connection.execute(
                    """
                    SELECT target_fact_id, ordinal
                    FROM pulsara_v3.memory_candidate_basis_refs
                    WHERE candidate_id=%s ORDER BY ordinal
                    """,
                    (prepared.candidate_id,),
                ).fetchall()
                existing_basis = connection.execute(
                    """
                    SELECT target_fact_id, ordinal
                    FROM pulsara_v3.memory_relations
                    WHERE source_fact_id=%s AND relation_kind='BASED_ON'
                    ORDER BY ordinal
                    """,
                    (winner_id,),
                ).fetchall()
                basis_matches = tuple(
                    (str(row["target_fact_id"]), int(row["ordinal"]))
                    for row in prepared_basis
                ) == tuple(
                    (str(row["target_fact_id"]), int(row["ordinal"]))
                    for row in existing_basis
                )
                reason = (
                    "SKIPPED_DUPLICATE_BASIS_UNAPPLIED"
                    if prepared_basis and not basis_matches
                    else "SKIPPED_DUPLICATE"
                )
                connection.execute(
                    """
                    UPDATE pulsara_v3.memory_candidates
                    SET status='SKIPPED', decision_kind='SKIP', final_kind=NULL,
                        decision_reason_code=%s, decision_public_summary=%s,
                        duplicate_winner_fact_id=%s,
                        decided_at=%s WHERE id=%s
                    """,
                    (
                        reason,
                        prepared.decision.public_summary,
                        winner_id,
                        decided_at,
                        prepared.candidate_id,
                    ),
                )
                return AcceptedMemoryGovernance(
                    prepared.candidate_id,
                    MemoryCandidateStatus.SKIPPED,
                    None,
                    duplicate_winner_fact_id=winner_id,
                )
            return self._freeze_existing_source_relation_settlement(
                connection, prepared, winner=winner
            )

    def _freeze_existing_source_relation_settlement(
        self,
        connection,
        prepared: PreparedMemoryGovernanceAcceptance,
        *,
        winner=None,
    ) -> PreparedExistingSourceRelationSettlement:
        fact = prepared.fact
        if fact is None or prepared.decision.decision_kind not in {
            MemoryDecisionKind.ACCEPT_AND_SUPERSEDE,
            MemoryDecisionKind.ACCEPT_AND_CONTRADICT,
        }:
            raise ConversationKernelConflict(
                "memory decision has no existing-source relation branch"
            )
        winner = winner or self._active_semantic_winner(connection, fact)
        if winner is None or not self._memory_fact_matches(winner, fact):
            raise ConversationKernelConflict("memory duplicate winner drifted")
        if str(winner["lifecycle"]) != "ACTIVE":
            raise ConversationKernelConflict("memory duplicate winner is not active")
        target = self._lock_governance_target(
            connection, prepared, allow_superseded_existing=True
        )
        if target is None or str(winner["id"]) == str(target["id"]):
            raise ConversationKernelConflict("existing memory relation target drifted")
        relation_kind = (
            MemoryRelationKind.SUPERSEDES
            if prepared.decision.decision_kind
            is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE
            else MemoryRelationKind.CONTRADICTS
        )
        existing = self._find_exact_relation(
            connection,
            source=winner,
            target=target,
            relation_kind=relation_kind,
            supersede_mode=prepared.decision.supersede_mode,
        )
        disposition = ExistingSourceRelationDisposition.APPLY_NEW_RELATION
        relation_fields = {
            "existing_relation_id": None,
            "existing_relation_decision_candidate_id": None,
            "existing_relation_source_fact_id": None,
            "existing_relation_target_fact_id": None,
        }
        if existing is not None:
            disposition = (
                ExistingSourceRelationDisposition.CONFIRM_EXISTING_RELATION
            )
            relation_fields = {
                "existing_relation_id": str(existing["id"]),
                "existing_relation_decision_candidate_id": str(
                    existing["decision_candidate_id"]
                ),
                "existing_relation_source_fact_id": str(existing["source_fact_id"]),
                "existing_relation_target_fact_id": str(existing["target_fact_id"]),
            }
            expected_target = (
                "SUPERSEDED"
                if relation_kind is MemoryRelationKind.SUPERSEDES
                else "ACTIVE"
            )
            if str(target["lifecycle"]) != expected_target:
                raise ConversationKernelConflict(
                    "existing memory relation lifecycle drifted"
                )
        elif str(target["lifecycle"]) != "ACTIVE":
            raise ConversationKernelConflict(
                "memory relation target is no longer active"
            )
        return prepare_existing_source_relation_settlement(
            parent=prepared,
            existing_source=self._memory_fact_settlement_identity(winner),
            target=self._memory_fact_settlement_identity(target),
            disposition=disposition,
            **relation_fields,
        )

    def _settle_existing_source_memory_relation_once(
        self,
        guard: HostWriterGuard,
        *,
        prepared: PreparedMemoryGovernanceAcceptance,
        settlement: PreparedExistingSourceRelationSettlement,
        decided_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryGovernance | PreparedExistingSourceRelationSettlement:
        fact = prepared.fact
        if fact is None:
            raise ConversationKernelConflict("memory relation settlement lacks a fact")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            self._lock_processing_candidate(connection, prepared)
            source = connection.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE id=%s FOR UPDATE",
                (settlement.existing_source.fact_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE id=%s FOR UPDATE",
                (settlement.target.fact_id,),
            ).fetchone()
            if (
                source is None
                or target is None
                or not self._memory_fact_matches(source, fact)
                or not self._memory_settlement_identity_matches(
                    source,
                    settlement.existing_source,
                    include_lifecycle=(
                        settlement.disposition
                        is ExistingSourceRelationDisposition.APPLY_NEW_RELATION
                    ),
                )
                or not self._memory_settlement_identity_matches(
                    target,
                    settlement.target,
                    include_lifecycle=(
                        settlement.disposition
                        is ExistingSourceRelationDisposition.CONFIRM_EXISTING_RELATION
                    ),
                )
            ):
                raise ConversationKernelConflict(
                    "existing memory settlement endpoint drifted"
                )
            if settlement.disposition is (
                ExistingSourceRelationDisposition.CONFIRM_EXISTING_RELATION
            ):
                if not self._confirm_existing_relation(connection, settlement):
                    raise ConversationKernelConflict(
                        "existing memory relation attribution drifted"
                    )
                if str(target["lifecycle"]) != settlement.settled_target_lifecycle:
                    raise ConversationKernelConflict(
                        "existing memory relation effect drifted"
                    )
                connection.execute(
                    """
                    UPDATE pulsara_v3.memory_candidates
                    SET status='SKIPPED', decision_kind='SKIP', final_kind=NULL,
                        decision_reason_code='SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT',
                        decision_public_summary=%s,
                        duplicate_winner_fact_id=%s, related_target_fact_id=%s,
                        decided_at=%s WHERE id=%s
                    """,
                    (
                        prepared.decision.public_summary,
                        settlement.existing_source.fact_id,
                        settlement.target.fact_id,
                        decided_at,
                        prepared.candidate_id,
                    ),
                )
                return AcceptedMemoryGovernance(
                    prepared.candidate_id,
                    MemoryCandidateStatus.SKIPPED,
                    None,
                    duplicate_winner_fact_id=settlement.existing_source.fact_id,
                    relation_id=settlement.existing_relation_id,
                )

            existing = self._find_exact_relation(
                connection,
                source=source,
                target=target,
                relation_kind=settlement.relation_kind,
                supersede_mode=settlement.supersede_mode,
            )
            if existing is not None:
                # No mutation has occurred.  Return a freshly frozen carrier
                # with the exact foreign attribution observed in this cut.
                return prepare_existing_source_relation_settlement(
                    parent=prepared,
                    existing_source=self._memory_fact_settlement_identity(source),
                    target=self._memory_fact_settlement_identity(target),
                    disposition=(
                        ExistingSourceRelationDisposition.CONFIRM_EXISTING_RELATION
                    ),
                    existing_relation_id=str(existing["id"]),
                    existing_relation_decision_candidate_id=str(
                        existing["decision_candidate_id"]
                    ),
                    existing_relation_source_fact_id=str(existing["source_fact_id"]),
                    existing_relation_target_fact_id=str(existing["target_fact_id"]),
                )
            if (
                str(source["lifecycle"]) != "ACTIVE"
                or str(target["lifecycle"]) != "ACTIVE"
            ):
                raise ConversationKernelConflict(
                    "existing memory settlement endpoint is no longer active"
                )
            relation_id = self._insert_relation(
                connection,
                candidate_id=prepared.candidate_id,
                source=source,
                target=target,
                relation_kind=settlement.relation_kind,
                supersede_mode=settlement.supersede_mode,
                ordinal=None,
                accepted_at=decided_at,
            )
            if relation_id != settlement.prepared_relation_id:
                raise ConversationKernelConflict(
                    "existing memory relation identity changed"
                )
            if settlement.relation_kind is MemoryRelationKind.SUPERSEDES:
                connection.execute(
                    """
                    UPDATE pulsara_v3.memory_facts
                    SET lifecycle='SUPERSEDED', updated_at=%s WHERE id=%s
                    """,
                    (decided_at, target["id"]),
                )
            connection.execute(
                """
                UPDATE pulsara_v3.memory_candidates
                SET status='APPLIED_TO_EXISTING', decision_kind=%s,
                    final_kind=%s, decision_public_summary=%s,
                    related_target_fact_id=%s, applied_existing_fact_id=%s,
                    decided_at=%s WHERE id=%s
                """,
                (
                    prepared.decision.decision_kind.value,
                    fact.fact_kind.value,
                    prepared.decision.public_summary,
                    target["id"],
                    source["id"],
                    decided_at,
                    prepared.candidate_id,
                ),
            )
            return AcceptedMemoryGovernance(
                prepared.candidate_id,
                MemoryCandidateStatus.APPLIED_TO_EXISTING,
                None,
                duplicate_winner_fact_id=str(source["id"]),
                relation_id=relation_id,
            )

    @staticmethod
    def _memory_fact_settlement_identity(row) -> FrozenMemoryFactSettlementIdentity:
        return FrozenMemoryFactSettlementIdentity(
            fact_id=str(row["id"]),
            memory_domain_id=str(row["memory_domain_id"]),
            scope_kind=MemoryScopeKind(str(row["scope_kind"])),
            scope_id=str(row["scope_id"]),
            fact_kind=MemoryFactKind(str(row["fact_kind"])),
            statement=str(row["statement"]),
            applies_when=(
                None if row["applies_when"] is None else str(row["applies_when"])
            ),
            do_not_apply_when=tuple(str(value) for value in row["do_not_apply_when"]),
            fact_semantic_digest=str(row["fact_semantic_digest"]),
            expected_lifecycle=str(row["lifecycle"]),
        )

    @staticmethod
    def _memory_settlement_identity_matches(
        row, expected, *, include_lifecycle: bool
    ) -> bool:
        actual = _MemoryOperations._memory_fact_settlement_identity(row)
        if include_lifecycle:
            return actual == expected
        return (
            actual.fact_id == expected.fact_id
            and actual.memory_domain_id == expected.memory_domain_id
            and actual.scope_kind is expected.scope_kind
            and actual.scope_id == expected.scope_id
            and actual.fact_kind is expected.fact_kind
            and actual.statement == expected.statement
            and actual.applies_when == expected.applies_when
            and actual.do_not_apply_when == expected.do_not_apply_when
            and actual.fact_semantic_digest == expected.fact_semantic_digest
        )

    @staticmethod
    def _read_prepared_memory_candidate(
        connection: Connection, candidate_id: str
    ) -> PreparedMemoryCandidateAcceptance:
        row = connection.execute(
            "SELECT * FROM pulsara_v3.memory_candidates WHERE id=%s",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("memory candidate is absent")
        tool_rows = connection.execute(
            """
            SELECT origin_session_id, tool_result_id, ordinal, evidence_kind,
                   citation_visibility
            FROM pulsara_v3.memory_candidate_tool_result_refs
            WHERE candidate_id=%s ORDER BY ordinal
            """,
            (candidate_id,),
        ).fetchall()
        basis_rows = connection.execute(
            """
            SELECT target_fact_id, target_scope_kind, target_scope_id, ordinal
            FROM pulsara_v3.memory_candidate_basis_refs
            WHERE candidate_id=%s ORDER BY ordinal
            """,
            (candidate_id,),
        ).fetchall()
        proposal = FrozenMemoryProposal(
            statement=str(row["statement"]),
            scope_kind=MemoryScopeKind(str(row["scope_kind"])),
            scope_id=str(row["scope_id"]),
            kind_hint=MemoryKindHint(str(row["kind_hint"])),
            applies_when=None
            if row["applies_when"] is None
            else str(row["applies_when"]),
            do_not_apply_when=tuple(str(value) for value in row["do_not_apply_when"]),
            based_on_memory_ids=tuple(
                str(value["target_fact_id"]) for value in basis_rows
            ),
            cited_tool_result_handles=(),
        )
        return prepare_memory_candidate(
            candidate_id=str(row["id"]),
            memory_domain_id=str(row["memory_domain_id"]),
            origin_workspace_id=str(row["origin_workspace_id"]),
            origin_session_id=str(row["origin_session_id"]),
            producer_kind=MemoryProducerKind(str(row["producer_kind"])),
            producer_entry_id=row["producer_entry_id"],
            producer_tool_call_id=row["producer_tool_call_id"],
            trigger_user_entry_id=row["trigger_user_entry_id"],
            producer_candidate_ordinal=row["producer_candidate_ordinal"],
            proposal=proposal,
            tool_result_refs=tuple(
                PreparedMemoryToolResultReference(
                    str(item["origin_session_id"]),
                    str(item["tool_result_id"]),
                    int(item["ordinal"]),
                    MemoryCitationEvidenceKind(str(item["evidence_kind"])),
                    MemoryCitationVisibility(str(item["citation_visibility"])),
                )
                for item in tool_rows
            ),
            basis_refs=tuple(
                PreparedMemoryBasisReference(
                    str(item["target_fact_id"]),
                    MemoryScopeKind(str(item["target_scope_kind"])),
                    str(item["target_scope_id"]),
                    int(item["ordinal"]),
                )
                for item in basis_rows
            ),
            visible_memory=FrozenModelVisibleMemoryProvenance(
                ModelVisibleMemoryProvenanceDisposition(
                    str(row["model_visible_memory_provenance_disposition"])
                ),
                tuple(str(value) for value in row["model_visible_memory_fact_ids"]),
            ),
        )

    def _lock_processing_candidate(self, connection, prepared):
        row = connection.execute(
            "SELECT * FROM pulsara_v3.memory_candidates WHERE id=%s FOR UPDATE",
            (prepared.candidate_id,),
        ).fetchone()
        if (
            row is None
            or str(row["status"]) != "PROCESSING"
            or str(row["candidate_acceptance_digest"])
            != prepared.candidate_acceptance_digest
            or str(row["memory_domain_id"]) != prepared.memory_domain_id
            or str(row["origin_workspace_id"]) != prepared.origin_workspace_id
            or str(row["scope_kind"]) != prepared.scope_kind.value
            or str(row["scope_id"]) != prepared.scope_id
        ):
            raise ConversationKernelConflict("memory governance candidate head drifted")
        observed = self._read_prepared_memory_candidate(
            connection, prepared.candidate_id
        )
        if observed.candidate_acceptance_digest != prepared.candidate_acceptance_digest:
            raise ConversationKernelConflict(
                "memory governance immutable proposal identity drifted"
            )
        return row

    @staticmethod
    def _active_semantic_winner(connection, fact):
        return connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND scope_kind=%s AND scope_id=%s
              AND fact_semantic_digest=%s AND lifecycle='ACTIVE'
            FOR UPDATE
            """,
            (
                fact.memory_domain_id,
                fact.scope_kind.value,
                fact.scope_id,
                fact.fact_semantic_digest,
            ),
        ).fetchone()

    @staticmethod
    def _memory_fact_matches(row, fact) -> bool:
        return (
            str(row["memory_domain_id"]) == fact.memory_domain_id
            and str(row["scope_kind"]) == fact.scope_kind.value
            and str(row["scope_id"]) == fact.scope_id
            and str(row["fact_kind"]) == fact.fact_kind.value
            and str(row["statement"]) == fact.statement
            and row["applies_when"] == fact.applies_when
            and tuple(row["do_not_apply_when"]) == fact.do_not_apply_when
            and str(row["fact_semantic_digest"]) == fact.fact_semantic_digest
            and str(row["search_contract_id"]) == fact.search_contract_id
            and int(row["search_contract_version"]) == fact.search_contract_version
            and tuple(row["search_terms"]) == fact.search_terms
        )

    @staticmethod
    def _insert_memory_fact(connection, fact, accepted_at):
        connection.execute(
            """
            INSERT INTO pulsara_v3.memory_facts (
                id, memory_domain_id, scope_kind, scope_id, source_candidate_id,
                lifecycle, fact_kind, statement, applies_when, do_not_apply_when,
                fact_semantic_digest, accepted_at, updated_at,
                search_contract_id, search_contract_version, search_terms
            ) VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                fact.fact_id,
                fact.memory_domain_id,
                fact.scope_kind.value,
                fact.scope_id,
                fact.source_candidate_id,
                fact.fact_kind.value,
                fact.statement,
                fact.applies_when,
                list(fact.do_not_apply_when),
                fact.fact_semantic_digest,
                accepted_at,
                accepted_at,
                fact.search_contract_id,
                fact.search_contract_version,
                list(fact.search_terms),
            ),
        )

    def _lock_basis_targets(self, connection, prepared):
        rows = connection.execute(
            """
            SELECT r.ordinal, f.*
            FROM pulsara_v3.memory_candidate_basis_refs AS r
            JOIN pulsara_v3.memory_facts AS f
              ON f.memory_domain_id=r.memory_domain_id
             AND f.scope_kind=r.target_scope_kind
             AND f.scope_id=r.target_scope_id AND f.id=r.target_fact_id
            WHERE r.candidate_id=%s ORDER BY r.ordinal
            FOR UPDATE OF f
            """,
            (prepared.candidate_id,),
        ).fetchall()
        if len(rows) != len(prepared.basis_targets):
            raise ConversationKernelConflict("memory basis reference drifted")
        for ordinal, (row, expected) in enumerate(
            zip(rows, prepared.basis_targets, strict=True)
        ):
            if int(row["ordinal"]) != ordinal or not (
                self._memory_settlement_identity_matches(
                    row, expected, include_lifecycle=True
                )
            ):
                raise ConversationKernelConflict("memory basis reference drifted")
        return rows

    def _lock_governance_target(
        self, connection, prepared, *, allow_superseded_existing: bool = False
    ):
        target_id = prepared.decision.related_target_fact_id
        if target_id is None:
            return None
        row = connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND id=%s FOR UPDATE
            """,
            (prepared.memory_domain_id, target_id),
        ).fetchone()
        if row is None or (
            str(row["lifecycle"]) != "ACTIVE"
            and not (
                allow_superseded_existing
                and str(row["lifecycle"]) == "SUPERSEDED"
            )
        ):
            raise ConversationKernelConflict("memory relation target drifted")
        expected_target = prepared.target
        if expected_target is None or not self._memory_settlement_identity_matches(
            row,
            expected_target,
            include_lifecycle=not allow_superseded_existing,
        ):
            raise ConversationKernelConflict("memory relation target identity drifted")
        fact = prepared.fact
        assert fact is not None
        same_scope = (
            str(row["scope_kind"]) == fact.scope_kind.value
            and str(row["scope_id"]) == fact.scope_id
        )
        target_kind = MemoryFactKind(str(row["fact_kind"]))
        decision = prepared.decision
        if not same_scope:
            raise ConversationKernelConflict("memory relation crosses exact scope")
        if decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_CONTRADICT:
            if target_kind is not fact.fact_kind:
                raise ConversationKernelConflict("memory contradiction crosses kind")
        elif decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE:
            same_kind = target_kind is fact.fact_kind
            if (
                decision.supersede_mode
                is MemorySupersedeMode.SAME_KIND_REPLACEMENT
            ) != same_kind:
                raise ConversationKernelConflict("memory supersede mode/kind drifted")
        return row

    def _insert_governance_relations(
        self, connection, *, prepared, source_fact, target, basis, decided_at
    ):
        decision = prepared.decision
        relation_ids = []
        if decision.decision_kind is MemoryDecisionKind.ACCEPT:
            if source_fact.fact_kind is not MemoryFactKind.DECISION and basis:
                raise ConversationKernelConflict("non-decision memory owns basis refs")
            for expected, row in enumerate(basis):
                if int(row["ordinal"]) != expected or str(row["lifecycle"]) != "ACTIVE":
                    raise ConversationKernelConflict("memory basis reference drifted")
                relation_ids.append(
                    self._insert_relation(
                        connection,
                        candidate_id=prepared.candidate_id,
                        source=self._fact_draft_row(source_fact),
                        target=row,
                        relation_kind=MemoryRelationKind.BASED_ON,
                        supersede_mode=None,
                        ordinal=expected,
                        accepted_at=decided_at,
                    )
                )
            frozen_ids = tuple(item.relation_id for item in prepared.relation_drafts)
            if tuple(relation_ids) != frozen_ids:
                raise ConversationKernelConflict(
                    "memory BASED_ON relation draft drifted"
                )
            return tuple(relation_ids)
        assert target is not None
        kind = (
            MemoryRelationKind.SUPERSEDES
            if decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE
            else MemoryRelationKind.CONTRADICTS
        )
        relation_ids.append(
            self._insert_relation(
                connection,
                candidate_id=prepared.candidate_id,
                source=self._fact_draft_row(source_fact),
                target=target,
                relation_kind=kind,
                supersede_mode=decision.supersede_mode,
                ordinal=None,
                accepted_at=decided_at,
            )
        )
        if kind is MemoryRelationKind.SUPERSEDES:
            connection.execute(
                "UPDATE pulsara_v3.memory_facts SET lifecycle='SUPERSEDED', updated_at=%s WHERE id=%s",
                (decided_at, target["id"]),
            )
        if tuple(relation_ids) != tuple(
            item.relation_id for item in prepared.relation_drafts
        ):
            raise ConversationKernelConflict("memory relation draft drifted")
        return tuple(relation_ids)

    @staticmethod
    def _fact_draft_row(fact):
        return {
            "id": fact.fact_id,
            "memory_domain_id": fact.memory_domain_id,
            "scope_kind": fact.scope_kind.value,
            "scope_id": fact.scope_id,
            "fact_kind": fact.fact_kind.value,
            "fact_semantic_digest": fact.fact_semantic_digest,
        }

    @staticmethod
    def _insert_relation(
        connection,
        *,
        candidate_id,
        source,
        target,
        relation_kind,
        supersede_mode,
        ordinal,
        accepted_at,
    ):
        relation_id = memory_relation_id(
            memory_domain_id=str(source["memory_domain_id"]),
            source_scope_kind=MemoryScopeKind(str(source["scope_kind"])),
            source_scope_id=str(source["scope_id"]),
            source_fact_id=str(source["id"]),
            relation_kind=relation_kind,
            target_scope_kind=MemoryScopeKind(str(target["scope_kind"])),
            target_scope_id=str(target["scope_id"]),
            target_fact_id=str(target["id"]),
            supersede_mode=supersede_mode,
        )
        connection.execute(
            """
            INSERT INTO pulsara_v3.memory_relations (
                id, memory_domain_id, decision_candidate_id,
                source_scope_kind, source_scope_id, source_fact_id, source_fact_kind,
                relation_kind,
                target_scope_kind, target_scope_id, target_fact_id, target_fact_kind,
                supersede_mode, ordinal, accepted_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                relation_id,
                source["memory_domain_id"],
                candidate_id,
                source["scope_kind"],
                source["scope_id"],
                source["id"],
                source["fact_kind"],
                relation_kind.value,
                target["scope_kind"],
                target["scope_id"],
                target["id"],
                target["fact_kind"],
                None if supersede_mode is None else supersede_mode.value,
                ordinal,
                accepted_at,
            ),
        )
        return relation_id

    @staticmethod
    def _find_exact_relation(
        connection, *, source, target, relation_kind, supersede_mode
    ):
        if relation_kind is MemoryRelationKind.CONTRADICTS:
            return connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_relations
                WHERE memory_domain_id=%s AND relation_kind='CONTRADICTS'
                  AND source_scope_kind=%s AND source_scope_id=%s
                  AND target_scope_kind=%s AND target_scope_id=%s
                  AND least(source_fact_id, target_fact_id)=least(%s,%s)
                  AND greatest(source_fact_id, target_fact_id)=greatest(%s,%s)
                  AND supersede_mode IS NULL
                """,
                (
                    source["memory_domain_id"],
                    source["scope_kind"],
                    source["scope_id"],
                    target["scope_kind"],
                    target["scope_id"],
                    source["id"],
                    target["id"],
                    source["id"],
                    target["id"],
                ),
            ).fetchone()
        return connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_relations
            WHERE memory_domain_id=%s AND source_scope_kind=%s
              AND source_scope_id=%s AND source_fact_id=%s
              AND relation_kind=%s AND target_scope_kind=%s
              AND target_scope_id=%s AND target_fact_id=%s
              AND supersede_mode IS NOT DISTINCT FROM %s
            """,
            (
                source["memory_domain_id"],
                source["scope_kind"],
                source["scope_id"],
                source["id"],
                relation_kind.value,
                target["scope_kind"],
                target["scope_id"],
                target["id"],
                None if supersede_mode is None else supersede_mode.value,
            ),
        ).fetchone()

    @staticmethod
    def _confirm_existing_relation(connection, prepared):
        if (
            prepared.disposition
            is ExistingSourceRelationDisposition.APPLY_NEW_RELATION
        ):
            row = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_relations
                WHERE id=%s AND decision_candidate_id=%s
                """,
                (prepared.prepared_relation_id, prepared.candidate_id),
            ).fetchone()
            expected_decision_candidate_id = prepared.candidate_id
            expected_source = prepared.existing_source.fact_id
            expected_target = prepared.target.fact_id
        else:
            assert prepared.existing_relation_id is not None
            row = connection.execute(
                "SELECT * FROM pulsara_v3.memory_relations WHERE id=%s",
                (prepared.existing_relation_id,),
            ).fetchone()
            expected_decision_candidate_id = (
                prepared.existing_relation_decision_candidate_id
            )
            expected_source = prepared.existing_relation_source_fact_id
            expected_target = prepared.existing_relation_target_fact_id
        if row is None:
            return False
        actual_source = (
            str(row["memory_domain_id"]),
            str(row["source_scope_kind"]),
            str(row["source_scope_id"]),
            str(row["source_fact_id"]),
            str(row["source_fact_kind"]),
        )
        actual_target = (
            str(row["memory_domain_id"]),
            str(row["target_scope_kind"]),
            str(row["target_scope_id"]),
            str(row["target_fact_id"]),
            str(row["target_fact_kind"]),
        )
        prepared_source = (
            prepared.existing_source.memory_domain_id,
            prepared.existing_source.scope_kind.value,
            prepared.existing_source.scope_id,
            prepared.existing_source.fact_id,
            prepared.existing_source.fact_kind.value,
        )
        prepared_target = (
            prepared.target.memory_domain_id,
            prepared.target.scope_kind.value,
            prepared.target.scope_id,
            prepared.target.fact_id,
            prepared.target.fact_kind.value,
        )
        if prepared.relation_kind is MemoryRelationKind.CONTRADICTS:
            endpoints_match = tuple(sorted((actual_source, actual_target))) == tuple(
                sorted((prepared_source, prepared_target))
            )
        else:
            endpoints_match = (
                actual_source == prepared_source
                and actual_target == prepared_target
            )
        return (
            str(row["id"]) == prepared.prepared_relation_id
            and str(row["decision_candidate_id"])
            == expected_decision_candidate_id
            and str(row["source_fact_id"]) == expected_source
            and str(row["target_fact_id"]) == expected_target
            and str(row["relation_kind"]) == prepared.relation_kind.value
            and row["supersede_mode"]
            == (
                None
                if prepared.supersede_mode is None
                else prepared.supersede_mode.value
            )
            and endpoints_match
        )

    @staticmethod
    def _lock_response_preference_scope(connection, fact):
        key = (
            f"pulsara:memory-response-preference:{fact.memory_domain_id}:"
            f"{fact.scope_kind.value}:{fact.scope_id}"
        )
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,)
        )

    @staticmethod
    def _response_preference_capacity_allows(
        connection, fact, *, superseded_target, include_new: bool
    ):
        rows = connection.execute(
            """
            SELECT id, statement, scope_kind
            FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND scope_kind=%s AND scope_id=%s
              AND lifecycle='ACTIVE' AND fact_kind='RESPONSE_PREFERENCE'
            ORDER BY accepted_at, id FOR UPDATE
            """,
            (fact.memory_domain_id, fact.scope_kind.value, fact.scope_id),
        ).fetchall()
        values = [
            memory_response_preference_item_payload(
                memory_id=str(row["id"]),
                scope_kind=str(row["scope_kind"]),
                statement=str(row["statement"]),
            )
            for row in rows
            if superseded_target is None
            or str(row["id"]) != str(superseded_target["id"])
        ]
        if include_new:
            values.append(
                memory_response_preference_item_payload(
                    memory_id=fact.fact_id,
                    scope_kind=fact.scope_kind,
                    statement=fact.statement,
                )
            )
        return len(values) <= MAXIMUM_ACTIVE_RESPONSE_PREFERENCES_PER_SCOPE and len(
            canonical_json_bytes(values)
        ) <= MAXIMUM_RESPONSE_PREFERENCE_SCOPE_PROJECTION_BYTES


def _decode_governance_projection(
    raw: bytes, *, maximum_bytes: int, truncated: bool
) -> tuple[str, bool]:
    value = raw[:maximum_bytes]
    while value:
        try:
            return value.decode("utf-8"), truncated or len(raw) > len(value)
        except UnicodeDecodeError as exc:
            value = value[: exc.start]
    return "", truncated or bool(raw)


def _governance_public_text(value: str) -> str:
    # Governance receives semantic evidence, never private URLs or transport
    # handles.  Tool/user bodies remain otherwise opaque and are not parsed.
    return _PRIVATE_OR_REMOTE_URL.sub("[url redacted]", value)


__all__ = [
    "AcceptedMemoryGovernance",
    "MAXIMUM_ACTIVE_RESPONSE_PREFERENCES_PER_SCOPE",
    "MAXIMUM_RESPONSE_PREFERENCE_SCOPE_PROJECTION_BYTES",
    "_MemoryOperations",
]
