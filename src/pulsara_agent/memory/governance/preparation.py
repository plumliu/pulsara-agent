"""Durable locator for confirmed governance input artifacts awaiting Prepared."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from typing import Protocol, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event import (
    AgentEvent,
    MemoryGovernanceBatchBlockedEvent,
    MemoryGovernanceBatchCompletedEvent,
    MemoryGovernanceBatchFailedEvent,
    MemoryGovernanceBatchPreparedEvent,
)
from pulsara_agent.event_log.protocol import EventLogTransactionCompanion
from pulsara_agent.memory.governance.claims import (
    GovernanceClaimTransitionCompanion,
    InMemoryMemoryGovernanceCandidateClaimRepository,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.governance_evidence import (
    GovernanceCandidateClaimStatus,
    GovernanceBatchInputReferenceFact,
    MemoryGovernanceCandidateClaimFact,
)


class GovernanceBatchPreparationStatus(StrEnum):
    STAGED = "staged"
    PREPARED = "prepared"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class GovernanceBatchPreparationRecord:
    runtime_session_id: str
    governance_batch_id: str
    batch_input_reference: GovernanceBatchInputReferenceFact
    preparing_claims_fingerprint: str
    source_ledger_through_sequence: int
    resolved_model_call_id: str
    status: GovernanceBatchPreparationStatus
    prepared_event_id: str | None
    terminal_event_id: str | None
    record_fingerprint: str

    def __post_init__(self) -> None:
        if self.governance_batch_id != self.batch_input_reference.governance_batch_id:
            raise ValueError("governance preparation batch reference drifted")
        if self.source_ledger_through_sequence < 0:
            raise ValueError("governance preparation high-water cannot be negative")
        if self.status is GovernanceBatchPreparationStatus.STAGED:
            if self.prepared_event_id is not None or self.terminal_event_id is not None:
                raise ValueError("staged governance input cannot have lifecycle refs")
        elif self.status is GovernanceBatchPreparationStatus.PREPARED:
            if self.prepared_event_id is None or self.terminal_event_id is not None:
                raise ValueError("prepared governance input requires only Prepared ref")
        elif self.prepared_event_id is None or self.terminal_event_id is None:
            raise ValueError("terminal governance input requires both lifecycle refs")
        expected = context_fingerprint(
            "governance-batch-preparation-record:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "governance_batch_id": self.governance_batch_id,
                "batch_input_reference_fingerprint": (
                    self.batch_input_reference.reference_fingerprint
                ),
                "preparing_claims_fingerprint": self.preparing_claims_fingerprint,
                "source_ledger_through_sequence": self.source_ledger_through_sequence,
                "resolved_model_call_id": self.resolved_model_call_id,
                "status": self.status.value,
                "prepared_event_id": self.prepared_event_id,
                "terminal_event_id": self.terminal_event_id,
            },
        )
        if self.record_fingerprint != expected:
            raise ValueError("governance preparation record fingerprint mismatch")

    @classmethod
    def build(
        cls,
        *,
        runtime_session_id: str,
        reference: GovernanceBatchInputReferenceFact,
        claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        source_ledger_through_sequence: int,
        resolved_model_call_id: str,
    ) -> "GovernanceBatchPreparationRecord":
        if not claims or any(
            claim.governance_batch_id != reference.governance_batch_id
            for claim in claims
        ):
            raise ValueError("governance preparation claims do not join the artifact")
        claims_fingerprint = context_fingerprint(
            "governance-preparing-claims:v1",
            tuple(claim.claim_fingerprint for claim in claims),
        )
        payload = {
            "runtime_session_id": runtime_session_id,
            "governance_batch_id": reference.governance_batch_id,
            "batch_input_reference_fingerprint": reference.reference_fingerprint,
            "preparing_claims_fingerprint": claims_fingerprint,
            "source_ledger_through_sequence": source_ledger_through_sequence,
            "resolved_model_call_id": resolved_model_call_id,
            "status": GovernanceBatchPreparationStatus.STAGED.value,
            "prepared_event_id": None,
            "terminal_event_id": None,
        }
        return cls(
            runtime_session_id=runtime_session_id,
            governance_batch_id=reference.governance_batch_id,
            batch_input_reference=reference,
            preparing_claims_fingerprint=claims_fingerprint,
            source_ledger_through_sequence=source_ledger_through_sequence,
            resolved_model_call_id=resolved_model_call_id,
            status=GovernanceBatchPreparationStatus.STAGED,
            prepared_event_id=None,
            terminal_event_id=None,
            record_fingerprint=context_fingerprint(
                "governance-batch-preparation-record:v1", payload
            ),
        )


class GovernanceBatchPreparationRepository(Protocol):
    def stage(self, record: GovernanceBatchPreparationRecord) -> None: ...

    def get(
        self,
        *,
        runtime_session_id: str,
        governance_batch_id: str,
    ) -> GovernanceBatchPreparationRecord | None: ...

    def transition_companion(
        self,
        *,
        expected_record: GovernanceBatchPreparationRecord,
        claim_companion: GovernanceClaimTransitionCompanion,
        target_status: GovernanceBatchPreparationStatus,
        terminal_event_id: str | None = None,
    ) -> EventLogTransactionCompanion: ...


@dataclass(slots=True)
class InMemoryGovernanceBatchPreparationRepository:
    _records: dict[tuple[str, str], GovernanceBatchPreparationRecord] = field(
        default_factory=dict
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def stage(self, record: GovernanceBatchPreparationRecord) -> None:
        key = (record.runtime_session_id, record.governance_batch_id)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None and existing != record:
                raise ValueError("governance preparation locator conflict")
            self._records[key] = record

    def get(
        self,
        *,
        runtime_session_id: str,
        governance_batch_id: str,
    ) -> GovernanceBatchPreparationRecord | None:
        with self._lock:
            return self._records.get((runtime_session_id, governance_batch_id))

    def transition_companion(
        self,
        *,
        expected_record: GovernanceBatchPreparationRecord,
        claim_companion: GovernanceClaimTransitionCompanion,
        target_status: GovernanceBatchPreparationStatus,
        terminal_event_id: str | None = None,
    ) -> EventLogTransactionCompanion:
        return GovernanceBatchStateTransitionCompanion(
            repository=self,
            expected_record=expected_record,
            claim_companion=claim_companion,
            target_status=target_status,
            terminal_event_id=terminal_event_id,
        )

    def _validate_transition_unlocked(
        self,
        expected: GovernanceBatchPreparationRecord,
        target_status: GovernanceBatchPreparationStatus,
    ) -> None:
        key = (expected.runtime_session_id, expected.governance_batch_id)
        if self._records.get(key) != expected:
            raise ValueError("governance batch input state CAS conflict")
        _validate_status_transition(expected.status, target_status)

    def _apply_transition_unlocked(
        self,
        expected: GovernanceBatchPreparationRecord,
        *,
        target_status: GovernanceBatchPreparationStatus,
        carrier_event_id: str,
    ) -> None:
        key = (expected.runtime_session_id, expected.governance_batch_id)
        self._records[key] = transitioned_governance_batch_preparation_record(
            expected,
            target_status=target_status,
            carrier_event_id=carrier_event_id,
        )


@dataclass(slots=True)
class PostgresGovernanceBatchPreparationRepository:
    dsn: str

    def __post_init__(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(GOVERNANCE_BATCH_PREPARATION_SCHEMA_SQL)

    def stage(self, record: GovernanceBatchPreparationRecord) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into memory_governance_batch_inputs (
                        runtime_session_id, governance_batch_id,
                        batch_input_reference, preparing_claims_fingerprint,
                        source_ledger_through_sequence, resolved_model_call_id,
                        status, prepared_event_id, terminal_event_id,
                        record_fingerprint
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (runtime_session_id, governance_batch_id) do nothing
                    """,
                    (
                        record.runtime_session_id,
                        record.governance_batch_id,
                        Jsonb(record.batch_input_reference.model_dump(mode="json")),
                        record.preparing_claims_fingerprint,
                        record.source_ledger_through_sequence,
                        record.resolved_model_call_id,
                        record.status.value,
                        record.prepared_event_id,
                        record.terminal_event_id,
                        record.record_fingerprint,
                    ),
                )
                cursor.execute(
                    """
                    select batch_input_reference, preparing_claims_fingerprint,
                           source_ledger_through_sequence, resolved_model_call_id,
                           status, prepared_event_id, terminal_event_id,
                           record_fingerprint
                    from memory_governance_batch_inputs
                    where runtime_session_id = %s and governance_batch_id = %s
                    """,
                    (record.runtime_session_id, record.governance_batch_id),
                )
                stored = cursor.fetchone()
                if stored is None or (
                    GovernanceBatchInputReferenceFact.model_validate(
                        stored["batch_input_reference"]
                    )
                    != record.batch_input_reference
                    or stored["preparing_claims_fingerprint"]
                    != record.preparing_claims_fingerprint
                    or stored["source_ledger_through_sequence"]
                    != record.source_ledger_through_sequence
                    or stored["resolved_model_call_id"] != record.resolved_model_call_id
                    or stored["status"] != record.status.value
                    or stored["prepared_event_id"] != record.prepared_event_id
                    or stored["terminal_event_id"] != record.terminal_event_id
                    or stored["record_fingerprint"] != record.record_fingerprint
                ):
                    raise ValueError("governance preparation locator conflict")

    def get(
        self,
        *,
        runtime_session_id: str,
        governance_batch_id: str,
    ) -> GovernanceBatchPreparationRecord | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select batch_input_reference, preparing_claims_fingerprint,
                           source_ledger_through_sequence, resolved_model_call_id,
                           status, prepared_event_id, terminal_event_id,
                           record_fingerprint
                    from memory_governance_batch_inputs
                    where runtime_session_id = %s and governance_batch_id = %s
                    """,
                    (runtime_session_id, governance_batch_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return GovernanceBatchPreparationRecord(
            runtime_session_id=runtime_session_id,
            governance_batch_id=governance_batch_id,
            batch_input_reference=GovernanceBatchInputReferenceFact.model_validate(
                row["batch_input_reference"]
            ),
            preparing_claims_fingerprint=str(row["preparing_claims_fingerprint"]),
            source_ledger_through_sequence=int(row["source_ledger_through_sequence"]),
            resolved_model_call_id=str(row["resolved_model_call_id"]),
            status=GovernanceBatchPreparationStatus(str(row["status"])),
            prepared_event_id=(
                str(row["prepared_event_id"])
                if row["prepared_event_id"] is not None
                else None
            ),
            terminal_event_id=(
                str(row["terminal_event_id"])
                if row["terminal_event_id"] is not None
                else None
            ),
            record_fingerprint=str(row["record_fingerprint"]),
        )

    def transition_companion(
        self,
        *,
        expected_record: GovernanceBatchPreparationRecord,
        claim_companion: GovernanceClaimTransitionCompanion,
        target_status: GovernanceBatchPreparationStatus,
        terminal_event_id: str | None = None,
    ) -> EventLogTransactionCompanion:
        return GovernanceBatchStateTransitionCompanion(
            repository=self,
            expected_record=expected_record,
            claim_companion=claim_companion,
            target_status=target_status,
            terminal_event_id=terminal_event_id,
        )

    def transition_with_cursor(
        self,
        cursor,
        *,
        expected: GovernanceBatchPreparationRecord,
        target_status: GovernanceBatchPreparationStatus,
        carrier_event_id: str,
    ) -> None:
        next_record = transitioned_governance_batch_preparation_record(
            expected,
            target_status=target_status,
            carrier_event_id=carrier_event_id,
        )
        cursor.execute(
            """
            update memory_governance_batch_inputs
            set status = %s,
                prepared_event_id = %s,
                terminal_event_id = %s,
                record_fingerprint = %s,
                updated_at = now()
            where runtime_session_id = %s and governance_batch_id = %s
              and status = %s and record_fingerprint = %s
            """,
            (
                next_record.status.value,
                next_record.prepared_event_id,
                next_record.terminal_event_id,
                next_record.record_fingerprint,
                expected.runtime_session_id,
                expected.governance_batch_id,
                expected.status.value,
                expected.record_fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("governance batch input state CAS conflict")


@dataclass(frozen=True, slots=True)
class GovernanceBatchStateTransitionCompanion:
    repository: (
        InMemoryGovernanceBatchPreparationRepository
        | PostgresGovernanceBatchPreparationRepository
    )
    expected_record: GovernanceBatchPreparationRecord
    claim_companion: GovernanceClaimTransitionCompanion
    target_status: GovernanceBatchPreparationStatus
    terminal_event_id: str | None = None

    def __post_init__(self) -> None:
        _validate_status_transition(self.expected_record.status, self.target_status)
        if self.target_status is GovernanceBatchPreparationStatus.PREPARED:
            if (
                self.claim_companion.target_status
                is not GovernanceCandidateClaimStatus.PREPARED
                or self.terminal_event_id is not None
            ):
                raise ValueError("Prepared input transition requires Prepared claims")
        elif (
            self.claim_companion.target_status
            is not GovernanceCandidateClaimStatus.TERMINAL
            or not self.terminal_event_id
            or self.claim_companion.terminal_record_id != self.terminal_event_id
        ):
            raise ValueError("terminal input transition requires matching terminal claims")

    def apply_postgres(
        self,
        cursor,
        stored_events: Sequence[AgentEvent],
    ) -> None:
        if not isinstance(self.repository, PostgresGovernanceBatchPreparationRepository):
            raise TypeError("in-memory governance input cannot join PostgreSQL")
        carrier_id = _transition_carrier_id(
            stored_events,
            target_status=self.target_status,
            terminal_event_id=self.terminal_event_id,
        )
        self.claim_companion.apply_postgres(cursor, stored_events)
        self.repository.transition_with_cursor(
            cursor,
            expected=self.expected_record,
            target_status=self.target_status,
            carrier_event_id=carrier_id,
        )

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        if not isinstance(self.repository, InMemoryGovernanceBatchPreparationRepository):
            raise TypeError("PostgreSQL governance input cannot join in-memory ledger")
        claim_repository = self.claim_companion.repository
        if not isinstance(
            claim_repository, InMemoryMemoryGovernanceCandidateClaimRepository
        ):
            raise TypeError("in-memory governance input requires in-memory claims")
        carrier_id = _transition_carrier_id(
            stored_events,
            target_status=self.target_status,
            terminal_event_id=self.terminal_event_id,
        )
        with claim_repository._lock, self.repository._lock:
            self.repository._validate_transition_unlocked(
                self.expected_record,
                self.target_status,
            )
            claim_repository._validate_transition_unlocked(
                self.claim_companion.expected_claims
            )
            claim_repository._apply_transition_unlocked(
                self.claim_companion.expected_claims,
                target_status=self.claim_companion.target_status,
                carrier_event_id=carrier_id,
            )
            self.repository._apply_transition_unlocked(
                self.expected_record,
                target_status=self.target_status,
                carrier_event_id=carrier_id,
            )


GOVERNANCE_BATCH_PREPARATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_governance_batch_inputs (
    runtime_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    governance_batch_id TEXT NOT NULL,
    batch_input_reference JSONB NOT NULL,
    preparing_claims_fingerprint TEXT NOT NULL,
    source_ledger_through_sequence BIGINT NOT NULL CHECK (source_ledger_through_sequence >= 0),
    resolved_model_call_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged', 'prepared', 'terminal')),
    prepared_event_id TEXT,
    terminal_event_id TEXT,
    record_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (runtime_session_id, governance_batch_id)
);
""".strip()


