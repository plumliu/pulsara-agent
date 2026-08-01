"""Pure immutable carriers for canonical rows in the runtime event ledger.

This module deliberately has no dependency on ``AgentEvent`` or on the
historical schema registry.  Construction and decoding belong to the EventLog
implementation boundary; this carrier only validates bytes and identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from pulsara_agent.primitives.context import (
    canonical_utc_timestamp,
    context_fingerprint,
)


STORED_EVENT_ENVELOPE_VERSION = "stored-agent-event:v1"


@dataclass(frozen=True, slots=True)
class RawStoredEventEnvelope:
    """Schema-bound canonical stored-event row before historical decoding."""

    stored_envelope_version: str
    event_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    sequence: int
    created_at_utc: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str
    envelope_fingerprint: str

    def __post_init__(self) -> None:
        if self.stored_envelope_version != STORED_EVENT_ENVELOPE_VERSION:
            raise ValueError("unsupported stored event envelope version")
        if not self.runtime_session_id:
            raise ValueError("stored event runtime session identity is required")
        if self.sequence < 1:
            raise ValueError("stored event sequence must be positive")
        if canonical_utc_timestamp(self.created_at_utc) != self.created_at_utc:
            raise ValueError("stored event created_at must be canonical UTC")
        payload_fingerprint = (
            f"sha256:{sha256(self.canonical_payload_bytes).hexdigest()}"
        )
        if self.payload_fingerprint != payload_fingerprint:
            raise ValueError("stored event payload fingerprint mismatch")
        try:
            payload = json.loads(self.canonical_payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError("stored event payload is not canonical JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("stored event payload must be a JSON object")
        wrapper = (
            payload.get("id"),
            str(payload.get("type")),
            payload.get("run_id"),
            payload.get("turn_id"),
            payload.get("reply_id"),
            payload.get("sequence"),
            canonical_utc_timestamp(str(payload.get("created_at"))),
        )
        expected_wrapper = (
            self.event_id,
            self.event_type,
            self.run_id,
            self.turn_id,
            self.reply_id,
            self.sequence,
            self.created_at_utc,
        )
        if wrapper != expected_wrapper:
            raise ValueError("stored event wrapper identity mismatch")
        expected_envelope = context_fingerprint(
            "stored-agent-event-envelope:v1",
            {
                "stored_envelope_version": self.stored_envelope_version,
                "event_id": self.event_id,
                "runtime_session_id": self.runtime_session_id,
                "run_id": self.run_id,
                "turn_id": self.turn_id,
                "reply_id": self.reply_id,
                "sequence": self.sequence,
                "created_at_utc": self.created_at_utc,
                "event_type": self.event_type,
                "event_schema_version": self.event_schema_version,
                "event_schema_fingerprint": self.event_schema_fingerprint,
                "event_domain_contract_fingerprint": (
                    self.event_domain_contract_fingerprint
                ),
                "payload_fingerprint": self.payload_fingerprint,
            },
        )
        if self.envelope_fingerprint != expected_envelope:
            raise ValueError("stored event envelope fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RawTranscriptDomainPrefixFact:
    """Physical ledger-prefix evidence shared by storage-facing projections."""

    through_sequence: int
    ledger_payload_bytes: int
    semantic_event_count: int
    semantic_accumulator: str
    ledger_continuity_accumulator: str

    def __post_init__(self) -> None:
        if (
            self.through_sequence < 0
            or self.ledger_payload_bytes < 0
            or self.semantic_event_count < 0
        ):
            raise ValueError("transcript prefix counters must be non-negative")
        if self.semantic_event_count > self.through_sequence:
            raise ValueError("transcript semantic count exceeds ledger prefix")
        if not self.semantic_accumulator or not self.ledger_continuity_accumulator:
            raise ValueError("transcript prefix accumulators are required")


@dataclass(frozen=True, slots=True)
class RawRuntimeProjectionCheckpoint:
    """Storage-neutral runtime projection checkpoint row."""

    projection_kind: str
    through_sequence: int
    projection_schema_version: str
    ledger_prefix: RawTranscriptDomainPrefixFact
    validation_base_through_sequence: int
    validation_base_state_payload: dict[str, Any]
    state_payload: dict[str, Any]
    payload_fingerprint: str

    def __post_init__(self) -> None:
        if not self.projection_kind or not self.projection_schema_version:
            raise ValueError("runtime projection checkpoint identity is required")
        if self.through_sequence < 0:
            raise ValueError("runtime projection checkpoint sequence is invalid")
        if self.ledger_prefix.through_sequence != self.through_sequence:
            raise ValueError("runtime projection checkpoint ledger prefix is not exact")
        if not 0 <= self.validation_base_through_sequence <= self.through_sequence:
            raise ValueError("runtime projection checkpoint validation base is invalid")
        if not self.payload_fingerprint.startswith("sha256:"):
            raise ValueError("runtime projection checkpoint fingerprint is invalid")


__all__ = [
    "RawRuntimeProjectionCheckpoint",
    "RawStoredEventEnvelope",
    "RawTranscriptDomainPrefixFact",
    "STORED_EVENT_ENVELOPE_VERSION",
]
