"""Non-blocking committed presentation observation boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Callable, Literal, TypeAlias
from uuid import uuid4

from pulsara_agent.ports.stored_event import StoredEventBatchCommitReceipt
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_history import (
    ModelActivityCell,
    OperationalActivityCell,
    SubagentActivityCell,
    TerminalProcessActivityCell,
    ToolActivityCell,
)
from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope
from pulsara_agent.primitives.terminal_presentation import (
    CommittedPresentationTapEntry,
    LiveCommittedFoldResult,
)


def build_committed_presentation_tap_entry(
    receipt: StoredEventBatchCommitReceipt,
    fold_result: LiveCommittedFoldResult,
) -> CommittedPresentationTapEntry:
    """Join the exact physical receipt with its canonical live fold result."""

    receipt.__post_init__()
    first = receipt.raw_stored_envelopes[0].sequence
    last = receipt.raw_stored_envelopes[-1].sequence
    if (
        fold_result.source_stored_batch_ordered_join_fingerprint
        != receipt.ordered_join_fingerprint
        or fold_result.source_first_sequence != first
        or fold_result.source_last_sequence != last
    ):
        raise ValueError("presentation tap receipt/fold join mismatch")
    payload = {
        "runtime_session_id": receipt.raw_stored_envelopes[0].runtime_session_id,
        "source_first_sequence": first,
        "source_last_sequence": last,
        "stored_batch_ordered_join_fingerprint": receipt.ordered_join_fingerprint,
        "source_envelope_accumulator": fold_result.source_envelope_accumulator,
        "fold_result_fingerprint": fold_result.live_result_fingerprint,
    }
    return CommittedPresentationTapEntry(
        schema_version="committed_presentation_tap_entry.v1",
        runtime_session_id=receipt.raw_stored_envelopes[0].runtime_session_id,
        source_first_sequence=first,
        source_last_sequence=last,
        raw_stored_envelopes=receipt.raw_stored_envelopes,
        stored_batch_ordered_join_fingerprint=receipt.ordered_join_fingerprint,
        canonical_fold_result=fold_result,
        tap_entry_fingerprint=context_fingerprint(
            "committed-presentation-tap-entry:v1", payload
        ),
    )


UiTapSubscriberStatus = Literal["catching_up", "live", "gap", "detached"]
OperationalActivityKind = Literal[
    "model_activity",
    "tool_activity",
    "terminal_process_activity",
    "subagent_activity",
]


@dataclass(frozen=True, slots=True)
class UiTapBootstrapReceipt:
    subscriber_id: str
    tap_generation: int
    snapshot_through_sequence: int
    frozen_ring_head_sequence: int
    retained_entries: tuple[CommittedPresentationTapEntry, ...]
    status: Literal["ready", "range_catch_up_required", "gap"]
    receipt_fingerprint: str


@dataclass(frozen=True, slots=True)
class UiTapSubscriberSnapshot:
    subscriber_id: str
    tap_generation: int
    status: UiTapSubscriberStatus
    last_consumed_sequence: int
    pending_entries: tuple[CommittedPresentationTapEntry, ...]
    gap_floor_sequence: int | None
    detach_reason: str | None


@dataclass(frozen=True, slots=True)
class OperationalActivityRemoval:
    operational_generation: int
    operational_cursor: int
    owner_kind: str
    owner_id: str
    owner_generation: int
    coalesce_key: str
    expected_activity_fingerprint: str
    removal_reason: Literal["durable_terminal", "owner_replaced", "explicit_retire"]
    removal_fingerprint: str


OperationalActivityChange: TypeAlias = (
    OperationalActivityCell | OperationalActivityRemoval
)


@dataclass(frozen=True, slots=True)
class OperationalActivitySnapshot:
    operational_generation: int
    operational_cursor: int
    ordered_activity_cells: tuple[OperationalActivityCell, ...]
    snapshot_fingerprint: str


@dataclass(frozen=True, slots=True)
class OperationalActivityRead:
    status: Literal["next", "no_change", "gap"]
    operational_generation: int
    operational_cursor: int
    ordered_changes: tuple[OperationalActivityChange, ...]


class UiOperationalActivityStore:
    """Bounded, coalescing process-local activity feed.

    This store owns neither durable terminal facts nor history placement. Its
    synchronous methods are deliberately non-awaiting so provider/runtime work
    can offer best-effort observations without taking a dependency on a client.
    """

    def __init__(
        self,
        *,
        runtime_session_id: str,
        maximum_resident_activities: int = 128,
        maximum_change_entries: int = 512,
        maximum_public_text_utf8_bytes: int = 8_000,
    ) -> None:
        if (
            maximum_resident_activities < 1
            or maximum_change_entries < 1
            or maximum_public_text_utf8_bytes < 1
        ):
            raise ValueError("operational activity bounds must be positive")
        self.runtime_session_id = runtime_session_id
        self.maximum_resident_activities = maximum_resident_activities
        self.maximum_change_entries = maximum_change_entries
        self.maximum_public_text_utf8_bytes = maximum_public_text_utf8_bytes
        self._lock = RLock()
        self._generation = 1
        self._cursor = 0
        self._resident: dict[str, OperationalActivityCell] = {}
        self._changes: list[OperationalActivityChange] = []
        self._diagnostic_counts: dict[str, int] = {}

    def offer_nowait(
        self,
        *,
        activity_kind: OperationalActivityKind,
        owner_kind: str,
        owner_id: str,
        owner_generation: int,
        coalesce_key: str,
        replacement_semantics: Literal["replace_same_key", "expire_at_terminal"],
        public_text: str,
    ) -> bool:
        try:
            if not owner_kind or not owner_id or not coalesce_key:
                raise ValueError("operational activity identity is required")
            if owner_generation < 0:
                raise ValueError("operational owner generation cannot be negative")
            bounded_text = _truncate_utf8(
                public_text, maximum_bytes=self.maximum_public_text_utf8_bytes
            )
            with self._lock:
                if (
                    coalesce_key not in self._resident
                    and len(self._resident) >= self.maximum_resident_activities
                ):
                    self._diagnostic_counts["resident_capacity_rejected"] = (
                        self._diagnostic_counts.get("resident_capacity_rejected", 0) + 1
                    )
                    return False
                current = self._resident.get(coalesce_key)
                if current is not None and (
                    current.owner_kind != owner_kind
                    or current.owner_id != owner_id
                    or current.owner_generation != owner_generation
                ):
                    raise ValueError("operational coalesce key changed owner identity")
                cursor = self._cursor + 1
                activity_type, schema_version = _operational_activity_type(
                    activity_kind
                )
                cell = build_frozen_fact(
                    activity_type,
                    schema_version=schema_version,
                    activity_kind=activity_kind,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    owner_generation=owner_generation,
                    operational_generation=self._generation,
                    operational_cursor=cursor,
                    coalesce_key=coalesce_key,
                    replacement_semantics=replacement_semantics,
                    bounded_public_text=bounded_text,
                )
                self._cursor = cursor
                self._resident[coalesce_key] = cell
                self._append_change_unlocked(cell)
            return True
        except BaseException as exc:
            try:
                with self._lock:
                    key = f"offer_error:{type(exc).__name__}"
                    self._diagnostic_counts[key] = (
                        self._diagnostic_counts.get(key, 0) + 1
                    )
            except BaseException:
                pass
            return False

    def retire_nowait(
        self,
        *,
        coalesce_key: str,
        owner_kind: str,
        owner_id: str,
        owner_generation: int,
        reason: Literal["durable_terminal", "owner_replaced", "explicit_retire"],
    ) -> bool:
        try:
            with self._lock:
                current = self._resident.get(coalesce_key)
                if current is None:
                    return True
                if (
                    current.owner_kind != owner_kind
                    or current.owner_id != owner_id
                    or current.owner_generation != owner_generation
                ):
                    raise ValueError("operational retirement owner identity mismatch")
                cursor = self._cursor + 1
                payload = {
                    "runtime_session_id": self.runtime_session_id,
                    "operational_generation": self._generation,
                    "operational_cursor": cursor,
                    "owner_kind": owner_kind,
                    "owner_id": owner_id,
                    "owner_generation": owner_generation,
                    "coalesce_key": coalesce_key,
                    "expected_activity_fingerprint": current.activity_fingerprint,
                    "removal_reason": reason,
                }
                removal = OperationalActivityRemoval(
                    operational_generation=self._generation,
                    operational_cursor=cursor,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    owner_generation=owner_generation,
                    coalesce_key=coalesce_key,
                    expected_activity_fingerprint=current.activity_fingerprint,
                    removal_reason=reason,
                    removal_fingerprint=context_fingerprint(
                        "operational-activity-removal:v1", payload
                    ),
                )
                self._cursor = cursor
                self._resident.pop(coalesce_key)
                self._append_change_unlocked(removal)
            return True
        except BaseException as exc:
            try:
                with self._lock:
                    key = f"retire_error:{type(exc).__name__}"
                    self._diagnostic_counts[key] = (
                        self._diagnostic_counts.get(key, 0) + 1
                    )
            except BaseException:
                pass
            return False

    def snapshot(self) -> OperationalActivitySnapshot:
        with self._lock:
            ordered = tuple(
                sorted(
                    self._resident.values(),
                    key=lambda item: (item.operational_cursor, item.coalesce_key),
                )
            )
            payload = {
                "runtime_session_id": self.runtime_session_id,
                "operational_generation": self._generation,
                "operational_cursor": self._cursor,
                "ordered_activity_fingerprints": tuple(
                    item.activity_fingerprint for item in ordered
                ),
            }
            return OperationalActivitySnapshot(
                operational_generation=self._generation,
                operational_cursor=self._cursor,
                ordered_activity_cells=ordered,
                snapshot_fingerprint=context_fingerprint(
                    "operational-activity-snapshot:v1", payload
                ),
            )

    def read_after(
        self, *, operational_generation: int, operational_cursor: int
    ) -> OperationalActivityRead:
        if operational_generation < 0 or operational_cursor < 0:
            raise ValueError("operational read cursor cannot be negative")
        with self._lock:
            if operational_generation != self._generation:
                return OperationalActivityRead(
                    status="gap",
                    operational_generation=self._generation,
                    operational_cursor=self._cursor,
                    ordered_changes=(),
                )
            if operational_cursor > self._cursor:
                return OperationalActivityRead(
                    status="gap",
                    operational_generation=self._generation,
                    operational_cursor=self._cursor,
                    ordered_changes=(),
                )
            if operational_cursor == self._cursor:
                return OperationalActivityRead(
                    status="no_change",
                    operational_generation=self._generation,
                    operational_cursor=self._cursor,
                    ordered_changes=(),
                )
            if (
                self._changes
                and _change_cursor(self._changes[0]) > operational_cursor + 1
            ):
                return OperationalActivityRead(
                    status="gap",
                    operational_generation=self._generation,
                    operational_cursor=self._cursor,
                    ordered_changes=(),
                )
            changes = tuple(
                item
                for item in self._changes
                if _change_cursor(item) > operational_cursor
            )
            return OperationalActivityRead(
                status="next",
                operational_generation=self._generation,
                operational_cursor=self._cursor,
                ordered_changes=changes,
            )

    def restart_generation(self) -> OperationalActivitySnapshot:
        with self._lock:
            self._generation += 1
            self._cursor = 0
            self._resident.clear()
            self._changes.clear()
        return self.snapshot()

    def _append_change_unlocked(self, change: OperationalActivityChange) -> None:
        self._changes.append(change)
        if len(self._changes) > self.maximum_change_entries:
            del self._changes[: len(self._changes) - self.maximum_change_entries]


@dataclass(slots=True)
class _Subscriber:
    subscriber_id: str
    generation: int
    status: UiTapSubscriberStatus
    last_consumed_sequence: int
    pending_entries: list[CommittedPresentationTapEntry] = field(default_factory=list)
    pending_bytes: int = 0
    gap_floor_sequence: int | None = None
    detach_reason: str | None = None


class UiCommittedEventTap:
    """Session-scoped bounded ring whose writer-side offer never raises."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        max_ring_entries: int = 256,
        max_ring_bytes: int = 16 * 1024 * 1024,
        max_subscribers: int = 16,
        max_subscriber_entries: int = 128,
        max_subscriber_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if (
            min(
                max_ring_entries,
                max_ring_bytes,
                max_subscribers,
                max_subscriber_entries,
                max_subscriber_bytes,
            )
            <= 0
        ):
            raise ValueError("UI committed tap bounds must be positive")
        self.runtime_session_id = runtime_session_id
        self.max_ring_entries = max_ring_entries
        self.max_ring_bytes = max_ring_bytes
        self.max_subscribers = max_subscribers
        self.max_subscriber_entries = max_subscriber_entries
        self.max_subscriber_bytes = max_subscriber_bytes
        self._lock = RLock()
        self._generation = 1
        self._ring: list[CommittedPresentationTapEntry] = []
        self._ring_bytes = 0
        # This high-water is independent from the bounded bootstrap ring.  A tap
        # generation reset deliberately drops resident entries, but it must not
        # make a later subscriber believe the durable ledger also moved backwards.
        self._latest_observed_sequence = 0
        self._subscribers: dict[str, _Subscriber] = {}
        self._diagnostic_counts: dict[str, int] = {}
        self._wakeup_callbacks: dict[str, Callable[[], None]] = {}

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def ring_floor_sequence(self) -> int:
        with self._lock:
            return self._ring[0].source_first_sequence if self._ring else 0

    @property
    def ring_head_sequence(self) -> int:
        with self._lock:
            return self._ring[-1].source_last_sequence if self._ring else 0

    @property
    def latest_observed_sequence(self) -> int:
        with self._lock:
            return self._latest_observed_sequence

    def offer_nowait(self, entry: CommittedPresentationTapEntry) -> bool:
        """Attempt an in-memory offer; all failures remain operational only."""

        try:
            entry.__post_init__()
            if entry.runtime_session_id != self.runtime_session_id:
                raise ValueError("presentation tap entry crosses runtime sessions")
            with self._lock:
                generation_before = self._generation
                accepted = self._offer_unlocked(entry)
                callbacks = (
                    tuple(self._wakeup_callbacks.values())
                    if accepted or self._generation != generation_before
                    else ()
                )
            self._notify_callbacks(callbacks)
            return accepted
        except BaseException as exc:  # the durable writer must never observe this
            callbacks: tuple[Callable[[], None], ...] = ()
            try:
                with self._lock:
                    key = f"offer_error:{type(exc).__name__}"
                    self._diagnostic_counts[key] = (
                        self._diagnostic_counts.get(key, 0) + 1
                    )
                    self._detach_generation_unlocked("offer_validation_failed")
                    callbacks = tuple(self._wakeup_callbacks.values())
            except BaseException:
                pass
            self._notify_callbacks(callbacks)
            return False

    def offer_committed_nowait(
        self,
        receipt: StoredEventBatchCommitReceipt,
        fold_result: LiveCommittedFoldResult,
    ) -> bool:
        """Build and offer the compound entry without exposing failures."""

        try:
            entry = build_committed_presentation_tap_entry(receipt, fold_result)
        except BaseException as exc:
            callbacks: tuple[Callable[[], None], ...] = ()
            try:
                receipt_is_valid = False
                try:
                    receipt.__post_init__()
                    receipt_is_valid = True
                except BaseException:
                    pass
                with self._lock:
                    # A valid physical receipt remains useful as a durable
                    # high-water even when its transcript-fold join is invalid.
                    # Never infer this value from an unvalidated carrier.
                    if receipt_is_valid:
                        self._latest_observed_sequence = max(
                            self._latest_observed_sequence,
                            receipt.raw_stored_envelopes[-1].sequence,
                        )
                    key = f"factory_error:{type(exc).__name__}"
                    self._diagnostic_counts[key] = (
                        self._diagnostic_counts.get(key, 0) + 1
                    )
                    self._detach_generation_unlocked("tap_entry_factory_failed")
                    callbacks = tuple(self._wakeup_callbacks.values())
            except BaseException:
                pass
            self._notify_callbacks(callbacks)
            return False
        return self.offer_nowait(entry)

    def _notify_callbacks(self, callbacks: tuple[Callable[[], None], ...]) -> None:
        for callback in callbacks:
            try:
                callback()
            except BaseException:
                with self._lock:
                    key = "wakeup_callback_error"
                    self._diagnostic_counts[key] = (
                        self._diagnostic_counts.get(key, 0) + 1
                    )

    def _offer_unlocked(self, entry: CommittedPresentationTapEntry) -> bool:
        self._latest_observed_sequence = max(
            self._latest_observed_sequence, entry.source_last_sequence
        )
        duplicate = next(
            (
                item
                for item in self._ring
                if item.source_first_sequence == entry.source_first_sequence
                and item.source_last_sequence == entry.source_last_sequence
            ),
            None,
        )
        if duplicate is not None:
            if duplicate.tap_entry_fingerprint == entry.tap_entry_fingerprint:
                return True
            self._detach_generation_unlocked("same_range_identity_conflict")
            return False
        if self._ring:
            head = self._ring[-1].source_last_sequence
            if entry.source_first_sequence != head + 1:
                reason = (
                    "partial_overlap"
                    if entry.source_first_sequence <= head
                    else "sequence_gap"
                )
                self._detach_generation_unlocked(reason)
                return False
        entry_bytes = _entry_resident_bytes(entry)
        if entry_bytes > self.max_ring_bytes:
            self._detach_generation_unlocked("entry_exceeds_ring_capacity")
            return False
        self._ring.append(entry)
        self._ring_bytes += entry_bytes
        for subscriber in self._subscribers.values():
            if subscriber.status not in {"catching_up", "live"}:
                continue
            if (
                subscriber.last_consumed_sequence
                and entry.source_first_sequence <= subscriber.last_consumed_sequence
            ):
                continue
            subscriber.pending_entries.append(entry)
            subscriber.pending_bytes += entry_bytes
            if (
                len(subscriber.pending_entries) > self.max_subscriber_entries
                or subscriber.pending_bytes > self.max_subscriber_bytes
            ):
                subscriber.status = "gap"
                subscriber.gap_floor_sequence = entry.source_first_sequence
                subscriber.pending_entries.clear()
                subscriber.pending_bytes = 0
        self._evict_ring_unlocked()
        return True

    def _evict_ring_unlocked(self) -> None:
        while self._ring and (
            len(self._ring) > self.max_ring_entries
            or self._ring_bytes > self.max_ring_bytes
        ):
            evicted = self._ring.pop(0)
            self._ring_bytes -= _entry_resident_bytes(evicted)
            # Existing subscribers own independent bounded pending buffers.
            # Evicting the bootstrap ring must not invalidate a whole entry that
            # has already been copied into one of those buffers.  Subscriber
            # overflow is classified where its own entry/byte bounds are
            # enforced in ``_offer_unlocked``; the ring floor only affects a
            # future bootstrap.

    def _detach_generation_unlocked(self, reason: str) -> None:
        self._generation += 1
        self._ring.clear()
        self._ring_bytes = 0
        for subscriber in self._subscribers.values():
            subscriber.status = "gap"
            subscriber.generation = self._generation
            subscriber.gap_floor_sequence = subscriber.last_consumed_sequence + 1
            subscriber.detach_reason = reason
            subscriber.pending_entries.clear()
            subscriber.pending_bytes = 0

    def begin_bootstrap(
        self, *, snapshot_through_sequence: int
    ) -> UiTapBootstrapReceipt:
        if snapshot_through_sequence < 0:
            raise ValueError("UI tap bootstrap high-water cannot be negative")
        with self._lock:
            if len(self._subscribers) >= self.max_subscribers:
                raise RuntimeError("UI committed tap subscriber capacity exhausted")
            subscriber_id = f"ui-tap:{uuid4().hex}"
            ring_head = (
                self._ring[-1].source_last_sequence
                if self._ring
                else snapshot_through_sequence
            )
            head = max(
                ring_head,
                snapshot_through_sequence,
                self._latest_observed_sequence,
            )
            overlapping = next(
                (
                    item
                    for item in self._ring
                    if item.source_first_sequence
                    <= snapshot_through_sequence
                    < item.source_last_sequence
                ),
                None,
            )
            retained = tuple(
                item
                for item in self._ring
                if item.source_first_sequence > snapshot_through_sequence
            )
            if overlapping is not None:
                status: Literal["ready", "range_catch_up_required", "gap"] = (
                    "range_catch_up_required"
                )
            elif head > snapshot_through_sequence and (
                not retained
                or retained[0].source_first_sequence > snapshot_through_sequence + 1
                or retained[-1].source_last_sequence < head
            ):
                status = "range_catch_up_required"
            else:
                status = "ready"
            subscriber = _Subscriber(
                subscriber_id=subscriber_id,
                generation=self._generation,
                status="catching_up",
                last_consumed_sequence=snapshot_through_sequence,
                pending_entries=list(retained),
                pending_bytes=sum(_entry_resident_bytes(item) for item in retained),
            )
            self._subscribers[subscriber_id] = subscriber
            payload = {
                "subscriber_id": subscriber_id,
                "tap_generation": self._generation,
                "snapshot_through_sequence": snapshot_through_sequence,
                "frozen_ring_head_sequence": head,
                "retained_entry_fingerprints": tuple(
                    item.tap_entry_fingerprint for item in retained
                ),
                "status": status,
            }
            return UiTapBootstrapReceipt(
                subscriber_id=subscriber_id,
                tap_generation=self._generation,
                snapshot_through_sequence=snapshot_through_sequence,
                frozen_ring_head_sequence=head,
                retained_entries=retained,
                status=status,
                receipt_fingerprint=context_fingerprint(
                    "ui-tap-bootstrap-receipt:v1", payload
                ),
            )

    def mark_live(self, subscriber_id: str, *, through_sequence: int) -> None:
        with self._lock:
            subscriber = self._subscribers[subscriber_id]
            if subscriber.status != "catching_up":
                raise RuntimeError("UI tap subscriber is not catching up")
            if subscriber.generation != self._generation:
                raise RuntimeError("UI tap subscriber generation is stale")
            subscriber.last_consumed_sequence = through_sequence
            subscriber.pending_entries = [
                item
                for item in subscriber.pending_entries
                if item.source_first_sequence > through_sequence
            ]
            subscriber.pending_bytes = sum(
                _entry_resident_bytes(item) for item in subscriber.pending_entries
            )
            subscriber.status = "live"

    def confirm_range_catch_up(
        self,
        subscriber_id: str,
        *,
        from_sequence_exclusive: int,
        through_sequence: int,
        covered_envelopes: tuple[RawStoredEventEnvelope, ...],
    ) -> None:
        """Install a restored range without inventing physical batch identity.

        Whole live entries buffered during bootstrap are compared with the exact
        raw rows covered by the joined range proof before they are discarded.
        Entries committed after the frozen catch-up high-water remain pending.
        """

        if through_sequence <= from_sequence_exclusive:
            raise ValueError("UI tap catch-up range must be non-empty")
        by_sequence = {item.sequence: item for item in covered_envelopes}
        if tuple(sorted(by_sequence)) != tuple(
            range(from_sequence_exclusive + 1, through_sequence + 1)
        ):
            raise ValueError("UI tap catch-up envelopes do not cover the exact range")
        with self._lock:
            subscriber = self._subscribers[subscriber_id]
            if subscriber.status != "catching_up":
                raise RuntimeError("UI tap subscriber is not catching up")
            if subscriber.generation != self._generation:
                raise RuntimeError("UI tap subscriber generation is stale")
            if subscriber.last_consumed_sequence != from_sequence_exclusive:
                raise ValueError("UI tap catch-up base changed")
            for entry in subscriber.pending_entries:
                if entry.source_last_sequence > through_sequence:
                    continue
                if entry.source_first_sequence <= from_sequence_exclusive:
                    raise ValueError("UI tap catch-up entry overlaps its restored base")
                expected = tuple(
                    by_sequence[sequence]
                    for sequence in range(
                        entry.source_first_sequence,
                        entry.source_last_sequence + 1,
                    )
                )
                if expected != entry.raw_stored_envelopes:
                    raise ValueError("UI tap buffered entry differs from restored rows")
            subscriber.last_consumed_sequence = through_sequence
            subscriber.pending_entries = [
                item
                for item in subscriber.pending_entries
                if item.source_first_sequence > through_sequence
            ]
            subscriber.pending_bytes = sum(
                _entry_resident_bytes(item) for item in subscriber.pending_entries
            )
            subscriber.status = "live"

    def snapshot_subscriber(self, subscriber_id: str) -> UiTapSubscriberSnapshot:
        with self._lock:
            subscriber = self._subscribers[subscriber_id]
            return UiTapSubscriberSnapshot(
                subscriber_id=subscriber.subscriber_id,
                tap_generation=subscriber.generation,
                status=subscriber.status,
                last_consumed_sequence=subscriber.last_consumed_sequence,
                pending_entries=tuple(subscriber.pending_entries),
                gap_floor_sequence=subscriber.gap_floor_sequence,
                detach_reason=subscriber.detach_reason,
            )

    def acknowledge(self, subscriber_id: str, *, through_sequence: int) -> None:
        with self._lock:
            subscriber = self._subscribers[subscriber_id]
            if subscriber.status not in {"catching_up", "live"}:
                raise RuntimeError("UI tap subscriber cannot acknowledge in this state")
            if through_sequence < subscriber.last_consumed_sequence:
                raise ValueError("UI tap acknowledgement moved backwards")
            subscriber.last_consumed_sequence = through_sequence
            subscriber.pending_entries = [
                item
                for item in subscriber.pending_entries
                if item.source_last_sequence > through_sequence
            ]
            subscriber.pending_bytes = sum(
                _entry_resident_bytes(item) for item in subscriber.pending_entries
            )

    def detach(self, subscriber_id: str, *, reason: str = "client_detach") -> None:
        with self._lock:
            subscriber = self._subscribers.pop(subscriber_id, None)
            if subscriber is not None:
                subscriber.status = "detached"
                subscriber.detach_reason = reason

    def register_wakeup_callback(self, callback: Callable[[], None]) -> str:
        """Register a non-awaiting hint; the ring remains the delivery authority."""

        callback_id = f"ui-tap-wakeup:{uuid4().hex}"
        with self._lock:
            self._wakeup_callbacks[callback_id] = callback
        return callback_id

    def unregister_wakeup_callback(self, callback_id: str) -> None:
        with self._lock:
            self._wakeup_callbacks.pop(callback_id, None)


def _entry_resident_bytes(entry: CommittedPresentationTapEntry) -> int:
    return 512 + sum(
        len(item.canonical_payload_bytes) + len(item.envelope_fingerprint)
        for item in entry.raw_stored_envelopes
    )


def _operational_activity_type(activity_kind: OperationalActivityKind):
    return {
        "model_activity": (ModelActivityCell, "presentation_model_activity_cell.v1"),
        "tool_activity": (ToolActivityCell, "presentation_tool_activity_cell.v1"),
        "terminal_process_activity": (
            TerminalProcessActivityCell,
            "presentation_terminal_process_activity_cell.v1",
        ),
        "subagent_activity": (
            SubagentActivityCell,
            "presentation_subagent_activity_cell.v1",
        ),
    }[activity_kind]


def _change_cursor(change: OperationalActivityChange) -> int:
    return change.operational_cursor


def _truncate_utf8(value: str, *, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")
