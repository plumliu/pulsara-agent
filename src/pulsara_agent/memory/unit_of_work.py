"""Transactional memory write unit of work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from pulsara_agent.event import EventContext
from pulsara_agent.event.candidates import CandidatePayload
from pulsara_agent.graph import DEFAULT_GRAPH_ID
from pulsara_agent.graph.postgres import PostgresGraphStore
from pulsara_agent.memory.archive import InMemoryArchiveStore
from pulsara_agent.memory.candidate_pool import (
    CANDIDATE_POOL_SCHEMA_SQL,
    GovernanceDecision,
    GovernanceWriteOutcome,
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
    decision_target_entry_ids,
)
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.lifecycle import MemoryLifecycle
from pulsara_agent.memory.protocols import ArtifactStore
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.memory.write_service import MemoryWriteService
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL, RUNTIME_TRUTH_SCHEMA_SQL


@dataclass(slots=True)
class MemoryWriteUnitOfWork:
    """Bind canonical graph, decision rows, and outbox writes to one connection."""

    dsn: str
    runtime_session_id: str
    graph_id: str | None = None
    archive: ArtifactStore | None = None
    workspace_root: str | Path | None = None
    gate: MemoryWriteGate = field(default_factory=MemoryWriteGate)

    connection: Connection | None = field(init=False, default=None)
    graph: PostgresGraphStore = field(init=False)
    decisions: "CandidateDecisionRepository" = field(init=False)
    outbox: "OutboxRepository" = field(init=False)
    lifecycle: MemoryLifecycle = field(init=False)
    memory_write_service: MemoryWriteService = field(init=False)

    def __enter__(self) -> "MemoryWriteUnitOfWork":
        self.connection = psycopg.connect(self.dsn)
        with self.connection.cursor() as cursor:
            cursor.execute(RUNTIME_TRUTH_SCHEMA_SQL)
            cursor.execute(CANDIDATE_POOL_SCHEMA_SQL)
            cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)
        resolved_graph_id = self.graph_id or DEFAULT_GRAPH_ID
        self.graph = PostgresGraphStore(
            connection=self.connection,
            initialize_schema=False,
        )
        self.decisions = CandidateDecisionRepository(self.connection)
        self.outbox = OutboxRepository(self.connection)
        self.lifecycle = MemoryLifecycle(graph=self.graph, mutable=self.graph)
        ledger = ExecutionEvidenceLedger(
            graph=self.graph,
            archive=self.archive or InMemoryArchiveStore(),
            gate=self.gate,
            graph_id=resolved_graph_id,
        )
        self.memory_write_service = MemoryWriteService(ledger=ledger)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self.connection is not None
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
            self.connection = None

    @property
    def resolved_graph_id(self) -> str:
        return self.graph_id or DEFAULT_GRAPH_ID

    def ensure_event_context_rows(self, context: EventContext) -> None:
        """Create session/run/turn parent rows for synthetic governance events."""

        assert self.connection is not None
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sessions (id, workspace_root)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    self.runtime_session_id,
                    str(self.workspace_root) if self.workspace_root is not None else None,
                ),
            )
            cursor.execute(
                """
                INSERT INTO runs (id, session_id)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (context.run_id, self.runtime_session_id),
            )
            cursor.execute("SELECT session_id FROM runs WHERE id = %s", (context.run_id,))
            row = cursor.fetchone()
            if row is not None and row[0] != self.runtime_session_id:
                raise ValueError(
                    f"run_id {context.run_id!r} already belongs to runtime session {row[0]!r}"
                )
            cursor.execute(
                """
                INSERT INTO turns (id, session_id, run_id, turn_index)
                SELECT %s, %s, %s, coalesce(max(turn_index), 0) + 1
                FROM turns
                WHERE run_id = %s
                ON CONFLICT (id) DO NOTHING
                """,
                (context.turn_id, self.runtime_session_id, context.run_id, context.run_id),
            )
            cursor.execute("SELECT session_id, run_id FROM turns WHERE id = %s", (context.turn_id,))
            row = cursor.fetchone()
            if row is None:
                return
            session_id, run_id = row
            if session_id != self.runtime_session_id or run_id != context.run_id:
                raise ValueError(
                    f"turn_id {context.turn_id!r} already belongs to runtime session {session_id!r} "
                    f"and run {run_id!r}"
                )


@dataclass(slots=True)
class CandidateDecisionRepository:
    connection: Connection

    def append_candidate(self, candidate: PooledMemoryCandidate) -> PooledMemoryCandidate:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_candidates (
                    entry_id,
                    payload,
                    origin,
                    source_session_id,
                    source_run_id,
                    source_turn_id,
                    source_reply_id,
                    source_tool_call_id,
                    user_quote,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
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
                    candidate.created_at,
                ),
            )
        return candidate

    def append_decision(self, record: MemoryGovernanceDecisionRecord) -> MemoryGovernanceDecisionRecord:
        self._validate_decision_targets(record)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_governance_decisions (
                    decision_id,
                    governance_batch_id,
                    decision,
                    write_outcome,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s::timestamptz)
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

    def _validate_decision_targets(self, record: MemoryGovernanceDecisionRecord) -> None:
        target_ids = decision_target_entry_ids(record.decision)
        if not target_ids:
            return
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT entry_id
                FROM memory_candidates
                WHERE entry_id = ANY(%s)
                """,
                (list(target_ids),),
            )
            existing = {row["entry_id"] for row in cursor.fetchall()}
        missing = [entry_id for entry_id in target_ids if entry_id not in existing]
        if missing:
            raise KeyError(f"governance decision references missing candidate entries: {missing}")


@dataclass(slots=True)
class OutboxRepository:
    connection: Connection

    def append_decision(
        self,
        record: MemoryGovernanceDecisionRecord,
        *,
        graph_id: str,
        target_entry_key: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        outbox_id = f"outbox:{uuid4().hex}"
        target_key = target_entry_key or target_entry_key_for_decision(record.decision)
        body = payload or {
            "kind": "memory_governance_decision_committed",
            "decision_record": record.model_dump(mode="json"),
        }
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_write_outbox (
                    outbox_id,
                    graph_id,
                    governance_batch_id,
                    decision_id,
                    target_entry_key,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (governance_batch_id, decision_id) DO NOTHING
                """,
                (
                    outbox_id,
                    graph_id,
                    record.governance_batch_id,
                    record.decision_id,
                    target_key,
                    Jsonb(body),
                ),
            )
        return outbox_id


def target_entry_key_for_decision(decision: GovernanceDecision) -> str:
    target_ids = decision_target_entry_ids(decision)
    if len(target_ids) == 1:
        return target_ids[0]
    encoded = json.dumps(sorted(target_ids), ensure_ascii=True, separators=(",", ":"))
    return "merge:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_payload_adapter = TypeAdapter(CandidatePayload)
_decision_adapter = TypeAdapter(GovernanceDecision)
_outcome_adapter = TypeAdapter(GovernanceWriteOutcome)
