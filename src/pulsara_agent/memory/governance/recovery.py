"""Session-owned governance execution owners and bounded reopen discovery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Generic, TypeVar

from pulsara_agent.event import (
    MemoryGovernanceBatchBlockedEvent,
    MemoryGovernanceBatchCompletedEvent,
    MemoryGovernanceBatchFailedEvent,
    MemoryGovernanceBatchPreparedEvent,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY, EventLog
from pulsara_agent.memory.governance.claims import (
    MemoryGovernanceCandidateClaimRepository,
)
from pulsara_agent.memory.governance.preparation import (
    GovernanceBatchPreparationRecord,
    GovernanceBatchPreparationRepository,
    GovernanceBatchPreparationStatus,
)
from pulsara_agent.primitives.governance_evidence import (
    GovernanceCandidateClaimStatus,
    MemoryGovernanceCandidateClaimFact,
)


T = TypeVar("T")


@dataclass(slots=True)
class GovernanceBatchExecutionOwnerRegistry(Generic[T]):
    """Keep one physical batch owner alive across caller detach/cancellation."""

    _tasks: dict[str, asyncio.Task[T]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _accepting: bool = field(default=True, init=False, repr=False)

    async def run(
        self,
        governance_batch_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("memory governance execution admission is closed")
            task = self._tasks.get(governance_batch_id)
            if task is None:
                task = asyncio.create_task(
                    operation(),
                    name=f"memory-governance:{governance_batch_id}",
                )
                self._tasks[governance_batch_id] = task
                task.add_done_callback(
                    lambda completed, batch_id=governance_batch_id: (
                        self._retire(batch_id, completed)
                    )
                )
        return await asyncio.shield(task)

    def _retire(self, governance_batch_id: str, task: asyncio.Task[T]) -> None:
        current = self._tasks.get(governance_batch_id)
        if current is task:
            self._tasks.pop(governance_batch_id, None)
        if not task.cancelled():
            task.exception()

    @property
    def pending_count(self) -> int:
        return len(self._tasks)

    async def stop_admission_and_drain(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        async with self._lock:
            self._accepting = False
            tasks = tuple(self._tasks.values())
        if not tasks:
            return
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("memory governance owner drain deadline exceeded")
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending:
            raise TimeoutError("memory governance physical owners did not drain")
        for task in done:
            task.result()


@dataclass(frozen=True, slots=True)
class RecoverableGovernanceBatch:
    governance_batch_id: str
    claims: tuple[MemoryGovernanceCandidateClaimFact, ...]
    preparation: GovernanceBatchPreparationRecord | None
    prepared_event: MemoryGovernanceBatchPreparedEvent | None

    @property
    def claim_status(self) -> GovernanceCandidateClaimStatus:
        statuses = {claim.status for claim in self.claims}
        if len(statuses) != 1:
            raise ValueError("governance recovery claim status is mixed")
        return next(iter(statuses))


@dataclass(slots=True)
class MemoryGovernanceBatchRecoveryService:
    runtime_session_id: str
    event_log: EventLog
    claim_repository: MemoryGovernanceCandidateClaimRepository
    preparation_repository: GovernanceBatchPreparationRepository
    max_open_batches: int = 128

    def discover_open_batches(self) -> tuple[RecoverableGovernanceBatch, ...]:
        batch_ids = self.claim_repository.open_batch_ids(
            runtime_session_id=self.runtime_session_id
        )
        if len(batch_ids) > self.max_open_batches:
            raise RuntimeError("open governance batch count exceeds recovery bound")
        event_ids = tuple(
            event_id
            for batch_id in batch_ids
            for event_id in governance_batch_lifecycle_event_ids(batch_id)
        )
        raw = self.event_log.read_raw_events_by_id_snapshot(event_ids)
        events = {
            envelope.event_id: envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for envelope in raw.events
        }
        output: list[RecoverableGovernanceBatch] = []
        for batch_id in batch_ids:
            claims = self.claim_repository.claims_for_batch(
                runtime_session_id=self.runtime_session_id,
                governance_batch_id=batch_id,
            )
            open_claims = tuple(
                claim
                for claim in claims
                if claim.status
                in {
                    GovernanceCandidateClaimStatus.PREPARING,
                    GovernanceCandidateClaimStatus.PREPARED,
                }
            )
            if not open_claims:
                continue
            terminal = tuple(
                events[event_id]
                for event_id in governance_batch_terminal_event_ids(batch_id)
                if event_id in events
            )
            if terminal:
                raise RuntimeError(
                    "terminal governance batch retained non-terminal claims"
                )
            prepared_raw = events.get(governance_batch_prepared_event_id(batch_id))
            prepared = (
                prepared_raw
                if isinstance(prepared_raw, MemoryGovernanceBatchPreparedEvent)
                else None
            )
            statuses = {claim.status for claim in open_claims}
            preparation = self.preparation_repository.get(
                runtime_session_id=self.runtime_session_id,
                governance_batch_id=batch_id,
            )
            if statuses == {GovernanceCandidateClaimStatus.PREPARED}:
                if prepared is None or any(
                    claim.prepared_event_id != prepared.id for claim in open_claims
                ):
                    raise RuntimeError(
                        "prepared governance claims lack their canonical event"
                    )
                if (
                    preparation is None
                    or preparation.status
                    is not GovernanceBatchPreparationStatus.PREPARED
                    or preparation.prepared_event_id != prepared.id
                ):
                    raise RuntimeError(
                        "prepared governance claims lack their batch-input state"
                    )
            elif statuses == {GovernanceCandidateClaimStatus.PREPARING}:
                if prepared is not None:
                    raise RuntimeError(
                        "governance Prepared event exists before claim transition"
                    )
                if (
                    preparation is not None
                    and preparation.status
                    is not GovernanceBatchPreparationStatus.STAGED
                ):
                    raise RuntimeError(
                        "preparing governance claims have invalid batch-input state"
                    )
            else:
                raise RuntimeError("governance recovery claims have mixed status")
            output.append(
                RecoverableGovernanceBatch(
                    governance_batch_id=batch_id,
                    claims=tuple(sorted(open_claims, key=lambda item: item.candidate_entry_id)),
                    preparation=preparation,
                    prepared_event=prepared,
                )
            )
        return tuple(output)


def governance_batch_prepared_event_id(governance_batch_id: str) -> str:
    return f"memory_governance_batch:{governance_batch_id}:prepared"


def governance_batch_terminal_event_ids(governance_batch_id: str) -> tuple[str, ...]:
    prefix = f"memory_governance_batch:{governance_batch_id}:"
    return (prefix + "completed", prefix + "failed", prefix + "blocked")


def governance_batch_lifecycle_event_ids(governance_batch_id: str) -> tuple[str, ...]:
    return (
        governance_batch_prepared_event_id(governance_batch_id),
        *governance_batch_terminal_event_ids(governance_batch_id),
    )


GovernanceBatchTerminalEvent = (
    MemoryGovernanceBatchCompletedEvent
    | MemoryGovernanceBatchFailedEvent
    | MemoryGovernanceBatchBlockedEvent
)


__all__ = [
    "GovernanceBatchExecutionOwnerRegistry",
    "MemoryGovernanceBatchRecoveryService",
    "RecoverableGovernanceBatch",
    "governance_batch_lifecycle_event_ids",
    "governance_batch_prepared_event_id",
    "governance_batch_terminal_event_ids",
]
