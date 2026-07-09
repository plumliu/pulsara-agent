"""Durable memory candidate pool.

The candidate pool is an append-only inbox for proposed durable memories. It is
not canonical semantic memory: only governance may turn a candidate into a
``mem:*`` graph node by calling ``MemoryWriteService``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pulsara_agent.event.candidates import CandidatePayload, MemoryCandidate
from pulsara_agent.event.events import utc_now
from pulsara_agent.ontology import memory


class CandidateOrigin(StrEnum):
    MAIN_AGENT_TOOL = "main_agent_tool"
    REFLECTION = "reflection"
    COMPACTION = "compaction"
    GOVERNANCE = "governance"


class PooledMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str = Field(default_factory=lambda: f"pool:{uuid4().hex}")
    payload: CandidatePayload
    origin: CandidateOrigin
    source_session_id: str
    source_run_id: str
    source_turn_id: str
    source_reply_id: str
    source_tool_call_id: str | None = None
    user_quote: str | None = None
    source_event_id: str | None = None
    source_artifact_id: str | None = None
    intent_fingerprint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class CandidatePoolProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: CandidatePayload
    origin: CandidateOrigin
    source_tool_call_id: str | None = None
    user_quote: str | None = None
    source_event_id: str | None = None
    source_artifact_id: str | None = None
    intent_fingerprint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_pooled(self, *, source_session_id: str, source_run_id: str, source_turn_id: str, source_reply_id: str) -> PooledMemoryCandidate:
        return PooledMemoryCandidate(
            payload=self.payload,
            origin=self.origin,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
            source_turn_id=source_turn_id,
            source_reply_id=source_reply_id,
            source_tool_call_id=self.source_tool_call_id,
            user_quote=self.user_quote,
            source_event_id=self.source_event_id,
            source_artifact_id=self.source_artifact_id,
            intent_fingerprint=self.intent_fingerprint,
            metadata=dict(self.metadata),
        )


class SkipDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["skip"] = "skip"
    target_entry_ids: tuple[str, ...]
    reason: str
    skip_reason: str | None = None


class SubmitAsIsDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["submit_as_is"] = "submit_as_is"
    target_entry_id: str
    reason: str


class CorrectAndSubmitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["correct_and_submit"] = "correct_and_submit"
    target_entry_id: str
    candidate: MemoryCandidate
    reason: str


class MergeAndSubmitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["merge_and_submit"] = "merge_and_submit"
    target_entry_ids: tuple[str, ...]
    candidate: MemoryCandidate
    reason: str


class SupersedeAndSubmitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["supersede_and_submit"] = "supersede_and_submit"
    target_entry_id: str
    candidate: MemoryCandidate
    superseded_memory_ids: tuple[str, ...]
    replacement_evidence_refs: tuple[str, ...] = ()
    reason: str


class ContradictAndSubmitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["contradict_and_submit"] = "contradict_and_submit"
    target_entry_id: str
    candidate: MemoryCandidate
    contradicted_memory_ids: tuple[str, ...]
    reason: str


GovernanceDecision = Annotated[
    SkipDecision
    | SubmitAsIsDecision
    | CorrectAndSubmitDecision
    | MergeAndSubmitDecision
    | SupersedeAndSubmitDecision
    | ContradictAndSubmitDecision,
    Field(discriminator="kind"),
]


class NoWriteOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["no_write"] = "no_write"


class WriteSucceededOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["write_succeeded"] = "write_succeeded"
    memory_id: str
    memory_type: str
    node_status: memory.NodeStatus
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    gate_reason: str
    write_event_ids: tuple[str, ...]
    superseded_memory_ids: tuple[str, ...] = ()
    contradicted_memory_ids: tuple[str, ...] = ()


class WriteFailedOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["write_failed"] = "write_failed"
    error_type: str
    message: str
    write_event_ids: tuple[str, ...]


GovernanceWriteOutcome = Annotated[
    NoWriteOutcome | WriteSucceededOutcome | WriteFailedOutcome,
    Field(discriminator="kind"),
]


class MemoryGovernanceDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(default_factory=lambda: f"decision:{uuid4().hex}")
    governance_batch_id: str
    decision: GovernanceDecision
    write_outcome: GovernanceWriteOutcome
    created_at: str = Field(default_factory=utc_now)


class CandidatePool(Protocol):
    def append_candidate(self, candidate: PooledMemoryCandidate) -> PooledMemoryCandidate: ...

    def get_candidate(self, entry_id: str) -> PooledMemoryCandidate: ...

    def list_candidates(self) -> list[PooledMemoryCandidate]: ...

    def list_pending(self, *, limit: int | None = None) -> list[PooledMemoryCandidate]: ...

    def append_decision(self, record: MemoryGovernanceDecisionRecord) -> MemoryGovernanceDecisionRecord: ...

    def list_decisions(self) -> list[MemoryGovernanceDecisionRecord]: ...


@dataclass(slots=True)
class InMemoryCandidatePool:
    _candidates: dict[str, PooledMemoryCandidate] = field(default_factory=dict)
    _decisions: list[MemoryGovernanceDecisionRecord] = field(default_factory=list)

    def append_candidate(self, candidate: PooledMemoryCandidate) -> PooledMemoryCandidate:
        if candidate.entry_id in self._candidates:
            raise ValueError(f"candidate pool entry already exists: {candidate.entry_id}")
        self._candidates[candidate.entry_id] = candidate.model_copy(deep=True)
        return candidate

    def get_candidate(self, entry_id: str) -> PooledMemoryCandidate:
        try:
            return self._candidates[entry_id].model_copy(deep=True)
        except KeyError as exc:
            raise KeyError(entry_id) from exc

    def list_candidates(self) -> list[PooledMemoryCandidate]:
        return [candidate.model_copy(deep=True) for candidate in self._candidates.values()]

    def list_pending(self, *, limit: int | None = None) -> list[PooledMemoryCandidate]:
        terminal = _terminal_entry_ids(self._decisions)
        pending = [
            candidate.model_copy(deep=True)
            for candidate in self._candidates.values()
            if candidate.entry_id not in terminal and candidate.origin != CandidateOrigin.GOVERNANCE
        ]
        if limit is not None:
            return pending[:limit]
        return pending

    def append_decision(self, record: MemoryGovernanceDecisionRecord) -> MemoryGovernanceDecisionRecord:
        if any(existing.decision_id == record.decision_id for existing in self._decisions):
            raise ValueError(f"governance decision already exists: {record.decision_id}")
        _validate_decision_targets(record, self._candidates.keys())
        self._decisions.append(record.model_copy(deep=True))
        return record

    def list_decisions(self) -> list[MemoryGovernanceDecisionRecord]:
        return [decision.model_copy(deep=True) for decision in self._decisions]


@dataclass(slots=True)
class PostgresCandidatePool:
    dsn: str

    def __post_init__(self) -> None:
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(CANDIDATE_POOL_SCHEMA_SQL)

    def append_candidate(self, candidate: PooledMemoryCandidate) -> PooledMemoryCandidate:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into memory_candidates (
                        entry_id,
                        payload,
                        origin,
                        source_session_id,
                        source_run_id,
                        source_turn_id,
                        source_reply_id,
                        source_tool_call_id,
                        user_quote,
                        source_event_id,
                        source_artifact_id,
                        intent_fingerprint,
                        metadata,
                        created_at
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                    """,
                    (
                        candidate.entry_id,
                        Jsonb(_payload_adapter.dump_python(candidate.payload, mode="json")),
                        candidate.origin.value,
                        candidate.source_session_id,
                        candidate.source_run_id,
                        candidate.source_turn_id,
                        candidate.source_reply_id,
                        candidate.source_tool_call_id,
                        candidate.user_quote,
                        candidate.source_event_id,
                        candidate.source_artifact_id,
                        candidate.intent_fingerprint,
                        Jsonb(candidate.metadata),
                        candidate.created_at,
                    ),
                )
        return candidate

    def get_candidate(self, entry_id: str) -> PooledMemoryCandidate:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select *
                    from memory_candidates
                    where entry_id = %s
                    """,
                    (entry_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise KeyError(entry_id)
        return _candidate_from_row(row)

    def list_candidates(self) -> list[PooledMemoryCandidate]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select *
                    from memory_candidates
                    order by created_at asc, entry_id asc
                    """
                )
                return [_candidate_from_row(row) for row in cursor.fetchall()]

    def list_pending(self, *, limit: int | None = None) -> list[PooledMemoryCandidate]:
        decisions = self.list_decisions()
        terminal = _terminal_entry_ids(decisions)
        pending = [
            candidate
            for candidate in self.list_candidates()
            if candidate.entry_id not in terminal and candidate.origin != CandidateOrigin.GOVERNANCE
        ]
        if limit is not None:
            return pending[:limit]
        return pending

    def append_decision(self, record: MemoryGovernanceDecisionRecord) -> MemoryGovernanceDecisionRecord:
        candidate_ids = {candidate.entry_id for candidate in self.list_candidates()}
        _validate_decision_targets(record, candidate_ids)
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into memory_governance_decisions (
                        decision_id,
                        governance_batch_id,
                        decision,
                        write_outcome,
                        created_at
                    )
                    values (%s, %s, %s, %s, %s::timestamptz)
                    """,
                    (
                        record.decision_id,
                        record.governance_batch_id,
                        Jsonb(_decision_adapter.dump_python(record.decision, mode="json")),
                        Jsonb(_outcome_adapter.dump_python(record.write_outcome, mode="json")),
                        record.created_at,
                    ),
                )
        return record

    def list_decisions(self) -> list[MemoryGovernanceDecisionRecord]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select *
                    from memory_governance_decisions
                    order by created_at asc, decision_id asc
                    """
                )
                return [_decision_from_row(row) for row in cursor.fetchall()]


