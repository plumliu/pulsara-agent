"""Single-owner coordination for provider input, segments, and durable batches."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from pulsara_agent.llm.drafts import SanitizedProviderSemanticEnvelope
from pulsara_agent.llm.sanitizing_transport import (
    SanitizingProviderTransportExecution,
)
from pulsara_agent.llm.segment import (
    MODEL_STREAM_COMMIT_MAX_CANDIDATE_BYTES,
    MODEL_STREAM_COMMIT_MAX_DURABLE_EVENTS,
    MODEL_STREAM_MAX_UNCONFIRMED_AGE_SECONDS,
    ModelStreamSegmentAccumulator,
    PreparedModelStreamSemanticEvent,
)
from pulsara_agent.primitives.model_call import ModelStreamSegmentSealReason


class ModelStreamInputSignalKind(StrEnum):
    DEADLINE = "deadline"
    READ = "read"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class ArbiterSignalStamp:
    monotonic_ns: int
    linearization_ordinal: int


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ModelStreamReadySignal(Generic[_T]):
    kind: ModelStreamInputSignalKind
    stamp: ArbiterSignalStamp | None
    deadline_monotonic_ns: int | None
    payload: _T | None

    @property
    def ordering_key(self) -> tuple[int, int, int]:
        if self.kind is ModelStreamInputSignalKind.DEADLINE:
            if self.deadline_monotonic_ns is None:
                raise ValueError("deadline signal lacks its absolute deadline")
            return (self.deadline_monotonic_ns, 0, 0)
        if self.stamp is None:
            raise ValueError("read/cancel signal lacks a linearization stamp")
        return (
            self.stamp.monotonic_ns,
            1,
            self.stamp.linearization_ordinal,
        )


class ModelStreamInputArbiter:
    """Assign stable stamps and totally order simultaneously ready signals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_ordinal = 0

    def stamp(
        self,
        *,
        observed_monotonic_ns: int | None = None,
    ) -> ArbiterSignalStamp:
        with self._lock:
            stamp = ArbiterSignalStamp(
                monotonic_ns=(
                    time.monotonic_ns()
                    if observed_monotonic_ns is None
                    else observed_monotonic_ns
                ),
                linearization_ordinal=self._next_ordinal,
            )
            self._next_ordinal += 1
            return stamp

    @staticmethod
    def order_ready(
        signals: tuple[ModelStreamReadySignal[object], ...],
    ) -> tuple[ModelStreamReadySignal[object], ...]:
        if len({signal.kind for signal in signals}) != len(signals):
            raise ValueError("input arbiter accepts at most one signal of each kind")
        return tuple(sorted(signals, key=lambda signal: signal.ordering_key))


class ModelStreamDurableBatchAccumulator:
    def __init__(self) -> None:
        self._events: list[PreparedModelStreamSemanticEvent] = []
        self._candidate_bytes = 0

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def candidate_bytes(self) -> int:
        return self._candidate_bytes

    @property
    def oldest_unconfirmed_at_monotonic_ns(self) -> int | None:
        if not self._events:
            return None
        return min(
            item.oldest_accepted_at_monotonic_ns for item in self._events
        )

    def add(self, item: PreparedModelStreamSemanticEvent) -> bool:
        if item.canonical_candidate_bytes > MODEL_STREAM_COMMIT_MAX_CANDIDATE_BYTES:
            raise ValueError("one model semantic candidate exceeds commit byte cap")
        would_exceed = bool(self._events) and (
            len(self._events) + 1 > MODEL_STREAM_COMMIT_MAX_DURABLE_EVENTS
            or self._candidate_bytes + item.canonical_candidate_bytes
            > MODEL_STREAM_COMMIT_MAX_CANDIDATE_BYTES
        )
        if would_exceed:
            return False
        self._events.append(item)
        self._candidate_bytes += item.canonical_candidate_bytes
        return True

    def must_commit(self) -> bool:
        return (
            len(self._events) >= MODEL_STREAM_COMMIT_MAX_DURABLE_EVENTS
            or self._candidate_bytes >= MODEL_STREAM_COMMIT_MAX_CANDIDATE_BYTES
        )

    def freeze(self) -> tuple[PreparedModelStreamSemanticEvent, ...]:
        return tuple(self._events)

    def clear_after_full_commit(self) -> None:
        self._events.clear()
        self._candidate_bytes = 0


class ModelStreamCoalescingCoordinator:
    """The sole process-local owner of adopted source and sealed event layout."""

    def __init__(
        self,
        *,
        transport: SanitizingProviderTransportExecution,
        segment_accumulator: ModelStreamSegmentAccumulator,
        arbiter: ModelStreamInputArbiter | None = None,
    ) -> None:
        self.transport = transport
        self.arbiter = arbiter or ModelStreamInputArbiter()
        self.segment_accumulator = segment_accumulator
        self.batch = ModelStreamDurableBatchAccumulator()
        self._pending_events: list[PreparedModelStreamSemanticEvent] = []

    @property
    def oldest_unconfirmed_deadline_monotonic_ns(self) -> int | None:
        timestamps = tuple(
            value
            for value in (
                self.segment_accumulator.oldest_unconfirmed_at_monotonic_ns,
                self.batch.oldest_unconfirmed_at_monotonic_ns,
            )
            if value is not None
        )
        if not timestamps:
            return None
        return min(timestamps) + int(
            MODEL_STREAM_MAX_UNCONFIRMED_AGE_SECONDS * 1_000_000_000
        )

    @property
    def owned_candidate_count(self) -> int:
        return self.batch.event_count + len(self._pending_events)

    @property
    def has_pending_candidates(self) -> bool:
        return bool(self._pending_events)

    def adopt(self, envelope: SanitizedProviderSemanticEnvelope) -> None:
        self.transport.require_adoptable(envelope)
        prepared = self.segment_accumulator.push(envelope)
        self._take_ownership(prepared)
        # The complete transition is coordinator-owned before the sanitizer
        # advances. No candidate may survive only in a worker stack frame.
        self.transport.acknowledge_adopted(envelope.envelope_id)

    def discard_unadopted(self, envelope: SanitizedProviderSemanticEnvelope) -> None:
        self.transport.discard_unadopted(envelope.envelope_id)

    def seal(self, reason: ModelStreamSegmentSealReason) -> bool:
        prepared = self.segment_accumulator.seal(reason)
        if prepared is None:
            return False
        self._take_ownership((prepared,))
        return True

    def confirm_current_batch_full(self) -> None:
        if self.batch.event_count == 0:
            raise RuntimeError("cannot confirm an empty model semantic batch")
        self.batch.clear_after_full_commit()
        while self._pending_events:
            item = self._pending_events[0]
            if not self.batch.add(item):
                break
            self._pending_events.pop(0)

    def _take_ownership(
        self,
        prepared: tuple[PreparedModelStreamSemanticEvent, ...],
    ) -> None:
        for item in prepared:
            if self._pending_events or not self.batch.add(item):
                self._pending_events.append(item)


__all__ = [
    "ArbiterSignalStamp",
    "ModelStreamCoalescingCoordinator",
    "ModelStreamDurableBatchAccumulator",
    "ModelStreamInputArbiter",
    "ModelStreamInputSignalKind",
    "ModelStreamReadySignal",
]
