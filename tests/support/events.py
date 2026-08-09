"""Typed event fixtures for tests that only need a non-transcript ledger fact."""

from __future__ import annotations

from pulsara_agent.event import AgentEvent, EventContext, ProjectionRequestedEvent
from pulsara_agent.ports.event_write import (
    CommittedCheckpointHandoff,
    CommittedEventSettlementReceipt,
    CommittedPublicationSettlement,
    CommittedSemanticFoldSettlement,
    RuntimeThreadEventSettlementReceipt,
)
from pulsara_agent.primitives.context import context_fingerprint


def typed_non_transcript_event(
    *,
    label: str | None = None,
    name: str | None = None,
    context: EventContext | None = None,
    event_id: str | None = None,
    id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    reply_id: str | None = None,
    sequence: int | None = None,
    metadata: dict[str, object] | None = None,
    value: object | None = None,
    payload: object | None = None,
) -> ProjectionRequestedEvent:
    """Build a real registered event without reopening a generic event escape hatch."""

    resolved_label = label or name
    if not resolved_label:
        raise ValueError("typed test event requires a label")
    if value is not None and payload is not None:
        raise ValueError("typed test event accepts value or payload, not both")
    ctx = context or EventContext(
        run_id=run_id or f"run:test:{resolved_label}",
        turn_id=turn_id or f"turn:test:{resolved_label}",
        reply_id=reply_id or f"reply:test:{resolved_label}",
    )
    semantic_payload = value if value is not None else payload
    fields: dict[str, object] = {
        **ctx.event_fields(),
        "projection_id": f"projection:test:{resolved_label}",
        "role": "test_support",
        "scope": context_fingerprint(
            "typed-test-non-transcript-event:v1",
            {
                "label": resolved_label,
                "payload": semantic_payload,
            },
        ),
        "token_budget": None,
        "sequence": sequence,
        "metadata": dict(metadata or {}),
    }
    resolved_event_id = event_id or id
    if resolved_event_id is not None:
        fields["id"] = resolved_event_id
    return ProjectionRequestedEvent.model_validate(fields)


def settled_test_event(
    event: AgentEvent,
    *,
    sequence: int,
) -> RuntimeThreadEventSettlementReceipt:
    """Return the same typed settlement shape required from thread recorders."""

    if sequence < 1:
        raise ValueError("test settlement sequence must be positive")
    committed = event.model_copy(update={"sequence": sequence})
    references = ((committed.id, sequence, f"test:{committed.id}"),)
    stored_batch_identity = f"test-batch:{committed.id}:{sequence}"
    payload = {
        "stored_batch_receipt_identity": stored_batch_identity,
        "requested_event_references": references,
        "durability": "full",
        "semantic_fold": CommittedSemanticFoldSettlement.HEALTHY.value,
        "checkpoint_handoff": CommittedCheckpointHandoff.NOT_APPLICABLE.value,
        "publication": CommittedPublicationSettlement.COMPLETED.value,
    }
    settlement = CommittedEventSettlementReceipt(
        stored_batch_receipt_identity=stored_batch_identity,
        requested_event_references=references,
        durability="full",
        semantic_fold=CommittedSemanticFoldSettlement.HEALTHY,
        checkpoint_handoff=CommittedCheckpointHandoff.NOT_APPLICABLE,
        publication=CommittedPublicationSettlement.COMPLETED,
        settlement_fingerprint=context_fingerprint(
            "committed-event-settlement-receipt:v1", payload
        ),
    )
    return RuntimeThreadEventSettlementReceipt(
        committed_event=committed,
        settlement=settlement,
    )


__all__ = ["settled_test_event", "typed_non_transcript_event"]
