"""Immutable event-write carrier shared across low-level boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from hashlib import sha256
from time import monotonic
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Mapping

from pulsara_agent.event import AgentEvent
from pulsara_agent.primitives.context import context_fingerprint

if TYPE_CHECKING:
    from pulsara_agent.ports.stored_event import StoredEventBatchCommitReceipt


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
    accounting_events: tuple[AgentEvent, ...]
    stored_batch_receipt: StoredEventBatchCommitReceipt | None
    business_accounting_partition_fingerprint: str
    commit_status: Literal["committed"]
    reducer_high_waters: Mapping[str, int]
    reconciliation_required: bool
    reducer_errors: tuple[CommittedReducerError, ...]
    publication_status: Literal["completed", "enqueued", "unavailable"]
    publisher_enqueued_through_sequence: int | None
    publication_errors: tuple[EventPublicationError, ...] = ()

    def __post_init__(self) -> None:
        business_ids = tuple(event.id for event in self.committed_events)
        accounting_ids = tuple(event.id for event in self.accounting_events)
        if set(business_ids).intersection(accounting_ids):
            raise ValueError("business/accounting event partition overlaps")
        if self.stored_batch_receipt is None:
            if business_ids or accounting_ids:
                raise ValueError(
                    "non-empty event write result requires its storage receipt"
                )
            physical_ids: tuple[str, ...] = ()
            receipt_fingerprint = None
        else:
            physical_ids = tuple(
                event.id for event in self.stored_batch_receipt.owned_stored_events
            )
            if set((*business_ids, *accounting_ids)) != set(physical_ids) or len(
                (*business_ids, *accounting_ids)
            ) != len(physical_ids):
                raise ValueError("business/accounting partition is not exhaustive")
            for subset in (business_ids, accounting_ids):
                positions = tuple(physical_ids.index(event_id) for event_id in subset)
                if positions != tuple(sorted(positions)):
                    raise ValueError("event write partition is not order-preserving")
            receipt_fingerprint = self.stored_batch_receipt.ordered_join_fingerprint
        expected = context_fingerprint(
            "event-write-business-accounting-partition:v1",
            {
                "stored_batch_ordered_join_fingerprint": receipt_fingerprint,
                "physical_event_ids": physical_ids,
                "business_event_ids": business_ids,
                "accounting_event_ids": accounting_ids,
            },
        )
        if self.business_accounting_partition_fingerprint != expected:
            raise ValueError("event write partition fingerprint mismatch")

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

    def with_partition(
        self,
        *,
        business_events: tuple[AgentEvent, ...],
        accounting_events: tuple[AgentEvent, ...],
    ) -> "EventWriteResult":
        return replace(
            self,
            committed_events=business_events,
            accounting_events=accounting_events,
            business_accounting_partition_fingerprint=(
                business_accounting_partition_fingerprint(
                    receipt=self.stored_batch_receipt,
                    business_events=business_events,
                    accounting_events=accounting_events,
                )
            ),
        )


class CommittedSemanticFoldSettlement(StrEnum):
    HEALTHY = "healthy"
    REPAIR_OWNER_INSTALLED = "repair_owner_installed"


class CommittedCheckpointHandoff(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ACCEPTED = "accepted"


class CommittedPublicationSettlement(StrEnum):
    COMPLETED = "completed"
    ENQUEUED = "enqueued"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CommittedEventSettlementReceipt:
    stored_batch_receipt_identity: str
    requested_event_references: tuple[tuple[str, int, str], ...]
    durability: Literal["full"]
    semantic_fold: CommittedSemanticFoldSettlement
    checkpoint_handoff: CommittedCheckpointHandoff
    publication: CommittedPublicationSettlement
    settlement_fingerprint: str

    def __post_init__(self) -> None:
        if not self.requested_event_references:
            raise ValueError("committed event settlement cannot be empty")
        expected = context_fingerprint(
            "committed-event-settlement-receipt:v1",
            {
                "stored_batch_receipt_identity": self.stored_batch_receipt_identity,
                "requested_event_references": self.requested_event_references,
                "durability": self.durability,
                "semantic_fold": self.semantic_fold.value,
                "checkpoint_handoff": self.checkpoint_handoff.value,
                "publication": self.publication.value,
            },
        )
        if self.settlement_fingerprint != expected:
            raise ValueError("committed event settlement fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RuntimeThreadEventSettlementReceipt:
    committed_event: AgentEvent
    settlement: CommittedEventSettlementReceipt

    def __post_init__(self) -> None:
        if self.committed_event.sequence is None:
            raise ValueError("thread event settlement requires a committed event")
        if not any(
            event_id == self.committed_event.id
            and sequence == self.committed_event.sequence
            for event_id, sequence, _ in self.settlement.requested_event_references
        ):
            raise ValueError("thread event settlement does not own its event")


@dataclass(frozen=True, slots=True)
class RuntimeThreadEventBatchSettlementReceipt:
    """Typed thread-boundary receipt for one accepted event batch."""

    committed_events: tuple[AgentEvent, ...]
    settlement: CommittedEventSettlementReceipt

    def __post_init__(self) -> None:
        if not self.committed_events:
            raise ValueError("thread event batch settlement cannot be empty")
        expected = tuple(
            (event.id, int(event.sequence or 0)) for event in self.committed_events
        )
        observed = tuple(
            (event_id, sequence)
            for event_id, sequence, _ in self.settlement.requested_event_references
        )
        if expected != observed:
            raise ValueError(
                "thread event batch settlement does not exactly own its events"
            )


def classify_committed_event_settlement(
    result: EventWriteResult,
    *,
    requested_event_ids: Iterable[str],
    required_reducer_ids: Iterable[str],
    repair_owner_installed: Callable[[str, int], bool],
    checkpoint_handoff_accepted: Callable[[str, int], bool],
    checkpointed_reducer_ids: Iterable[str] = (),
    require_publication: bool,
) -> CommittedEventSettlementReceipt:
    """Narrow the low-level multidimensional result at one central boundary."""

    receipt = result.stored_batch_receipt
    if result.commit_status != "committed" or receipt is None:
        raise EventCommitError("event settlement lacks durable FULL receipt")
    requested = tuple(requested_event_ids)
    by_id = {event.id: event for event in result.committed_events}
    if len(requested) != len(set(requested)) or any(item not in by_id for item in requested):
        raise EventReconciliationRequired(
            "event settlement requested identity is absent or ambiguous"
        )
    target = max(int(by_id[item].sequence or 0) for item in requested)
    required = tuple(dict.fromkeys(required_reducer_ids))
    failed = {
        error.reducer_id for error in result.reducer_errors if error.reducer_id in required
    }
    repair_owned = False
    for reducer_id in required:
        if (
            reducer_id not in failed
            and (result.reducer_high_waters.get(reducer_id) or 0) >= target
        ):
            continue
        if not repair_owner_installed(reducer_id, target):
            raise EventReconciliationRequired(
                f"committed reducer {reducer_id!r} lacks exact repair ownership"
            )
        repair_owned = True
    checkpointed = tuple(dict.fromkeys(checkpointed_reducer_ids))
    healthy_checkpointed = tuple(
        reducer_id for reducer_id in checkpointed if reducer_id not in failed
    )
    if checkpointed and failed.intersection(checkpointed):
        # A failed semantic fold is owned by the reducer-repair service.  There
        # is no resulting semantic head that the checkpoint owner can accept
        # yet, so checkpoint settlement is deliberately orthogonal and not
        # applicable.  The repair receipt will hand its rebuilt head to the
        # checkpoint owner after the semantic authority is installed.
        checkpoint = CommittedCheckpointHandoff.NOT_APPLICABLE
    elif healthy_checkpointed and all(
        checkpoint_handoff_accepted(reducer_id, target)
        for reducer_id in healthy_checkpointed
    ):
        checkpoint = CommittedCheckpointHandoff.ACCEPTED
    elif healthy_checkpointed:
        raise EventReconciliationRequired(
            "committed checkpoint semantic head lacks maintenance ownership"
        )
    else:
        checkpoint = CommittedCheckpointHandoff.NOT_APPLICABLE
    publication = CommittedPublicationSettlement(result.publication_status)
    if require_publication and (
        publication is CommittedPublicationSettlement.UNAVAILABLE
        or result.publication_errors
    ):
        raise EventPublicationAfterCommitError(result)
    references = tuple(
        (
            event_id,
            int(by_id[event_id].sequence or 0),
            next(
                envelope.envelope_fingerprint
                for envelope in receipt.raw_stored_envelopes
                if envelope.event_id == event_id
            ),
        )
        for event_id in requested
    )
    payload = {
        "stored_batch_receipt_identity": receipt.ordered_join_fingerprint,
        "requested_event_references": references,
        "durability": "full",
        "semantic_fold": (
            CommittedSemanticFoldSettlement.REPAIR_OWNER_INSTALLED.value
            if repair_owned
            else CommittedSemanticFoldSettlement.HEALTHY.value
        ),
        "checkpoint_handoff": checkpoint.value,
        "publication": publication.value,
    }
    return CommittedEventSettlementReceipt(
        stored_batch_receipt_identity=receipt.ordered_join_fingerprint,
        requested_event_references=references,
        durability="full",
        semantic_fold=(
            CommittedSemanticFoldSettlement.REPAIR_OWNER_INSTALLED
            if repair_owned
            else CommittedSemanticFoldSettlement.HEALTHY
        ),
        checkpoint_handoff=checkpoint,
        publication=publication,
        settlement_fingerprint=context_fingerprint(
            "committed-event-settlement-receipt:v1", payload
        ),
    )


def business_accounting_partition_fingerprint(
    *,
    receipt: StoredEventBatchCommitReceipt | None,
    business_events: tuple[AgentEvent, ...],
    accounting_events: tuple[AgentEvent, ...],
) -> str:
    physical_ids = (
        tuple(event.id for event in receipt.owned_stored_events)
        if receipt is not None
        else ()
    )
    return context_fingerprint(
        "event-write-business-accounting-partition:v1",
        {
            "stored_batch_ordered_join_fingerprint": (
                receipt.ordered_join_fingerprint if receipt is not None else None
            ),
            "physical_event_ids": physical_ids,
            "business_event_ids": tuple(event.id for event in business_events),
            "accounting_event_ids": tuple(event.id for event in accounting_events),
        },
    )


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
        super().__init__(
            "runtime event write caller cancelled after physical resolution"
        )


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
    "CommittedCheckpointHandoff",
    "CommittedEventSettlementReceipt",
    "CommittedPublicationSettlement",
    "CommittedReducerError",
    "CommittedSemanticFoldSettlement",
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
    "RuntimeThreadEventBatchSettlementReceipt",
    "RuntimeThreadEventSettlementReceipt",
    "business_accounting_partition_fingerprint",
    "classify_committed_event_settlement",
    "event_batch_commit_outcome_from_error",
]
