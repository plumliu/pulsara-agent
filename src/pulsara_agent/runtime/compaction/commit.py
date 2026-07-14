"""Typed commit boundary for compaction facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.runtime.session import (
    EventPublicationAfterCommitError,
    EventPublicationError,
    EventWriteResult,
    RuntimeSession,
)
from pulsara_agent.runtime.state import LoopState


@dataclass(frozen=True, slots=True)
class CompactionEventCommitResult:
    candidate_event_id: str
    committed_event: AgentEvent
    committed_through_sequence: int
    publication_status: Literal["completed", "enqueued", "unavailable"]
    publication_errors: tuple[EventPublicationError, ...]


class CompactionEventCommitPort(Protocol):
    async def commit_event(
        self,
        event: AgentEvent,
        *,
        state: LoopState | None = None,
    ) -> CompactionEventCommitResult: ...


class CompactionCommitCancelledAfterCommit(asyncio.CancelledError):
    """The caller was cancelled, but the candidate reached durable commit."""

    def __init__(self, result: CompactionEventCommitResult) -> None:
        self.result = result
        super().__init__("compaction event committed while caller was cancelled")


class CompactionPendingCommitNotDurable(RuntimeError):
    """A cancellation-owned candidate was confirmed absent from the ledger."""


@dataclass(frozen=True, slots=True)
class PendingCompactionEventCommit:
    """Process owner for a write that outlived caller cancellation."""

    candidate_event: AgentEvent
    task: asyncio.Task[object]
    runtime_session: RuntimeSession | None = None
    event_log: EventLog | None = None

    async def resolve(self, *, timeout_seconds: float) -> CompactionEventCommitResult:
        try:
            raw = await asyncio.wait_for(
                asyncio.shield(self.task),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise
        except EventPublicationAfterCommitError as exc:
            return _runtime_write_result(self.candidate_event, exc.result)
        except BaseException as task_error:
            if self.runtime_session is not None:
                outcome = self.runtime_session.resolved_event_write_outcome(
                    task_error
                )
                if outcome.status == "none":
                    raise CompactionPendingCommitNotDurable(
                        "cancelled compaction candidate was not committed"
                    ) from task_error
                if outcome.status == "unknown":
                    raise
                committed = outcome.committed_events[0]
                return _committed_event_result(
                    self.candidate_event,
                    committed,
                    publication_status="enqueued",
                )
            if self.event_log is not None:
                committed = self.event_log.get_by_id(self.candidate_event.id)
                if committed is None:
                    raise CompactionPendingCommitNotDurable(
                        "cancelled compaction candidate was not committed"
                    ) from task_error
                if committed.model_dump(
                    mode="json", exclude={"sequence"}
                ) != self.candidate_event.model_dump(
                    mode="json", exclude={"sequence"}
                ):
                    raise RuntimeError(
                        "cancelled compaction candidate identity conflict"
                    ) from task_error
                return _committed_event_result(
                    self.candidate_event,
                    committed,
                    publication_status="unavailable",
                )
            raise
        if isinstance(raw, EventWriteResult):
            return _runtime_write_result(self.candidate_event, raw)
        if isinstance(raw, AgentEvent):
            return _committed_event_result(
                self.candidate_event,
                raw,
                publication_status="unavailable",
            )
        raise RuntimeError("pending compaction commit returned an invalid result")


class CompactionCommitPendingAfterCancellation(asyncio.CancelledError):
    """Caller cancellation transferred an in-flight write to the service."""

    def __init__(self, pending: PendingCompactionEventCommit) -> None:
        self.pending = pending
        super().__init__("compaction commit remains in flight after caller cancellation")


@dataclass(frozen=True, slots=True)
class RuntimeSessionCompactionEventCommitPort:
    runtime_session: RuntimeSession

    async def commit_event(
        self,
        event: AgentEvent,
        *,
        state: LoopState | None = None,
    ) -> CompactionEventCommitResult:
        task = asyncio.create_task(
            self.runtime_session.write_event(event, state=state)
        )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            # The shielded writer task remains the sole owner. It will resolve
            # the original deadline and typed commit outcome without a second
            # event-loop confirmation query.
            raise CompactionCommitPendingAfterCancellation(
                PendingCompactionEventCommit(
                    candidate_event=event,
                    task=task,
                    runtime_session=self.runtime_session,
                )
            ) from cancelled
        committed = result.committed_events[0]
        if committed.sequence is None:
            raise RuntimeError("compaction commit returned an unsequenced event")
        return CompactionEventCommitResult(
            candidate_event_id=event.id,
            committed_event=committed,
            committed_through_sequence=committed.sequence,
            publication_status=result.publication_status,
            publication_errors=result.publication_errors,
        )


@dataclass(frozen=True, slots=True)
class DirectEventLogCompactionEventCommitPort:
    """Component-test adapter; production wiring uses RuntimeSession."""

    event_log: EventLog

    async def commit_event(
        self,
        event: AgentEvent,
        *,
        state: LoopState | None = None,
    ) -> CompactionEventCommitResult:
        del state
        task = asyncio.create_task(asyncio.to_thread(self.event_log.append, event))
        try:
            committed = await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            try:
                committed = await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
            except asyncio.TimeoutError:
                raise CompactionCommitPendingAfterCancellation(
                    PendingCompactionEventCommit(
                        candidate_event=event,
                        task=task,
                        event_log=self.event_log,
                    )
                ) from cancelled
            except BaseException:
                stored = self.event_log.get_by_id(event.id)
                if stored is None:
                    raise cancelled
                committed = stored
            if committed.sequence is None:
                raise RuntimeError("compaction commit returned an unsequenced event")
            normalized = CompactionEventCommitResult(
                candidate_event_id=event.id,
                committed_event=committed,
                committed_through_sequence=committed.sequence,
                publication_status="unavailable",
                publication_errors=(),
            )
            raise CompactionCommitCancelledAfterCommit(normalized) from cancelled
        if committed.sequence is None:
            raise RuntimeError("compaction commit returned an unsequenced event")
        return CompactionEventCommitResult(
            candidate_event_id=event.id,
            committed_event=committed,
            committed_through_sequence=committed.sequence,
            publication_status="unavailable",
            publication_errors=(),
        )


def _consume_background_task(task: asyncio.Task[object]) -> None:
    """Keep cancellation from orphaning a commit task or warning on completion."""

    def consume(done: asyncio.Task[object]) -> None:
        try:
            done.exception()
        except BaseException:
            pass

    task.add_done_callback(consume)


def _runtime_write_result(
    candidate: AgentEvent,
    result: EventWriteResult,
) -> CompactionEventCommitResult:
    if len(result.committed_events) != 1:
        raise RuntimeError("compaction commit returned an invalid batch")
    return _committed_event_result(
        candidate,
        result.committed_events[0],
        publication_status=result.publication_status,
        publication_errors=result.publication_errors,
    )


def _committed_event_result(
    candidate: AgentEvent,
    committed: AgentEvent,
    *,
    publication_status: Literal["completed", "enqueued", "unavailable"],
    publication_errors: tuple[EventPublicationError, ...] = (),
) -> CompactionEventCommitResult:
    if committed.id != candidate.id or committed.sequence is None:
        raise RuntimeError("compaction commit returned a mismatched/unsequenced event")
    return CompactionEventCommitResult(
        candidate_event_id=candidate.id,
        committed_event=committed,
        committed_through_sequence=committed.sequence,
        publication_status=publication_status,
        publication_errors=publication_errors,
    )


__all__ = [
    "CompactionEventCommitPort",
    "CompactionEventCommitResult",
    "CompactionCommitCancelledAfterCommit",
    "CompactionCommitPendingAfterCancellation",
    "CompactionPendingCommitNotDurable",
    "DirectEventLogCompactionEventCommitPort",
    "RuntimeSessionCompactionEventCommitPort",
    "PendingCompactionEventCommit",
]