def governance_batch_context(governance_batch_id: str):
    from pulsara_agent.event import EventContext

    return EventContext(
        run_id=f"run:governance/{governance_batch_id}",
        turn_id=f"turn:governance/{governance_batch_id}",
        reply_id=f"reply:governance/{governance_batch_id}",
    )


def new_governance_batch_id() -> str:
    return f"governance:{uuid4().hex}"


def decision_target_entry_ids(decision: GovernanceDecision) -> tuple[str, ...]:
    if isinstance(decision, SkipDecision | MergeAndSubmitDecision):
        return decision.target_entry_ids
    return (decision.target_entry_id,)


def _terminal_entry_ids(decisions: Sequence[MemoryGovernanceDecisionRecord]) -> set[str]:
    terminal: set[str] = set()
    for record in decisions:
        decision = record.decision
        outcome = record.write_outcome
        if isinstance(decision, SkipDecision):
            terminal.update(decision.target_entry_ids)
        elif isinstance(outcome, WriteSucceededOutcome):
            terminal.update(decision_target_entry_ids(decision))
    return terminal


def _validate_decision_targets(
    record: MemoryGovernanceDecisionRecord,
    candidate_ids: Iterable[str],
) -> None:
    existing = set(candidate_ids)
    missing = [entry_id for entry_id in decision_target_entry_ids(record.decision) if entry_id not in existing]
    if missing:
        raise KeyError(f"governance decision references missing candidate entries: {missing}")


