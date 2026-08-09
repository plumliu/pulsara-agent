"""Process-local interaction control plane for Protocol v3.

The owner is intentionally small: it keeps only the current public interaction
view and a bounded transition ring.  Secret values and accepted decisions are
owned elsewhere.  A subscriber supplies its last confirmed revision on every
read, so a lost response cannot advance server-side authority.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from uuid import uuid4

from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS


class LiveControlEventKind(StrEnum):
    INTERACTION_OPENED = "INTERACTION_OPENED"
    INTERACTION_REPLACED = "INTERACTION_REPLACED"
    INTERACTION_CLOSED = "INTERACTION_CLOSED"


class LiveControlObservationKind(StrEnum):
    EVENTS = "EVENTS"
    GAP = "GAP"
    DETACHED = "DETACHED"


@dataclass(frozen=True, slots=True)
class CurrentInteractionView:
    interaction_id: str
    interaction_kind: str
    public_prompt: str
    public_options: tuple[str, ...]
    expires_at_utc: str


@dataclass(frozen=True, slots=True)
class LiveControlEvent:
    owner_epoch: int
    revision: int
    kind: LiveControlEventKind
    interaction: CurrentInteractionView | None
    closed_interaction_id: str | None


@dataclass(frozen=True, slots=True)
class LiveControlSnapshot:
    session_id: str
    owner_epoch: int
    revision: int
    current_interaction: CurrentInteractionView | None


@dataclass(frozen=True, slots=True)
class LiveControlObservation:
    kind: LiveControlObservationKind
    owner_epoch: int
    after_revision: int
    through_revision: int
    latest_revision: int
    events: tuple[LiveControlEvent, ...]


@dataclass(slots=True)
class _Subscriber:
    owner_epoch: int


class SessionLiveControlOwner:
    def __init__(
        self,
        *,
        session_id: str,
        owner_epoch: int = 1,
        maximum_events: int = STAGE2_LIMITS.live_control_hard_events,
        maximum_public_bytes: int = STAGE2_LIMITS.live_control_hard_bytes,
    ) -> None:
        if (
            not session_id
            or owner_epoch < 1
            or maximum_events < 1
            or maximum_public_bytes < 1
        ):
            raise ValueError("live-control owner bounds are invalid")
        self._session_id = session_id
        self._maximum_events = maximum_events
        self._maximum_public_bytes = maximum_public_bytes
        self._owner_epoch = owner_epoch
        self._revision = 0
        self._current: CurrentInteractionView | None = None
        self._ring: deque[tuple[LiveControlEvent, int]] = deque()
        self._ring_bytes = 0
        self._subscribers: dict[str, _Subscriber] = {}
        self._closed = False
        self._lock = RLock()

    def snapshot_and_subscribe(self) -> tuple[str, LiveControlSnapshot]:
        with self._lock:
            if self._closed:
                raise RuntimeError("live-control owner is closed")
            if len(self._subscribers) >= STAGE2_LIMITS.live_observer_hard_count:
                raise RuntimeError("live-control observer capacity is exhausted")
            subscriber_id = f"live-control-subscriber:{uuid4().hex}"
            self._subscribers[subscriber_id] = _Subscriber(self._owner_epoch)
            return subscriber_id, self._snapshot()

    def current_snapshot(self) -> LiveControlSnapshot:
        """Read the current same-Host value without creating a subscriber."""

        with self._lock:
            self._require_open()
            return self._snapshot()

    def observe(
        self,
        subscriber_id: str,
        *,
        owner_epoch: int,
        after_revision: int,
        maximum_events: int,
    ) -> LiveControlObservation:
        if after_revision < 0 or not 1 <= maximum_events <= self._maximum_events:
            raise ValueError("live-control observation bound is invalid")
        with self._lock:
            subscriber = self._subscribers.get(subscriber_id)
            if subscriber is None or self._closed:
                return LiveControlObservation(
                    LiveControlObservationKind.DETACHED,
                    self._owner_epoch,
                    after_revision,
                    after_revision,
                    self._revision,
                    (),
                )
            if (
                owner_epoch != self._owner_epoch
                or subscriber.owner_epoch != self._owner_epoch
                or after_revision > self._revision
            ):
                return self._gap(after_revision)
            oldest = self._ring[0][0].revision if self._ring else self._revision + 1
            if after_revision + 1 < oldest:
                return self._gap(after_revision)
            events = tuple(
                event for event, _ in self._ring if event.revision > after_revision
            )[:maximum_events]
            through = events[-1].revision if events else after_revision
            return LiveControlObservation(
                LiveControlObservationKind.EVENTS,
                self._owner_epoch,
                after_revision,
                through,
                self._revision,
                events,
            )

    def install_interaction(
        self,
        interaction: CurrentInteractionView,
        *,
        replace_expected_interaction_id: str | None = None,
    ) -> LiveControlEvent:
        size = self._interaction_size(interaction)
        with self._lock:
            self._require_open()
            current = self._current
            if current is None and replace_expected_interaction_id is not None:
                raise RuntimeError("live-control replacement subject is absent")
            if current is not None and (
                replace_expected_interaction_id != current.interaction_id
            ):
                raise RuntimeError("live-control replacement subject is stale")
            kind = (
                LiveControlEventKind.INTERACTION_OPENED
                if current is None
                else LiveControlEventKind.INTERACTION_REPLACED
            )
            self._current = interaction
            return self._append(
                kind,
                interaction,
                None if current is None else current.interaction_id,
                size,
            )

    def close_interaction(self, *, expected_interaction_id: str) -> LiveControlEvent:
        with self._lock:
            self._require_open()
            current = self._current
            if current is None or current.interaction_id != expected_interaction_id:
                raise RuntimeError("live-control close subject is stale")
            self._current = None
            return self._append(
                LiveControlEventKind.INTERACTION_CLOSED,
                None,
                expected_interaction_id,
                len(expected_interaction_id.encode("utf-8")),
            )

    def detach(self, subscriber_id: str) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._subscribers.clear()
            self._ring.clear()
            self._ring_bytes = 0
            self._current = None

    def _append(
        self,
        kind: LiveControlEventKind,
        interaction: CurrentInteractionView | None,
        closed_interaction_id: str | None,
        size: int,
    ) -> LiveControlEvent:
        self._revision += 1
        event = LiveControlEvent(
            self._owner_epoch,
            self._revision,
            kind,
            interaction,
            closed_interaction_id,
        )
        self._ring.append((event, size))
        self._ring_bytes += size
        while self._ring and (
            len(self._ring) > self._maximum_events
            or self._ring_bytes > self._maximum_public_bytes
        ):
            _, removed = self._ring.popleft()
            self._ring_bytes -= removed
        return event

    def _snapshot(self) -> LiveControlSnapshot:
        return LiveControlSnapshot(
            self._session_id,
            self._owner_epoch,
            self._revision,
            self._current,
        )

    def _gap(self, after_revision: int) -> LiveControlObservation:
        return LiveControlObservation(
            LiveControlObservationKind.GAP,
            self._owner_epoch,
            after_revision,
            after_revision,
            self._revision,
            (),
        )

    def _interaction_size(self, interaction: CurrentInteractionView) -> int:
        values: tuple[str, ...] = (
            interaction.interaction_id,
            interaction.interaction_kind,
            interaction.public_prompt,
            *interaction.public_options,
            interaction.expires_at_utc,
        )
        size = sum(len(value.encode("utf-8")) for value in values)
        if size > self._maximum_public_bytes:
            raise ValueError("live-control public view exceeds its physical bound")
        return size

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("live-control owner is closed")


__all__ = [
    "CurrentInteractionView",
    "LiveControlEvent",
    "LiveControlEventKind",
    "LiveControlObservation",
    "LiveControlObservationKind",
    "LiveControlSnapshot",
    "SessionLiveControlOwner",
]
