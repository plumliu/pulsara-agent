"""Atomic immutable event slices for context input projection."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import TYPE_CHECKING, Callable, Protocol

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.event_log.protocol import EventLog, EventLogReadSnapshot
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.primitives.context import (
    ContextEventRangeFact,
    ContextEventReferenceFact,
    canonical_json_bytes,
    canonical_utc_timestamp,
    context_fingerprint,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.context_input.io_service import ContextInputIoService


class ContextEventSliceError(RuntimeError):
    pass


def _payload_fingerprint(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True, init=False)
class FrozenStoredEvent:
    event_id: str
    event_type: str
    sequence: int
    created_at_utc: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str

    @classmethod
    def from_stored_event(cls, event: AgentEvent) -> "FrozenStoredEvent":
        if event.sequence is None or event.sequence < 1:
            raise ContextEventSliceError("stored event requires positive sequence")
        payload_bytes = canonical_json_bytes(dump_agent_event(event))
        instance = object.__new__(cls)
        object.__setattr__(instance, "event_id", event.id)
        object.__setattr__(instance, "event_type", str(event.type))
        object.__setattr__(instance, "sequence", event.sequence)
        object.__setattr__(
            instance, "created_at_utc", canonical_utc_timestamp(event.created_at)
        )
        object.__setattr__(instance, "canonical_payload_bytes", payload_bytes)
        object.__setattr__(
            instance, "payload_fingerprint", _payload_fingerprint(payload_bytes)
        )
        instance._validate()
        return instance

    def decode_owned(self) -> AgentEvent:
        self._validate()
        payload = json.loads(self.canonical_payload_bytes.decode("utf-8"))
        event = load_agent_event(payload)
        if dump_agent_event(event) != payload:
            raise ContextEventSliceError(
                "event payload is not strict round-trip stable"
            )
        return event

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
        if self.sequence < 1:
            raise ContextEventSliceError("stored event sequence must be positive")
        if self.payload_fingerprint != _payload_fingerprint(
            self.canonical_payload_bytes
        ):
            raise ContextEventSliceError("stored event payload fingerprint mismatch")
        try:
            payload = json.loads(self.canonical_payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise ContextEventSliceError(
                "stored event bytes cannot be decoded"
            ) from exc
        if not isinstance(payload, dict):
            raise ContextEventSliceError("stored event payload must be a JSON object")
        try:
            payload_created_at = canonical_utc_timestamp(str(payload["created_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextEventSliceError(
                "stored event payload lacks canonical wrapper identity"
            ) from exc
        expected = (
            payload.get("id"),
            str(payload.get("type")),
            payload.get("sequence"),
            payload_created_at,
        )
        actual = (
            self.event_id,
            self.event_type,
            self.sequence,
            self.created_at_utc,
        )
        if actual != expected:
            raise ContextEventSliceError("stored event wrapper identity mismatch")


@dataclass(frozen=True, slots=True)
class ContextEventSlice:
    runtime_session_id: str
    from_sequence: int
    through_sequence: int
    events: tuple[FrozenStoredEvent, ...]
    event_ids_fingerprint: str
    event_payloads_fingerprint: str

    def __post_init__(self) -> None:
        if not self.runtime_session_id:
            raise ContextEventSliceError("event slice requires runtime session ID")
        if self.from_sequence < 1 or self.from_sequence > self.through_sequence:
            raise ContextEventSliceError("event slice range is invalid")
        expected_sequences = tuple(range(self.from_sequence, self.through_sequence + 1))
        actual_sequences = tuple(event.sequence for event in self.events)
        if actual_sequences != expected_sequences:
            raise ContextEventSliceError("event slice sequences are not contiguous")
        ids = tuple(event.event_id for event in self.events)
        if len(ids) != len(set(ids)):
            raise ContextEventSliceError("event slice IDs are not unique")
        for event in self.events:
            event._validate()
        expected_ids = context_fingerprint("context-event-slice-ids:v1", ids)
        expected_payloads = context_fingerprint(
            "context-event-slice-payloads:v1",
            tuple(event.payload_fingerprint for event in self.events),
        )
        if self.event_ids_fingerprint != expected_ids:
            raise ContextEventSliceError("event slice ID fingerprint mismatch")
        if self.event_payloads_fingerprint != expected_payloads:
            raise ContextEventSliceError("event slice payload fingerprint mismatch")

    @classmethod
    def from_read_snapshot(
        cls,
        *,
        runtime_session_id: str,
        minimum_sequence: int,
        snapshot: EventLogReadSnapshot,
    ) -> "ContextEventSlice":
        frozen = tuple(
            FrozenStoredEvent.from_stored_event(event) for event in snapshot.events
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
        matches = tuple(event for event in self.events if event.event_id == event_id)
        if len(matches) != 1:
            raise ContextEventSliceError(
                f"event slice does not contain exactly one {event_id!r}"
            )
        return matches[0]

    def subslice(self, *, from_sequence: int) -> "ContextEventSlice":
        if from_sequence < self.from_sequence or from_sequence > self.through_sequence:
            raise ContextEventSliceError(
                "context subslice start is outside source range"
            )
        events = tuple(
            event for event in self.events if event.sequence >= from_sequence
        )
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

    def to_range_fact(self) -> ContextEventRangeFact:
        return ContextEventRangeFact(
            runtime_session_id=self.runtime_session_id,
            first_sequence=self.from_sequence,
            through_sequence=self.through_sequence,
            event_count=len(self.events),
            event_ids_fingerprint=self.event_ids_fingerprint,
            event_payloads_fingerprint=self.event_payloads_fingerprint,
        )


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
    ) -> EventLogReadSnapshot:
        deadline = monotonic() + self._read_timeout_seconds
        if self._io_service is None:
            return await asyncio.to_thread(
                self._event_log.read_range_snapshot,
                minimum_sequence=minimum_sequence,
                through_sequence=through_sequence,
            )
        return await self._io_service.execute(
            operation_name="context-event-slice-read",
            operation=lambda: self._event_log.read_range_snapshot(
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
    return FrozenStoredEvent.from_stored_event(event).to_reference(runtime_session_id)


__all__ = [
    "ContextEventSlice",
    "ContextEventSliceError",
    "ContextEventSliceReader",
    "EventLogContextEventSliceReader",
    "FrozenStoredEvent",
    "event_reference_from_stored",
]
