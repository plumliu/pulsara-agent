"""Durable governance-event handoff from the memory UOW to RuntimeSession."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import uuid4

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


class GovernanceEventDispatchError(RuntimeError):
    """A durable governance event batch has not reached the runtime ledger."""


@dataclass(frozen=True, slots=True)
class GovernanceEventDispatchTicket:
    outbox_id: str
    runtime_session_id: str
    governance_batch_id: str
    decision_id: str
    events: tuple[AgentEvent, ...]
    payload_fingerprint: str

    def __post_init__(self) -> None:
        if not self.events or any(event.sequence is not None for event in self.events):
            raise ValueError("governance outbox requires uncommitted event candidates")
        if len({event.id for event in self.events}) != len(self.events):
            raise ValueError("governance outbox event IDs must be unique")
        expected = _event_batch_fingerprint(self.events)
        if self.payload_fingerprint != expected:
            raise ValueError("governance outbox payload fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class _ClaimedGovernanceEventBatch:
    ticket: GovernanceEventDispatchTicket
    claim_token: str | None
    status: Literal["pending", "applied"]


@dataclass(slots=True)
class GovernanceEventOutboxRepository:
    """Stage one stable runtime-event batch inside the memory transaction."""

    connection: Connection
    runtime_session_id: str

    def append_batch(
        self,
        events: Sequence[AgentEvent],
        *,
        governance_batch_id: str,
        decision_id: str,
    ) -> GovernanceEventDispatchTicket:
        candidates = tuple(events)
        ticket = build_governance_event_dispatch_ticket(
            candidates,
            runtime_session_id=self.runtime_session_id,
            governance_batch_id=governance_batch_id,
            decision_id=decision_id,
        )
        fingerprint = ticket.payload_fingerprint
        outbox_id = ticket.outbox_id
        payload = [dump_agent_event(event) for event in candidates]
        event_ids = [event.id for event in candidates]
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                INSERT INTO memory_governance_event_outbox (
                    outbox_id,
                    runtime_session_id,
                    governance_batch_id,
                    decision_id,
                    event_ids,
                    events_payload,
                    payload_fingerprint
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (outbox_id) DO NOTHING
                """,
                (
                    outbox_id,
                    self.runtime_session_id,
                    governance_batch_id,
                    decision_id,
                    Jsonb(event_ids),
                    Jsonb(payload),
                    fingerprint,
                ),
            )
            cursor.execute(
                """
                SELECT runtime_session_id, governance_batch_id, decision_id,
                       event_ids, events_payload, payload_fingerprint
                FROM memory_governance_event_outbox
                WHERE outbox_id = %s
                """,
                (outbox_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise GovernanceEventDispatchError("governance event outbox insert vanished")
        durable = _ticket_from_row(outbox_id, row)
        if durable != ticket:
            raise GovernanceEventDispatchError(
                "governance event outbox identity conflict"
            )
        return durable


@dataclass(slots=True)
class EphemeralGovernanceEventOutboxRepository:
    """In-memory/test-double staging with the same stable ticket identity."""

    runtime_session_id: str

    def append_batch(
        self,
        events: Sequence[AgentEvent],
        *,
        governance_batch_id: str,
        decision_id: str,
    ) -> GovernanceEventDispatchTicket:
        return build_governance_event_dispatch_ticket(
            events,
            runtime_session_id=self.runtime_session_id,
            governance_batch_id=governance_batch_id,
            decision_id=decision_id,
        )


@dataclass(slots=True)
class PostgresGovernanceEventOutboxStore:
    dsn: str
    runtime_session_id: str
    claim_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.claim_seconds <= 0:
            raise ValueError("governance outbox claim duration must be positive")
        with psycopg.connect(self.dsn) as connection:
            connection.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    def claim(self, outbox_id: str | None = None) -> _ClaimedGovernanceEventBatch | None:
        claim_token = f"governance-event-claim:{uuid4().hex}"
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                if outbox_id is None:
                    selection = """
                        runtime_session_id = %s
                        AND status = 'pending'
                        AND (claimed_until IS NULL OR claimed_until <= now())
                    """
                    parameters = (self.runtime_session_id,)
                else:
                    selection = """
                        runtime_session_id = %s
                        AND outbox_id = %s
                        AND (
                            status = 'applied'
                            OR (
                                status = 'pending'
                                AND (claimed_until IS NULL OR claimed_until <= now())
                            )
                        )
                    """
                    parameters = (self.runtime_session_id, outbox_id)
                cursor.execute(
                    f"""
                    SELECT outbox_id, runtime_session_id, governance_batch_id,
                           decision_id, event_ids, events_payload,
                           payload_fingerprint, status
                    FROM memory_governance_event_outbox
                    WHERE {selection}
                    ORDER BY created_at, outbox_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    parameters,
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                ticket = _ticket_from_row(row["outbox_id"], row)
                if row["status"] == "applied":
                    return _ClaimedGovernanceEventBatch(
                        ticket=ticket,
                        claim_token=None,
                        status="applied",
                    )
                cursor.execute(
                    """
                    UPDATE memory_governance_event_outbox
                    SET claim_token = %s,
                        claimed_until = now() + (%s * interval '1 second'),
                        attempt_count = attempt_count + 1,
                        last_error_code = NULL
                    WHERE outbox_id = %s AND status = 'pending'
                    """,
                    (claim_token, self.claim_seconds, ticket.outbox_id),
                )
        return _ClaimedGovernanceEventBatch(
            ticket=ticket,
            claim_token=claim_token,
            status="pending",
        )

    def mark_applied(self, claim: _ClaimedGovernanceEventBatch) -> None:
        if claim.status == "applied":
            return
        with psycopg.connect(self.dsn) as connection:
            result = connection.execute(
                """
                UPDATE memory_governance_event_outbox
                SET status = 'applied',
                    claim_token = NULL,
                    claimed_until = NULL,
                    last_error_code = NULL,
                    applied_at = now()
                WHERE outbox_id = %s AND status = 'pending' AND claim_token = %s
                """,
                (claim.ticket.outbox_id, claim.claim_token),
            )
            if result.rowcount != 1:
                raise GovernanceEventDispatchError(
                    "governance event outbox lost its exact claim"
                )

    def mark_failed(
        self,
        claim: _ClaimedGovernanceEventBatch,
        *,
        error_code: str,
    ) -> None:
        if claim.status == "applied":
            return
        stable_code = error_code[:128] or "governance_event_dispatch_failed"
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                """
                UPDATE memory_governance_event_outbox
                SET claim_token = NULL,
                    claimed_until = NULL,
                    last_error_code = %s
                WHERE outbox_id = %s AND status = 'pending' AND claim_token = %s
                """,
                (stable_code, claim.ticket.outbox_id, claim.claim_token),
            )

    def has_pending(self) -> bool:
        with psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM memory_governance_event_outbox
                    WHERE runtime_session_id = %s AND status = 'pending'
                )
                """,
                (self.runtime_session_id,),
            ).fetchone()
        return bool(row and row[0])


@dataclass(slots=True)
class GovernanceEventOutboxDispatcher:
    store: PostgresGovernanceEventOutboxStore
    event_commit_port: Callable[[Sequence[AgentEvent]], Sequence[AgentEvent]]

    def dispatch_ticket(
        self,
        ticket: GovernanceEventDispatchTicket,
    ) -> tuple[AgentEvent, ...]:
        claim = self.store.claim(ticket.outbox_id)
        if claim is None:
            raise GovernanceEventDispatchError(
                "governance event outbox is owned by another dispatcher"
            )
        if claim.ticket != ticket:
            self.store.mark_failed(claim, error_code="outbox_ticket_mismatch")
            raise GovernanceEventDispatchError("governance event outbox ticket drifted")
        return self._dispatch_claim(claim)

    def dispatch_pending(self, *, limit: int = 100) -> tuple[AgentEvent, ...]:
        if limit < 1:
            raise ValueError("governance event dispatch limit must be positive")
        committed: list[AgentEvent] = []
        for _ in range(limit):
            claim = self.store.claim()
            if claim is None:
                break
            committed.extend(self._dispatch_claim(claim))
        return tuple(committed)

    def has_pending(self) -> bool:
        return self.store.has_pending()

    def _dispatch_claim(
        self,
        claim: _ClaimedGovernanceEventBatch,
    ) -> tuple[AgentEvent, ...]:
        try:
            committed = tuple(self.event_commit_port(claim.ticket.events))
            _validate_committed_batch(claim.ticket, committed)
            self.store.mark_applied(claim)
            return committed
        except BaseException as exc:
            self.store.mark_failed(claim, error_code=type(exc).__name__)
            raise


def _event_batch_fingerprint(events: Sequence[AgentEvent]) -> str:
    candidates = tuple(events)
    if not candidates:
        raise ValueError("governance event outbox batch cannot be empty")
    return context_fingerprint(
        "memory-governance-event-outbox:v1",
        tuple(dump_agent_event(event) for event in candidates),
    )


def build_governance_event_dispatch_ticket(
    events: Sequence[AgentEvent],
    *,
    runtime_session_id: str,
    governance_batch_id: str,
    decision_id: str,
) -> GovernanceEventDispatchTicket:
    candidates = tuple(events)
    fingerprint = _event_batch_fingerprint(candidates)
    return GovernanceEventDispatchTicket(
        outbox_id=_outbox_id(
            runtime_session_id=runtime_session_id,
            governance_batch_id=governance_batch_id,
            decision_id=decision_id,
            payload_fingerprint=fingerprint,
        ),
        runtime_session_id=runtime_session_id,
        governance_batch_id=governance_batch_id,
        decision_id=decision_id,
        events=candidates,
        payload_fingerprint=fingerprint,
    )


def _outbox_id(
    *,
    runtime_session_id: str,
    governance_batch_id: str,
    decision_id: str,
    payload_fingerprint: str,
) -> str:
    digest = sha256(
        "\x00".join(
            (
                runtime_session_id,
                governance_batch_id,
                decision_id,
                payload_fingerprint,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"memory_governance_events:{digest}"


def _ticket_from_row(outbox_id: str, row) -> GovernanceEventDispatchTicket:
    events = tuple(load_agent_event(payload) for payload in row["events_payload"])
    if tuple(row["event_ids"]) != tuple(event.id for event in events):
        raise GovernanceEventDispatchError("governance outbox event IDs drifted")
    return GovernanceEventDispatchTicket(
        outbox_id=outbox_id,
        runtime_session_id=row["runtime_session_id"],
        governance_batch_id=row["governance_batch_id"],
        decision_id=row["decision_id"],
        events=events,
        payload_fingerprint=row["payload_fingerprint"],
    )


def _validate_committed_batch(
    ticket: GovernanceEventDispatchTicket,
    committed: Sequence[AgentEvent],
) -> None:
    stored = tuple(committed)
    if tuple(event.id for event in stored) != tuple(
        event.id for event in ticket.events
    ):
        raise GovernanceEventDispatchError(
            "governance event commit returned another event batch"
        )
    for candidate, event in zip(ticket.events, stored, strict=True):
        if event.sequence is None or event.model_copy(update={"sequence": None}) != candidate:
            raise GovernanceEventDispatchError(
                "governance event commit payload differs from durable outbox"
            )


__all__ = [
    "GovernanceEventDispatchError",
    "GovernanceEventDispatchTicket",
    "GovernanceEventOutboxDispatcher",
    "GovernanceEventOutboxRepository",
    "EphemeralGovernanceEventOutboxRepository",
    "PostgresGovernanceEventOutboxStore",
    "build_governance_event_dispatch_ticket",
]
