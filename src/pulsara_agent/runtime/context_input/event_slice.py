"""Atomic immutable event slices for context input projection."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
import json
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Iterator, Protocol, Sequence, overload

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.event_log.protocol import (
    EventLog,
    RawEventLogReadSnapshot,
    RawStoredEventEnvelope,
)
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventSchemaDomainRegistry,
)
from pulsara_agent.primitives.context import (
    ContextEventRangeFact,
    ContextEventReferenceFact,
    context_fingerprint,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.context_input.io_service import ContextInputIoService


class ContextEventSliceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, init=False)
class FrozenStoredEvent:
    event_id: str
    event_type: str
    sequence: int
    created_at_utc: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str
    envelope_fingerprint: str

    @classmethod
    def from_raw_envelope(
        cls, envelope: RawStoredEventEnvelope
    ) -> "FrozenStoredEvent":
        instance = object.__new__(cls)
        object.__setattr__(instance, "event_id", envelope.event_id)
        object.__setattr__(instance, "event_type", envelope.event_type)
        object.__setattr__(instance, "sequence", envelope.sequence)
        object.__setattr__(instance, "created_at_utc", envelope.created_at_utc)
        object.__setattr__(instance, "runtime_session_id", envelope.runtime_session_id)
        object.__setattr__(instance, "run_id", envelope.run_id)
        object.__setattr__(instance, "turn_id", envelope.turn_id)
        object.__setattr__(instance, "reply_id", envelope.reply_id)
        object.__setattr__(
            instance, "event_schema_version", envelope.event_schema_version
        )
        object.__setattr__(
            instance,
            "event_schema_fingerprint",
            envelope.event_schema_fingerprint,
        )
        object.__setattr__(
            instance,
            "event_domain_contract_fingerprint",
            envelope.event_domain_contract_fingerprint,
        )
        object.__setattr__(
            instance, "canonical_payload_bytes", envelope.canonical_payload_bytes
        )
        object.__setattr__(
            instance, "payload_fingerprint", envelope.payload_fingerprint
        )
        object.__setattr__(
            instance, "envelope_fingerprint", envelope.envelope_fingerprint
        )
        instance._validate()
        return instance

    @classmethod
    def from_stored_event(
        cls,
        event: AgentEvent,
        *,
        runtime_session_id: str = "context-event-reference",
    ) -> "FrozenStoredEvent":
        envelope = RawStoredEventEnvelope.from_stored_event(
            event=event,
            runtime_session_id=runtime_session_id,
            schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
        )
        return cls.from_raw_envelope(envelope)

    def decode_owned(
        self,
        registry: EventSchemaDomainRegistry,
    ) -> AgentEvent:
        self._validate()
        envelope = self.to_raw_envelope()
        try:
            return envelope.decode_owned(registry)
        except Exception as exc:
            raise ContextEventSliceError("historical event decode failed") from exc

    def to_raw_envelope(self) -> RawStoredEventEnvelope:
        return RawStoredEventEnvelope(
            stored_envelope_version="stored-agent-event:v1",
            event_id=self.event_id,
            runtime_session_id=self.runtime_session_id,
            run_id=self.run_id,
            turn_id=self.turn_id,
            reply_id=self.reply_id,
            sequence=self.sequence,
            created_at_utc=self.created_at_utc,
            event_type=self.event_type,
            event_schema_version=self.event_schema_version,
            event_schema_fingerprint=self.event_schema_fingerprint,
            event_domain_contract_fingerprint=self.event_domain_contract_fingerprint,
            canonical_payload_bytes=self.canonical_payload_bytes,
            payload_fingerprint=self.payload_fingerprint,
            envelope_fingerprint=self.envelope_fingerprint,
        )

    def to_reference(self, runtime_session_id: str) -> ContextEventReferenceFact:
        self._validate()
        return ContextEventReferenceFact(
            runtime_session_id=runtime_session_id,
            event_id=self.event_id,
            sequence=self.sequence,
            event_type=self.event_type,
            payload_fingerprint=self.payload_fingerprint,
        )

    def _validate(self) -> None:
        try:
            self.to_raw_envelope()
        except Exception as exc:
            raise ContextEventSliceError(
                "stored event wrapper identity or schema envelope is invalid"
            ) from exc


@dataclass(frozen=True, slots=True)
class ChunkedFrozenEvents(Sequence[FrozenStoredEvent]):
    """Immutable append-only chunks with tuple-compatible read semantics."""

    chunks: tuple[tuple[FrozenStoredEvent, ...], ...]
    id_chunks: tuple[dict[str, FrozenStoredEvent], ...]
    length: int

    @classmethod
    def from_events(
        cls, events: Sequence[FrozenStoredEvent]
    ) -> "ChunkedFrozenEvents":
        chunk = tuple(events)
        return cls(
            chunks=(chunk,) if chunk else (),
            id_chunks=({event.event_id: event for event in chunk},) if chunk else (),
            length=len(chunk),
        )

    def append(
        self, events: Sequence[FrozenStoredEvent]
    ) -> "ChunkedFrozenEvents":
        chunk = tuple(events)
        if not chunk:
            return self
        return ChunkedFrozenEvents(
            chunks=(*self.chunks, chunk),
            id_chunks=(
                *self.id_chunks,
                {event.event_id: event for event in chunk},
            ),
            length=self.length + len(chunk),
        )

    def event_by_id(self, event_id: str) -> FrozenStoredEvent | None:
        for index in reversed(self.id_chunks):
            event = index.get(event_id)
            if event is not None:
                return event
        return None

    def subsequence(self, start_index: int) -> "ChunkedFrozenEvents":
        if start_index < 0 or start_index >= self.length:
            raise IndexError(start_index)
        if start_index == 0:
            return self
        offset = start_index
        selected: list[tuple[FrozenStoredEvent, ...]] = []
        for chunk in self.chunks:
            if offset >= len(chunk):
                offset -= len(chunk)
                continue
            selected.append(chunk[offset:])
            offset = 0
        chunks = tuple(item for item in selected if item)
        return ChunkedFrozenEvents(
            chunks=chunks,
            id_chunks=tuple(
                {event.event_id: event for event in chunk} for chunk in chunks
            ),
            length=self.length - start_index,
        )

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[FrozenStoredEvent]:
        for chunk in self.chunks:
            yield from chunk

    def __reversed__(self) -> Iterator[FrozenStoredEvent]:
        for chunk in reversed(self.chunks):
            yield from reversed(chunk)

    @overload
    def __getitem__(self, index: int) -> FrozenStoredEvent: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[FrozenStoredEvent, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> FrozenStoredEvent | tuple[FrozenStoredEvent, ...]:
        if isinstance(index, slice):
            return tuple(self)[index]
        normalized = index if index >= 0 else self.length + index
        if normalized < 0 or normalized >= self.length:
            raise IndexError(index)
        offset = normalized
        for chunk in self.chunks:
            if offset < len(chunk):
                return chunk[offset]
            offset -= len(chunk)
        raise IndexError(index)


@dataclass(frozen=True, slots=True)
class ContextEventSlice:
    runtime_session_id: str
    from_sequence: int
    through_sequence: int
    events: ChunkedFrozenEvents | tuple[FrozenStoredEvent, ...]
    event_ids_fingerprint: str
    event_payloads_fingerprint: str
    _id_prefix_hasher: Any = field(init=False, repr=False, compare=False)
    _payload_prefix_hasher: Any = field(init=False, repr=False, compare=False)
    _payload_byte_count: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        events = (
            self.events
            if isinstance(self.events, ChunkedFrozenEvents)
            else ChunkedFrozenEvents.from_events(self.events)
        )
        object.__setattr__(self, "events", events)
        if not self.runtime_session_id:
            raise ContextEventSliceError("event slice requires runtime session ID")
        if self.from_sequence < 1 or self.from_sequence > self.through_sequence:
            raise ContextEventSliceError("event slice range is invalid")
        expected_sequence = self.from_sequence
        for event in events:
            if event.sequence != expected_sequence:
                raise ContextEventSliceError(
                    "event slice sequences are not contiguous"
                )
            expected_sequence += 1
        if expected_sequence - 1 != self.through_sequence:
            raise ContextEventSliceError("event slice sequences are not contiguous")
        ids = tuple(event.event_id for event in self.events)
        if len(ids) != len(set(ids)):
            raise ContextEventSliceError("event slice IDs are not unique")
        for event in self.events:
            event._validate()
        id_hasher = _sequence_prefix_hasher(
            "context-event-slice-ids:v1", ids
        )
        payload_hasher = _sequence_prefix_hasher(
            "context-event-slice-payloads:v1",
            tuple(event.payload_fingerprint for event in events),
        )
        expected_ids = _finish_sequence_fingerprint(id_hasher)
        expected_payloads = _finish_sequence_fingerprint(payload_hasher)
        if self.event_ids_fingerprint != expected_ids:
            raise ContextEventSliceError("event slice ID fingerprint mismatch")
        if self.event_payloads_fingerprint != expected_payloads:
            raise ContextEventSliceError("event slice payload fingerprint mismatch")
        object.__setattr__(self, "_id_prefix_hasher", id_hasher)
        object.__setattr__(self, "_payload_prefix_hasher", payload_hasher)
        object.__setattr__(
            self,
            "_payload_byte_count",
            sum(len(event.canonical_payload_bytes) for event in events),
        )

    @classmethod
    def from_frozen_events(
        cls,
        *,
        runtime_session_id: str,
        events: Sequence[FrozenStoredEvent],
    ) -> "ContextEventSlice":
        """Build one exact contiguous range from already validated envelopes."""

        frozen = tuple(events)
        if not frozen:
            raise ContextEventSliceError("event slice cannot be empty")
        ids = tuple(event.event_id for event in frozen)
        payloads = tuple(event.payload_fingerprint for event in frozen)
        return cls(
            runtime_session_id=runtime_session_id,
            from_sequence=frozen[0].sequence,
            through_sequence=frozen[-1].sequence,
            events=frozen,
            event_ids_fingerprint=context_fingerprint(
                "context-event-slice-ids:v1", ids
            ),
            event_payloads_fingerprint=context_fingerprint(
                "context-event-slice-payloads:v1", payloads
            ),
        )

    @classmethod
    def from_read_snapshot(
        cls,
        *,
        runtime_session_id: str,
        minimum_sequence: int,
        snapshot: RawEventLogReadSnapshot,
    ) -> "ContextEventSlice":
        frozen = tuple(
            FrozenStoredEvent.from_raw_envelope(event) for event in snapshot.events
        )
        ids = tuple(event.event_id for event in frozen)
        payloads = tuple(event.payload_fingerprint for event in frozen)
        return cls(
            runtime_session_id=runtime_session_id,
            from_sequence=minimum_sequence,
            through_sequence=snapshot.through_sequence,
            events=frozen,
            event_ids_fingerprint=context_fingerprint(
                "context-event-slice-ids:v1", ids
            ),
            event_payloads_fingerprint=context_fingerprint(
                "context-event-slice-payloads:v1", payloads
            ),
        )

    def event_by_id(self, event_id: str) -> FrozenStoredEvent:
        match = self.events.event_by_id(event_id)
        if match is None:
            raise ContextEventSliceError(
                f"event slice does not contain exactly one {event_id!r}"
            )
        return match

    @property
    def payload_byte_count(self) -> int:
        return self._payload_byte_count

    def event_by_sequence(self, sequence: int) -> FrozenStoredEvent:
        if sequence < self.from_sequence or sequence > self.through_sequence:
            raise ContextEventSliceError("event sequence is outside source range")
        return self.events[sequence - self.from_sequence]

    def subslice(self, *, from_sequence: int) -> "ContextEventSlice":
        if from_sequence < self.from_sequence or from_sequence > self.through_sequence:
            raise ContextEventSliceError(
                "context subslice start is outside source range"
            )
        if from_sequence == self.from_sequence:
            return self
        events = self.events.subsequence(from_sequence - self.from_sequence)
        ids = tuple(event.event_id for event in events)
        payloads = tuple(event.payload_fingerprint for event in events)
        return ContextEventSlice(
            runtime_session_id=self.runtime_session_id,
            from_sequence=from_sequence,
            through_sequence=self.through_sequence,
            events=events,
            event_ids_fingerprint=context_fingerprint(
                "context-event-slice-ids:v1", ids
            ),
            event_payloads_fingerprint=context_fingerprint(
                "context-event-slice-payloads:v1", payloads
            ),
        )

    def extend_snapshot(
        self, snapshot: RawEventLogReadSnapshot
    ) -> "ContextEventSlice":
        if not snapshot.events:
            raise ContextEventSliceError("context slice delta cannot be empty")
        if snapshot.events[0].sequence != self.through_sequence + 1:
            raise ContextEventSliceError("context slice delta is not contiguous")
        if snapshot.through_sequence < snapshot.events[-1].sequence:
            raise ContextEventSliceError("context slice delta high-water is invalid")
        appended = tuple(
            FrozenStoredEvent.from_raw_envelope(event) for event in snapshot.events
        )
        id_hasher = self._id_prefix_hasher.copy()
        payload_hasher = self._payload_prefix_hasher.copy()
        _append_sequence_values(id_hasher, (event.event_id for event in appended), len(self.events))
        _append_sequence_values(
            payload_hasher,
            (event.payload_fingerprint for event in appended),
            len(self.events),
        )
        instance = object.__new__(ContextEventSlice)
        object.__setattr__(instance, "runtime_session_id", self.runtime_session_id)
        object.__setattr__(instance, "from_sequence", self.from_sequence)
        object.__setattr__(instance, "through_sequence", snapshot.through_sequence)
        object.__setattr__(instance, "events", self.events.append(appended))
        object.__setattr__(
            instance,
            "event_ids_fingerprint",
            _finish_sequence_fingerprint(id_hasher),
        )
        object.__setattr__(
            instance,
            "event_payloads_fingerprint",
            _finish_sequence_fingerprint(payload_hasher),
        )
        object.__setattr__(instance, "_id_prefix_hasher", id_hasher)
        object.__setattr__(instance, "_payload_prefix_hasher", payload_hasher)
        object.__setattr__(
            instance,
            "_payload_byte_count",
            self._payload_byte_count
            + sum(len(event.canonical_payload_bytes) for event in appended),
        )
        return instance

    def to_range_fact(self) -> ContextEventRangeFact:
        return ContextEventRangeFact(
            runtime_session_id=self.runtime_session_id,
            first_sequence=self.from_sequence,
            through_sequence=self.through_sequence,
            event_count=len(self.events),
            event_ids_fingerprint=self.event_ids_fingerprint,
            event_payloads_fingerprint=self.event_payloads_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class ContextEventAuthorityView:
    """One bounded primary delta plus exact/sparse local authority ranges."""

    primary_slice: ContextEventSlice
    named_slices: tuple[ContextEventSlice, ...] = ()
    events: tuple[FrozenStoredEvent, ...] = field(init=False)
    _events_by_id: dict[str, FrozenStoredEvent] = field(
        init=False, repr=False, compare=False
    )
    _events_by_sequence: dict[int, FrozenStoredEvent] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        owner = self.primary_slice.runtime_session_id
        local = tuple(
            item for item in self.named_slices if item.runtime_session_id == owner
        )
        by_id: dict[str, FrozenStoredEvent] = {}
        by_sequence: dict[int, FrozenStoredEvent] = {}
        for event_slice in (self.primary_slice, *local):
            if event_slice.through_sequence > self.primary_slice.through_sequence:
                raise ContextEventSliceError(
                    "named authority exceeds primary ledger high-water"
                )
            for event in event_slice.events:
                prior_id = by_id.get(event.event_id)
                prior_sequence = by_sequence.get(event.sequence)
                if prior_id is not None and prior_id != event:
                    raise ContextEventSliceError("authority event ID payload conflict")
                if prior_sequence is not None and prior_sequence != event:
                    raise ContextEventSliceError("authority sequence payload conflict")
                by_id[event.event_id] = event
                by_sequence[event.sequence] = event
        events = tuple(by_sequence[key] for key in sorted(by_sequence))
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "_events_by_id", by_id)
        object.__setattr__(self, "_events_by_sequence", by_sequence)

    @property
    def runtime_session_id(self) -> str:
        return self.primary_slice.runtime_session_id

    @property
    def from_sequence(self) -> int:
        return min(event.sequence for event in self.events)

    @property
    def through_sequence(self) -> int:
        return self.primary_slice.through_sequence

    def event_by_id(self, event_id: str) -> FrozenStoredEvent:
        event = self._events_by_id.get(event_id)
        if event is None:
            raise ContextEventSliceError(
                f"event authority does not contain exactly one {event_id!r}"
            )
        return event

    def event_by_sequence(self, sequence: int) -> FrozenStoredEvent:
        event = self._events_by_sequence.get(sequence)
        if event is None:
            raise ContextEventSliceError("event sequence is outside authority ranges")
        return event

    def to_range_fact(self) -> ContextEventRangeFact:
        return self.primary_slice.to_range_fact()

    def named_range_facts(self) -> tuple[ContextEventRangeFact, ...]:
        return tuple(
            item.to_range_fact()
            for item in self.named_slices
            if item.runtime_session_id == self.runtime_session_id
        )


@dataclass(frozen=True, slots=True)
class SparseAuthorityCursor:
    """Memoized sparse roots plus the ledger high-water already inspected."""

    observed_ledger_high_water: int
    relevant_through_sequence: int
    run_start_event: FrozenStoredEvent
    spawn_event: FrozenStoredEvent
    relevant_events: tuple[FrozenStoredEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.observed_ledger_high_water < 1:
            raise ContextEventSliceError(
                "sparse authority cursor high-water must be positive"
            )
        if not 1 <= self.relevant_through_sequence <= self.observed_ledger_high_water:
            raise ContextEventSliceError(
                "sparse authority cursor relevant range is invalid"
            )
        if self.run_start_event.sequence > self.relevant_through_sequence:
            raise ContextEventSliceError(
                "sparse authority cursor RunStart exceeds its relevant range"
            )
        if self.spawn_event.sequence > self.relevant_through_sequence:
            raise ContextEventSliceError(
                "sparse authority cursor spawn exceeds its relevant range"
            )
        sequences = tuple(item.sequence for item in self.relevant_events)
        if sequences != tuple(sorted(set(sequences))):
            raise ContextEventSliceError(
                "sparse authority cursor events must be ordered and unique"
            )
        if any(
            item.sequence > self.relevant_through_sequence
            for item in self.relevant_events
        ):
            raise ContextEventSliceError(
                "sparse authority cursor event exceeds relevant high-water"
            )


class InMemoryContextAuthoritySliceCache:
    """Bounded memoization for immutable canonical authority prefixes."""

    def __init__(
        self,
        *,
        max_entries: int = 8,
        max_payload_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_entries < 1 or max_payload_bytes < 1:
            raise ValueError("authority slice cache bounds must be positive")
        self._max_entries = max_entries
        self._max_payload_bytes = max_payload_bytes
        self._entries: OrderedDict[tuple[str, str, int], ContextEventSlice] = (
            OrderedDict()
        )
        self._sparse_cursors: OrderedDict[
            tuple[str, str], SparseAuthorityCursor
        ] = OrderedDict()
        self._payload_bytes = 0
        self._lock = RLock()

    def get(self, key: tuple[str, str, int]) -> ContextEventSlice | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def get_sparse_cursor(
        self, key: tuple[str, str]
    ) -> SparseAuthorityCursor | None:
        with self._lock:
            value = self._sparse_cursors.get(key)
            if value is not None:
                self._sparse_cursors.move_to_end(key)
            return value

    def put_sparse_cursor(
        self,
        key: tuple[str, str],
        value: SparseAuthorityCursor,
    ) -> None:
        with self._lock:
            self._sparse_cursors.pop(key, None)
            self._sparse_cursors[key] = value
            while len(self._sparse_cursors) > self._max_entries:
                self._sparse_cursors.popitem(last=False)

    def latest_for_basis(
        self,
        *,
        runtime_session_id: str,
        basis_id: str,
    ) -> ContextEventSlice | None:
        with self._lock:
            matches = tuple(
                (key, value)
                for key, value in self._entries.items()
                if key[0] == runtime_session_id and key[1] == basis_id
            )
            if not matches:
                return None
            key, value = max(
                matches,
                key=lambda item: item[1].through_sequence,
            )
            self._entries.move_to_end(key)
            return value

    def put(
        self, key: tuple[str, str, int], value: ContextEventSlice
    ) -> None:
        payload_bytes = _slice_payload_bytes(value)
        if payload_bytes > self._max_payload_bytes:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._payload_bytes -= _slice_payload_bytes(previous)
            self._entries[key] = value
            self._payload_bytes += payload_bytes
            while (
                len(self._entries) > self._max_entries
                or self._payload_bytes > self._max_payload_bytes
            ):
                _, evicted = self._entries.popitem(last=False)
                self._payload_bytes -= _slice_payload_bytes(evicted)


def _slice_payload_bytes(value: ContextEventSlice) -> int:
    return value._payload_byte_count


def _sequence_prefix_hasher(namespace: str, values: Sequence[str]):
    hasher = sha256()
    hasher.update(namespace.encode("utf-8"))
    hasher.update(b"\x00[")
    _append_sequence_values(hasher, values, 0)
    return hasher


def _append_sequence_values(hasher, values, existing_count: int) -> None:
    count = existing_count
    for value in values:
        if count:
            hasher.update(b",")
        hasher.update(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        count += 1


def _finish_sequence_fingerprint(prefix_hasher) -> str:
    completed = prefix_hasher.copy()
    completed.update(b"]")
    return f"sha256:{completed.hexdigest()}"


class ContextEventSliceReader(Protocol):
    async def read_through_current_high_water(
        self,
        *,
        runtime_session_id: str,
        minimum_sequence: int,
    ) -> ContextEventSlice: ...

    async def read_through(
        self,
        *,
        runtime_session_id: str,
        through_sequence: int,
    ) -> ContextEventSlice: ...


class EventLogContextEventSliceReader:
    def __init__(
        self,
        *,
        event_log: EventLog,
        runtime_session_id: str,
        reconciliation_required: Callable[[], bool] | None = None,
        io_service: ContextInputIoService | None = None,
        read_timeout_seconds: float = 30.0,
    ) -> None:
        self._event_log = event_log
        self._runtime_session_id = runtime_session_id
        self._reconciliation_required = reconciliation_required or (lambda: False)
        self._io_service = io_service
        self._read_timeout_seconds = read_timeout_seconds

    async def read_through_current_high_water(
        self,
        *,
        runtime_session_id: str,
        minimum_sequence: int,
    ) -> ContextEventSlice:
        self._validate_owner(runtime_session_id)
        self._validate_ledger()
        snapshot = await self._read_snapshot(
            minimum_sequence=minimum_sequence,
            through_sequence=None,
        )
        self._validate_ledger()
        return ContextEventSlice.from_read_snapshot(
            runtime_session_id=runtime_session_id,
            minimum_sequence=minimum_sequence,
            snapshot=snapshot,
        )

    async def read_through(
        self,
        *,
        runtime_session_id: str,
        through_sequence: int,
    ) -> ContextEventSlice:
        self._validate_owner(runtime_session_id)
        self._validate_ledger()
        snapshot = await self._read_snapshot(
            minimum_sequence=1,
            through_sequence=through_sequence,
        )
        self._validate_ledger()
        return ContextEventSlice.from_read_snapshot(
            runtime_session_id=runtime_session_id,
            minimum_sequence=1,
            snapshot=snapshot,
        )

    def _validate_owner(self, runtime_session_id: str) -> None:
        if runtime_session_id != self._runtime_session_id:
            raise ContextEventSliceError("event slice runtime owner mismatch")

    async def _read_snapshot(
        self,
        *,
        minimum_sequence: int,
        through_sequence: int | None,
    ) -> RawEventLogReadSnapshot:
        deadline = monotonic() + self._read_timeout_seconds
        if self._io_service is None:
            return await asyncio.to_thread(
                self._event_log.read_raw_range_snapshot,
                minimum_sequence=minimum_sequence,
                through_sequence=through_sequence,
            )
        return await self._io_service.execute(
            operation_name="context-event-slice-read",
            operation=lambda: self._event_log.read_raw_range_snapshot(
                minimum_sequence=minimum_sequence,
                through_sequence=through_sequence,
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )

    def _validate_ledger(self) -> None:
        if self._reconciliation_required():
            raise ContextEventSliceError(
                "event ledger requires reconciliation before context read"
            )


def event_reference_from_stored(
    event: AgentEvent,
    *,
    runtime_session_id: str,
) -> ContextEventReferenceFact:
    return FrozenStoredEvent.from_stored_event(
        event, runtime_session_id=runtime_session_id
    ).to_reference(runtime_session_id)


__all__ = [
    "ContextEventSlice",
    "ContextEventSliceError",
    "ContextEventSliceReader",
    "EventLogContextEventSliceReader",
    "InMemoryContextAuthoritySliceCache",
    "FrozenStoredEvent",
    "event_reference_from_stored",
]
