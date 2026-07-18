"""Durable all-or-none ownership for memory-governance candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Protocol, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event import (
    AgentEvent,
    MemoryCandidateEvidenceRejectedEvent,
    MemoryGovernanceBatchBlockedEvent,
    MemoryGovernanceBatchCompletedEvent,
    MemoryGovernanceBatchFailedEvent,
    MemoryGovernanceBatchPreparedEvent,
)
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    CandidatePool,
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
    candidate_from_storage_row,
    decision_from_storage_row,
    decision_target_entry_ids,
    _mark_candidate_evidence_rejected_with_cursor,
    pooled_candidate_row_fingerprint,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    GovernanceCandidateClaimStatus,
    MemoryGovernanceCandidateClaimFact,
)


@dataclass(frozen=True, slots=True)
class ClaimedGovernanceBatch:
    governance_batch_id: str
    candidates: tuple[PooledMemoryCandidate, ...]
    claims: tuple[MemoryGovernanceCandidateClaimFact, ...]

    def __post_init__(self) -> None:
        candidate_ids = tuple(item.entry_id for item in self.candidates)
        claim_ids = tuple(item.candidate_entry_id for item in self.claims)
        if candidate_ids != claim_ids or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("claimed governance candidates and claims do not join")
        if any(
            claim.governance_batch_id != self.governance_batch_id
            or claim.status is not GovernanceCandidateClaimStatus.PREPARING
            or claim.candidate_row_fingerprint
            != pooled_candidate_row_fingerprint(candidate)
            for candidate, claim in zip(self.candidates, self.claims, strict=True)
        ):
            raise ValueError("claimed governance batch identity drifted")


class MemoryGovernanceCandidateClaimRepository(Protocol):
    def claim_pending_batch(
        self,
        *,
        runtime_session_id: str,
        governance_batch_id: str,
        limit: int,
    ) -> ClaimedGovernanceBatch: ...

    def claims_for_batch(
        self, *, runtime_session_id: str, governance_batch_id: str
    ) -> tuple[MemoryGovernanceCandidateClaimFact, ...]: ...

    def open_batch_ids(
        self,
        *,
        runtime_session_id: str,
    ) -> tuple[str, ...]: ...

    def transition_companion(
        self,
        *,
        runtime_session_id: str,
        expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        target_status: GovernanceCandidateClaimStatus,
        terminal_record_id: str | None = None,
    ) -> "GovernanceClaimTransitionCompanion": ...


@dataclass(frozen=True, slots=True)
class GovernanceClaimTransitionCompanion:
    repository: (
        "InMemoryMemoryGovernanceCandidateClaimRepository"
        " | PostgresMemoryGovernanceCandidateClaimRepository"
    )
    runtime_session_id: str
    expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...]
    target_status: GovernanceCandidateClaimStatus
    terminal_record_id: str | None = None

    def __post_init__(self) -> None:
        if not self.expected_claims:
            raise ValueError("claim transition requires claims")
        if any(
            item.status
            not in {
                GovernanceCandidateClaimStatus.PREPARING,
                GovernanceCandidateClaimStatus.PREPARED,
            }
            for item in self.expected_claims
        ):
            raise ValueError("claim transition source status is terminal")
        if self.target_status is GovernanceCandidateClaimStatus.PREPARED:
            if self.terminal_record_id is not None or any(
                item.status is not GovernanceCandidateClaimStatus.PREPARING
                for item in self.expected_claims
            ):
                raise ValueError("prepared transition requires preparing claims")
        elif self.target_status is GovernanceCandidateClaimStatus.TERMINAL:
            if not self.terminal_record_id:
                raise ValueError("terminal transition requires terminal record")
        else:
            raise ValueError("unsupported claim transition target")

    def apply_postgres(
        self, cursor, stored_events: Sequence[AgentEvent]
    ) -> None:
        if not isinstance(
            self.repository, PostgresMemoryGovernanceCandidateClaimRepository
        ):
            raise TypeError("in-memory claims cannot join a PostgreSQL transaction")
        carrier = _transition_carrier(
            stored_events,
            target_status=self.target_status,
            terminal_record_id=self.terminal_record_id,
        )
        self.repository.transition_with_cursor(
            cursor,
            runtime_session_id=self.runtime_session_id,
            expected_claims=self.expected_claims,
            target_status=self.target_status,
            carrier_event_id=carrier.id,
        )
        if isinstance(carrier, MemoryCandidateEvidenceRejectedEvent):
            self.repository.insert_rejection_with_cursor(
                cursor,
                runtime_session_id=self.runtime_session_id,
                claim=_claim_for_candidate(
                    self.expected_claims,
                    carrier.rejection.candidate_entry_id,
                ),
                event=carrier,
            )

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        if not isinstance(
            self.repository, InMemoryMemoryGovernanceCandidateClaimRepository
        ):
            raise TypeError("PostgreSQL claims cannot join an in-memory transaction")
        carrier = _transition_carrier(
            stored_events,
            target_status=self.target_status,
            terminal_record_id=self.terminal_record_id,
        )
        self.repository.transition(
            runtime_session_id=self.runtime_session_id,
            expected_claims=self.expected_claims,
            target_status=self.target_status,
            carrier_event_id=carrier.id,
            rejection_event=(
                carrier
                if isinstance(carrier, MemoryCandidateEvidenceRejectedEvent)
                else None
            ),
        )


@dataclass(slots=True)
class InMemoryMemoryGovernanceCandidateClaimRepository:
    candidate_pool: CandidatePool
    _claims: dict[str, MemoryGovernanceCandidateClaimFact] = field(
        default_factory=dict
    )
    _rejections: dict[tuple[str, int], MemoryCandidateEvidenceRejectedEvent] = field(
        default_factory=dict
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def claim_pending_batch(
        self,
        *,
        runtime_session_id: str,
        governance_batch_id: str,
        limit: int,
    ) -> ClaimedGovernanceBatch:
        if limit < 1:
            raise ValueError("governance claim limit must be positive")
        with self._lock:
            existing = tuple(
                claim
                for claim in self._claims.values()
                if claim.governance_batch_id == governance_batch_id
                and claim.status is GovernanceCandidateClaimStatus.PREPARING
            )
            if existing:
                ordered = tuple(sorted(existing, key=lambda item: item.candidate_entry_id))
                candidates = tuple(
                    self.candidate_pool.get_candidate(item.candidate_entry_id)
                    for item in ordered
                )
                return ClaimedGovernanceBatch(
                    governance_batch_id=governance_batch_id,
                    candidates=candidates,
                    claims=ordered,
                )
            selected_candidates = tuple(
                candidate
                for candidate in self.candidate_pool.list_pending()
                if candidate.source_session_id == runtime_session_id
                and candidate.origin is not CandidateOrigin.GOVERNANCE
                and (
                    candidate.entry_id not in self._claims
                    or self._claims[candidate.entry_id].status
                    is GovernanceCandidateClaimStatus.RELEASED
                )
            )[:limit]
            candidates = tuple(
                sorted(selected_candidates, key=lambda item: item.entry_id)
            )
            claims = tuple(
                _new_claim(candidate, governance_batch_id, self._claims.get(candidate.entry_id))
                for candidate in candidates
            )
            for claim in claims:
                self._claims[claim.candidate_entry_id] = claim
            return ClaimedGovernanceBatch(
                governance_batch_id=governance_batch_id,
                candidates=candidates,
                claims=claims,
            )

    def claims_for_batch(
        self, *, runtime_session_id: str, governance_batch_id: str
    ) -> tuple[MemoryGovernanceCandidateClaimFact, ...]:
        del runtime_session_id
        with self._lock:
            return tuple(
                sorted(
                    (
                        claim
                        for claim in self._claims.values()
                        if claim.governance_batch_id == governance_batch_id
                    ),
                    key=lambda item: item.candidate_entry_id,
                )
            )

    def open_batch_ids(
        self,
        *,
        runtime_session_id: str,
    ) -> tuple[str, ...]:
        candidates = {
            candidate.entry_id: candidate
            for candidate in self.candidate_pool.list_candidates()
        }
        with self._lock:
            return tuple(
                sorted(
                    {
                        claim.governance_batch_id
                        for claim in self._claims.values()
                        if claim.status
                        in {
                            GovernanceCandidateClaimStatus.PREPARING,
                            GovernanceCandidateClaimStatus.PREPARED,
                        }
                        and candidates[claim.candidate_entry_id].source_session_id
                        == runtime_session_id
                    }
                )
            )

    def transition_companion(
        self,
        *,
        runtime_session_id: str,
        expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        target_status: GovernanceCandidateClaimStatus,
        terminal_record_id: str | None = None,
    ) -> GovernanceClaimTransitionCompanion:
        return GovernanceClaimTransitionCompanion(
            repository=self,
            runtime_session_id=runtime_session_id,
            expected_claims=expected_claims,
            target_status=target_status,
            terminal_record_id=terminal_record_id,
        )

    def transition(
        self,
        *,
        runtime_session_id: str,
        expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        target_status: GovernanceCandidateClaimStatus,
        carrier_event_id: str,
        rejection_event: MemoryCandidateEvidenceRejectedEvent | None = None,
    ) -> None:
        del runtime_session_id
        with self._lock:
            self._validate_transition_unlocked(expected_claims)
            rejection_key: tuple[str, int] | None = None
            if rejection_event is not None:
                rejection_claim = _claim_for_candidate(
                    expected_claims,
                    rejection_event.rejection.candidate_entry_id,
                )
                rejection_key = (
                    rejection_claim.candidate_entry_id,
                    rejection_claim.claim_generation,
                )
                existing = self._rejections.get(rejection_key)
                if existing is not None and existing != rejection_event:
                    raise ValueError("governance evidence rejection conflict")
                candidate_rejection = (
                    self.candidate_pool.evidence_rejection_event_id(
                        rejection_claim.candidate_entry_id
                    )
                )
                if (
                    candidate_rejection is not None
                    and candidate_rejection != rejection_event.id
                ):
                    raise ValueError("candidate evidence rejection identity conflict")
            self._apply_transition_unlocked(
                expected_claims,
                target_status=target_status,
                carrier_event_id=carrier_event_id,
            )
            if rejection_key is not None and rejection_event is not None:
                self._rejections[rejection_key] = rejection_event
                self.candidate_pool.mark_evidence_rejected(
                    entry_id=rejection_event.rejection.candidate_entry_id,
                    rejection_event_id=rejection_event.id,
                )

    def _validate_transition_unlocked(
        self,
        expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
    ) -> None:
        for expected in expected_claims:
            if self._claims.get(expected.candidate_entry_id) != expected:
                raise ValueError("governance claim CAS conflict")

    def _apply_transition_unlocked(
        self,
        expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        *,
        target_status: GovernanceCandidateClaimStatus,
        carrier_event_id: str,
    ) -> None:
        for expected in expected_claims:
            self._claims[expected.candidate_entry_id] = _transitioned_claim(
                expected,
                target_status=target_status,
                carrier_event_id=carrier_event_id,
            )


@dataclass(slots=True)
class PostgresMemoryGovernanceCandidateClaimRepository:
    dsn: str

    def __post_init__(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(MEMORY_GOVERNANCE_CLAIM_SCHEMA_SQL)

    def claim_pending_batch(
        self,
        *,
        runtime_session_id: str,
        governance_batch_id: str,
        limit: int,
    ) -> ClaimedGovernanceBatch:
        for attempt in range(4):
            try:
                return self._claim_pending_batch_once(
                    runtime_session_id=runtime_session_id,
                    governance_batch_id=governance_batch_id,
                    limit=limit,
                )
            except (
                psycopg.errors.DeadlockDetected,
                psycopg.errors.SerializationFailure,
            ):
                if attempt == 3:
                    raise
        raise AssertionError("unreachable governance claim retry state")

    def _claim_pending_batch_once(
        self,
        *,
        runtime_session_id: str,
        governance_batch_id: str,
        limit: int,
    ) -> ClaimedGovernanceBatch:
        if limit < 1:
            raise ValueError("governance claim limit must be positive")
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute("set transaction isolation level serializable")
                cursor.execute(
                    """
                    select claim_payload
                    from memory_governance_candidate_claims
                    where runtime_session_id = %s
                      and governance_batch_id = %s
                      and status = 'preparing'
                    order by candidate_entry_id
                    for update
                    """,
                    (runtime_session_id, governance_batch_id),
                )
                existing_rows = cursor.fetchall()
                if existing_rows:
                    claims = tuple(
                        MemoryGovernanceCandidateClaimFact.model_validate(
                            row["claim_payload"]
                        )
                        for row in existing_rows
                    )
                    cursor.execute(
                        """
                        select * from memory_candidates
                        where entry_id = any(%s)
                        order by entry_id
                        """,
                        ([item.candidate_entry_id for item in claims],),
                    )
                    candidates = tuple(
                        candidate_from_storage_row(row) for row in cursor.fetchall()
                    )
                    return ClaimedGovernanceBatch(
                        governance_batch_id=governance_batch_id,
                        candidates=candidates,
                        claims=claims,
                    )
                cursor.execute(
                    "select * from memory_governance_decisions order by decision_id"
                )
                decisions = tuple(
                    decision_from_storage_row(row) for row in cursor.fetchall()
                )
                terminal_ids = _terminal_candidate_ids(decisions)
                cursor.execute(
                    """
                    select candidate.*, claim.claim_payload as existing_claim_payload
                    from memory_candidates as candidate
                    left join memory_governance_candidate_claims as claim
                      on claim.candidate_entry_id = candidate.entry_id
                    where candidate.source_session_id = %s
                      and candidate.origin <> 'governance'
                      and candidate.evidence_rejection_event_id is null
                      and (claim.status is null or claim.status = 'released')
                    order by candidate.created_at, candidate.entry_id
                    for update of candidate
                    """,
                    (runtime_session_id,),
                )
                selected: list[
                    tuple[PooledMemoryCandidate, MemoryGovernanceCandidateClaimFact | None]
                ] = []
                for row in cursor.fetchall():
                    candidate = candidate_from_storage_row(row)
                    if candidate.entry_id in terminal_ids:
                        continue
                    previous = (
                        MemoryGovernanceCandidateClaimFact.model_validate(
                            row["existing_claim_payload"]
                        )
                        if row["existing_claim_payload"] is not None
                        else None
                    )
                    selected.append((candidate, previous))
                    if len(selected) == limit:
                        break
                selected.sort(key=lambda item: item[0].entry_id)
                candidates = tuple(item[0] for item in selected)
                claims = tuple(
                    _new_claim(candidate, governance_batch_id, previous)
                    for candidate, previous in selected
                )
                for claim in claims:
                    cursor.execute(
                        """
                        insert into memory_governance_candidate_claims (
                            candidate_entry_id, runtime_session_id,
                            candidate_row_fingerprint, governance_batch_id,
                            claim_generation, status, prepared_event_id,
                            terminal_record_id, previous_claim_fingerprint,
                            claim_fingerprint, claim_payload, updated_at
                        ) values (%s, %s, %s, %s, %s, %s, null, null, %s, %s, %s, now())
                        on conflict (candidate_entry_id) do update set
                            runtime_session_id = excluded.runtime_session_id,
                            candidate_row_fingerprint = excluded.candidate_row_fingerprint,
                            governance_batch_id = excluded.governance_batch_id,
                            claim_generation = excluded.claim_generation,
                            status = excluded.status,
                            prepared_event_id = null,
                            terminal_record_id = null,
                            previous_claim_fingerprint = excluded.previous_claim_fingerprint,
                            claim_fingerprint = excluded.claim_fingerprint,
                            claim_payload = excluded.claim_payload,
                            updated_at = now()
                        where memory_governance_candidate_claims.status = 'released'
                        """,
                        (
                            claim.candidate_entry_id,
                            runtime_session_id,
                            claim.candidate_row_fingerprint,
                            claim.governance_batch_id,
                            claim.claim_generation,
                            claim.status.value,
                            claim.previous_claim_fingerprint,
                            claim.claim_fingerprint,
                            Jsonb(claim.model_dump(mode="json")),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("governance candidate claim conflict")
        return ClaimedGovernanceBatch(
            governance_batch_id=governance_batch_id,
            candidates=candidates,
            claims=claims,
        )

    def claims_for_batch(
        self, *, runtime_session_id: str, governance_batch_id: str
    ) -> tuple[MemoryGovernanceCandidateClaimFact, ...]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select claim_payload
                    from memory_governance_candidate_claims
                    where runtime_session_id = %s and governance_batch_id = %s
                    order by candidate_entry_id
                    """,
                    (runtime_session_id, governance_batch_id),
                )
                return tuple(
                    MemoryGovernanceCandidateClaimFact.model_validate(
                        row["claim_payload"]
                    )
                    for row in cursor.fetchall()
                )

    def open_batch_ids(
        self,
        *,
        runtime_session_id: str,
    ) -> tuple[str, ...]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select distinct governance_batch_id
                    from memory_governance_candidate_claims
                    where runtime_session_id = %s
                      and status in ('preparing', 'prepared')
                    order by governance_batch_id
                    """,
                    (runtime_session_id,),
                )
                return tuple(str(row["governance_batch_id"]) for row in cursor.fetchall())

    def transition_companion(
        self,
        *,
        runtime_session_id: str,
        expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        target_status: GovernanceCandidateClaimStatus,
        terminal_record_id: str | None = None,
    ) -> GovernanceClaimTransitionCompanion:
        return GovernanceClaimTransitionCompanion(
            repository=self,
            runtime_session_id=runtime_session_id,
            expected_claims=expected_claims,
            target_status=target_status,
            terminal_record_id=terminal_record_id,
        )

    def transition_with_cursor(
        self,
        cursor,
        *,
        runtime_session_id: str,
        expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        target_status: GovernanceCandidateClaimStatus,
        carrier_event_id: str,
    ) -> None:
        for expected in expected_claims:
            next_claim = _transitioned_claim(
                expected,
                target_status=target_status,
                carrier_event_id=carrier_event_id,
            )
            cursor.execute(
                """
                update memory_governance_candidate_claims
                set status = %s,
                    prepared_event_id = %s,
                    terminal_record_id = %s,
                    claim_fingerprint = %s,
                    claim_payload = %s,
                    updated_at = now()
                where runtime_session_id = %s
                  and candidate_entry_id = %s
                  and governance_batch_id = %s
                  and claim_generation = %s
                  and status = %s
                  and claim_fingerprint = %s
                """,
                (
                    next_claim.status.value,
                    next_claim.prepared_event_id,
                    next_claim.terminal_record_id,
                    next_claim.claim_fingerprint,
                    Jsonb(next_claim.model_dump(mode="json")),
                    runtime_session_id,
                    expected.candidate_entry_id,
                    expected.governance_batch_id,
                    expected.claim_generation,
                    expected.status.value,
                    expected.claim_fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("governance claim transition CAS conflict")

    def insert_rejection_with_cursor(
        self,
        cursor,
        *,
        runtime_session_id: str,
        claim: MemoryGovernanceCandidateClaimFact,
        event: MemoryCandidateEvidenceRejectedEvent,
    ) -> None:
        cursor.execute(
            """
            insert into memory_candidate_evidence_rejections (
                runtime_session_id, candidate_entry_id, claim_generation,
                governance_batch_id, rejection_event_id, rejection_payload
            ) values (%s, %s, %s, %s, %s, %s)
            on conflict (candidate_entry_id, claim_generation) do nothing
            """,
            (
                runtime_session_id,
                claim.candidate_entry_id,
                claim.claim_generation,
                event.governance_batch_id,
                event.id,
                Jsonb(event.rejection.model_dump(mode="json")),
            ),
        )
        cursor.execute(
            """
            select governance_batch_id, rejection_event_id, rejection_payload
            from memory_candidate_evidence_rejections
            where candidate_entry_id = %s and claim_generation = %s
            """,
            (claim.candidate_entry_id, claim.claim_generation),
        )
        stored = cursor.fetchone()
        if stored is None or (
            stored["governance_batch_id"] != event.governance_batch_id
            or stored["rejection_event_id"] != event.id
            or stored["rejection_payload"] != event.rejection.model_dump(mode="json")
        ):
            raise ValueError("governance evidence rejection conflict")
        _mark_candidate_evidence_rejected_with_cursor(
            cursor,
            entry_id=claim.candidate_entry_id,
            rejection_event_id=event.id,
        )


def _new_claim(
    candidate: PooledMemoryCandidate,
    governance_batch_id: str,
    previous: MemoryGovernanceCandidateClaimFact | None,
) -> MemoryGovernanceCandidateClaimFact:
    if previous is not None and previous.status is not GovernanceCandidateClaimStatus.RELEASED:
        raise ValueError("only released claims may be reclaimed")
    return build_frozen_fact(
        MemoryGovernanceCandidateClaimFact,
        schema_version="memory_governance_candidate_claim.v1",
        candidate_entry_id=candidate.entry_id,
        candidate_row_fingerprint=pooled_candidate_row_fingerprint(candidate),
        governance_batch_id=governance_batch_id,
        claim_generation=1 if previous is None else previous.claim_generation + 1,
        status=GovernanceCandidateClaimStatus.PREPARING,
        prepared_event_id=None,
        terminal_record_id=None,
        previous_claim_fingerprint=(
            previous.claim_fingerprint if previous is not None else None
        ),
    )


def _transitioned_claim(
    claim: MemoryGovernanceCandidateClaimFact,
    *,
    target_status: GovernanceCandidateClaimStatus,
    carrier_event_id: str,
) -> MemoryGovernanceCandidateClaimFact:
    return build_frozen_fact(
        MemoryGovernanceCandidateClaimFact,
        schema_version="memory_governance_candidate_claim.v1",
        candidate_entry_id=claim.candidate_entry_id,
        candidate_row_fingerprint=claim.candidate_row_fingerprint,
        governance_batch_id=claim.governance_batch_id,
        claim_generation=claim.claim_generation,
        status=target_status,
        prepared_event_id=(
            carrier_event_id
            if target_status is GovernanceCandidateClaimStatus.PREPARED
            else claim.prepared_event_id
        ),
        terminal_record_id=(
            carrier_event_id
            if target_status is GovernanceCandidateClaimStatus.TERMINAL
            else None
        ),
        previous_claim_fingerprint=claim.previous_claim_fingerprint,
    )


def _transition_carrier(
    stored_events: Sequence[AgentEvent],
    *,
    target_status: GovernanceCandidateClaimStatus,
    terminal_record_id: str | None,
) -> AgentEvent:
    if target_status is GovernanceCandidateClaimStatus.PREPARED:
        matches = tuple(
            event
            for event in stored_events
            if isinstance(event, MemoryGovernanceBatchPreparedEvent)
        )
    else:
        matches = tuple(
            event
            for event in stored_events
            if event.id == terminal_record_id
            and isinstance(
                event,
                MemoryCandidateEvidenceRejectedEvent
                | MemoryGovernanceBatchCompletedEvent
                | MemoryGovernanceBatchFailedEvent
                | MemoryGovernanceBatchBlockedEvent,
            )
        )
    if len(matches) != 1:
        raise ValueError("claim transition lacks one matching carrier event")
    return matches[0]


def _terminal_candidate_ids(
    decisions: Sequence[MemoryGovernanceDecisionRecord],
) -> set[str]:
    result: set[str] = set()
    for record in decisions:
        if record.decision.kind == "skip" or record.write_outcome.kind == "write_succeeded":
            result.update(decision_target_entry_ids(record.decision))
    return result


MEMORY_GOVERNANCE_CLAIM_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_governance_candidate_claims (
    candidate_entry_id TEXT PRIMARY KEY REFERENCES memory_candidates(entry_id),
    runtime_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    candidate_row_fingerprint TEXT NOT NULL,
    governance_batch_id TEXT NOT NULL,
    claim_generation INTEGER NOT NULL CHECK (claim_generation >= 1),
    status TEXT NOT NULL CHECK (status IN ('preparing', 'prepared', 'terminal', 'released')),
    prepared_event_id TEXT,
    terminal_record_id TEXT,
    previous_claim_fingerprint TEXT,
    claim_fingerprint TEXT NOT NULL,
    claim_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_governance_claims_batch
    ON memory_governance_candidate_claims(runtime_session_id, governance_batch_id, status);

CREATE TABLE IF NOT EXISTS memory_candidate_evidence_rejections (
    runtime_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    candidate_entry_id TEXT NOT NULL REFERENCES memory_candidates(entry_id),
    claim_generation INTEGER NOT NULL CHECK (claim_generation >= 1),
    governance_batch_id TEXT NOT NULL,
    rejection_event_id TEXT NOT NULL,
    rejection_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (candidate_entry_id, claim_generation)
);

CREATE INDEX IF NOT EXISTS idx_memory_candidate_evidence_rejections_session
    ON memory_candidate_evidence_rejections(runtime_session_id, created_at);
""".strip()


def _claim_for_candidate(
    claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
    candidate_entry_id: str,
) -> MemoryGovernanceCandidateClaimFact:
    matches = tuple(
        claim for claim in claims if claim.candidate_entry_id == candidate_entry_id
    )
    if len(matches) != 1:
        raise ValueError("governance rejection lacks one matching claim")
    return matches[0]


__all__ = [
    "ClaimedGovernanceBatch",
    "GovernanceClaimTransitionCompanion",
    "InMemoryMemoryGovernanceCandidateClaimRepository",
    "MemoryGovernanceCandidateClaimRepository",
    "PostgresMemoryGovernanceCandidateClaimRepository",
]
