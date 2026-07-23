"""Transactional producer-event to memory-candidate projection outbox."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Protocol, Sequence

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    MemoryReflectionCompletedEvent,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.memory.candidates.pool import CandidatePool, PooledMemoryCandidate
from pulsara_agent.primitives.governance_evidence import (
    CandidateProjectionOutboxItemFact,
    CandidateProjectionProducerKind,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import EventWriteResult, RuntimeSession


@dataclass(frozen=True, slots=True)
class CandidateProjectionOutboxRow:
    item: CandidateProjectionOutboxItemFact
    candidate: PooledMemoryCandidate

    def validate(self, *, runtime_session_id: str, producer_event: AgentEvent) -> None:
        if self.candidate.entry_id != self.item.candidate_entry_id:
            raise ValueError("candidate projection outbox entry ID drifted")
        if self.candidate.payload != self.item.candidate_payload:
            raise ValueError("candidate projection outbox payload drifted")
        identity = stable_event_identity(
            producer_event,
            runtime_session_id=runtime_session_id,
        )
        if self.item.producer_event_identity != identity:
            raise ValueError("candidate projection producer identity drifted")
        if self.candidate.source_session_id != runtime_session_id:
            raise ValueError("candidate projection crosses runtime sessions")


class CandidateProjectionOutboxRepository(Protocol):
    def transaction_companion(
        self,
        *,
        runtime_session_id: str,
        producer_event: AgentEvent,
        rows: tuple[CandidateProjectionOutboxRow, ...],
    ) -> "CandidateProjectionTransactionCompanion": ...

    def list_pending(
        self,
        *,
        runtime_session_id: str,
        limit: int = 100,
    ) -> tuple[CandidateProjectionOutboxRow, ...]: ...

    def mark_applied(
        self,
        *,
        runtime_session_id: str,
        producer_event_id: str,
        candidate_entry_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CandidateProjectionTransactionCompanion:
    repository: "InMemoryCandidateProjectionOutbox | PostgresCandidateProjectionOutbox"
    runtime_session_id: str
    producer_event: AgentEvent
    rows: tuple[CandidateProjectionOutboxRow, ...]

    def __post_init__(self) -> None:
        _validate_producer_rows(
            runtime_session_id=self.runtime_session_id,
            producer_event=self.producer_event,
            rows=self.rows,
        )

    def apply_postgres(
        self,
        cursor,
        stored_events: Sequence[AgentEvent],
    ) -> None:
        if not isinstance(self.repository, PostgresCandidateProjectionOutbox):
            raise TypeError("in-memory outbox cannot join a PostgreSQL transaction")
        stored = _stored_producer(self.producer_event.id, stored_events)
        _validate_producer_rows(
            runtime_session_id=self.runtime_session_id,
            producer_event=stored,
            rows=self.rows,
        )
        self.repository.insert_with_cursor(cursor, rows=self.rows)

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        if not isinstance(self.repository, InMemoryCandidateProjectionOutbox):
            raise TypeError("PostgreSQL outbox cannot join an in-memory transaction")
        stored = _stored_producer(self.producer_event.id, stored_events)
        _validate_producer_rows(
            runtime_session_id=self.runtime_session_id,
            producer_event=stored,
            rows=self.rows,
        )
        self.repository.insert_rows(self.rows)


@dataclass(slots=True)
class InMemoryCandidateProjectionOutbox:
    _rows: dict[tuple[str, str, str], CandidateProjectionOutboxRow] = field(
        default_factory=dict
    )
    _applied: set[tuple[str, str, str]] = field(default_factory=set)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def transaction_companion(
        self,
        *,
        runtime_session_id: str,
        producer_event: AgentEvent,
        rows: tuple[CandidateProjectionOutboxRow, ...],
    ) -> CandidateProjectionTransactionCompanion:
        return CandidateProjectionTransactionCompanion(
            repository=self,
            runtime_session_id=runtime_session_id,
            producer_event=producer_event,
            rows=rows,
        )

    def insert_rows(self, rows: tuple[CandidateProjectionOutboxRow, ...]) -> None:
        with self._lock:
            for row in rows:
                key = _row_key(row)
                existing = self._rows.get(key)
                if existing is not None and existing != row:
                    raise ValueError("candidate projection outbox payload conflict")
            self._rows.update((_row_key(row), row) for row in rows)

    def list_pending(
        self,
        *,
        runtime_session_id: str,
        limit: int = 100,
    ) -> tuple[CandidateProjectionOutboxRow, ...]:
        with self._lock:
            rows = tuple(
                row
                for key, row in sorted(self._rows.items())
                if key[0] == runtime_session_id and key not in self._applied
            )
            return rows[:limit]

    def mark_applied(
        self,
        *,
        runtime_session_id: str,
        producer_event_id: str,
        candidate_entry_id: str,
    ) -> None:
        key = (runtime_session_id, producer_event_id, candidate_entry_id)
        with self._lock:
            if key not in self._rows:
                raise KeyError(key)
            self._applied.add(key)


@dataclass(slots=True)
class PostgresCandidateProjectionOutbox:
    connection_provider: VerifiedPostgresConnectionProviderProtocol

    def _connection(self, *, row_factory: object | None = None):
        return self.connection_provider.connection(
            lane=PostgresConnectionLane.GOVERNANCE,
            row_factory=row_factory,
            deadline_monotonic=monotonic() + 30.0,
        )

    def transaction_companion(
        self,
        *,
        runtime_session_id: str,
        producer_event: AgentEvent,
        rows: tuple[CandidateProjectionOutboxRow, ...],
    ) -> CandidateProjectionTransactionCompanion:
        return CandidateProjectionTransactionCompanion(
            repository=self,
            runtime_session_id=runtime_session_id,
            producer_event=producer_event,
            rows=rows,
        )

    def insert_with_cursor(
        self,
        cursor,
        *,
        rows: tuple[CandidateProjectionOutboxRow, ...],
    ) -> None:
        for row in rows:
            identity = row.item.producer_event_identity
            candidate_payload = row.candidate.model_dump(mode="json")
            cursor.execute(
                """
                insert into memory_candidate_projection_outbox (
                    runtime_session_id,
                    producer_kind,
                    producer_event_id,
                    candidate_entry_id,
                    candidate_index,
                    outbox_item_fingerprint,
                    producer_payload_fingerprint,
                    producer_event_identity,
                    candidate_payload_fingerprint,
                    candidate_attribution_fingerprint,
                    candidate_payload,
                    status
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                on conflict (
                    runtime_session_id,
                    producer_kind,
                    producer_event_id,
                    candidate_entry_id
                ) do nothing
                """,
                (
                    identity.runtime_session_id,
                    row.item.producer_kind.value,
                    identity.event_id,
                    row.item.candidate_entry_id,
                    row.item.candidate_index,
                    row.item.item_fingerprint,
                    identity.payload_fingerprint,
                    Jsonb(identity.model_dump(mode="json")),
                    row.item.candidate_payload_fingerprint,
                    row.item.candidate_attribution_fingerprint,
                    Jsonb(candidate_payload),
                ),
            )
            cursor.execute(
                """
                select outbox_item_fingerprint, candidate_payload
                from memory_candidate_projection_outbox
                where runtime_session_id = %s
                  and producer_kind = %s
                  and producer_event_id = %s
                  and candidate_entry_id = %s
                """,
                (
                    identity.runtime_session_id,
                    row.item.producer_kind.value,
                    identity.event_id,
                    row.item.candidate_entry_id,
                ),
            )
            stored = cursor.fetchone()
            if (
                stored is None
                or stored["outbox_item_fingerprint"]
                != row.item.item_fingerprint
                or stored["candidate_payload"] != candidate_payload
            ):
                raise ValueError("candidate projection outbox id/payload conflict")

    def list_pending(
        self,
        *,
        runtime_session_id: str,
        limit: int = 100,
    ) -> tuple[CandidateProjectionOutboxRow, ...]:
        with self._connection(row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select *
                    from memory_candidate_projection_outbox
                    where runtime_session_id = %s and status = 'pending'
                    order by created_at, producer_event_id, candidate_index
                    limit %s
                    """,
                    (runtime_session_id, limit),
                )
                rows = cursor.fetchall()
        return tuple(_row_from_postgres(item) for item in rows)

    def mark_applied(
        self,
        *,
        runtime_session_id: str,
        producer_event_id: str,
        candidate_entry_id: str,
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update memory_candidate_projection_outbox
                    set status = 'applied', applied_at = now(), updated_at = now()
                    where runtime_session_id = %s
                      and producer_event_id = %s
                      and candidate_entry_id = %s
                      and status in ('pending', 'applying', 'applied')
                    """,
                    (runtime_session_id, producer_event_id, candidate_entry_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError((runtime_session_id, producer_event_id, candidate_entry_id))


@dataclass(slots=True)
class CandidateProjectionOutboxDispatcher:
    runtime_session_id: str
    repository: CandidateProjectionOutboxRepository
    candidate_pool: CandidatePool

    def flush(self, *, limit: int = 100) -> int:
        applied = 0
        for row in self.repository.list_pending(
            runtime_session_id=self.runtime_session_id,
            limit=limit,
        ):
            try:
                self.candidate_pool.append_candidate(row.candidate)
            except ValueError:
                existing = self.candidate_pool.get_candidate(row.candidate.entry_id)
                if existing != row.candidate:
                    raise ValueError("candidate projection target payload conflict")
            self.repository.mark_applied(
                runtime_session_id=self.runtime_session_id,
                producer_event_id=row.item.producer_event_identity.event_id,
                candidate_entry_id=row.item.candidate_entry_id,
            )
            applied += 1
        return applied


@dataclass(slots=True)
class MemoryCandidateProjectionCommitPort:
    """RuntimeSession-owned atomic producer event/account/outbox commit port."""

    runtime_session: "RuntimeSession"
    repository: CandidateProjectionOutboxRepository
    dispatcher: CandidateProjectionOutboxDispatcher
    _dispatch_retry_required: bool = field(default=False, init=False, repr=False)
    _owned_tasks: dict[str, asyncio.Task["EventWriteResult"]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _owned_bundles: dict[str, "_CandidateProjectionCommitBundle"] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _blocked_errors: dict[str, BaseException] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _owner_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _accepting: bool = field(default=True, init=False, repr=False)

    async def commit_producer_bundle(
        self,
        *,
        producer_event: AgentEvent,
        rows: tuple[CandidateProjectionOutboxRow, ...],
    ) -> "EventWriteResult":
        bundle = _CandidateProjectionCommitBundle(
            producer_event=producer_event,
            rows=rows,
        )
        producer_event_id = producer_event.id
        async with self._owner_lock:
            if not self._accepting:
                raise RuntimeError("candidate projection commit admission is closed")
            existing_bundle = self._owned_bundles.get(producer_event_id)
            if existing_bundle is not None and existing_bundle != bundle:
                raise ValueError("candidate projection stable bundle identity conflict")
            blocked_error = self._blocked_errors.get(producer_event_id)
            if blocked_error is not None:
                raise RuntimeError(
                    "candidate projection reconciliation owner is blocked"
                ) from blocked_error
            task = self._owned_tasks.get(producer_event_id)
            if task is None:
                self._owned_bundles[producer_event_id] = bundle
                task = asyncio.create_task(
                    self._commit_owned_bundle(bundle),
                    name=f"memory-candidate-projection:{producer_event_id}",
                )
                self._owned_tasks[producer_event_id] = task
                task.add_done_callback(
                    lambda completed, event_id=producer_event_id: self._retire_owner(
                        event_id,
                        completed,
                    )
                )
        return await asyncio.shield(task)

    async def stop_admission_and_drain(self, *, deadline_monotonic: float) -> None:
        async with self._owner_lock:
            self._accepting = False
            tasks = tuple(self._owned_tasks.values())
        if tasks:
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("candidate projection owner drain deadline exceeded")
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                raise TimeoutError("candidate projection owners did not drain")
            for task in done:
                task.result()
        if self._blocked_errors:
            first = next(iter(self._blocked_errors.values()))
            raise RuntimeError(
                "candidate projection reconciliation owner remains blocked"
            ) from first

    @property
    def pending_owner_count(self) -> int:
        return len(self._owned_tasks) + len(self._blocked_errors)

    async def _commit_owned_bundle(
        self,
        bundle: "_CandidateProjectionCommitBundle",
    ) -> "EventWriteResult":
        from pulsara_agent.runtime.event_write_service import (
            PendingRuntimeEventWriteError,
        )
        from pulsara_agent.runtime.session import (
            EventCommitError,
            EventReconciliationRequired,
            EventWriteCancelled,
        )

        retry_delay = 0.05
        operation = "write"
        while True:
            try:
                if operation == "write":
                    companion = self.repository.transaction_companion(
                        runtime_session_id=self.runtime_session.runtime_session_id,
                        producer_event=bundle.producer_event,
                        rows=bundle.rows,
                    )
                    result = await self.runtime_session.write_events(
                        (bundle.producer_event,),
                        transaction_companion=companion,
                    )
                else:
                    result = (
                        await self.runtime_session.confirm_and_handoff_event_batch_async(
                            (bundle.producer_event,),
                            deadline_monotonic=(
                                self.runtime_session.event_write_service
                                .new_deadline_monotonic()
                            ),
                        )
                    )
            except EventWriteCancelled as cancelled:
                if cancelled.outcome.status == "full":
                    assert cancelled.outcome.result is not None
                    result = cancelled.outcome.result
                elif cancelled.outcome.status == "unknown":
                    operation = "confirm"
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 1.0)
                    continue
                else:
                    operation = "write"
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 1.0)
                    continue
            except EventCommitError as error:
                operation = (
                    "confirm" if error.commit_outcome == "unknown" else "write"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 1.0)
                continue
            except PendingRuntimeEventWriteError:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 1.0)
                continue
            except EventReconciliationRequired as error:
                self._blocked_errors[bundle.producer_event.id] = error
                raise

            # Projection is intentionally after the producer/account transaction.
            # Failure leaves a durable pending outbox row for restart/close drain.
            try:
                await self._run_dispatch(limit=100)
            except Exception:
                self._dispatch_retry_required = True
            else:
                self._dispatch_retry_required = False
            return result

    def _retire_owner(
        self,
        producer_event_id: str,
        task: asyncio.Task["EventWriteResult"],
    ) -> None:
        current = self._owned_tasks.get(producer_event_id)
        if current is task:
            self._owned_tasks.pop(producer_event_id, None)
            if producer_event_id not in self._blocked_errors:
                self._owned_bundles.pop(producer_event_id, None)
        if not task.cancelled():
            task.exception()

    async def flush_pending(
        self,
        *,
        limit: int = 100,
        deadline_monotonic: float | None = None,
    ) -> int:
        """Project already-durable producer rows before governance claims them."""

        try:
            applied = await self._run_dispatch(
                limit=limit,
                deadline_monotonic=deadline_monotonic,
            )
        except BaseException:
            self._dispatch_retry_required = True
            raise
        self._dispatch_retry_required = False
        return applied

    @property
    def dispatch_retry_required(self) -> bool:
        return self._dispatch_retry_required

    async def _run_dispatch(
        self,
        *,
        limit: int,
        deadline_monotonic: float | None = None,
    ) -> int:
        return await self.runtime_session.context_input_io_service.execute(
            operation_name="memory-candidate-projection-outbox-dispatch",
            operation=lambda: self.dispatcher.flush(limit=limit),
            deadline_monotonic=deadline_monotonic or monotonic() + 30.0,
        )


@dataclass(frozen=True, slots=True)
class _CandidateProjectionCommitBundle:
    producer_event: AgentEvent
    rows: tuple[CandidateProjectionOutboxRow, ...]


def _validate_producer_rows(
    *,
    runtime_session_id: str,
    producer_event: AgentEvent,
    rows: tuple[CandidateProjectionOutboxRow, ...],
) -> None:
    expected_kind: CandidateProjectionProducerKind
    if isinstance(producer_event, MemoryReflectionCompletedEvent):
        expected_kind = CandidateProjectionProducerKind.REFLECTION
    elif isinstance(producer_event, ContextCompactionMemoryCandidatesProposedEvent):
        expected_kind = CandidateProjectionProducerKind.COMPACTION
    else:
        raise TypeError("candidate projection producer event is unsupported")
    if len(rows) != len({row.item.candidate_entry_id for row in rows}):
        raise ValueError("candidate projection outbox entries must be unique")
    indices = tuple(row.item.candidate_index for row in rows)
    if indices != tuple(range(len(indices))):
        raise ValueError("candidate projection outbox indices must be contiguous")
    for row in rows:
        if row.item.producer_kind is not expected_kind:
            raise ValueError("candidate projection outbox producer kind drifted")
        row.validate(
            runtime_session_id=runtime_session_id,
            producer_event=producer_event,
        )


def _stored_producer(
    producer_event_id: str,
    stored_events: Sequence[AgentEvent],
) -> AgentEvent:
    matches = tuple(event for event in stored_events if event.id == producer_event_id)
    if len(matches) != 1:
        raise ValueError("transaction companion lacks one stored producer event")
    return matches[0]


def _row_key(row: CandidateProjectionOutboxRow) -> tuple[str, str, str]:
    identity = row.item.producer_event_identity
    return (
        identity.runtime_session_id,
        identity.event_id,
        row.item.candidate_entry_id,
    )


def _row_from_postgres(row: dict[str, object]) -> CandidateProjectionOutboxRow:
    candidate = PooledMemoryCandidate.model_validate(row["candidate_payload"])
    identity = stable_event_identity_from_row(row)
    item = CandidateProjectionOutboxItemFact.model_validate(
        {
            "schema_version": "candidate_projection_outbox_item.v1",
            "producer_kind": row["producer_kind"],
            "producer_event_identity": identity,
            "candidate_entry_id": row["candidate_entry_id"],
            "candidate_index": row["candidate_index"],
            "candidate_payload": candidate.payload,
            "candidate_payload_fingerprint": row["candidate_payload_fingerprint"],
            "candidate_attribution_fingerprint": row["candidate_attribution_fingerprint"],
            "item_fingerprint": row["outbox_item_fingerprint"],
        }
    )
    return CandidateProjectionOutboxRow(item=item, candidate=candidate)


def stable_event_identity_from_row(row: dict[str, object]):
    from pulsara_agent.primitives.frozen import StableEventIdentityFact

    return StableEventIdentityFact.model_validate(row["producer_event_identity"])




__all__ = [
    "CandidateProjectionOutboxDispatcher",
    "CandidateProjectionOutboxRepository",
    "CandidateProjectionOutboxRow",
    "CandidateProjectionTransactionCompanion",
    "InMemoryCandidateProjectionOutbox",
    "MemoryCandidateProjectionCommitPort",
    "PostgresCandidateProjectionOutbox",
]
