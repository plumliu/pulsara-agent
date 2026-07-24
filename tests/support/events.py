"""Typed event fixtures for tests that only need a non-transcript ledger fact."""

from __future__ import annotations

from pulsara_agent.event import EventContext, ProjectionRequestedEvent
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


__all__ = ["typed_non_transcript_event"]
