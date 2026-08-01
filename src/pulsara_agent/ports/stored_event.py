"""Renderer-neutral committed-storage receipt and restored-range carriers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol, Sequence, TypeAlias

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.primitives._context_base import canonical_json_bytes
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope

_PAIR_FACTORY_TOKEN = object()
_ENCODER_CONTRACT_FINGERPRINT = context_fingerprint(
    "stored-event-encoder-contract:v1",
    "canonical-agent-event-schema-bound-encode-once",
)


def _event_type(event: AgentEvent) -> str:
    return str(event.type)


def _validate_owned_raw_join(
    event: AgentEvent, envelope: RawStoredEventEnvelope
) -> None:
    envelope.__post_init__()
    if event.sequence is None:
        raise ValueError("stored pair requires a committed event")
    if (
        event.id != envelope.event_id
        or event.run_id != envelope.run_id
        or event.turn_id != envelope.turn_id
        or event.reply_id != envelope.reply_id
        or event.sequence != envelope.sequence
        or _event_type(event) != envelope.event_type
    ):
        raise ValueError("owned event and raw envelope identity mismatch")
    owned_payload = canonical_json_bytes(event.model_dump(mode="json"))
    if owned_payload != envelope.canonical_payload_bytes:
        raise ValueError("owned event and raw envelope payload mismatch")


def _pair_payload(
    *,
    event: AgentEvent,
    envelope: RawStoredEventEnvelope,
    proof_kind: Literal["encoder_built", "decoder_hydrated"],
    contract_fingerprint: str,
) -> dict[str, object]:
    _validate_owned_raw_join(event, envelope)
    return {
        "proof_kind": proof_kind,
        "runtime_session_id": envelope.runtime_session_id,
        "event_id": envelope.event_id,
        "sequence": envelope.sequence,
        "event_type": envelope.event_type,
        "event_schema_version": envelope.event_schema_version,
        "event_schema_fingerprint": envelope.event_schema_fingerprint,
        "event_domain_contract_fingerprint": (
            envelope.event_domain_contract_fingerprint
        ),
        "payload_fingerprint": envelope.payload_fingerprint,
        "envelope_fingerprint": envelope.envelope_fingerprint,
        "codec_contract_fingerprint": contract_fingerprint,
    }


def _validate_pair_fingerprint(
    *,
    event: AgentEvent,
    envelope: RawStoredEventEnvelope,
    proof_kind: Literal["encoder_built", "decoder_hydrated"],
    contract_fingerprint: str,
    pair_fingerprint: str,
    factory_token: object,
) -> None:
    if factory_token is not _PAIR_FACTORY_TOKEN:
        raise TypeError("stored event pair proofs are factory-owned")
    expected = context_fingerprint(
        "stored-event-pair-proof:v1",
        _pair_payload(
            event=event,
            envelope=envelope,
            proof_kind=proof_kind,
            contract_fingerprint=contract_fingerprint,
        ),
    )
    if expected != pair_fingerprint:
        raise ValueError("stored event pair proof fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class EncoderBuiltStoredEventPair:
    owned_stored_event: AgentEvent
    raw_stored_envelope: RawStoredEventEnvelope
    encoder_contract_fingerprint: str
    pair_fingerprint: str
    _factory_token: object

    def __post_init__(self) -> None:
        _validate_pair_fingerprint(
            event=self.owned_stored_event,
            envelope=self.raw_stored_envelope,
            proof_kind="encoder_built",
            contract_fingerprint=self.encoder_contract_fingerprint,
            pair_fingerprint=self.pair_fingerprint,
            factory_token=self._factory_token,
        )

    @property
    def codec_contract_fingerprint(self) -> str:
        return self.encoder_contract_fingerprint


@dataclass(frozen=True, slots=True)
class DecoderHydratedStoredEventPair:
    owned_stored_event: AgentEvent
    raw_stored_envelope: RawStoredEventEnvelope
    historical_decoder_contract_fingerprint: str
    pair_fingerprint: str
    _factory_token: object

    def __post_init__(self) -> None:
        _validate_pair_fingerprint(
            event=self.owned_stored_event,
            envelope=self.raw_stored_envelope,
            proof_kind="decoder_hydrated",
            contract_fingerprint=self.historical_decoder_contract_fingerprint,
            pair_fingerprint=self.pair_fingerprint,
            factory_token=self._factory_token,
        )

    @property
    def codec_contract_fingerprint(self) -> str:
        return self.historical_decoder_contract_fingerprint


_StoredEventPairProof: TypeAlias = (
    EncoderBuiltStoredEventPair | DecoderHydratedStoredEventPair
)


def _build_encoder_pair(
    *, event: AgentEvent, envelope: RawStoredEventEnvelope
) -> EncoderBuiltStoredEventPair:
    payload = _pair_payload(
        event=event,
        envelope=envelope,
        proof_kind="encoder_built",
        contract_fingerprint=_ENCODER_CONTRACT_FINGERPRINT,
    )
    return EncoderBuiltStoredEventPair(
        owned_stored_event=event,
        raw_stored_envelope=envelope,
        encoder_contract_fingerprint=_ENCODER_CONTRACT_FINGERPRINT,
        pair_fingerprint=context_fingerprint("stored-event-pair-proof:v1", payload),
        _factory_token=_PAIR_FACTORY_TOKEN,
    )


def build_encoder_stored_event_pair(
    event: AgentEvent,
    envelope: RawStoredEventEnvelope,
) -> EncoderBuiltStoredEventPair:
    """Build a proof at the normal canonical encoder boundary."""

    return _build_encoder_pair(event=event, envelope=envelope)


def build_decoder_hydrated_stored_event_pair(
    event: AgentEvent,
    envelope: RawStoredEventEnvelope,
    *,
    decoder_contract_fingerprint: str,
) -> DecoderHydratedStoredEventPair:
    """Bind output already produced by the sole historical decoder owner."""

    payload = _pair_payload(
        event=event,
        envelope=envelope,
        proof_kind="decoder_hydrated",
        contract_fingerprint=decoder_contract_fingerprint,
    )
    return DecoderHydratedStoredEventPair(
        owned_stored_event=event,
        raw_stored_envelope=envelope,
        historical_decoder_contract_fingerprint=decoder_contract_fingerprint,
        pair_fingerprint=context_fingerprint("stored-event-pair-proof:v1", payload),
        _factory_token=_PAIR_FACTORY_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class StoredEventBatchCommitReceipt:
    """Complete physical transaction result in canonical storage order."""

    owned_stored_events: tuple[AgentEvent, ...]
    raw_stored_envelopes: tuple[RawStoredEventEnvelope, ...]
    ordered_join_fingerprint: str

    def __post_init__(self) -> None:
        if not self.owned_stored_events:
            raise ValueError("stored event batch receipt cannot be empty")
        if len(self.owned_stored_events) != len(self.raw_stored_envelopes):
            raise ValueError("stored event batch receipt tuple length mismatch")
        for event, envelope in zip(
            self.owned_stored_events, self.raw_stored_envelopes, strict=True
        ):
            _validate_owned_raw_join(event, envelope)
        sequences = tuple(item.sequence for item in self.raw_stored_envelopes)
        if sequences != tuple(range(sequences[0], sequences[-1] + 1)):
            raise ValueError("stored event batch receipt must be contiguous")
        sessions = {item.runtime_session_id for item in self.raw_stored_envelopes}
        if len(sessions) != 1:
            raise ValueError("stored event batch receipt crosses runtime sessions")
        expected = context_fingerprint(
            "stored-event-batch-ordered-join:v1",
            tuple(
                (
                    item.runtime_session_id,
                    item.sequence,
                    item.event_id,
                    item.event_type,
                    item.event_schema_version,
                    item.event_schema_fingerprint,
                    item.event_domain_contract_fingerprint,
                    item.payload_fingerprint,
                    item.envelope_fingerprint,
                )
                for item in self.raw_stored_envelopes
            ),
        )
        if self.ordered_join_fingerprint != expected:
            raise ValueError("stored event batch ordered join fingerprint mismatch")


def build_stored_event_batch_commit_receipt(
    pairs: Sequence[_StoredEventPairProof],
) -> StoredEventBatchCommitReceipt:
    if not pairs:
        raise ValueError("stored event batch receipt requires pair proofs")
    owned = tuple(item.owned_stored_event for item in pairs)
    raw = tuple(item.raw_stored_envelope for item in pairs)
    # Validate proof objects before deliberately dropping process-local proof data.
    for pair in pairs:
        pair.__post_init__()
    return StoredEventBatchCommitReceipt(
        owned_stored_events=owned,
        raw_stored_envelopes=raw,
        ordered_join_fingerprint=context_fingerprint(
            "stored-event-batch-ordered-join:v1",
            tuple(
                (
                    item.runtime_session_id,
                    item.sequence,
                    item.event_id,
                    item.event_type,
                    item.event_schema_version,
                    item.event_schema_fingerprint,
                    item.event_domain_contract_fingerprint,
                    item.payload_fingerprint,
                    item.envelope_fingerprint,
                )
                for item in raw
            ),
        ),
    )


RestoredRangeSourceKind = Literal[
    "reopen_restore", "runtime_catch_up", "doctor", "repair"
]


@dataclass(frozen=True, slots=True)
class JoinedRawStoredEventRangeProof:
    """Contiguous historical decode proof without transaction-boundary claims."""

    runtime_session_id: str
    source_kind: RestoredRangeSourceKind
    from_sequence_exclusive: int
    through_sequence: int
    owned_stored_events: tuple[AgentEvent, ...]
    raw_stored_envelopes: tuple[RawStoredEventEnvelope, ...]
    historical_decoder_id: str
    historical_decoder_version: str
    historical_decoder_contract_fingerprint: str
    ordered_range_envelope_accumulator: str
    range_proof_fingerprint: str

    def __post_init__(self) -> None:
        if self.from_sequence_exclusive < 0:
            raise ValueError("restored range lower bound cannot be negative")
        if not self.raw_stored_envelopes:
            raise ValueError("empty restored range uses the no-op disposition")
        if len(self.owned_stored_events) != len(self.raw_stored_envelopes):
            raise ValueError("restored range owned/raw length mismatch")
        expected_sequences = tuple(
            range(self.from_sequence_exclusive + 1, self.through_sequence + 1)
        )
        if (
            tuple(item.sequence for item in self.raw_stored_envelopes)
            != expected_sequences
        ):
            raise ValueError("restored range does not cover its exact interval")
        for event, envelope in zip(
            self.owned_stored_events, self.raw_stored_envelopes, strict=True
        ):
            if envelope.runtime_session_id != self.runtime_session_id:
                raise ValueError("restored range crosses runtime sessions")
            _validate_owned_raw_join(event, envelope)
        accumulator = context_fingerprint(
            "joined-raw-stored-event-range:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "from_sequence_exclusive": self.from_sequence_exclusive,
                "through_sequence": self.through_sequence,
                "ordered_envelopes": tuple(
                    (item.sequence, item.event_id, item.envelope_fingerprint)
                    for item in self.raw_stored_envelopes
                ),
            },
        )
        if accumulator != self.ordered_range_envelope_accumulator:
            raise ValueError("restored range accumulator mismatch")
        expected = context_fingerprint(
            "joined-raw-stored-event-range-proof:v1",
            {
                "source_kind": self.source_kind,
                "runtime_session_id": self.runtime_session_id,
                "from_sequence_exclusive": self.from_sequence_exclusive,
                "through_sequence": self.through_sequence,
                "historical_decoder_id": self.historical_decoder_id,
                "historical_decoder_version": self.historical_decoder_version,
                "historical_decoder_contract_fingerprint": (
                    self.historical_decoder_contract_fingerprint
                ),
                "ordered_range_envelope_accumulator": accumulator,
            },
        )
        if expected != self.range_proof_fingerprint:
            raise ValueError("restored range proof fingerprint mismatch")


class CommittedReducerIngressPort(Protocol):
    """Closed dual ingress for live physical receipts and historical ranges."""

    def apply_live_committed(
        self,
        receipt: StoredEventBatchCommitReceipt,
    ) -> object: ...

    def fold_restored_range(
        self,
        range_proof: JoinedRawStoredEventRangeProof,
    ) -> object: ...


class CommittedReducerRebuildPort(Protocol):
    """Reset one reducer before RuntimeSession performs bounded range folding."""

    def reset_for_rebuild(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GroupingIndependentOwnedEventReducerAdapter:
    """Explicit adapter for reducers proven independent of input grouping."""

    apply_owned_events: Callable[[tuple[AgentEvent, ...]], object]
    reset_owned_events: Callable[[], None] | None = None

    def apply_live_committed(
        self,
        receipt: StoredEventBatchCommitReceipt,
    ) -> object:
        return self.apply_owned_events(receipt.owned_stored_events)

    def fold_restored_range(
        self,
        range_proof: JoinedRawStoredEventRangeProof,
    ) -> object:
        return self.apply_owned_events(range_proof.owned_stored_events)

    def reset_for_rebuild(self) -> None:
        if self.reset_owned_events is None:
            raise RuntimeError("committed reducer has no bounded rebuild owner")
        self.reset_owned_events()


__all__ = [
    "CommittedReducerIngressPort",
    "CommittedReducerRebuildPort",
    "GroupingIndependentOwnedEventReducerAdapter",
    "JoinedRawStoredEventRangeProof",
    "RestoredRangeSourceKind",
    "StoredEventBatchCommitReceipt",
    "build_decoder_hydrated_stored_event_pair",
    "build_encoder_stored_event_pair",
    "build_stored_event_batch_commit_receipt",
]
