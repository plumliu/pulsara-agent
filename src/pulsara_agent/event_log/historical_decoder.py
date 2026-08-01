"""The sole historical-decoder seam for canonical stored event envelopes."""

from __future__ import annotations

from typing import Sequence

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.event_log.serialization import EventSchemaDomainRegistry
from pulsara_agent.ports.stored_event import (
    JoinedRawStoredEventRangeProof,
    RestoredRangeSourceKind,
    build_decoder_hydrated_stored_event_pair,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope


HISTORICAL_STORED_EVENT_DECODER_ID = "pulsara.agent-event"
HISTORICAL_STORED_EVENT_DECODER_VERSION = "v1"


def decode_raw_stored_event_envelope(
    envelope: RawStoredEventEnvelope,
    registry: EventSchemaDomainRegistry,
) -> AgentEvent:
    """Decode one row through its exact historical schema/domain binding."""

    binding = registry.resolve_historical_binding(
        event_type=envelope.event_type,
        event_schema_version=envelope.event_schema_version,
        event_schema_fingerprint=envelope.event_schema_fingerprint,
        event_domain_contract_fingerprint=(envelope.event_domain_contract_fingerprint),
    )
    event = binding.decode_owned_payload(envelope.canonical_payload_bytes)
    if not isinstance(event, object) or getattr(event, "id", None) != envelope.event_id:
        raise ValueError("decoded historical event identity mismatch")
    if getattr(event, "sequence", None) != envelope.sequence:
        raise ValueError("decoded historical event sequence mismatch")
    return event  # type: ignore[return-value]


def build_decoder_stored_event_pair(
    envelope: RawStoredEventEnvelope,
    registry: EventSchemaDomainRegistry,
):
    """Decode and bind one canonical row at the historical-decoder boundary."""

    event = decode_raw_stored_event_envelope(envelope, registry)
    binding = registry.resolve_historical_binding(
        event_type=envelope.event_type,
        event_schema_version=envelope.event_schema_version,
        event_schema_fingerprint=envelope.event_schema_fingerprint,
        event_domain_contract_fingerprint=envelope.event_domain_contract_fingerprint,
    )
    return build_decoder_hydrated_stored_event_pair(
        event,
        envelope,
        decoder_contract_fingerprint=binding.decoder_contract_fingerprint,
    )


def build_joined_raw_stored_event_range_proof(
    *,
    runtime_session_id: str,
    source_kind: RestoredRangeSourceKind,
    from_sequence_exclusive: int,
    raw_stored_envelopes: Sequence[RawStoredEventEnvelope],
    registry: EventSchemaDomainRegistry,
) -> JoinedRawStoredEventRangeProof | None:
    """Decode a contiguous raw range without inventing a transaction receipt."""

    raw = tuple(raw_stored_envelopes)
    if not raw:
        return None
    pairs = tuple(build_decoder_stored_event_pair(item, registry) for item in raw)
    aggregate_decoder_contract = context_fingerprint(
        "historical-event-decoder-range-contract:v1",
        tuple(sorted({item.codec_contract_fingerprint for item in pairs})),
    )
    through = raw[-1].sequence
    accumulator = context_fingerprint(
        "joined-raw-stored-event-range:v1",
        {
            "runtime_session_id": runtime_session_id,
            "from_sequence_exclusive": from_sequence_exclusive,
            "through_sequence": through,
            "ordered_envelopes": tuple(
                (item.sequence, item.event_id, item.envelope_fingerprint)
                for item in raw
            ),
        },
    )
    payload = {
        "source_kind": source_kind,
        "runtime_session_id": runtime_session_id,
        "from_sequence_exclusive": from_sequence_exclusive,
        "through_sequence": through,
        "historical_decoder_id": HISTORICAL_STORED_EVENT_DECODER_ID,
        "historical_decoder_version": HISTORICAL_STORED_EVENT_DECODER_VERSION,
        "historical_decoder_contract_fingerprint": aggregate_decoder_contract,
        "ordered_range_envelope_accumulator": accumulator,
    }
    return JoinedRawStoredEventRangeProof(
        runtime_session_id=runtime_session_id,
        source_kind=source_kind,
        from_sequence_exclusive=from_sequence_exclusive,
        through_sequence=through,
        owned_stored_events=tuple(item.owned_stored_event for item in pairs),
        raw_stored_envelopes=raw,
        historical_decoder_id=HISTORICAL_STORED_EVENT_DECODER_ID,
        historical_decoder_version=HISTORICAL_STORED_EVENT_DECODER_VERSION,
        historical_decoder_contract_fingerprint=aggregate_decoder_contract,
        ordered_range_envelope_accumulator=accumulator,
        range_proof_fingerprint=context_fingerprint(
            "joined-raw-stored-event-range-proof:v1", payload
        ),
    )


__all__ = [
    "HISTORICAL_STORED_EVENT_DECODER_ID",
    "HISTORICAL_STORED_EVENT_DECODER_VERSION",
    "build_decoder_stored_event_pair",
    "build_joined_raw_stored_event_range_proof",
    "decode_raw_stored_event_envelope",
]
