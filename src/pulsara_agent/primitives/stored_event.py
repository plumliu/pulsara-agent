"""Pure immutable carriers for canonical rows in the runtime event ledger.

This module deliberately has no dependency on ``AgentEvent`` or on the
historical schema registry.  Construction and decoding belong to the EventLog
implementation boundary; this carrier only validates bytes and identity.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from hashlib import sha256

from pulsara_agent.primitives._context_base import canonical_json_bytes
from pulsara_agent.primitives.context import (
    canonical_utc_timestamp,
    context_fingerprint,
)


STORED_EVENT_ENVELOPE_VERSION = "stored-agent-event:v1"
CANONICAL_JSON_OBJECT_CODEC_ID = "pulsara.canonical_json_object"
CANONICAL_JSON_OBJECT_CODEC_VERSION = "1"
_CANONICAL_JSON_OBJECT_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class CanonicalJsonObjectCarrier:
    """Recursively immutable storage carrier for one canonical JSON object.

    The carrier deliberately retains only canonical UTF-8.  Adapters may decode
    a fresh object for JSONB or domain hydration, but producer-owned containers
    can never remain reachable through this fact.
    """

    codec_id: str
    codec_version: str
    canonical_utf8: bytes
    canonical_payload_fingerprint: str
    _factory_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _CANONICAL_JSON_OBJECT_FACTORY_TOKEN:
            raise TypeError("canonical JSON object carriers are factory-owned")
        if self.codec_id != CANONICAL_JSON_OBJECT_CODEC_ID:
            raise ValueError("canonical JSON object codec identity drifted")
        if self.codec_version != CANONICAL_JSON_OBJECT_CODEC_VERSION:
            raise ValueError("canonical JSON object codec version drifted")
        try:
            decoded = json.loads(self.canonical_utf8.decode("utf-8"))
        except Exception as exc:
            raise ValueError("canonical JSON object bytes are invalid") from exc
        if not isinstance(decoded, dict):
            raise ValueError("canonical JSON carrier root must be an object")
        if canonical_json_bytes(decoded) != self.canonical_utf8:
            raise ValueError("canonical JSON object bytes are not canonical")
        expected = context_fingerprint(
            "canonical-json-object-carrier:v1",
            {
                "codec_id": self.codec_id,
                "codec_version": self.codec_version,
                "canonical_utf8": self.canonical_utf8.decode("utf-8"),
            },
        )
        if self.canonical_payload_fingerprint != expected:
            raise ValueError("canonical JSON object fingerprint mismatch")

    def decode_object(self) -> dict[str, object]:
        """Return a fresh adapter/domain value derived from canonical bytes."""

        value = json.loads(self.canonical_utf8.decode("utf-8"))
        if not isinstance(value, dict):  # protected again for type narrowing
            raise ValueError("canonical JSON carrier root must be an object")
        return value


def canonical_json_object_carrier(value: object) -> CanonicalJsonObjectCarrier:
    """Build the only valid storage carrier for a JSON object."""

    if not isinstance(value, dict):
        raise ValueError("canonical JSON carrier root must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError("canonical JSON object keys must be strings")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not strict canonical JSON") from exc
    # json.dumps accepts several Python-only Mapping shapes.  Decode once here
    # and require an object root so every downstream adapter sees JSON values.
    decoded = json.loads(canonical.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("canonical JSON carrier root must be an object")
    return CanonicalJsonObjectCarrier(
        codec_id=CANONICAL_JSON_OBJECT_CODEC_ID,
        codec_version=CANONICAL_JSON_OBJECT_CODEC_VERSION,
        canonical_utf8=canonical,
        canonical_payload_fingerprint=context_fingerprint(
            "canonical-json-object-carrier:v1",
            {
                "codec_id": CANONICAL_JSON_OBJECT_CODEC_ID,
                "codec_version": CANONICAL_JSON_OBJECT_CODEC_VERSION,
                "canonical_utf8": canonical.decode("utf-8"),
            },
        ),
        _factory_token=_CANONICAL_JSON_OBJECT_FACTORY_TOKEN,
    )


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
    validation_base_state: CanonicalJsonObjectCarrier
    state: CanonicalJsonObjectCarrier
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
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.payload_fingerprint) is None:
            raise ValueError("runtime projection checkpoint fingerprint is invalid")

    @property
    def validation_base_state_payload(self) -> dict[str, object]:
        """Fresh compatibility view; canonical bytes remain the authority."""

        return self.validation_base_state.decode_object()

    @property
    def state_payload(self) -> dict[str, object]:
        """Fresh compatibility view; canonical bytes remain the authority."""

        return self.state.decode_object()


def runtime_projection_checkpoint_fingerprint(
    *,
    projection_kind: str,
    through_sequence: int,
    projection_schema_version: str,
    ledger_prefix: RawTranscriptDomainPrefixFact,
    validation_base_through_sequence: int,
    validation_base_state: CanonicalJsonObjectCarrier,
    state: CanonicalJsonObjectCarrier,
) -> str:
    """Return the sole fingerprint representation for checkpoint rows."""

    return context_fingerprint(
        "runtime-projection-checkpoint:v2",
        {
            "projection_kind": projection_kind,
            "through_sequence": through_sequence,
            "projection_schema_version": projection_schema_version,
            "ledger_prefix": asdict(ledger_prefix),
            "validation_base_through_sequence": validation_base_through_sequence,
            "validation_base_state_payload": (validation_base_state.decode_object()),
            "state_payload": state.decode_object(),
        },
    )


def build_raw_runtime_projection_checkpoint(
    *,
    projection_kind: str,
    through_sequence: int,
    projection_schema_version: str,
    ledger_prefix: RawTranscriptDomainPrefixFact,
    validation_base_through_sequence: int,
    validation_base_state: CanonicalJsonObjectCarrier,
    state: CanonicalJsonObjectCarrier,
) -> RawRuntimeProjectionCheckpoint:
    """Build one immutable checkpoint row from already canonical carriers."""

    fingerprint = runtime_projection_checkpoint_fingerprint(
        projection_kind=projection_kind,
        through_sequence=through_sequence,
        projection_schema_version=projection_schema_version,
        ledger_prefix=ledger_prefix,
        validation_base_through_sequence=validation_base_through_sequence,
        validation_base_state=validation_base_state,
        state=state,
    )
    return RawRuntimeProjectionCheckpoint(
        projection_kind=projection_kind,
        through_sequence=through_sequence,
        projection_schema_version=projection_schema_version,
        ledger_prefix=ledger_prefix,
        validation_base_through_sequence=validation_base_through_sequence,
        validation_base_state=validation_base_state,
        state=state,
        payload_fingerprint=fingerprint,
    )


__all__ = [
    "CANONICAL_JSON_OBJECT_CODEC_ID",
    "CANONICAL_JSON_OBJECT_CODEC_VERSION",
    "CanonicalJsonObjectCarrier",
    "RawRuntimeProjectionCheckpoint",
    "RawStoredEventEnvelope",
    "RawTranscriptDomainPrefixFact",
    "STORED_EVENT_ENVELOPE_VERSION",
    "build_raw_runtime_projection_checkpoint",
    "canonical_json_object_carrier",
    "runtime_projection_checkpoint_fingerprint",
]
