"""RuntimeSession-owned commit boundary for context-compaction facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Literal, Protocol

from pulsara_agent.event import AgentEvent
from pulsara_agent.primitives.runtime_event_vocabulary import (
    RuntimeEventOperationDeadlineBudget,
)
from pulsara_agent.runtime.session import (
    EventPublicationError,
    EventWriteResult,
    RuntimeSession,
)
from pulsara_agent.runtime._retry import bounded_none_retry_delay_seconds
from pulsara_agent.runtime.state import LoopState


@dataclass(frozen=True, slots=True)
class CompactionEventCommitResult:
    candidate_event_id: str
    candidate_deadline_budget: RuntimeEventOperationDeadlineBudget
    committed_event: AgentEvent
    committed_through_sequence: int
    publication_status: Literal["completed", "enqueued", "unavailable"]
    publication_errors: tuple[EventPublicationError, ...]


@dataclass(frozen=True, slots=True)
class CompactionEventBatchCommitResult:
    candidate_event_ids: tuple[str, ...]
    candidate_deadline_budget: RuntimeEventOperationDeadlineBudget
    committed_events: tuple[AgentEvent, ...]
    committed_through_sequence: int
    publication_status: Literal["completed", "enqueued", "unavailable"]
    publication_errors: tuple[EventPublicationError, ...]


class CompactionEventCommitPort(Protocol):
    async def commit_event(
        self,
        event: AgentEvent,
        *,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
        use_terminal_deadline: bool = False,
        state: LoopState | None = None,
        publication_terminal_maintenance_lease: object | None = None,
    ) -> CompactionEventCommitResult: ...

    async def commit_events(
        self,
        events: tuple[AgentEvent, ...],
        *,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
        use_terminal_deadline: bool = False,
        state: LoopState | None = None,
        publication_terminal_maintenance_lease: object | None = None,
    ) -> CompactionEventBatchCommitResult: ...


class CompactionCommitCancelledAfterCommit(asyncio.CancelledError):
    """The caller detached after the candidate reached durable FULL."""

    def __init__(self, result: CompactionEventCommitResult) -> None:
        self.result = result
        super().__init__("compaction event committed while caller was cancelled")


class CompactionPendingCommitNotDurable(RuntimeError):
    """A cancellation-owned candidate reached a confirmed NONE outcome."""


@dataclass(frozen=True, slots=True)
class PendingCompactionEventCommit:
    """Sole physical owner for a candidate after caller cancellation."""

    candidate_event: AgentEvent
    task: asyncio.Task[EventWriteResult]
    runtime_session: RuntimeSession
    deadline_budget: RuntimeEventOperationDeadlineBudget
    use_terminal_deadline: bool

    async def resolve(self, *, timeout_seconds: float) -> CompactionEventCommitResult:
        try:
            raw = await asyncio.wait_for(
                asyncio.shield(self.task),
                timeout=timeout_seconds,
            )
        except BaseException as task_error:
            outcome = self.runtime_session.resolved_event_write_outcome(task_error)
            if outcome.status == "none":
                raise CompactionPendingCommitNotDurable(
                    "cancelled compaction candidate was not committed"
                ) from task_error
            if outcome.status != "full" or outcome.result is None:
                raise
            raw = outcome.result
        return _runtime_write_result(
            self.candidate_event,
            raw,
            deadline_budget=self.deadline_budget,
        )


class CompactionCommitPendingAfterCancellation(asyncio.CancelledError):
    """Caller cancellation transferred the exact writer task to the service."""

    def __init__(self, pending: PendingCompactionEventCommit) -> None:
        self.pending = pending
        super().__init__("compaction commit remains in flight after caller cancellation")


class CompactionBatchCommitCancelledAfterCommit(asyncio.CancelledError):
    """The caller detached after an exact candidate batch reached durable FULL."""

    def __init__(self, result: CompactionEventBatchCommitResult) -> None:
        self.result = result
        super().__init__("compaction event batch committed while caller was cancelled")


@dataclass(frozen=True, slots=True)
class PendingCompactionEventBatchCommit:
    """Sole physical owner for an exact event batch after caller cancellation."""

    candidate_events: tuple[AgentEvent, ...]
    task: asyncio.Task[EventWriteResult]
    runtime_session: RuntimeSession
    deadline_budget: RuntimeEventOperationDeadlineBudget
    use_terminal_deadline: bool

    async def resolve(
        self, *, timeout_seconds: float
    ) -> CompactionEventBatchCommitResult:
        try:
            raw = await asyncio.wait_for(
                asyncio.shield(self.task),
                timeout=timeout_seconds,
            )
        except BaseException as task_error:
            outcome = self.runtime_session.resolved_event_write_outcome(task_error)
            if outcome.status == "none":
                raise CompactionPendingCommitNotDurable(
                    "cancelled compaction batch was not committed"
                ) from task_error
            if outcome.status != "full" or outcome.result is None:
                raise
            raw = outcome.result
        return _runtime_batch_write_result(
            self.candidate_events,
            raw,
            deadline_budget=self.deadline_budget,
        )


class CompactionBatchCommitPendingAfterCancellation(asyncio.CancelledError):
    """Cancellation transferred an exact batch writer task to the service."""

    def __init__(self, pending: PendingCompactionEventBatchCommit) -> None:
        self.pending = pending
        super().__init__(
            "compaction batch commit remains in flight after caller cancellation"
        )


@dataclass(frozen=True, slots=True)
class RuntimeSessionCompactionEventCommitPort:
    runtime_session: RuntimeSession

    async def commit_event(
        self,
        event: AgentEvent,
        *,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
        use_terminal_deadline: bool = False,
        state: LoopState | None = None,
        publication_terminal_maintenance_lease: object | None = None,
    ) -> CompactionEventCommitResult:
        deadline = (
            deadline_budget.terminal_deadline_monotonic
            if use_terminal_deadline
            else deadline_budget.ordinary_deadline_monotonic
        )
        task = asyncio.create_task(
            self._write_exact_candidate(
                event,
                deadline_monotonic=deadline,
                state=state,
                publication_terminal_maintenance_lease=(
                    publication_terminal_maintenance_lease
                ),
            ),
            name=f"pulsara-compaction-commit:{event.id}",
        )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            if task.done() and not task.cancelled():
                try:
                    result = task.result()
                except BaseException:
                    pass
                else:
                    raise CompactionCommitCancelledAfterCommit(
                        _runtime_write_result(
                            event,
                            result,
                            deadline_budget=deadline_budget,
                        )
                    ) from cancelled
            raise CompactionCommitPendingAfterCancellation(
                PendingCompactionEventCommit(
                    candidate_event=event,
                    task=task,
                    runtime_session=self.runtime_session,
                    deadline_budget=deadline_budget,
                    use_terminal_deadline=use_terminal_deadline,
                )
            ) from cancelled
        return _runtime_write_result(
            event,
            result,
            deadline_budget=deadline_budget,
        )

    async def commit_events(
        self,
        events: tuple[AgentEvent, ...],
        *,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
        use_terminal_deadline: bool = False,
        state: LoopState | None = None,
        publication_terminal_maintenance_lease: object | None = None,
    ) -> CompactionEventBatchCommitResult:
        if not events:
            raise ValueError("compaction commit batch must not be empty")
        if len({event.id for event in events}) != len(events):
            raise ValueError("compaction commit batch contains duplicate event ids")
        deadline = (
            deadline_budget.terminal_deadline_monotonic
            if use_terminal_deadline
            else deadline_budget.ordinary_deadline_monotonic
        )
        task = asyncio.create_task(
            self._write_exact_candidates(
                events,
                deadline_monotonic=deadline,
                state=state,
                publication_terminal_maintenance_lease=(
                    publication_terminal_maintenance_lease
                ),
            ),
            name=f"pulsara-compaction-batch-commit:{events[0].id}",
        )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            if task.done() and not task.cancelled():
                try:
                    result = task.result()
                except BaseException:
                    pass
                else:
                    raise CompactionBatchCommitCancelledAfterCommit(
                        _runtime_batch_write_result(
                            events,
                            result,
                            deadline_budget=deadline_budget,
                        )
                    ) from cancelled
            raise CompactionBatchCommitPendingAfterCancellation(
                PendingCompactionEventBatchCommit(
                    candidate_events=events,
                    task=task,
                    runtime_session=self.runtime_session,
                    deadline_budget=deadline_budget,
                    use_terminal_deadline=use_terminal_deadline,
                )
            ) from cancelled
        return _runtime_batch_write_result(
            events,
            result,
            deadline_budget=deadline_budget,
        )

    async def _write_exact_candidate(
        self,
        event: AgentEvent,
        *,
        deadline_monotonic: float,
        state: LoopState | None,
        publication_terminal_maintenance_lease: object | None,
    ) -> EventWriteResult:
        attempt_generation = 0
        while monotonic() < deadline_monotonic:
            try:
                return await self.runtime_session.write_event_with_deadline(
                    event,
                    deadline_monotonic=deadline_monotonic,
                    state=state,
                    publication_terminal_maintenance_lease=(
                        publication_terminal_maintenance_lease
                    ),
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                outcome = self.runtime_session.resolved_event_write_outcome(error)
                if outcome.status == "none":
                    attempt_generation += 1
                    delay = bounded_none_retry_delay_seconds(
                        attempt_generation,
                        deadline_monotonic=deadline_monotonic,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                if outcome.status == "full" and outcome.result is not None:
                    return outcome.result
                raise
        raise TimeoutError("compaction candidate ordinary deadline expired")

    async def _write_exact_candidates(
        self,
        events: tuple[AgentEvent, ...],
        *,
        deadline_monotonic: float,
        state: LoopState | None,
        publication_terminal_maintenance_lease: object | None,
    ) -> EventWriteResult:
        attempt_generation = 0
        while monotonic() < deadline_monotonic:
            try:
                return await self.runtime_session.write_events_with_deadline(
                    events,
                    deadline_monotonic=deadline_monotonic,
                    state=state,
                    publication_terminal_maintenance_lease=(
                        publication_terminal_maintenance_lease
                    ),
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                outcome = self.runtime_session.resolved_event_write_outcome(error)
                if outcome.status == "none":
                    attempt_generation += 1
                    delay = bounded_none_retry_delay_seconds(
                        attempt_generation,
                        deadline_monotonic=deadline_monotonic,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                if outcome.status == "full" and outcome.result is not None:
                    return outcome.result
                raise
        raise TimeoutError("compaction candidate batch ordinary deadline expired")


def _runtime_write_result(
    candidate: AgentEvent,
    result: EventWriteResult,
    *,
    deadline_budget: RuntimeEventOperationDeadlineBudget,
) -> CompactionEventCommitResult:
    if len(result.committed_events) != 1:
        raise RuntimeError("compaction commit returned an invalid batch")
    committed = result.committed_events[0]
    if committed.id != candidate.id or committed.sequence is None:
        raise RuntimeError("compaction commit returned a mismatched event")
    return CompactionEventCommitResult(
        candidate_event_id=candidate.id,
        candidate_deadline_budget=deadline_budget,
        committed_event=committed,
        committed_through_sequence=committed.sequence,
        publication_status=result.publication_status,
        publication_errors=result.publication_errors,
    )


def _runtime_batch_write_result(
    candidates: tuple[AgentEvent, ...],
    result: EventWriteResult,
    *,
    deadline_budget: RuntimeEventOperationDeadlineBudget,
) -> CompactionEventBatchCommitResult:
    committed = tuple(result.committed_events)
    candidate_ids = tuple(event.id for event in candidates)
    committed_ids = tuple(event.id for event in committed)
    if committed_ids != candidate_ids:
        raise RuntimeError("compaction batch commit returned mismatched events")
    sequences = tuple(event.sequence for event in committed)
    if any(sequence is None for sequence in sequences):
        raise RuntimeError("compaction batch commit returned unsequenced events")
    concrete_sequences = tuple(int(sequence) for sequence in sequences)
    if tuple(sorted(concrete_sequences)) != concrete_sequences or len(
        set(concrete_sequences)
    ) != len(concrete_sequences):
        raise RuntimeError("compaction batch commit returned unordered events")
    return CompactionEventBatchCommitResult(
        candidate_event_ids=candidate_ids,
        candidate_deadline_budget=deadline_budget,
        committed_events=committed,
        committed_through_sequence=concrete_sequences[-1],
        publication_status=result.publication_status,
        publication_errors=result.publication_errors,
    )


__all__ = [
    "CompactionBatchCommitCancelledAfterCommit",
    "CompactionBatchCommitPendingAfterCancellation",
    "CompactionCommitCancelledAfterCommit",
    "CompactionCommitPendingAfterCancellation",
    "CompactionEventBatchCommitResult",
    "CompactionEventCommitPort",
    "CompactionEventCommitResult",
    "CompactionPendingCommitNotDurable",
    "PendingCompactionEventBatchCommit",
    "PendingCompactionEventCommit",
    "RuntimeSessionCompactionEventCommitPort",
]
