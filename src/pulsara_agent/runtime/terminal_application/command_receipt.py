"""Durable stable command receipts for renderer-neutral terminal clients."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock
from time import monotonic
from typing import Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event_log.in_memory import InMemoryEventLog
from pulsara_agent.event_log.postgres import PostgresEventLog
from pulsara_agent.event_log.postgres_pool import (
    PostgresConnectionLane,
    postgres_event_connection,
)
from pulsara_agent.ports.terminal_application import TerminalCommandOutcome
from pulsara_agent.primitives.context import context_fingerprint


COMMAND_RECEIPT_SCHEMA_VERSION = "terminal_command_receipt.v1"


@dataclass(frozen=True, slots=True)
class TerminalCommandReceipt:
    runtime_session_id: str
    client_instance_id: str
    command_id: str
    command_kind: str
    request_semantic_fingerprint: str
    target_id: str
    target_generation: int
    receipt_revision: int
    outcome: TerminalCommandOutcome
    receipt_fingerprint: str

    def __post_init__(self) -> None:
        if self.receipt_revision < 1:
            raise ValueError("terminal command receipt revision is invalid")
        expected = _receipt_fingerprint(
            runtime_session_id=self.runtime_session_id,
            client_instance_id=self.client_instance_id,
            command_id=self.command_id,
            command_kind=self.command_kind,
            request_semantic_fingerprint=self.request_semantic_fingerprint,
            target_id=self.target_id,
            target_generation=self.target_generation,
            receipt_revision=self.receipt_revision,
            outcome=self.outcome,
        )
        if self.receipt_fingerprint != expected:
            raise ValueError("terminal command receipt fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class TerminalCommandAdmission:
    receipt: TerminalCommandReceipt
    execution_owner_won: bool


class TerminalCommandReceiptStorage(Protocol):
    def admit_pending(
        self,
        *,
        runtime_session_id: str,
        client_instance_id: str,
        command_id: str,
        command_kind: str,
        request_semantic_fingerprint: str,
        target_id: str,
        target_generation: int,
        pending_outcome: TerminalCommandOutcome,
        deadline_monotonic: float,
    ) -> TerminalCommandAdmission: ...

    def complete(
        self,
        *,
        runtime_session_id: str,
        client_instance_id: str,
        command_id: str,
        request_semantic_fingerprint: str,
        outcome: TerminalCommandOutcome,
        deadline_monotonic: float,
    ) -> TerminalCommandReceipt: ...

    def query(
        self,
        *,
        runtime_session_id: str,
        client_instance_id: str,
        command_id: str,
        deadline_monotonic: float,
    ) -> TerminalCommandReceipt | None: ...

    def list_pending(
        self,
        *,
        runtime_session_id: str,
        maximum_items: int,
        deadline_monotonic: float,
    ) -> tuple[TerminalCommandReceipt, ...]: ...


@dataclass(slots=True)
class InMemoryTerminalCommandReceiptStorage:
    records: dict[tuple[str, str, str], TerminalCommandReceipt]
    lock: Lock

    def admit_pending(self, **values) -> TerminalCommandAdmission:
        values.pop("deadline_monotonic")
        key = (
            values["runtime_session_id"],
            values["client_instance_id"],
            values["command_id"],
        )
        with self.lock:
            existing = self.records.get(key)
            if existing is not None:
                _validate_existing(existing, values)
                return TerminalCommandAdmission(existing, False)
            receipt = _build_receipt(
                receipt_revision=1, outcome=values.pop("pending_outcome"), **values
            )
            self.records[key] = receipt
            return TerminalCommandAdmission(receipt, True)

    def complete(self, **values) -> TerminalCommandReceipt:
        values.pop("deadline_monotonic")
        key = (
            values["runtime_session_id"],
            values["client_instance_id"],
            values["command_id"],
        )
        with self.lock:
            existing = self.records.get(key)
            if existing is None:
                raise RuntimeError("terminal command completion lacks admission")
            if (
                existing.request_semantic_fingerprint
                != values["request_semantic_fingerprint"]
            ):
                raise ValueError("terminal command completion conflicts with admission")
            outcome = values["outcome"]
            if existing.outcome.status != "pending_confirmation":
                if existing.outcome != outcome:
                    raise ValueError("terminal command has a different durable winner")
                return existing
            receipt = _build_receipt(
                runtime_session_id=existing.runtime_session_id,
                client_instance_id=existing.client_instance_id,
                command_id=existing.command_id,
                command_kind=existing.command_kind,
                request_semantic_fingerprint=existing.request_semantic_fingerprint,
                target_id=existing.target_id,
                target_generation=existing.target_generation,
                receipt_revision=existing.receipt_revision + 1,
                outcome=outcome,
            )
            self.records[key] = receipt
            return receipt

    def query(self, **values) -> TerminalCommandReceipt | None:
        values.pop("deadline_monotonic")
        if values["runtime_session_id"] == "":
            raise ValueError("terminal command query session is required")
        with self.lock:
            return self.records.get(
                (
                    values["runtime_session_id"],
                    values["client_instance_id"],
                    values["command_id"],
                )
            )

    def list_pending(self, **values) -> tuple[TerminalCommandReceipt, ...]:
        values.pop("deadline_monotonic")
        runtime_session_id = str(values["runtime_session_id"])
        maximum_items = int(values["maximum_items"])
        if not runtime_session_id or not 1 <= maximum_items <= 4_096:
            raise ValueError("terminal pending-command query is malformed")
        with self.lock:
            pending = tuple(
                receipt
                for key, receipt in sorted(self.records.items())
                if key[0] == runtime_session_id
                and receipt.outcome.status == "pending_confirmation"
            )
        return pending[:maximum_items]


@dataclass(slots=True)
class PostgresTerminalCommandReceiptStorage:
    event_log: PostgresEventLog

    def admit_pending(self, **values) -> TerminalCommandAdmission:
        deadline = float(values.pop("deadline_monotonic"))
        pending = values.pop("pending_outcome")
        candidate = _build_receipt(receipt_revision=1, outcome=pending, **values)
        with self._connection(deadline) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                _apply_deadline(cursor, deadline)
                cursor.execute(
                    """
                    insert into terminal_command_receipts (
                        session_id, client_instance_id, command_id, command_kind,
                        request_semantic_fingerprint, target_id, target_generation,
                        receipt_revision, outcome_payload, receipt_fingerprint,
                        updated_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    on conflict (session_id, client_instance_id, command_id)
                    do nothing
                    returning session_id
                    """,
                    (
                        candidate.runtime_session_id,
                        candidate.client_instance_id,
                        candidate.command_id,
                        candidate.command_kind,
                        candidate.request_semantic_fingerprint,
                        candidate.target_id,
                        candidate.target_generation,
                        candidate.receipt_revision,
                        Jsonb(asdict(candidate.outcome)),
                        candidate.receipt_fingerprint,
                    ),
                )
                owner_won = cursor.fetchone() is not None
                receipt = candidate if owner_won else self._select(cursor, values)
        if receipt is None:
            raise RuntimeError("terminal command admission winner disappeared")
        _validate_existing(receipt, {**values, "pending_outcome": pending})
        return TerminalCommandAdmission(receipt, owner_won)

    def complete(self, **values) -> TerminalCommandReceipt:
        deadline = float(values.pop("deadline_monotonic"))
        outcome = values["outcome"]
        with self._connection(deadline) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                _apply_deadline(cursor, deadline)
                existing = self._select(cursor, values, for_update=True)
                if existing is None:
                    raise RuntimeError("terminal command completion lacks admission")
                if (
                    existing.request_semantic_fingerprint
                    != values["request_semantic_fingerprint"]
                ):
                    raise ValueError(
                        "terminal command completion conflicts with admission"
                    )
                if existing.outcome.status != "pending_confirmation":
                    if existing.outcome != outcome:
                        raise ValueError(
                            "terminal command has a different durable winner"
                        )
                    return existing
                receipt = _build_receipt(
                    runtime_session_id=existing.runtime_session_id,
                    client_instance_id=existing.client_instance_id,
                    command_id=existing.command_id,
                    command_kind=existing.command_kind,
                    request_semantic_fingerprint=existing.request_semantic_fingerprint,
                    target_id=existing.target_id,
                    target_generation=existing.target_generation,
                    receipt_revision=existing.receipt_revision + 1,
                    outcome=outcome,
                )
                cursor.execute(
                    """
                    update terminal_command_receipts
                    set receipt_revision = %s,
                        outcome_payload = %s,
                        receipt_fingerprint = %s,
                        updated_at = now()
                    where session_id = %s
                      and client_instance_id = %s
                      and command_id = %s
                      and receipt_revision = %s
                    """,
                    (
                        receipt.receipt_revision,
                        Jsonb(asdict(receipt.outcome)),
                        receipt.receipt_fingerprint,
                        receipt.runtime_session_id,
                        receipt.client_instance_id,
                        receipt.command_id,
                        existing.receipt_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("terminal command completion CAS lost")
                return receipt

    def query(self, **values) -> TerminalCommandReceipt | None:
        deadline = float(values.pop("deadline_monotonic"))
        with self._connection(deadline, write=False) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                _apply_deadline(cursor, deadline)
                return self._select(cursor, values)

    def list_pending(self, **values) -> tuple[TerminalCommandReceipt, ...]:
        deadline = float(values.pop("deadline_monotonic"))
        runtime_session_id = str(values["runtime_session_id"])
        maximum_items = int(values["maximum_items"])
        if not runtime_session_id or not 1 <= maximum_items <= 4_096:
            raise ValueError("terminal pending-command query is malformed")
        with self._connection(deadline, write=False) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                _apply_deadline(cursor, deadline)
                cursor.execute(
                    """
                    select session_id, client_instance_id, command_id, command_kind,
                           request_semantic_fingerprint, target_id, target_generation,
                           receipt_revision, outcome_payload, receipt_fingerprint
                    from terminal_command_receipts
                    where session_id = %s
                      and outcome_payload ->> 'status' = 'pending_confirmation'
                    order by updated_at, client_instance_id, command_id
                    limit %s
                    """,
                    (runtime_session_id, maximum_items),
                )
                return tuple(_receipt_from_row(row) for row in cursor.fetchall())

    def _select(self, cursor, values, *, for_update: bool = False):
        cursor.execute(
            f"""
            select session_id, client_instance_id, command_id, command_kind,
                   request_semantic_fingerprint, target_id, target_generation,
                   receipt_revision, outcome_payload, receipt_fingerprint
            from terminal_command_receipts
            where session_id = %s and client_instance_id = %s and command_id = %s
            {"for update" if for_update else ""}
            """,
            (
                values["runtime_session_id"],
                values["client_instance_id"],
                values["command_id"],
            ),
        )
        row = cursor.fetchone()
        return None if row is None else _receipt_from_row(row)

    def _connection(self, deadline: float, *, write: bool = True):
        return postgres_event_connection(
            self.event_log.connection_provider,
            lane=(
                PostgresConnectionLane.CRITICAL_WRITE
                if write
                else PostgresConnectionLane.BOUNDED_READ
            ),
            deadline_monotonic=deadline,
        )


def build_terminal_command_receipt_storage(event_log) -> TerminalCommandReceiptStorage:
    in_memory_records = getattr(event_log, "_terminal_command_receipts", None)
    in_memory_lock = getattr(event_log, "_lock", None)
    if isinstance(event_log, InMemoryEventLog) or (
        isinstance(in_memory_records, dict) and in_memory_lock is not None
    ):
        return InMemoryTerminalCommandReceiptStorage(
            records=in_memory_records,
            lock=in_memory_lock,
        )
    if isinstance(event_log, PostgresEventLog):
        return PostgresTerminalCommandReceiptStorage(event_log)
    raise TypeError("unsupported EventLog for terminal command receipts")


def _build_receipt(*, receipt_revision: int, outcome: TerminalCommandOutcome, **values):
    payload = {
        "runtime_session_id": values["runtime_session_id"],
        "client_instance_id": values["client_instance_id"],
        "command_id": values["command_id"],
        "command_kind": values["command_kind"],
        "request_semantic_fingerprint": values["request_semantic_fingerprint"],
        "target_id": values["target_id"],
        "target_generation": values["target_generation"],
        "receipt_revision": receipt_revision,
        "outcome": outcome,
    }
    return TerminalCommandReceipt(
        **payload,
        receipt_fingerprint=_receipt_fingerprint(**payload),
    )


def _receipt_fingerprint(**values) -> str:
    canonical = dict(values)
    outcome = canonical.get("outcome")
    if isinstance(outcome, TerminalCommandOutcome):
        canonical["outcome"] = asdict(outcome)
    return context_fingerprint(
        "terminal-command-receipt:v1",
        {"schema_version": COMMAND_RECEIPT_SCHEMA_VERSION, **canonical},
    )


def _receipt_from_row(row) -> TerminalCommandReceipt:
    outcome_payload = dict(row["outcome_payload"])
    outcome_payload["durable_reference_ids"] = tuple(
        outcome_payload["durable_reference_ids"]
    )
    outcome = TerminalCommandOutcome(**outcome_payload)
    return TerminalCommandReceipt(
        runtime_session_id=str(row["session_id"]),
        client_instance_id=str(row["client_instance_id"]),
        command_id=str(row["command_id"]),
        command_kind=str(row["command_kind"]),
        request_semantic_fingerprint=str(row["request_semantic_fingerprint"]),
        target_id=str(row["target_id"]),
        target_generation=int(row["target_generation"]),
        receipt_revision=int(row["receipt_revision"]),
        outcome=outcome,
        receipt_fingerprint=str(row["receipt_fingerprint"]),
    )


def _validate_existing(existing: TerminalCommandReceipt, values) -> None:
    expected = (
        values["runtime_session_id"],
        values["client_instance_id"],
        values["command_id"],
        values["command_kind"],
        values["request_semantic_fingerprint"],
        values["target_id"],
        values["target_generation"],
    )
    observed = (
        existing.runtime_session_id,
        existing.client_instance_id,
        existing.command_id,
        existing.command_kind,
        existing.request_semantic_fingerprint,
        existing.target_id,
        existing.target_generation,
    )
    if observed != expected:
        raise ValueError("terminal command identity conflicts with durable receipt")


def _apply_deadline(cursor, deadline: float) -> None:
    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        raise TimeoutError("terminal command receipt deadline expired")
    remaining_ms = int(max(1.0, remaining_seconds * 1000.0))
    cursor.execute(
        "select set_config('statement_timeout', %s, true)", (f"{remaining_ms}ms",)
    )
    cursor.execute(
        "select set_config('lock_timeout', %s, true)", (f"{remaining_ms}ms",)
    )


__all__ = [
    "TerminalCommandAdmission",
    "TerminalCommandReceipt",
    "TerminalCommandReceiptStorage",
    "build_terminal_command_receipt_storage",
]
