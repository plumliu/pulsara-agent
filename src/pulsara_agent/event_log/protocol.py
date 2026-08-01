"""EventLog storage boundary."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal, Protocol, Sequence, runtime_checkable

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.event_log.historical_decoder import build_decoder_stored_event_pair
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventSchemaDomainRegistry,
    canonical_event_payload_bytes,
    freeze_event_write_candidate,
    payload_sha256,
)
from pulsara_agent.message.message import Msg
from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationAccountStateFact,
    PhysicalChargeContractFact,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
)
import pulsara_agent.primitives.stored_event as stored_event_types
from pulsara_agent.primitives.stored_event import (
    RawRuntimeProjectionCheckpoint,
    RawTranscriptDomainPrefixFact,
)
import pulsara_agent.ports.stored_event as stored_event_ports
from pulsara_agent.ports.event_write import FrozenEventWriteCandidate


DEFAULT_SPARSE_EVENT_READ_MAX_EVENTS = 16_384
DEFAULT_SPARSE_EVENT_READ_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class EventIdConflict(RuntimeError):
    """An event id already names a different immutable event payload."""

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"Event id already belongs to a different event: {event_id}")


class EventLogWriteConflict(RuntimeError):
    """Conditional append observed a different session high-water mark."""

    def __init__(
        self, *, expected_last_sequence: int, actual_last_sequence: int
    ) -> None:
        self.expected_last_sequence = expected_last_sequence
        self.actual_last_sequence = actual_last_sequence
        super().__init__(
            "EventLog conditional write conflict: "
            f"expected last sequence {expected_last_sequence}, actual {actual_last_sequence}"
        )


class MaterializationAccountStateConflict(RuntimeError):
    """The event ledger and its materialization account lost their shared CAS."""

    def __init__(
        self,
        *,
        expected_state_fingerprint: str | None,
        actual_state_fingerprint: str | None,
    ) -> None:
        self.expected_state_fingerprint = expected_state_fingerprint
        self.actual_state_fingerprint = actual_state_fingerprint
        super().__init__(
            "Ledger materialization account CAS conflict: "
            f"expected {expected_state_fingerprint!r}, "
            f"actual {actual_state_fingerprint!r}"
        )


class EventLogTransactionCompanion(Protocol):
    """Typed business mutation committed with one materialization append.

    The PostgreSQL method runs on the EventLog transaction cursor.  The
    in-memory method must validate and publish atomically from the caller's
    perspective and must not perform fallible work after mutation.
    """

    def apply_postgres(
        self,
        cursor: Any,
        stored_events: Sequence[AgentEvent],
    ) -> None: ...

    def apply_in_memory(
        self,
        stored_events: Sequence[AgentEvent],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class EventLogPreparedCandidateBatchIdentity:
    """Exact sequence-null event batch accepted by a candidate-bound companion."""

    ordered_candidates: tuple[FrozenEventWriteCandidate, ...]
    ordered_candidate_event_ids: tuple[str, ...]
    ordered_candidate_schema_binding_fingerprints: tuple[str, ...]
    ordered_candidate_payload_fingerprints: tuple[str, ...]
    exact_ordered_batch_fingerprint: str

    def __post_init__(self) -> None:
        if not self.ordered_candidates:
            raise ValueError("candidate-bound event batch cannot be empty")
        event_ids = tuple(item.event_id for item in self.ordered_candidates)
        schema_bindings = tuple(
            _candidate_schema_binding_fingerprint(item)
            for item in self.ordered_candidates
        )
        payloads = tuple(item.payload_fingerprint for item in self.ordered_candidates)
        if self.ordered_candidate_event_ids != event_ids:
            raise ValueError("prepared event IDs do not match ordered candidates")
        if self.ordered_candidate_schema_binding_fingerprints != schema_bindings:
            raise ValueError("prepared schema bindings do not match ordered candidates")
        if self.ordered_candidate_payload_fingerprints != payloads:
            raise ValueError("prepared payloads do not match ordered candidates")
        expected = context_fingerprint(
            "event-log-prepared-candidate-batch:v1",
            {
                "ordered_candidate_event_ids": event_ids,
                "ordered_candidate_schema_binding_fingerprints": schema_bindings,
                "ordered_candidate_payload_fingerprints": payloads,
            },
        )
        if self.exact_ordered_batch_fingerprint != expected:
            raise ValueError("prepared event batch fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class EventLogStoredCandidateBatchRebindReceipt:
    """Proof that stored events are exactly the prepared batch plus sequence."""

    exact_ordered_batch_fingerprint: str
    ordered_event_ids: tuple[str, ...]
    ordered_assigned_sequences: tuple[int, ...]
    ordered_normalized_payload_fingerprints: tuple[str, ...]
    sequence_continuity_fingerprint: str
    receipt_fingerprint: str

    def __post_init__(self) -> None:
        count = len(self.ordered_event_ids)
        if (
            count == 0
            or len(self.ordered_assigned_sequences) != count
            or len(self.ordered_normalized_payload_fingerprints) != count
        ):
            raise ValueError("stored candidate rebind receipt shape mismatch")
        first = self.ordered_assigned_sequences[0]
        if self.ordered_assigned_sequences != tuple(range(first, first + count)):
            raise ValueError("stored candidate sequence assignment is not contiguous")
        expected_continuity = context_fingerprint(
            "event-log-stored-sequence-continuity:v1",
            tuple(
                zip(
                    self.ordered_event_ids, self.ordered_assigned_sequences, strict=True
                )
            ),
        )
        if self.sequence_continuity_fingerprint != expected_continuity:
            raise ValueError("stored candidate continuity fingerprint mismatch")
        expected = context_fingerprint(
            "event-log-stored-candidate-rebind-receipt:v1",
            {
                "exact_ordered_batch_fingerprint": self.exact_ordered_batch_fingerprint,
                "ordered_event_ids": self.ordered_event_ids,
                "ordered_assigned_sequences": self.ordered_assigned_sequences,
                "ordered_normalized_payload_fingerprints": (
                    self.ordered_normalized_payload_fingerprints
                ),
                "sequence_continuity_fingerprint": self.sequence_continuity_fingerprint,
            },
        )
        if self.receipt_fingerprint != expected:
            raise ValueError("stored candidate rebind receipt fingerprint mismatch")


@runtime_checkable
class CandidateBoundEventLogTransactionCompanion(
    EventLogTransactionCompanion, Protocol
):
    @property
    def prepared_candidate_batch_identity(
        self,
    ) -> EventLogPreparedCandidateBatchIdentity: ...

    def accept_stored_candidate_rebind_receipt(
        self,
        receipt: EventLogStoredCandidateBatchRebindReceipt,
    ) -> None: ...


@runtime_checkable
class CandidateBatchBindableEventLogTransactionCompanion(Protocol):
    """Intent that becomes an EventLog companion after accounting is frozen."""

    def bind_candidate_batch(
        self,
        candidates: Sequence[FrozenEventWriteCandidate],
    ) -> CandidateBoundEventLogTransactionCompanion: ...


def build_prepared_candidate_batch_identity(
    candidates: Sequence[FrozenEventWriteCandidate],
) -> EventLogPreparedCandidateBatchIdentity:
    ordered = tuple(candidates)
    event_ids = tuple(item.event_id for item in ordered)
    schema_bindings = tuple(
        _candidate_schema_binding_fingerprint(item) for item in ordered
    )
    payloads = tuple(item.payload_fingerprint for item in ordered)
    return EventLogPreparedCandidateBatchIdentity(
        ordered_candidates=ordered,
        ordered_candidate_event_ids=event_ids,
        ordered_candidate_schema_binding_fingerprints=schema_bindings,
        ordered_candidate_payload_fingerprints=payloads,
        exact_ordered_batch_fingerprint=context_fingerprint(
            "event-log-prepared-candidate-batch:v1",
            {
                "ordered_candidate_event_ids": event_ids,
                "ordered_candidate_schema_binding_fingerprints": schema_bindings,
                "ordered_candidate_payload_fingerprints": payloads,
            },
        ),
    )


def rebind_stored_candidate_batch(
    prepared: EventLogPreparedCandidateBatchIdentity,
    stored_events: Sequence[AgentEvent],
    *,
    registry: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> EventLogStoredCandidateBatchRebindReceipt:
    stored = tuple(stored_events)
    candidates = prepared.ordered_candidates
    if len(stored) != len(candidates):
        raise ValueError("stored event batch is not the prepared candidate batch")
    normalized_fingerprints: list[str] = []
    assigned_sequences: list[int] = []
    for candidate, event in zip(candidates, stored, strict=True):
        if event.id != candidate.event_id or event.sequence is None:
            raise ValueError("stored event identity/sequence does not match candidate")
        binding = registry.resolve_historical_binding(
            event_type=candidate.event_type,
            event_schema_version=candidate.event_schema_version,
            event_schema_fingerprint=candidate.event_schema_fingerprint,
            event_domain_contract_fingerprint=(
                candidate.event_domain_contract_fingerprint
            ),
        )
        normalized_payload = event.model_dump(mode="json")
        normalized_payload["sequence"] = None
        normalized_bytes = canonical_json_bytes(normalized_payload)
        rebound = binding.decode_owned_payload(normalized_bytes)
        if not isinstance(rebound, type(event)):
            raise ValueError("historical candidate decoder changed event type")
        if canonical_event_payload_bytes(rebound) != normalized_bytes:
            raise ValueError("historical candidate re-encoding is not canonical")
        normalized_fingerprint = payload_sha256(normalized_bytes)
        if (
            normalized_bytes != candidate.canonical_payload_bytes
            or normalized_fingerprint != candidate.payload_fingerprint
        ):
            raise ValueError("stored event differs from sequence-null candidate")
        normalized_fingerprints.append(normalized_fingerprint)
        assigned_sequences.append(event.sequence)
    event_ids = tuple(item.event_id for item in candidates)
    sequences = tuple(assigned_sequences)
    payloads = tuple(normalized_fingerprints)
    continuity = context_fingerprint(
        "event-log-stored-sequence-continuity:v1",
        tuple(zip(event_ids, sequences, strict=True)),
    )
    receipt_payload = {
        "exact_ordered_batch_fingerprint": prepared.exact_ordered_batch_fingerprint,
        "ordered_event_ids": event_ids,
        "ordered_assigned_sequences": sequences,
        "ordered_normalized_payload_fingerprints": payloads,
        "sequence_continuity_fingerprint": continuity,
    }
    return EventLogStoredCandidateBatchRebindReceipt(
        **receipt_payload,
        receipt_fingerprint=context_fingerprint(
            "event-log-stored-candidate-rebind-receipt:v1",
            receipt_payload,
        ),
    )


def _candidate_schema_binding_fingerprint(
    candidate: FrozenEventWriteCandidate,
) -> str:
    return context_fingerprint(
        "event-log-candidate-schema-binding:v1",
        {
            "event_type": candidate.event_type,
            "event_schema_version": candidate.event_schema_version,
            "event_schema_fingerprint": candidate.event_schema_fingerprint,
            "event_domain_contract_fingerprint": (
                candidate.event_domain_contract_fingerprint
            ),
        },
    )


@dataclass(frozen=True, slots=True)
class EventBatchConfirmation:
    committed_events: tuple[AgentEvent, ...]
    missing_event_ids: tuple[str, ...]
    actual_last_sequence: int


@dataclass(frozen=True, slots=True)
class StoredEventCandidateMatch:
    candidate_index: int
    candidate_event_id: str
    candidate_payload_fingerprint: str
    owned_stored_event: AgentEvent
    raw_stored_envelope: stored_event_types.RawStoredEventEnvelope
    join_fingerprint: str


@dataclass(frozen=True, slots=True)
class EventBatchConfirmationEvidence:
    exact_ordered_candidate_batch_fingerprint: str
    matched_candidates: tuple[StoredEventCandidateMatch, ...]
    missing_event_ids: tuple[str, ...]
    actual_last_sequence: int
    evidence_fingerprint: str


EventBatchConfirmationDisposition = Literal[
    "full", "none", "partial", "conflict", "unavailable"
]


@dataclass(frozen=True, slots=True)
class ConfirmedFullStoredBatch:
    receipt: stored_event_ports.StoredEventBatchCommitReceipt
    confirmation_evidence_fingerprint: str
    classifier_contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class StoredEventBatchConfirmation:
    disposition: EventBatchConfirmationDisposition
    evidence: EventBatchConfirmationEvidence | None
    confirmed_full_batch: ConfirmedFullStoredBatch | None

    def __post_init__(self) -> None:
        if (self.disposition == "full") != (self.confirmed_full_batch is not None):
            raise ValueError("only FULL confirmation can carry a stored batch receipt")
        if self.disposition != "unavailable" and self.evidence is None:
            raise ValueError("available confirmation requires typed evidence")
        if self.disposition == "unavailable" and self.evidence is not None:
            raise ValueError("unavailable confirmation cannot claim read evidence")


_STORED_BATCH_CONFIRMATION_CLASSIFIER_FINGERPRINT = context_fingerprint(
    "stored-event-candidate-classifier-contract:v1",
    "full|none|partial|conflict|unavailable;ordered-contiguous-exact-payload",
)


def classify_stored_event_candidate_batch(
    *,
    candidates: Sequence[AgentEvent],
    raw_by_event_id: dict[str, stored_event_types.RawStoredEventEnvelope],
    actual_last_sequence: int,
    schema_registry: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> StoredEventBatchConfirmation:
    """Classify exact candidate IDs against canonical stored rows."""

    frozen = tuple(
        freeze_event_write_candidate(item.model_copy(update={"sequence": None}))
        for item in candidates
    )
    candidate_batch_fingerprint = context_fingerprint(
        "exact-ordered-stored-event-candidate-batch:v1",
        tuple(item.fingerprint_payload() for item in frozen),
    )
    matches: list[StoredEventCandidateMatch] = []
    pair_proofs: list[object] = []
    missing: list[str] = []
    conflict = False
    seen_ids: set[str] = set()
    for index, (candidate, frozen_candidate) in enumerate(
        zip(candidates, frozen, strict=True)
    ):
        if candidate.id in seen_ids:
            conflict = True
            continue
        seen_ids.add(candidate.id)
        envelope = raw_by_event_id.get(candidate.id)
        if envelope is None:
            missing.append(candidate.id)
            continue
        if not same_event_raw_payload(candidate, envelope):
            conflict = True
            continue
        try:
            pair = build_decoder_stored_event_pair(envelope, schema_registry)
        except Exception:
            conflict = True
            continue
        owned = pair.owned_stored_event
        join_payload = {
            "candidate_index": index,
            "candidate_event_id": candidate.id,
            "candidate_payload_fingerprint": frozen_candidate.payload_fingerprint,
            "stored_sequence": envelope.sequence,
            "stored_payload_fingerprint": envelope.payload_fingerprint,
            "stored_envelope_fingerprint": envelope.envelope_fingerprint,
            "pair_fingerprint": pair.pair_fingerprint,
        }
        matches.append(
            StoredEventCandidateMatch(
                candidate_index=index,
                candidate_event_id=candidate.id,
                candidate_payload_fingerprint=frozen_candidate.payload_fingerprint,
                owned_stored_event=owned,
                raw_stored_envelope=envelope,
                join_fingerprint=context_fingerprint(
                    "stored-event-candidate-match:v1", join_payload
                ),
            )
        )
        pair_proofs.append(pair)
    sequences = tuple(item.raw_stored_envelope.sequence for item in matches)
    if matches and (
        tuple(item.candidate_index for item in matches)
        != tuple(sorted(item.candidate_index for item in matches))
        or sequences != tuple(range(sequences[0], sequences[-1] + 1))
    ):
        conflict = True
    evidence_payload = {
        "exact_ordered_candidate_batch_fingerprint": candidate_batch_fingerprint,
        "matched_candidate_joins": tuple(item.join_fingerprint for item in matches),
        "missing_event_ids": tuple(missing),
        "actual_last_sequence": actual_last_sequence,
    }
    evidence = EventBatchConfirmationEvidence(
        exact_ordered_candidate_batch_fingerprint=candidate_batch_fingerprint,
        matched_candidates=tuple(matches),
        missing_event_ids=tuple(missing),
        actual_last_sequence=actual_last_sequence,
        evidence_fingerprint=context_fingerprint(
            "event-batch-confirmation-evidence:v1", evidence_payload
        ),
    )
    if conflict:
        disposition: EventBatchConfirmationDisposition = "conflict"
    elif not matches:
        disposition = "none"
    elif missing:
        disposition = "partial"
    elif len(matches) == len(candidates):
        disposition = "full"
    else:
        disposition = "conflict"
    if disposition != "full":
        return StoredEventBatchConfirmation(
            disposition=disposition,
            evidence=evidence,
            confirmed_full_batch=None,
        )
    receipt = stored_event_ports.build_stored_event_batch_commit_receipt(pair_proofs)
    return StoredEventBatchConfirmation(
        disposition="full",
        evidence=evidence,
        confirmed_full_batch=ConfirmedFullStoredBatch(
            receipt=receipt,
            confirmation_evidence_fingerprint=evidence.evidence_fingerprint,
            classifier_contract_fingerprint=(
                _STORED_BATCH_CONFIRMATION_CLASSIFIER_FINGERPRINT
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class EventLogReadSnapshot:
    """One atomic ordered read boundary from a single runtime ledger."""

    through_sequence: int
    events: tuple[AgentEvent, ...]


@dataclass(frozen=True, slots=True)
class RawEventLogReadSnapshot:
    through_sequence: int
    events: tuple[stored_event_types.RawStoredEventEnvelope, ...]
    snapshot_fingerprint: str

    def __post_init__(self) -> None:
        if self.events:
            sequences = tuple(item.sequence for item in self.events)
            if sequences != tuple(range(sequences[0], sequences[-1] + 1)):
                raise ValueError("raw event snapshot must be contiguous")
            if sequences[-1] != self.through_sequence:
                raise ValueError("raw event snapshot does not reach its high-water")
        expected = context_fingerprint(
            "raw-event-log-read-snapshot:v1",
            {
                "through_sequence": self.through_sequence,
                "envelopes": tuple(item.envelope_fingerprint for item in self.events),
            },
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("raw event snapshot fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RawEventIdSelectionSnapshot:
    """One atomic high-water plus an exact, caller-ordered ID selection."""

    through_sequence: int
    events: tuple[stored_event_types.RawStoredEventEnvelope, ...]


@dataclass(frozen=True, slots=True)
class RawEventTypeSelectionSnapshot:
    """One atomic high-water plus a sparse, type-filtered event selection."""

    through_sequence: int
    events: tuple[stored_event_types.RawStoredEventEnvelope, ...]

    def __post_init__(self) -> None:
        sequences = tuple(item.sequence for item in self.events)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(
            set(sequences)
        ):
            raise ValueError("raw event type selection must be ordered and unique")
        if sequences and sequences[-1] > self.through_sequence:
            raise ValueError("raw event type selection exceeds its high-water")


@dataclass(frozen=True, slots=True)
class RawTranscriptDomainDeltaSnapshot:
    runtime_session_id: str
    before: RawTranscriptDomainPrefixFact
    after: RawTranscriptDomainPrefixFact
    semantic_events: tuple[stored_event_types.RawStoredEventEnvelope, ...]
    registry_contract_fingerprint: str
    snapshot_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        runtime_session_id: str,
        before: RawTranscriptDomainPrefixFact,
        after: RawTranscriptDomainPrefixFact,
        semantic_events: tuple[stored_event_types.RawStoredEventEnvelope, ...],
        registry_contract_fingerprint: str,
    ) -> "RawTranscriptDomainDeltaSnapshot":
        values = {
            "runtime_session_id": runtime_session_id,
            "before": before,
            "after": after,
            "semantic_events": semantic_events,
            "registry_contract_fingerprint": registry_contract_fingerprint,
        }
        return cls(
            **values,
            snapshot_fingerprint=context_fingerprint(
                "raw-transcript-domain-delta-snapshot:v1",
                {
                    "runtime_session_id": runtime_session_id,
                    "before": asdict(before),
                    "after": asdict(after),
                    "semantic_envelopes": tuple(
                        item.envelope_fingerprint for item in semantic_events
                    ),
                    "registry_contract_fingerprint": (registry_contract_fingerprint),
                },
            ),
        )

    def __post_init__(self) -> None:
        if not self.runtime_session_id or not self.registry_contract_fingerprint:
            raise ValueError("transcript domain delta identity is required")
        if self.after.through_sequence < self.before.through_sequence:
            raise ValueError("transcript domain delta range is reversed")
        expected_count = (
            self.after.semantic_event_count - self.before.semantic_event_count
        )
        if expected_count != len(self.semantic_events):
            raise ValueError("transcript semantic delta count proof mismatch")
        sequences = tuple(item.sequence for item in self.semantic_events)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(
            set(sequences)
        ):
            raise ValueError("transcript semantic delta must be ordered and unique")
        if any(
            item.sequence <= self.before.through_sequence
            or item.sequence > self.after.through_sequence
            for item in self.semantic_events
        ):
            raise ValueError("transcript semantic delta exceeds proven range")
        expected = context_fingerprint(
            "raw-transcript-domain-delta-snapshot:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "before": asdict(self.before),
                "after": asdict(self.after),
                "semantic_envelopes": tuple(
                    item.envelope_fingerprint for item in self.semantic_events
                ),
                "registry_contract_fingerprint": self.registry_contract_fingerprint,
            },
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("transcript semantic delta snapshot fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RawEventSelectionBounds:
    max_events: int
    max_payload_bytes: int

    def __post_init__(self) -> None:
        if self.max_events < 1 or self.max_payload_bytes < 1:
            raise ValueError("raw event selection bounds must be positive")


@dataclass(frozen=True, slots=True)
class RawLedgerUsageSnapshot:
    """Bounded aggregate used by AP0 physical-account shadow bootstrap."""

    through_sequence: int
    event_count: int
    candidate_payload_bytes: int

    def __post_init__(self) -> None:
        if (
            min(
                self.through_sequence,
                self.event_count,
                self.candidate_payload_bytes,
            )
            < 0
        ):
            raise ValueError("ledger usage snapshot values must be non-negative")
        if self.event_count != self.through_sequence:
            raise ValueError("append-only ledger event count must equal high-water")


def _selection_bounds_payload(bounds: RawEventSelectionBounds) -> dict[str, int]:
    return {
        "max_events": bounds.max_events,
        "max_payload_bytes": bounds.max_payload_bytes,
    }


@dataclass(frozen=True, slots=True)
class RawContextAuthorityBundleRequest:
    primary_minimum_sequence: int
    run_id: str
    run_sparse_event_types: tuple[str, ...]
    session_sparse_event_types: tuple[str, ...]
    exact_event_ids: tuple[str, ...]
    primary_bounds: RawEventSelectionBounds
    run_sparse_bounds: RawEventSelectionBounds
    session_sparse_bounds: RawEventSelectionBounds
    exact_bounds: RawEventSelectionBounds

    def __post_init__(self) -> None:
        if self.primary_minimum_sequence < 1:
            raise ValueError("authority bundle primary sequence must be positive")
        if not self.run_id:
            raise ValueError("authority bundle run id is required")
        for values, label in (
            (self.run_sparse_event_types, "run sparse event types"),
            (self.session_sparse_event_types, "session sparse event types"),
            (self.exact_event_ids, "exact event ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"authority bundle {label} must be unique")
        if len(self.exact_event_ids) > self.exact_bounds.max_events:
            raise ValueError("authority bundle exact ids exceed their event bound")

    @property
    def request_fingerprint(self) -> str:
        return context_fingerprint(
            "raw-context-authority-bundle-request:v1",
            {
                "primary_minimum_sequence": self.primary_minimum_sequence,
                "run_id": self.run_id,
                "run_sparse_event_types": self.run_sparse_event_types,
                "session_sparse_event_types": self.session_sparse_event_types,
                "exact_event_ids": self.exact_event_ids,
                "primary_bounds": _selection_bounds_payload(self.primary_bounds),
                "run_sparse_bounds": _selection_bounds_payload(self.run_sparse_bounds),
                "session_sparse_bounds": _selection_bounds_payload(
                    self.session_sparse_bounds
                ),
                "exact_bounds": _selection_bounds_payload(self.exact_bounds),
            },
        )


@dataclass(frozen=True, slots=True)
class RawContextAuthorityBundle:
    runtime_session_id: str
    request_fingerprint: str
    through_sequence: int
    primary_events: tuple[stored_event_types.RawStoredEventEnvelope, ...]
    run_sparse_events: tuple[stored_event_types.RawStoredEventEnvelope, ...]
    session_sparse_events: tuple[stored_event_types.RawStoredEventEnvelope, ...]
    exact_events: tuple[stored_event_types.RawStoredEventEnvelope, ...]
    ledger_prefix: RawTranscriptDomainPrefixFact
    snapshot_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        runtime_session_id: str,
        request: RawContextAuthorityBundleRequest,
        through_sequence: int,
        primary_events: tuple[stored_event_types.RawStoredEventEnvelope, ...],
        run_sparse_events: tuple[stored_event_types.RawStoredEventEnvelope, ...],
        session_sparse_events: tuple[stored_event_types.RawStoredEventEnvelope, ...],
        exact_events: tuple[stored_event_types.RawStoredEventEnvelope, ...],
        ledger_prefix: RawTranscriptDomainPrefixFact,
    ) -> "RawContextAuthorityBundle":
        if ledger_prefix.through_sequence != through_sequence:
            raise ValueError("authority bundle ledger prefix high-water drifted")
        if primary_events:
            if primary_events[0].sequence != request.primary_minimum_sequence:
                raise ValueError("authority bundle primary start sequence drifted")
            if primary_events[-1].sequence != through_sequence:
                raise ValueError("authority bundle primary range is truncated")
        elif request.primary_minimum_sequence <= through_sequence:
            raise ValueError("authority bundle primary range is unexpectedly empty")
        values = {
            "runtime_session_id": runtime_session_id,
            "request_fingerprint": request.request_fingerprint,
            "through_sequence": through_sequence,
            "primary_events": primary_events,
            "run_sparse_events": run_sparse_events,
            "session_sparse_events": session_sparse_events,
            "exact_events": exact_events,
            "ledger_prefix": ledger_prefix,
        }
        return cls(
            **values,
            snapshot_fingerprint=context_fingerprint(
                "raw-context-authority-bundle:v1",
                {
                    **values,
                    "ledger_prefix": asdict(ledger_prefix),
                    "primary_events": tuple(
                        item.envelope_fingerprint for item in primary_events
                    ),
                    "run_sparse_events": tuple(
                        item.envelope_fingerprint for item in run_sparse_events
                    ),
                    "session_sparse_events": tuple(
                        item.envelope_fingerprint for item in session_sparse_events
                    ),
                    "exact_events": tuple(
                        item.envelope_fingerprint for item in exact_events
                    ),
                },
            ),
        )

    def __post_init__(self) -> None:
        if not self.runtime_session_id or self.through_sequence < 0:
            raise ValueError("authority bundle identity is invalid")
        if self.ledger_prefix.through_sequence != self.through_sequence:
            raise ValueError("authority bundle prefix high-water mismatch")
        for events, label in (
            (self.primary_events, "primary"),
            (self.run_sparse_events, "run sparse"),
            (self.session_sparse_events, "session sparse"),
            (self.exact_events, "exact"),
        ):
            sequences = tuple(item.sequence for item in events)
            if sequences != tuple(sorted(sequences)) or len(sequences) != len(
                set(sequences)
            ):
                raise ValueError(f"authority bundle {label} events are not ordered")
            if sequences and sequences[-1] > self.through_sequence:
                raise ValueError(f"authority bundle {label} exceeds its high-water")
        primary_sequences = tuple(item.sequence for item in self.primary_events)
        if primary_sequences and primary_sequences != tuple(
            range(primary_sequences[0], self.through_sequence + 1)
        ):
            raise ValueError("authority bundle primary events are not contiguous")
        expected = context_fingerprint(
            "raw-context-authority-bundle:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "request_fingerprint": self.request_fingerprint,
                "through_sequence": self.through_sequence,
                "primary_events": tuple(
                    item.envelope_fingerprint for item in self.primary_events
                ),
                "run_sparse_events": tuple(
                    item.envelope_fingerprint for item in self.run_sparse_events
                ),
                "session_sparse_events": tuple(
                    item.envelope_fingerprint for item in self.session_sparse_events
                ),
                "exact_events": tuple(
                    item.envelope_fingerprint for item in self.exact_events
                ),
                "ledger_prefix": asdict(self.ledger_prefix),
            },
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("authority bundle fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RawReplyEventGroup:
    reply_id: str
    events: tuple[stored_event_types.RawStoredEventEnvelope, ...]

    def __post_init__(self) -> None:
        if not self.reply_id:
            raise ValueError("reply event group id is required")
        if any(item.reply_id != self.reply_id for item in self.events):
            raise ValueError("reply event group contains another reply")
        sequences = tuple(item.sequence for item in self.events)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(
            set(sequences)
        ):
            raise ValueError("reply event group must be ordered and unique")


@dataclass(frozen=True, slots=True)
class RawReplySelectionSnapshot:
    """One bounded multi-reply read through a caller-frozen ledger high-water."""

    through_sequence: int
    groups: tuple[RawReplyEventGroup, ...]

    def __post_init__(self) -> None:
        reply_ids = tuple(item.reply_id for item in self.groups)
        if len(reply_ids) != len(set(reply_ids)):
            raise ValueError("reply selection groups must be unique")
        if any(
            event.sequence > self.through_sequence
            for group in self.groups
            for event in group.events
        ):
            raise ValueError("reply selection exceeds its frozen high-water")


@dataclass(frozen=True, slots=True)
class RawCheckpointLedgerCandidate:
    """One checkpoint catalog row and its authority delta from one DB snapshot."""

    checkpoint_id: str
    checkpoint_through_sequence: int
    checkpoint_event: stored_event_types.RawStoredEventEnvelope
    delta_events: tuple[stored_event_types.RawStoredEventEnvelope, ...]
    delta_event_count: int
    delta_payload_bytes: int
    event_bound_satisfied: bool
    byte_bound_satisfied: bool

    def __post_init__(self) -> None:
        if not self.checkpoint_id:
            raise ValueError("checkpoint ledger candidate id is required")
        if self.checkpoint_through_sequence < 1:
            raise ValueError("checkpoint ledger candidate through sequence is invalid")
        if self.delta_event_count < 0 or self.delta_payload_bytes < 0:
            raise ValueError("checkpoint ledger candidate delta accounting is invalid")
        if self.event_bound_satisfied:
            if len(self.delta_events) != self.delta_event_count:
                raise ValueError("checkpoint ledger candidate delta count mismatch")
            expected = tuple(
                range(
                    self.checkpoint_through_sequence + 1,
                    self.checkpoint_through_sequence + self.delta_event_count + 1,
                )
            )
            if tuple(item.sequence for item in self.delta_events) != expected:
                raise ValueError("checkpoint ledger candidate delta is not contiguous")
            actual_bytes = sum(
                len(item.canonical_payload_bytes) for item in self.delta_events
            )
            if actual_bytes != self.delta_payload_bytes:
                raise ValueError("checkpoint ledger candidate byte count mismatch")
        elif self.delta_events:
            raise ValueError("out-of-bound checkpoint delta must not carry events")


@dataclass(frozen=True, slots=True)
class RawCheckpointLedgerSnapshot:
    """Checkpoint catalog and bounded deltas captured under one ledger snapshot."""

    runtime_session_id: str
    requested_through_sequence: int
    ledger_high_water_observed: int
    candidates: tuple[RawCheckpointLedgerCandidate, ...]
    confirmed_checkpoint_count: int
    contract_compatible_checkpoint_count: int
    nearest_compatible_checkpoint_id: str | None
    nearest_compatible_checkpoint_through_sequence: int | None
    snapshot_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        runtime_session_id: str,
        requested_through_sequence: int,
        ledger_high_water_observed: int,
        candidates: tuple[RawCheckpointLedgerCandidate, ...],
        confirmed_checkpoint_count: int,
        contract_compatible_checkpoint_count: int,
        nearest_compatible_checkpoint_id: str | None,
        nearest_compatible_checkpoint_through_sequence: int | None,
    ) -> "RawCheckpointLedgerSnapshot":
        values = {
            "runtime_session_id": runtime_session_id,
            "requested_through_sequence": requested_through_sequence,
            "ledger_high_water_observed": ledger_high_water_observed,
            "candidates": candidates,
            "confirmed_checkpoint_count": confirmed_checkpoint_count,
            "contract_compatible_checkpoint_count": (
                contract_compatible_checkpoint_count
            ),
            "nearest_compatible_checkpoint_id": nearest_compatible_checkpoint_id,
            "nearest_compatible_checkpoint_through_sequence": (
                nearest_compatible_checkpoint_through_sequence
            ),
        }
        fingerprint_payload = {
            **values,
            "candidates": tuple(
                {
                    "checkpoint_id": item.checkpoint_id,
                    "checkpoint_through_sequence": item.checkpoint_through_sequence,
                    "checkpoint_envelope": item.checkpoint_event.envelope_fingerprint,
                    "delta_envelopes": tuple(
                        event.envelope_fingerprint for event in item.delta_events
                    ),
                    "delta_event_count": item.delta_event_count,
                    "delta_payload_bytes": item.delta_payload_bytes,
                    "event_bound_satisfied": item.event_bound_satisfied,
                    "byte_bound_satisfied": item.byte_bound_satisfied,
                }
                for item in candidates
            ),
        }
        return cls(
            **values,
            snapshot_fingerprint=context_fingerprint(
                "raw-checkpoint-ledger-snapshot:v1", fingerprint_payload
            ),
        )

    def __post_init__(self) -> None:
        if not self.runtime_session_id:
            raise ValueError("checkpoint ledger snapshot runtime session is required")
        if self.requested_through_sequence < 1:
            raise ValueError("checkpoint ledger requested high-water is invalid")
        if self.ledger_high_water_observed < self.requested_through_sequence:
            raise ValueError(
                "checkpoint ledger snapshot does not cover requested prefix"
            )
        if self.confirmed_checkpoint_count < len(self.candidates):
            raise ValueError("checkpoint ledger catalog count is inconsistent")
        if self.contract_compatible_checkpoint_count < len(self.candidates):
            raise ValueError("checkpoint compatible count is inconsistent")
        candidate_ids = tuple(item.checkpoint_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("checkpoint ledger candidates must be unique")
        expected = context_fingerprint(
            "raw-checkpoint-ledger-snapshot:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "requested_through_sequence": self.requested_through_sequence,
                "ledger_high_water_observed": self.ledger_high_water_observed,
                "candidates": tuple(
                    {
                        "checkpoint_id": item.checkpoint_id,
                        "checkpoint_through_sequence": (
                            item.checkpoint_through_sequence
                        ),
                        "checkpoint_envelope": (
                            item.checkpoint_event.envelope_fingerprint
                        ),
                        "delta_envelopes": tuple(
                            event.envelope_fingerprint for event in item.delta_events
                        ),
                        "delta_event_count": item.delta_event_count,
                        "delta_payload_bytes": item.delta_payload_bytes,
                        "event_bound_satisfied": item.event_bound_satisfied,
                        "byte_bound_satisfied": item.byte_bound_satisfied,
                    }
                    for item in self.candidates
                ),
                "confirmed_checkpoint_count": self.confirmed_checkpoint_count,
                "contract_compatible_checkpoint_count": (
                    self.contract_compatible_checkpoint_count
                ),
                "nearest_compatible_checkpoint_id": (
                    self.nearest_compatible_checkpoint_id
                ),
                "nearest_compatible_checkpoint_through_sequence": (
                    self.nearest_compatible_checkpoint_through_sequence
                ),
            },
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("checkpoint ledger snapshot fingerprint mismatch")


class EventLog(Protocol):
    """Append-only runtime event log contract."""

    def ensure_runtime_session_owner(self) -> None:
        """Ensure the durable session owner exists before pre-event artifacts."""
        ...

    def append(
        self,
        event: AgentEvent,
        *,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> AgentEvent: ...

    def extend(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[AgentEvent]: ...

    def commit_batch(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> stored_event_ports.StoredEventBatchCommitReceipt: ...

    def read_materialization_account_state(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> LedgerMaterializationAccountStateFact | None: ...

    def read_runtime_projection_checkpoint(
        self,
        projection_kind: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> RawRuntimeProjectionCheckpoint | None: ...

    def write_runtime_projection_checkpoint(
        self,
        checkpoint: RawRuntimeProjectionCheckpoint,
        *,
        deadline_monotonic: float | None = None,
    ) -> None: ...

    def extend_with_materialization_state(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_account_state_fingerprint: str | None,
        resulting_account_state: LedgerMaterializationAccountStateFact,
        physical_charge_contract: PhysicalChargeContractFact,
        transaction_companion: EventLogTransactionCompanion | None = None,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> stored_event_ports.StoredEventBatchCommitReceipt: ...

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
        after_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[AgentEvent]: ...

    def get_by_id(
        self,
        event_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> AgentEvent | None: ...

    def confirm_batch(
        self,
        candidates: Sequence[AgentEvent],
        *,
        deadline_monotonic: float | None = None,
    ) -> EventBatchConfirmation: ...

    def confirm_stored_batch(
        self,
        candidates: Sequence[AgentEvent],
        *,
        deadline_monotonic: float | None = None,
    ) -> StoredEventBatchConfirmation: ...

    def read_ledger_usage_snapshot(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> RawLedgerUsageSnapshot: ...

    def read_range_snapshot(
        self,
        *,
        minimum_sequence: int,
        through_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> EventLogReadSnapshot: ...

    def read_raw_range_snapshot(
        self,
        *,
        minimum_sequence: int,
        through_sequence: int | None = None,
        max_events: int | None = None,
        max_payload_bytes: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> RawEventLogReadSnapshot: ...

    def read_joined_raw_range(
        self,
        *,
        source_kind: stored_event_ports.RestoredRangeSourceKind,
        from_sequence_exclusive: int,
        through_sequence: int | None = None,
        max_events: int | None = None,
        max_payload_bytes: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> stored_event_ports.JoinedRawStoredEventRangeProof | None: ...

    def read_raw_events_by_id(
        self,
        event_ids: tuple[str, ...],
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[stored_event_types.RawStoredEventEnvelope, ...]: ...

    def read_raw_events_by_id_snapshot(
        self,
        event_ids: tuple[str, ...],
        *,
        deadline_monotonic: float | None = None,
    ) -> RawEventIdSelectionSnapshot: ...

    def read_raw_events_by_type(
        self,
        event_type: str,
        *,
        limit: int,
        through_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> tuple[stored_event_types.RawStoredEventEnvelope, ...]: ...

    def read_raw_events_by_types(
        self,
        event_types: tuple[str, ...],
        *,
        active_runs_only: bool = False,
        run_ids: tuple[str, ...] | None = None,
        minimum_sequence: int = 1,
        through_sequence: int | None = None,
        max_events: int = DEFAULT_SPARSE_EVENT_READ_MAX_EVENTS,
        max_payload_bytes: int = DEFAULT_SPARSE_EVENT_READ_MAX_PAYLOAD_BYTES,
        deadline_monotonic: float | None = None,
    ) -> RawEventTypeSelectionSnapshot: ...

    def read_transcript_domain_delta(
        self,
        *,
        after_sequence: int,
        through_sequence: int | None = None,
        max_events: int = DEFAULT_SPARSE_EVENT_READ_MAX_EVENTS,
        max_payload_bytes: int = DEFAULT_SPARSE_EVENT_READ_MAX_PAYLOAD_BYTES,
        registry_contract_fingerprint: str,
        deadline_monotonic: float | None = None,
    ) -> RawTranscriptDomainDeltaSnapshot: ...

    def read_context_authority_bundle(
        self,
        request: RawContextAuthorityBundleRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> RawContextAuthorityBundle: ...

    def read_raw_ledger_prefix(
        self,
        *,
        through_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> RawTranscriptDomainPrefixFact: ...

    def read_raw_reply_events(
        self,
        reply_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[stored_event_types.RawStoredEventEnvelope, ...]: ...

    def read_raw_replies_snapshot(
        self,
        reply_ids: tuple[str, ...],
        *,
        through_sequence: int,
        max_total_events: int,
        max_total_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> RawReplySelectionSnapshot: ...

    def read_raw_run_events(
        self,
        run_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[stored_event_types.RawStoredEventEnvelope, ...]: ...

    def read_raw_model_call_events(
        self,
        resolved_model_call_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[stored_event_types.RawStoredEventEnvelope, ...]: ...

    def read_raw_checkpoint_ledger_snapshot(
        self,
        *,
        checkpoint_event_type: str,
        requested_through_sequence: int,
        graph_reducer_id: str,
        graph_reducer_version: str,
        graph_reducer_contract_fingerprint: str,
        preferred_checkpoint_id: str | None,
        max_delta_events: int,
        max_delta_bytes: int,
        max_checkpoint_candidates: int,
        deadline_monotonic: float | None = None,
    ) -> RawCheckpointLedgerSnapshot: ...

    def replay(self, reply_id: str) -> Msg: ...

    def next_sequence(self, *, deadline_monotonic: float | None = None) -> int: ...


def raw_checkpoint_catalog_identity(
    envelope: stored_event_types.RawStoredEventEnvelope,
) -> tuple[str, int, str, str, str]:
    """Read bounded checkpoint catalog keys without current-union decoding."""

    payload = json.loads(envelope.canonical_payload_bytes.decode("utf-8"))
    checkpoint = payload.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise ValueError("checkpoint catalog payload is missing checkpoint fact")
    checkpoint_id = checkpoint.get("checkpoint_id")
    through_sequence = checkpoint.get("through_sequence")
    reducer_id = checkpoint.get("graph_reducer_id")
    reducer_version = checkpoint.get("graph_reducer_version")
    reducer_fingerprint = checkpoint.get("graph_reducer_contract_fingerprint")
    if (
        not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or not isinstance(through_sequence, int)
        or through_sequence < 1
        or not isinstance(reducer_id, str)
        or not reducer_id
        or not isinstance(reducer_version, str)
        or not reducer_version
        or not isinstance(reducer_fingerprint, str)
        or not reducer_fingerprint
    ):
        raise ValueError("checkpoint catalog identity is malformed")
    return (
        checkpoint_id,
        through_sequence,
        reducer_id,
        reducer_version,
        reducer_fingerprint,
    )


def same_event_payload(candidate: AgentEvent, stored: AgentEvent) -> bool:
    """Compare one immutable event fact while ignoring its assigned sequence."""

    if candidate.id != stored.id:
        return False
    return candidate.model_dump(mode="json", exclude={"sequence"}) == stored.model_dump(
        mode="json",
        exclude={"sequence"},
    )


def same_event_raw_payload(
    candidate: AgentEvent,
    stored: stored_event_types.RawStoredEventEnvelope,
) -> bool:
    """Compare a live candidate with canonical stored bytes before decoding.

    EventLog assigns sequence at commit time, so confirmation normalizes only
    that field.  Every other wrapper and payload field remains immutable.
    """

    if candidate.id != stored.event_id:
        return False
    normalized = candidate.model_copy(update={"sequence": stored.sequence})
    return canonical_event_payload_bytes(normalized) == stored.canonical_payload_bytes
