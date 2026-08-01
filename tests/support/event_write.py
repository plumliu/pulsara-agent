"""Exact stored-receipt fixtures for tests that replace the Runtime writer."""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log.protocol import EventLog
from pulsara_agent.event_log.serialization import build_raw_stored_event_envelope
from pulsara_agent.ports.event_write import (
    CommittedReducerError,
    EventPublicationError,
    EventWriteResult,
    business_accounting_partition_fingerprint,
)
from pulsara_agent.ports.stored_event import (
    StoredEventBatchCommitReceipt,
    build_encoder_stored_event_pair,
    build_stored_event_batch_commit_receipt,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TranscriptProjectionStateStore,
)


def stored_receipt_fixture(
    events: tuple[AgentEvent, ...],
    *,
    runtime_session_id: str,
) -> StoredEventBatchCommitReceipt:
    """Build the same immutable receipt shape as a normal physical writer."""

    if not events or any(event.sequence is None for event in events):
        raise ValueError("stored receipt fixture requires sequenced events")
    return build_stored_event_batch_commit_receipt(
        tuple(
            build_encoder_stored_event_pair(
                event,
                build_raw_stored_event_envelope(
                    event=event,
                    runtime_session_id=runtime_session_id,
                ),
            )
            for event in events
        )
    )


def committed_event_write_result_fixture(
    events: tuple[AgentEvent, ...],
    *,
    runtime_session_id: str,
    receipt: StoredEventBatchCommitReceipt | None = None,
    accounting_events: tuple[AgentEvent, ...] = (),
    reducer_high_waters: Mapping[str, int] | None = None,
    reconciliation_required: bool = False,
    reducer_errors: tuple[CommittedReducerError, ...] = (),
    publication_status: str = "completed",
    publisher_enqueued_through_sequence: int | None = None,
    publication_errors: tuple[EventPublicationError, ...] = (),
) -> EventWriteResult:
    """Construct a production-shaped result without a decoded-event shortcut."""

    physical_receipt = receipt or stored_receipt_fixture(
        events,
        runtime_session_id=runtime_session_id,
    )
    accounting_ids = {event.id for event in accounting_events}
    business_events = tuple(event for event in events if event.id not in accounting_ids)
    return EventWriteResult(
        committed_events=business_events,
        accounting_events=accounting_events,
        stored_batch_receipt=physical_receipt,
        business_accounting_partition_fingerprint=(
            business_accounting_partition_fingerprint(
                receipt=physical_receipt,
                business_events=business_events,
                accounting_events=accounting_events,
            )
        ),
        commit_status="committed",
        reducer_high_waters=reducer_high_waters or {},
        reconciliation_required=reconciliation_required,
        reducer_errors=reducer_errors,
        publication_status=publication_status,  # type: ignore[arg-type]
        publisher_enqueued_through_sequence=(
            publisher_enqueued_through_sequence
            if publisher_enqueued_through_sequence is not None
            else max((event.sequence or 0 for event in events), default=0)
        ),
        publication_errors=publication_errors,
    )


def restore_transcript_projection_fixture(
    *,
    event_log: EventLog,
    reducer: TranscriptProjectionStateStore,
) -> None:
    """Restore test authority through the formal raw-range proof boundary."""

    usage = event_log.read_ledger_usage_snapshot(deadline_monotonic=monotonic() + 5.0)
    if usage.event_count == 0:
        return
    proof = event_log.read_joined_raw_range(
        source_kind="doctor",
        from_sequence_exclusive=0,
        through_sequence=usage.through_sequence,
        max_events=usage.event_count,
        max_payload_bytes=max(1, usage.candidate_payload_bytes),
        deadline_monotonic=monotonic() + 5.0,
    )
    if proof is None:
        raise AssertionError("non-empty test ledger produced no raw range proof")
    reducer.fold_restored_range(proof)


__all__ = [
    "committed_event_write_result_fixture",
    "restore_transcript_projection_fixture",
    "stored_receipt_fixture",
]
