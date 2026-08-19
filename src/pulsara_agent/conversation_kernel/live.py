"""Process-local Stage 2 live event plane.

The bus is an observation aid, never mutation authority.  Producers perform a
bounded synchronous offer; a slow observer receives a typed GAP and cannot
block a provider stream, canonical transaction, or Host close.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import json
from threading import RLock
from typing import Callable
from uuid import uuid4

from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.ports.live_agent_event import (
    DataDeltaPayload,
    DataEndPayload,
    DataStartPayload,
    InteractionClosedPayload,
    InteractionOpenedPayload,
    InteractionReplacedPayload,
    LivePayload,
    SubagentProgressPayload,
    TerminalMonitorClosedPayload,
    TerminalMonitorObservationPayload,
    TerminalMonitorOpenedPayload,
    TerminalProcessCompletedPayload,
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ThinkingDeltaPayload,
    ThinkingEndPayload,
    ThinkingStartPayload,
    TodoSnapshotUpdatedPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    ToolResultDeltaPayload,
    ToolResultEndPayload,
    ToolResultStartPayload,
    payload_to_mapping,
)


LIVE_PAYLOAD_TYPE_BY_EVENT: dict[LiveEventType, type[object]] = {
    LiveEventType.TEXT_START: TextStartPayload,
    LiveEventType.TEXT_DELTA: TextDeltaPayload,
    LiveEventType.TEXT_END: TextEndPayload,
    LiveEventType.THINKING_START: ThinkingStartPayload,
    LiveEventType.THINKING_DELTA: ThinkingDeltaPayload,
    LiveEventType.THINKING_END: ThinkingEndPayload,
    LiveEventType.DATA_START: DataStartPayload,
    LiveEventType.DATA_DELTA: DataDeltaPayload,
    LiveEventType.DATA_END: DataEndPayload,
    LiveEventType.TOOL_CALL_START: ToolCallStartPayload,
    LiveEventType.TOOL_CALL_DELTA: ToolCallDeltaPayload,
    LiveEventType.TOOL_CALL_END: ToolCallEndPayload,
    LiveEventType.TOOL_RESULT_START: ToolResultStartPayload,
    LiveEventType.TOOL_RESULT_DELTA: ToolResultDeltaPayload,
    LiveEventType.TOOL_RESULT_END: ToolResultEndPayload,
    LiveEventType.INTERACTION_OPENED: InteractionOpenedPayload,
    LiveEventType.INTERACTION_REPLACED: InteractionReplacedPayload,
    LiveEventType.INTERACTION_CLOSED: InteractionClosedPayload,
    LiveEventType.TERMINAL_PROCESS_COMPLETED: TerminalProcessCompletedPayload,
    LiveEventType.TERMINAL_MONITOR_OPENED: TerminalMonitorOpenedPayload,
    LiveEventType.TERMINAL_MONITOR_OBSERVATION: TerminalMonitorObservationPayload,
    LiveEventType.TERMINAL_MONITOR_CLOSED: TerminalMonitorClosedPayload,
    LiveEventType.SUBAGENT_PROGRESS: SubagentProgressPayload,
    LiveEventType.TODO_SNAPSHOT_UPDATED: TodoSnapshotUpdatedPayload,
}

if set(LIVE_PAYLOAD_TYPE_BY_EVENT) != set(LiveEventType):
    raise RuntimeError("live payload registry must cover exact 24 event types")


class LiveObservationKind(StrEnum):
    EVENTS = "EVENTS"
    GAP = "GAP"
    DETACHED = "DETACHED"


class LiveSettlementKind(StrEnum):
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"


class LiveChannelKind(StrEnum):
    MODEL_OUTPUT = "MODEL_OUTPUT"
    TOOL_RESULT = "TOOL_RESULT"
    TERMINAL_EXTENSION = "TERMINAL_EXTENSION"
    SUBAGENT_EXTENSION = "SUBAGENT_EXTENSION"


class LiveBlockKind(StrEnum):
    TEXT = "TEXT"
    THINKING = "THINKING"
    DATA = "DATA"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    OPERATIONAL = "OPERATIONAL"


@dataclass(frozen=True, slots=True)
class LiveAgentEvent:
    generation: int
    revision: int
    event_type: LiveEventType
    session_id: str
    turn_id: str
    draft_identity: str
    payload: LivePayload
    scope_kind: str
    scope_subagent_task_id: str | None
    channel_kind: LiveChannelKind
    channel_tool_call_id: str | None
    channel_attempt_id: str | None
    generation_id: str
    proposed_entry_id: str | None
    block_id: str
    block_ordinal: int
    block_kind: LiveBlockKind


@dataclass(frozen=True, slots=True)
class LiveGenerationSettlement:
    generation: int
    revision: int
    kind: LiveSettlementKind
    session_id: str
    turn_id: str
    draft_identity: str
    committed_entry_id: str | None
    reason_code: str | None
    scope_kind: str
    scope_subagent_task_id: str | None
    channel_kind: LiveChannelKind
    channel_tool_call_id: str | None
    channel_attempt_id: str | None
    generation_id: str
    proposed_entry_id: str | None


@dataclass(frozen=True, slots=True)
class LiveObservation:
    kind: LiveObservationKind
    generation: int
    after_revision: int
    latest_revision: int
    events: tuple[LiveAgentEvent, ...]
    settlements: tuple[LiveGenerationSettlement, ...]


@dataclass(frozen=True, slots=True)
class LiveSnapshot:
    generation: int
    retained_from_revision: int
    through_revision: int
    events: tuple[LiveAgentEvent, ...]
    settlements: tuple[LiveGenerationSettlement, ...]
    truncated_before: bool


@dataclass(slots=True)
class _Observer:
    generation: int
    detached: bool = False


class LiveAgentEventBus:
    def __init__(
        self,
        *,
        maximum_events: int = STAGE2_LIMITS.live_ring_hard_events,
        maximum_payload_bytes: int = STAGE2_LIMITS.live_ring_hard_bytes,
    ):
        if maximum_events < 1 or maximum_payload_bytes < 1:
            raise ValueError("live bus limits must be finite and positive")
        self._maximum_events = maximum_events
        self._maximum_payload_bytes = maximum_payload_bytes
        self._generation = 1
        self._revision = 0
        self._ring: deque[tuple[LiveAgentEvent | LiveGenerationSettlement, int]] = (
            deque()
        )
        self._ring_bytes = 0
        self._observers: dict[str, _Observer] = {}
        self._lock = RLock()
        self._closed = False
        self._extension_tap: Callable[[LiveAgentEvent], None] | None = None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def current_cut(self) -> tuple[int, int]:
        with self._lock:
            return self._generation, self._revision

    def bind_extension_tap(self, tap: Callable[[LiveAgentEvent], None]) -> None:
        with self._lock:
            if self._extension_tap is not None:
                raise RuntimeError("live extension tap is already bound")
            if self._revision != 0:
                raise RuntimeError("live extension tap must bind before first offer")
            self._extension_tap = tap

    def offer_nowait(
        self,
        *,
        event_type: LiveEventType,
        session_id: str,
        turn_id: str,
        draft_identity: str,
        payload: LivePayload,
        scope_kind: str = "ROOT",
        scope_subagent_task_id: str | None = None,
        channel_kind: LiveChannelKind = LiveChannelKind.MODEL_OUTPUT,
        channel_tool_call_id: str | None = None,
        channel_attempt_id: str | None = None,
        generation_id: str | None = None,
        proposed_entry_id: str | None = None,
        block_id: str | None = None,
        block_ordinal: int = 0,
        block_kind: LiveBlockKind | None = None,
    ) -> LiveAgentEvent | None:
        resolved_proposed = proposed_entry_id
        if resolved_proposed is None and channel_kind in {
            LiveChannelKind.MODEL_OUTPUT,
            LiveChannelKind.TOOL_RESULT,
        }:
            resolved_proposed = draft_identity
        resolved_generation = generation_id or f"live-generation:{draft_identity}"
        if not isinstance(payload, LIVE_PAYLOAD_TYPE_BY_EVENT[event_type]):
            raise TypeError("live payload does not match its event type")
        payload_mapping = payload_to_mapping(payload)
        resolved_block = block_id or str(payload_mapping.get("block_identity") or "")
        resolved_kind = block_kind or _block_kind(event_type)
        _validate_live_identity(
            event_type=event_type,
            draft_identity=draft_identity,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            channel_kind=channel_kind,
            channel_tool_call_id=channel_tool_call_id,
            channel_attempt_id=channel_attempt_id,
            generation_id=resolved_generation,
            proposed_entry_id=resolved_proposed,
            block_id=resolved_block,
            block_ordinal=block_ordinal,
            block_kind=resolved_kind,
        )
        try:
            encoded = json.dumps(
                payload_mapping,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            self._invalidate_generation_nowait()
            return None
        payload_bytes = len(encoded)
        if payload_bytes > self._maximum_payload_bytes:
            self._invalidate_generation_nowait()
            return None
        with self._lock:
            if self._closed:
                return None
            self._revision += 1
            event = LiveAgentEvent(
                generation=self._generation,
                revision=self._revision,
                event_type=event_type,
                session_id=session_id,
                turn_id=turn_id,
                draft_identity=draft_identity,
                # JSON round-trip removes caller-owned mutable containers and
                # proves the exact byte charge used by the bounded ring.
                payload=payload,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                channel_kind=channel_kind,
                channel_tool_call_id=channel_tool_call_id,
                channel_attempt_id=channel_attempt_id,
                generation_id=resolved_generation,
                proposed_entry_id=resolved_proposed,
                block_id=resolved_block,
                block_ordinal=block_ordinal,
                block_kind=resolved_kind,
            )
            self._ring.append((event, payload_bytes))
            self._ring_bytes += payload_bytes
            self._trim_ring()
            tap = self._extension_tap
        if tap is not None:
            try:
                tap(event)
            except BaseException:
                pass
        return event

    def subscribe(self) -> tuple[str, int, int]:
        observer_id, snapshot = self.subscribe_with_snapshot(maximum_events=0)
        return observer_id, snapshot.generation, snapshot.through_revision

    def subscribe_with_snapshot(
        self,
        *,
        maximum_events: int = STAGE2_LIMITS.live_snapshot_default_events,
        maximum_bytes: int = STAGE2_LIMITS.live_snapshot_default_bytes,
    ) -> tuple[str, LiveSnapshot]:
        if maximum_events == 0:
            maximum_events = STAGE2_LIMITS.live_snapshot_hard_events
        if maximum_bytes == 0:
            maximum_bytes = STAGE2_LIMITS.live_snapshot_hard_bytes
        if not 1 <= maximum_events <= STAGE2_LIMITS.live_snapshot_hard_events:
            raise ValueError("live snapshot event bound is invalid")
        if not 1 <= maximum_bytes <= STAGE2_LIMITS.live_snapshot_hard_bytes:
            raise ValueError("live snapshot byte bound is invalid")
        with self._lock:
            if self._closed:
                raise RuntimeError("live bus is closed")
            if len(self._observers) >= STAGE2_LIMITS.live_observer_hard_count:
                raise RuntimeError("live observer capacity is exhausted")
            observer_id = f"live-observer:{uuid4().hex}"
            self._observers[observer_id] = _Observer(
                generation=self._generation,
            )
            retained_reversed: list[LiveAgentEvent | LiveGenerationSettlement] = []
            retained_bytes = 0
            for item, item_bytes in reversed(self._ring):
                if len(retained_reversed) >= maximum_events:
                    break
                if retained_reversed and retained_bytes + item_bytes > maximum_bytes:
                    break
                if not retained_reversed and item_bytes > maximum_bytes:
                    # A single event may fit the shared ring yet exceed this
                    # independently negotiated snapshot bound.  Returning an
                    # empty truncated suffix is safer than a partial carrier.
                    break
                retained_reversed.append(item)
                retained_bytes += item_bytes
            retained = tuple(reversed(retained_reversed))
            first_revision = retained[0].revision if retained else self._revision + 1
            snapshot = LiveSnapshot(
                generation=self._generation,
                retained_from_revision=first_revision,
                through_revision=self._revision,
                events=tuple(
                    item for item in retained if isinstance(item, LiveAgentEvent)
                ),
                settlements=tuple(
                    item
                    for item in retained
                    if isinstance(item, LiveGenerationSettlement)
                ),
                truncated_before=(
                    self._revision > 0 and (not retained or first_revision > 1)
                ),
            )
            return observer_id, snapshot

    def offer_settlement_nowait(
        self,
        *,
        kind: LiveSettlementKind,
        session_id: str,
        turn_id: str,
        draft_identity: str,
        committed_entry_id: str | None = None,
        reason_code: str | None = None,
        scope_kind: str = "ROOT",
        scope_subagent_task_id: str | None = None,
        channel_kind: LiveChannelKind = LiveChannelKind.MODEL_OUTPUT,
        channel_tool_call_id: str | None = None,
        channel_attempt_id: str | None = None,
        generation_id: str | None = None,
        proposed_entry_id: str | None = None,
    ) -> LiveGenerationSettlement | None:
        if (kind is LiveSettlementKind.COMMITTED) != (committed_entry_id is not None):
            raise ValueError("live settlement committed-entry union is invalid")
        if (kind is LiveSettlementKind.ABORTED) != (reason_code is not None):
            raise ValueError("live settlement reason union is invalid")
        resolved_proposed = proposed_entry_id
        if resolved_proposed is None and channel_kind in {
            LiveChannelKind.MODEL_OUTPUT,
            LiveChannelKind.TOOL_RESULT,
        }:
            resolved_proposed = draft_identity
        resolved_generation = generation_id or f"live-generation:{draft_identity}"
        _validate_live_channel(
            draft_identity=draft_identity,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            channel_kind=channel_kind,
            channel_tool_call_id=channel_tool_call_id,
            channel_attempt_id=channel_attempt_id,
            generation_id=resolved_generation,
            proposed_entry_id=resolved_proposed,
        )
        payload_bytes = sum(
            len(value.encode("utf-8"))
            for value in (
                session_id,
                turn_id,
                draft_identity,
                committed_entry_id or "",
                reason_code or "",
            )
        )
        if payload_bytes > self._maximum_payload_bytes:
            self._invalidate_generation_nowait()
            return None
        with self._lock:
            if self._closed:
                return None
            self._revision += 1
            settlement = LiveGenerationSettlement(
                generation=self._generation,
                revision=self._revision,
                kind=kind,
                session_id=session_id,
                turn_id=turn_id,
                draft_identity=draft_identity,
                committed_entry_id=committed_entry_id,
                reason_code=reason_code,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                channel_kind=channel_kind,
                channel_tool_call_id=channel_tool_call_id,
                channel_attempt_id=channel_attempt_id,
                generation_id=resolved_generation,
                proposed_entry_id=resolved_proposed,
            )
            self._ring.append((settlement, payload_bytes))
            self._ring_bytes += payload_bytes
            self._trim_ring()
            return settlement

    def observe(
        self, observer_id: str, *, after_revision: int, maximum_events: int
    ) -> LiveObservation:
        if maximum_events < 1:
            raise ValueError("observation bound must be positive")
        if after_revision < 0:
            raise ValueError("live observation cursor must be non-negative")
        with self._lock:
            observer = self._observers.get(observer_id)
            if observer is None or observer.detached or self._closed:
                return LiveObservation(
                    kind=LiveObservationKind.DETACHED,
                    generation=self._generation,
                    after_revision=0,
                    latest_revision=self._revision,
                    events=(),
                    settlements=(),
                )
            if observer.generation != self._generation:
                return LiveObservation(
                    kind=LiveObservationKind.GAP,
                    generation=self._generation,
                    after_revision=after_revision,
                    latest_revision=self._revision,
                    events=(),
                    settlements=(),
                )
            after = after_revision
            oldest = self._ring[0][0].revision if self._ring else self._revision + 1
            if after > self._revision or after + 1 < oldest:
                return LiveObservation(
                    kind=LiveObservationKind.GAP,
                    generation=self._generation,
                    after_revision=after,
                    latest_revision=self._revision,
                    events=(),
                    settlements=(),
                )
            deliveries = tuple(
                delivery for delivery, _ in self._ring if delivery.revision > after
            )[:maximum_events]
            return LiveObservation(
                kind=LiveObservationKind.EVENTS,
                generation=self._generation,
                after_revision=after,
                latest_revision=(deliveries[-1].revision if deliveries else after),
                events=tuple(
                    item for item in deliveries if isinstance(item, LiveAgentEvent)
                ),
                settlements=tuple(
                    item
                    for item in deliveries
                    if isinstance(item, LiveGenerationSettlement)
                ),
            )

    def _trim_ring(self) -> None:
        while self._ring and (
            len(self._ring) > self._maximum_events
            or self._ring_bytes > self._maximum_payload_bytes
        ):
            _, removed = self._ring.popleft()
            self._ring_bytes -= removed

    def detach(self, observer_id: str) -> None:
        with self._lock:
            observer = self._observers.pop(observer_id, None)
            if observer is not None:
                observer.detached = True

    def replace_generation(self) -> int:
        with self._lock:
            self._generation += 1
            self._revision = 0
            self._ring.clear()
            self._ring_bytes = 0
            self._observers.clear()
            return self._generation

    def invalidate_observation_generation_nowait(self) -> None:
        """Detach slow/current observers with the existing typed GAP seam.

        Process-local producers use this only when a bounded live handoff has
        already lost provisional data.  It never affects canonical execution
        or installs a durable cursor/ack owner.
        """

        self._invalidate_generation_nowait()

    def _invalidate_generation_nowait(self) -> None:
        # Oversize/malformed producer output is an observation-plane GAP, not
        # a silent missing delta and never a canonical execution failure.
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            self._revision = 0
            self._ring.clear()
            self._ring_bytes = 0
            # Keep existing observer identities on their old generation so
            # their next level read returns GAP.  A generation replacement is
            # the separate administrative operation that detaches observers.

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ring.clear()
            self._ring_bytes = 0
            self._observers.clear()
            self._extension_tap = None


def _block_kind(event_type: LiveEventType) -> LiveBlockKind:
    prefix = (
        event_type.value.removesuffix("Start").removesuffix("Delta").removesuffix("End")
    )
    return {
        "Text": LiveBlockKind.TEXT,
        "Thinking": LiveBlockKind.THINKING,
        "Data": LiveBlockKind.DATA,
        "ToolCall": LiveBlockKind.TOOL_CALL,
        "ToolResult": LiveBlockKind.TOOL_RESULT,
        "TerminalProcessCompleted": LiveBlockKind.OPERATIONAL,
        "TerminalMonitorOpened": LiveBlockKind.OPERATIONAL,
        "TerminalMonitorObservation": LiveBlockKind.OPERATIONAL,
        "TerminalMonitorClosed": LiveBlockKind.OPERATIONAL,
        "SubagentProgress": LiveBlockKind.OPERATIONAL,
        "TodoSnapshotUpdated": LiveBlockKind.OPERATIONAL,
        "InteractionOpened": LiveBlockKind.OPERATIONAL,
        "InteractionReplaced": LiveBlockKind.OPERATIONAL,
        "InteractionClosed": LiveBlockKind.OPERATIONAL,
    }[prefix]


def _validate_live_channel(
    *,
    draft_identity: str,
    scope_kind: str,
    scope_subagent_task_id: str | None,
    channel_kind: LiveChannelKind,
    channel_tool_call_id: str | None,
    channel_attempt_id: str | None,
    generation_id: str,
    proposed_entry_id: str | None,
) -> None:
    if not draft_identity or not generation_id:
        raise ValueError("live generation identity is incomplete")
    if (scope_kind == "ROOT") != (scope_subagent_task_id is None):
        raise ValueError("live conversation scope union is invalid")
    if scope_kind not in {"ROOT", "SUBAGENT_TASK"}:
        raise ValueError("live conversation scope is unknown")
    if channel_kind is LiveChannelKind.MODEL_OUTPUT:
        valid_channel = (
            proposed_entry_id == draft_identity
            and channel_tool_call_id is None
            and channel_attempt_id is None
        )
    elif channel_kind is LiveChannelKind.TOOL_RESULT:
        valid_channel = (
            proposed_entry_id == draft_identity
            and bool(channel_tool_call_id)
            and bool(channel_attempt_id)
        )
    else:
        valid_channel = (
            proposed_entry_id is None
            and channel_tool_call_id is None
            and channel_attempt_id is None
        )
    if not valid_channel:
        raise ValueError("live channel identity union is invalid")


def _validate_live_identity(
    *,
    event_type: LiveEventType,
    draft_identity: str,
    scope_kind: str,
    scope_subagent_task_id: str | None,
    channel_kind: LiveChannelKind,
    channel_tool_call_id: str | None,
    channel_attempt_id: str | None,
    generation_id: str,
    proposed_entry_id: str | None,
    block_id: str,
    block_ordinal: int,
    block_kind: LiveBlockKind,
) -> None:
    _validate_live_channel(
        draft_identity=draft_identity,
        scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        channel_kind=channel_kind,
        channel_tool_call_id=channel_tool_call_id,
        channel_attempt_id=channel_attempt_id,
        generation_id=generation_id,
        proposed_entry_id=proposed_entry_id,
    )
    if not block_id or block_ordinal < 0 or block_kind is not _block_kind(event_type):
        raise ValueError("live block identity is invalid")


__all__ = [
    "LiveAgentEvent",
    "LiveAgentEventBus",
    "LiveBlockKind",
    "LiveChannelKind",
    "LiveGenerationSettlement",
    "LiveObservation",
    "LiveObservationKind",
    "LiveSettlementKind",
    "LiveSnapshot",
]
