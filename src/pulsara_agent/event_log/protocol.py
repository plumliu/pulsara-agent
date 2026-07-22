"""EventLog storage boundary."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Iterable, Protocol, Sequence

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.event_log.serialization import (
    EventSchemaDomainRegistry,
    canonical_event_created_at,
    canonical_event_payload_bytes,
)
from pulsara_agent.message.message import Msg
from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationAccountStateFact,
    PhysicalChargeContractFact,
)
from pulsara_agent.primitives.context import (
    canonical_utc_timestamp,
    context_fingerprint,
)


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
class EventBatchConfirmation:
    committed_events: tuple[AgentEvent, ...]
    missing_event_ids: tuple[str, ...]
    actual_last_sequence: int


@dataclass(frozen=True, slots=True)
class EventLogReadSnapshot:
    """One atomic ordered read boundary from a single runtime ledger."""

    through_sequence: int
    events: tuple[AgentEvent, ...]


@dataclass(frozen=True, slots=True)
class RawStoredEventEnvelope:
    """Schema-aware immutable storage envelope, before current-union decode."""

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
        if self.stored_envelope_version != "stored-agent-event:v1":
            raise ValueError("unsupported stored event envelope version")
        if self.sequence < 1:
            raise ValueError("stored event sequence must be positive")
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

    @classmethod
    def from_stored_event(
        cls,
        *,
        event: AgentEvent,
        runtime_session_id: str,
        schema_registry: EventSchemaDomainRegistry,
    ) -> "RawStoredEventEnvelope":
        if event.sequence is None or event.sequence < 1:
            raise ValueError("raw stored envelope requires a committed event")
        binding = schema_registry.resolve_for_event(event)
        contract = binding.schema_contract
        payload = canonical_event_payload_bytes(event)
        payload_fingerprint = f"sha256:{sha256(payload).hexdigest()}"
        values = {
            "stored_envelope_version": "stored-agent-event:v1",
            "event_id": event.id,
            "runtime_session_id": runtime_session_id,
            "run_id": event.run_id,
            "turn_id": event.turn_id,
            "reply_id": event.reply_id,
            "sequence": event.sequence,
            "created_at_utc": canonical_event_created_at(event),
            "event_type": str(event.type),
            "event_schema_version": contract.event_schema_version,
            "event_schema_fingerprint": contract.event_schema_fingerprint,
            "event_domain_contract_fingerprint": (contract.domain_contract_fingerprint),
            "canonical_payload_bytes": payload,
            "payload_fingerprint": payload_fingerprint,
        }
        return cls(
            **values,
            envelope_fingerprint=context_fingerprint(
                "stored-agent-event-envelope:v1",
                {
                    key: value
                    for key, value in values.items()
                    if key != "canonical_payload_bytes"
                },
            ),
        )

    def decode_owned(self, registry: EventSchemaDomainRegistry) -> AgentEvent:
        binding = registry.resolve_historical_binding(
            event_type=self.event_type,
            event_schema_version=self.event_schema_version,
            event_schema_fingerprint=self.event_schema_fingerprint,
            event_domain_contract_fingerprint=(self.event_domain_contract_fingerprint),
        )
        event = binding.decode_owned_payload(self.canonical_payload_bytes)
        if not hasattr(event, "id") or getattr(event, "id") != self.event_id:
            raise ValueError("decoded historical event identity mismatch")
        return event  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class RawEventLogReadSnapshot:
    through_sequence: int
    events: tuple[RawStoredEventEnvelope, ...]
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
    events: tuple[RawStoredEventEnvelope, ...]


@dataclass(frozen=True, slots=True)
class RawEventTypeSelectionSnapshot:
    """One atomic high-water plus a sparse, type-filtered event selection."""

    through_sequence: int
    events: tuple[RawStoredEventEnvelope, ...]

    def __post_init__(self) -> None:
        sequences = tuple(item.sequence for item in self.events)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(
            set(sequences)
        ):
            raise ValueError("raw event type selection must be ordered and unique")
        if sequences and sequences[-1] > self.through_sequence:
            raise ValueError("raw event type selection exceeds its high-water")


@dataclass(frozen=True, slots=True)
class RawTranscriptDomainPrefixFact:
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
            raise ValueError(
                "runtime projection checkpoint ledger prefix is not exact"
            )
        if not 0 <= self.validation_base_through_sequence <= self.through_sequence:
            raise ValueError(
                "runtime projection checkpoint validation base is invalid"
            )
        if not self.payload_fingerprint.startswith("sha256:"):
            raise ValueError("runtime projection checkpoint fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class RawTranscriptDomainDeltaSnapshot:
    runtime_session_id: str
    before: RawTranscriptDomainPrefixFact
    after: RawTranscriptDomainPrefixFact
    semantic_events: tuple[RawStoredEventEnvelope, ...]
    registry_contract_fingerprint: str
    snapshot_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        runtime_session_id: str,
        before: RawTranscriptDomainPrefixFact,
        after: RawTranscriptDomainPrefixFact,
        semantic_events: tuple[RawStoredEventEnvelope, ...],
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
    primary_events: tuple[RawStoredEventEnvelope, ...]
    run_sparse_events: tuple[RawStoredEventEnvelope, ...]
    session_sparse_events: tuple[RawStoredEventEnvelope, ...]
    exact_events: tuple[RawStoredEventEnvelope, ...]
    ledger_prefix: RawTranscriptDomainPrefixFact
    snapshot_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        runtime_session_id: str,
        request: RawContextAuthorityBundleRequest,
        through_sequence: int,
        primary_events: tuple[RawStoredEventEnvelope, ...],
        run_sparse_events: tuple[RawStoredEventEnvelope, ...],
        session_sparse_events: tuple[RawStoredEventEnvelope, ...],
        exact_events: tuple[RawStoredEventEnvelope, ...],
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
    events: tuple[RawStoredEventEnvelope, ...]

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
    checkpoint_event: RawStoredEventEnvelope
    delta_events: tuple[RawStoredEventEnvelope, ...]
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
    ) -> list[AgentEvent]: ...

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
        after_sequence: int | None = None,
    ) -> list[AgentEvent]: ...

    def get_by_id(self, event_id: str) -> AgentEvent | None: ...

    def confirm_batch(
        self,
        candidates: Sequence[AgentEvent],
        *,
        deadline_monotonic: float | None = None,
    ) -> EventBatchConfirmation: ...

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

    def read_raw_events_by_id(
        self,
        event_ids: tuple[str, ...],
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]: ...

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
    ) -> tuple[RawStoredEventEnvelope, ...]: ...

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
    ) -> tuple[RawStoredEventEnvelope, ...]: ...

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
    ) -> tuple[RawStoredEventEnvelope, ...]: ...

    def read_raw_model_call_events(
        self,
        resolved_model_call_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]: ...

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

    def next_sequence(self) -> int: ...


def raw_checkpoint_catalog_identity(
    envelope: RawStoredEventEnvelope,
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
    stored: RawStoredEventEnvelope,
) -> bool:
    """Compare a live candidate with canonical stored bytes before decoding.

    EventLog assigns sequence at commit time, so confirmation normalizes only
    that field.  Every other wrapper and payload field remains immutable.
    """

    if candidate.id != stored.event_id:
        return False
    normalized = candidate.model_copy(update={"sequence": stored.sequence})
    return canonical_event_payload_bytes(normalized) == stored.canonical_payload_bytes
