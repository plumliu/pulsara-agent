"""Canonical transcript-domain classification and prefix accumulators."""

from __future__ import annotations

from typing import Literal

from pulsara_agent.event import EventType
from pulsara_agent.event_log.serialization import canonical_event_payload_bytes
from pulsara_agent.primitives.context import context_fingerprint


TranscriptStorageDomain = Literal[
    "transcript_semantic",
    "transcript_acceleration",
    "non_transcript",
]

TRANSCRIPT_SEMANTIC_EVENT_TYPES = frozenset(
    {
        EventType.RUN_START.value,
        EventType.RUN_END.value,
        EventType.MODEL_CALL_TERMINAL_PROJECTION_COMMITTED.value,
        EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED.value,
        EventType.CAPABILITY_GATE_DECISION.value,
        EventType.TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED.value,
        EventType.REQUIRE_EXTERNAL_EXECUTION.value,
        EventType.EXTERNAL_EXECUTION_RESULT.value,
        EventType.PLAN_EXIT_RESOLVED.value,
        EventType.CONTEXT_COMPACTION_COMPLETED.value,
        EventType.CONTEXT_WINDOW_OPENED.value,
        EventType.CONTEXT_WINDOW_CLOSED.value,
        EventType.CONTEXT_WINDOW_COMPACTION_STARTED.value,
        EventType.CONTEXT_WINDOW_COMPACTION_COMPLETED.value,
        EventType.CONTEXT_WINDOW_COMPACTION_FAILED.value,
        EventType.CONTEXT_PROJECTION_REWRITE_PAGE.value,
    }
)

EXPLICIT_NON_TRANSCRIPT_EVENT_TYPES = frozenset(
    {
        EventType.TOOL_EXECUTION_SUSPENDED.value,
        EventType.MCP_INPUT_REQUIRED_RESOLUTION_SUBMITTED.value,
        EventType.MCP_INPUT_REQUIRED_EXPIRED.value,
        EventType.MCP_INPUT_REQUIRED_BINDING_CHANGED.value,
        EventType.MCP_INPUT_REQUIRED_RESUME_FAILED.value,
        EventType.MCP_INPUT_REQUIRED_INTERACTION_CLOSED.value,
        EventType.CONTEXT_COMPACTION_REQUESTED.value,
        EventType.MID_TURN_CONTEXT_COMPACTION_SKIPPED.value,
        EventType.TOOL_RESULT_EVIDENCE_PROJECTION_FAILED.value,
    }
)

TRANSCRIPT_ACCELERATION_EVENT_TYPES = frozenset(
    {
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT.value,
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED.value,
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED.value,
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED.value,
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED.value,
        EventType.LEDGER_MATERIALIZATION_ACCOUNT_GENESIS.value,
        EventType.LEDGER_MATERIALIZATION_CONSUMER_REGISTERED.value,
        EventType.LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED.value,
        EventType.LEDGER_MATERIALIZATION_CONSUMER_RETIRED.value,
        EventType.LEDGER_MATERIALIZATION_GENERATION_ADVANCED.value,
        EventType.PHYSICAL_OPERATION_RESERVATION_CREATED.value,
        EventType.PHYSICAL_OPERATION_CHARGE_APPLIED.value,
        EventType.PHYSICAL_OPERATION_RESERVATION_SUSPENDED.value,
        EventType.PHYSICAL_OPERATION_RESERVATION_SETTLED.value,
        EventType.CHECKPOINT_DISPATCH_BARRIER_INSTALLED.value,
        EventType.CHECKPOINT_DISPATCH_BARRIER_RELEASED.value,
    }
)

EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR = context_fingerprint(
    "transcript-prefix-accumulator:v1",
    "empty",
)
EMPTY_LEDGER_CONTINUITY_ACCUMULATOR = context_fingerprint(
    "ledger-continuity-accumulator:v1",
    "empty",
)


def classify_transcript_event_type(event_type: str) -> TranscriptStorageDomain:
    if event_type in TRANSCRIPT_SEMANTIC_EVENT_TYPES:
        return "transcript_semantic"
    if event_type in TRANSCRIPT_ACCELERATION_EVENT_TYPES:
        return "transcript_acceleration"
    return "non_transcript"


def advance_transcript_semantic_accumulator(
    previous: str,
    *,
    event,
    event_schema_version: str,
    event_schema_fingerprint: str,
) -> str:
    candidate = event.model_copy(update={"sequence": None})
    return context_fingerprint(
        "transcript-prefix-accumulator:v1",
        {
            "previous": previous,
            "event_type": str(event.type),
            "event_schema_version": event_schema_version,
            "event_schema_fingerprint": event_schema_fingerprint,
            "semantic_payload_utf8": canonical_event_payload_bytes(candidate).decode(
                "utf-8"
            ),
        },
    )


def advance_ledger_continuity_accumulator(
    previous: str,
    *,
    envelope_fingerprint: str,
) -> str:
    return context_fingerprint(
        "ledger-continuity-accumulator:v1",
        {
            "previous": previous,
            "stored_envelope_fingerprint": envelope_fingerprint,
        },
    )


__all__ = [
    "EMPTY_LEDGER_CONTINUITY_ACCUMULATOR",
    "EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR",
    "EXPLICIT_NON_TRANSCRIPT_EVENT_TYPES",
    "TRANSCRIPT_ACCELERATION_EVENT_TYPES",
    "TRANSCRIPT_SEMANTIC_EVENT_TYPES",
    "TranscriptStorageDomain",
    "advance_ledger_continuity_accumulator",
    "advance_transcript_semantic_accumulator",
    "classify_transcript_event_type",
]