def _validate_status_transition(
    source: GovernanceBatchPreparationStatus,
    target: GovernanceBatchPreparationStatus,
) -> None:
    legal = {
        GovernanceBatchPreparationStatus.STAGED: GovernanceBatchPreparationStatus.PREPARED,
        GovernanceBatchPreparationStatus.PREPARED: GovernanceBatchPreparationStatus.TERMINAL,
    }
    if legal.get(source) is not target:
        raise ValueError("illegal governance batch input state transition")


def transitioned_governance_batch_preparation_record(
    record: GovernanceBatchPreparationRecord,
    *,
    target_status: GovernanceBatchPreparationStatus,
    carrier_event_id: str,
) -> GovernanceBatchPreparationRecord:
    _validate_status_transition(record.status, target_status)
    payload = {
        "runtime_session_id": record.runtime_session_id,
        "governance_batch_id": record.governance_batch_id,
        "batch_input_reference_fingerprint": (
            record.batch_input_reference.reference_fingerprint
        ),
        "preparing_claims_fingerprint": record.preparing_claims_fingerprint,
        "source_ledger_through_sequence": record.source_ledger_through_sequence,
        "resolved_model_call_id": record.resolved_model_call_id,
        "status": target_status.value,
        "prepared_event_id": (
            carrier_event_id
            if target_status is GovernanceBatchPreparationStatus.PREPARED
            else record.prepared_event_id
        ),
        "terminal_event_id": (
            carrier_event_id
            if target_status is GovernanceBatchPreparationStatus.TERMINAL
            else None
        ),
    }
    return GovernanceBatchPreparationRecord(
        runtime_session_id=record.runtime_session_id,
        governance_batch_id=record.governance_batch_id,
        batch_input_reference=record.batch_input_reference,
        preparing_claims_fingerprint=record.preparing_claims_fingerprint,
        source_ledger_through_sequence=record.source_ledger_through_sequence,
        resolved_model_call_id=record.resolved_model_call_id,
        status=target_status,
        prepared_event_id=payload["prepared_event_id"],
        terminal_event_id=payload["terminal_event_id"],
        record_fingerprint=context_fingerprint(
            "governance-batch-preparation-record:v1", payload
        ),
    )


def _transition_carrier_id(
    stored_events: Sequence[AgentEvent],
    *,
    target_status: GovernanceBatchPreparationStatus,
    terminal_event_id: str | None,
) -> str:
    if target_status is GovernanceBatchPreparationStatus.PREPARED:
        matches = tuple(
            event
            for event in stored_events
            if isinstance(event, MemoryGovernanceBatchPreparedEvent)
        )
    else:
        matches = tuple(
            event
            for event in stored_events
            if event.id == terminal_event_id
            and isinstance(
                event,
                MemoryGovernanceBatchCompletedEvent
                | MemoryGovernanceBatchFailedEvent
                | MemoryGovernanceBatchBlockedEvent,
            )
        )
    if len(matches) != 1:
        raise ValueError("governance input transition lacks one carrier event")
    return matches[0].id


__all__ = [
    "GovernanceBatchPreparationRecord",
    "GovernanceBatchPreparationRepository",
    "GovernanceBatchPreparationStatus",
    "GovernanceBatchStateTransitionCompanion",
    "InMemoryGovernanceBatchPreparationRepository",
    "PostgresGovernanceBatchPreparationRepository",
    "transitioned_governance_batch_preparation_record",
]
