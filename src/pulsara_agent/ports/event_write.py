"""Immutable event-write carrier shared across low-level boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Any, Literal, Mapping

from pulsara_agent.event import AgentEvent


@dataclass(frozen=True, slots=True)
class FrozenEventWriteCandidate:
    """One pre-commit event payload frozen against an exact schema binding."""

    event_id: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_type:
            raise ValueError("event write candidate identity is required")
        if (
            f"sha256:{sha256(self.canonical_payload_bytes).hexdigest()}"
            != self.payload_fingerprint
        ):
            raise ValueError("event write candidate payload fingerprint mismatch")
        try:
            payload = json.loads(self.canonical_payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError(
                "event write candidate payload is not canonical JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("event write candidate payload must be an object")
        if (
            payload.get("id") != self.event_id
            or str(payload.get("type")) != self.event_type
            or payload.get("sequence") is not None
        ):
            raise ValueError("event write candidate wrapper identity mismatch")

    def fingerprint_payload(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_schema_version": self.event_schema_version,
            "event_schema_fingerprint": self.event_schema_fingerprint,
            "event_domain_contract_fingerprint": (
                self.event_domain_contract_fingerprint
            ),
            "canonical_payload_utf8": self.canonical_payload_bytes.decode("utf-8"),
            "payload_fingerprint": self.payload_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class CommittedReducerError:
    reducer_id: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class EventPublicationError:
    event_id: str
    sequence: int
    subscriber_id: str | None
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class EventWriteResult:
    committed_events: tuple[AgentEvent, ...]
    commit_status: Literal["committed"]
    reducer_high_waters: Mapping[str, int]
    reconciliation_required: bool
    reducer_errors: tuple[CommittedReducerError, ...]
    publication_status: Literal["completed", "enqueued", "unavailable"]
    publisher_enqueued_through_sequence: int | None
    publication_errors: tuple[EventPublicationError, ...] = ()
    accounting_events: tuple[AgentEvent, ...] = ()

    def require_reduced(self, reducer_id: str) -> tuple[AgentEvent, ...]:
        last_sequence = max(
            (event.sequence or 0 for event in self.committed_events),
            default=0,
        )
        reducer_sequence = self.reducer_high_waters.get(reducer_id)
        reducer_failed = any(
            error.reducer_id == reducer_id for error in self.reducer_errors
        )
        if (
            reducer_sequence is None
            or reducer_sequence < last_sequence
            or reducer_failed
        ):
            raise EventReconciliationRequired(
                f"Committed reducer {reducer_id!r} did not apply through sequence "
                f"{last_sequence}"
            )
        return self.committed_events


@dataclass(frozen=True, slots=True)
class EventBatchCommitOutcome:
    status: Literal["full", "none", "unknown"]
    deadline_monotonic: float
    result: EventWriteResult | None = None

    def __post_init__(self) -> None:
        if self.status == "full" and self.result is None:
            raise ValueError("FULL event commit outcome requires its write result")
        if self.status != "full" and self.result is not None:
            raise ValueError("non-FULL event commit outcome cannot carry a result")

    @property
    def committed_events(self) -> tuple[AgentEvent, ...]:
        return self.result.committed_events if self.result is not None else ()


class EventCommitError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        commit_outcome: Literal["none", "unknown"] = "none",
        deadline_monotonic: float | None = None,
    ) -> None:
        self.commit_outcome = commit_outcome
        self.deadline_monotonic = deadline_monotonic
        super().__init__(message)


class EventWriteCancelled(asyncio.CancelledError):
    def __init__(self, outcome: EventBatchCommitOutcome) -> None:
        self.outcome = outcome
        super().__init__(f"event write cancelled with {outcome.status} commit outcome")


class EventReconciliationRequired(RuntimeError):
    """A committed reducer is inconsistent and mutation must fail closed."""


class EventPublicationAfterCommitError(RuntimeError):
    def __init__(self, result: EventWriteResult) -> None:
        self.result = result
        super().__init__("Event batch committed but one or more observers failed")


class PendingRuntimeEventWriteError(RuntimeError):
    """The session cannot accept or drain another physical event write."""


class RuntimeEventWriteCancelled(asyncio.CancelledError):
    """Caller cancellation observed the terminal result of its physical owner."""

    def __init__(
        self,
        *,
        operation_result: Any | None,
        operation_error: BaseException | None,
        deadline_monotonic: float,
    ) -> None:
        self.operation_result = operation_result
        self.operation_error = operation_error
        self.deadline_monotonic = deadline_monotonic
        super().__init__("runtime event write caller cancelled after physical resolution")


def event_batch_commit_outcome_from_error(
    error: BaseException,
) -> EventBatchCommitOutcome | None:
    if isinstance(error, EventWriteCancelled):
        return error.outcome
    if isinstance(error, EventPublicationAfterCommitError):
        return EventBatchCommitOutcome(
            status="full",
            deadline_monotonic=monotonic(),
            result=error.result,
        )
    if isinstance(error, EventCommitError):
        return EventBatchCommitOutcome(
            status=error.commit_outcome,
            deadline_monotonic=error.deadline_monotonic or monotonic(),
        )
    return None


__all__ = [
    "CommittedReducerError",
    "EventBatchCommitOutcome",
    "EventCommitError",
    "EventPublicationAfterCommitError",
    "EventPublicationError",
    "EventReconciliationRequired",
    "EventWriteCancelled",
    "EventWriteResult",
    "FrozenEventWriteCandidate",
    "PendingRuntimeEventWriteError",
    "RuntimeEventWriteCancelled",
    "event_batch_commit_outcome_from_error",
]