def _candidate_from_row(row: dict[str, Any]) -> PooledMemoryCandidate:
    created_at = row["created_at"]
    return PooledMemoryCandidate(
        entry_id=row["entry_id"],
        payload=_payload_adapter.validate_python(row["payload"]),
        origin=CandidateOrigin(row["origin"]),
        source_session_id=row["source_session_id"],
        source_run_id=row["source_run_id"],
        source_turn_id=row["source_turn_id"],
        source_reply_id=row["source_reply_id"],
        source_tool_call_id=row["source_tool_call_id"],
        user_quote=row["user_quote"],
        source_event_id=row.get("source_event_id"),
        source_artifact_id=row.get("source_artifact_id"),
        intent_fingerprint=row.get("intent_fingerprint"),
        metadata=dict(row.get("metadata") or {}),
        created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
    )


def _decision_from_row(row: dict[str, Any]) -> MemoryGovernanceDecisionRecord:
    created_at = row["created_at"]
    return MemoryGovernanceDecisionRecord(
        decision_id=row["decision_id"],
        governance_batch_id=row["governance_batch_id"],
        decision=_decision_adapter.validate_python(row["decision"]),
        write_outcome=_outcome_adapter.validate_python(row["write_outcome"]),
        created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
    )


_payload_adapter = TypeAdapter(CandidatePayload)
_decision_adapter = TypeAdapter(GovernanceDecision)
_outcome_adapter = TypeAdapter(GovernanceWriteOutcome)


CANDIDATE_POOL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_candidates (
    entry_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    origin TEXT NOT NULL,
    source_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    source_reply_id TEXT NOT NULL,
    source_tool_call_id TEXT,
    user_quote TEXT,
    source_event_id TEXT,
    source_artifact_id TEXT,
    intent_fingerprint TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE memory_candidates
    ADD COLUMN IF NOT EXISTS source_event_id TEXT;

ALTER TABLE memory_candidates
    ADD COLUMN IF NOT EXISTS source_artifact_id TEXT;

ALTER TABLE memory_candidates
    ADD COLUMN IF NOT EXISTS intent_fingerprint TEXT;

ALTER TABLE memory_candidates
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_memory_candidates_source_run
    ON memory_candidates(source_run_id, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_candidates_origin
    ON memory_candidates(origin);

CREATE INDEX IF NOT EXISTS idx_memory_candidates_session_origin_fingerprint
    ON memory_candidates(source_session_id, origin, intent_fingerprint)
    WHERE intent_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_governance_decisions (
    decision_id TEXT PRIMARY KEY,
    governance_batch_id TEXT NOT NULL,
    decision JSONB NOT NULL,
    write_outcome JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_governance_decisions_batch
    ON memory_governance_decisions(governance_batch_id, created_at);
""".strip()
