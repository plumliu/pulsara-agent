"""Process-owned memory admission for disposable transcript evidence cursors."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock, RLock
from uuid import uuid4
from weakref import WeakMethod

from pulsara_agent.runtime.authority_materialization.evidence_cursor import (
    VerifiedTranscriptProjectionCursorSnapshot,
)


@dataclass(frozen=True, slots=True)
class CursorResidentBudgetLimits:
    max_resident_charge_bytes: int = 512 * 1024 * 1024
    max_resident_chunks: int = 4_096
    max_resident_cursors: int = 64

    def __post_init__(self) -> None:
        if min(
            self.max_resident_charge_bytes,
            self.max_resident_chunks,
            self.max_resident_cursors,
        ) <= 0:
            raise ValueError("cursor resident budget limits must be positive")


@dataclass(frozen=True, slots=True)
class CursorResidentCharge:
    payload_bytes: int
    identity_utf8_bytes: int
    envelope_object_reserve_bytes: int
    chunk_object_reserve_bytes: int
    cursor_object_reserve_bytes: int
    total_charge_bytes: int
    chunk_count: int

    def __post_init__(self) -> None:
        parts = (
            self.payload_bytes,
            self.identity_utf8_bytes,
            self.envelope_object_reserve_bytes,
            self.chunk_object_reserve_bytes,
            self.cursor_object_reserve_bytes,
        )
        if min((*parts, self.chunk_count)) < 0:
            raise ValueError("cursor resident charge cannot be negative")
        if self.total_charge_bytes != sum(parts):
            raise ValueError("cursor resident charge total is inconsistent")


@dataclass(frozen=True, slots=True)
class CursorResidentHandle:
    resident_entry_id: str
    owner_runtime_session_id: str
    anchor_generation: int
    cursor: VerifiedTranscriptProjectionCursorSnapshot
    charge: CursorResidentCharge


@dataclass(frozen=True, slots=True)
class CursorResidentAdmissionReservation:
    reservation_id: str
    owner_runtime_session_id: str
    anchor_generation: int
    candidate: VerifiedTranscriptProjectionCursorSnapshot
    candidate_charge: CursorResidentCharge
    replaces_resident_entry_id: str | None
    planned_eviction_entry_ids: tuple[str, ...]
    provisional_handle: CursorResidentHandle


@dataclass(frozen=True, slots=True)
class CursorResidentBudgetDiagnostic:
    max_resident_charge_bytes: int
    max_resident_chunks: int
    max_resident_cursors: int
    resident_charge_bytes: int
    resident_chunk_count: int
    resident_cursor_count: int
    pending_admission_count: int
    active_borrow_count: int
    admission_count: int
    admission_rejected_count: int
    eviction_count: int
    evicted_charge_bytes: int


@dataclass(slots=True)
class _ResidentEntry:
    handle: CursorResidentHandle
    eviction_callback: "_EvictionCallback"
    borrow_count: int
    lru_tick: int
    retired: bool = False


@dataclass(frozen=True, slots=True)
class _ResidentAggregate:
    charge_bytes: int
    chunk_count: int
    cursor_count: int


class _AdmissionPhase(StrEnum):
    PREPARED = "prepared"
    CALLBACKS = "callbacks"


@dataclass(slots=True)
class _PendingAdmission:
    reservation: CursorResidentAdmissionReservation
    phase: _AdmissionPhase
    candidate_eviction_callback: "_EvictionCallback"


@dataclass(frozen=True, slots=True)
class _EvictionCallback:
    weak_method: WeakMethod | None
    function: Callable[[str], bool] | None

    @classmethod
    def capture(cls, callback: Callable[[str], bool]) -> "_EvictionCallback":
        if getattr(callback, "__self__", None) is not None:
            return cls(weak_method=WeakMethod(callback), function=None)
        return cls(weak_method=None, function=callback)

    def invoke(self, entry_id: str) -> bool:
        callback = self.function
        if self.weak_method is not None:
            callback = self.weak_method()
        return True if callback is None else callback(entry_id)


class CursorResidentLease:
    def __init__(
        self,
        manager: "CursorResidentBudgetManager",
        handle: CursorResidentHandle,
    ) -> None:
        self.handle = handle
        self._manager = manager
        self._released = False

    def __enter__(self) -> VerifiedTranscriptProjectionCursorSnapshot:
        return self.handle.cursor

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._manager._release(self.handle)


class CursorResidentBudgetManager:
    """Bounded process-wide owner for evictable cursor snapshots."""

    def __init__(self, limits: CursorResidentBudgetLimits | None = None) -> None:
        self.limits = limits or CursorResidentBudgetLimits()
        self._lock = RLock()
        self._entries: dict[str, _ResidentEntry] = {}
        self._pending: dict[str, _PendingAdmission] = {}
        self._completed: OrderedDict[str, str] = OrderedDict()
        self._pending_eviction_ids: set[str] = set()
        self._tick = 0
        self._entry_serial = 0
        self._admission_count = 0
        self._admission_rejected_count = 0
        self._eviction_count = 0
        self._evicted_charge_bytes = 0

    def prepare_admission(
        self,
        *,
        owner_runtime_session_id: str,
        anchor_generation: int,
        candidate: VerifiedTranscriptProjectionCursorSnapshot,
        replaces: CursorResidentHandle | None,
        eviction_callback: Callable[[str], bool],
    ) -> CursorResidentAdmissionReservation | None:
        charge = estimate_cursor_resident_charge(candidate)
        if (
            charge.total_charge_bytes > self.limits.max_resident_charge_bytes
            or charge.chunk_count > self.limits.max_resident_chunks
        ):
            with self._lock:
                self._admission_rejected_count += 1
            return None
        replacement_id = replaces.resident_entry_id if replaces is not None else None
        with self._lock:
            try:
                victims = self._plan_evictions(
                    candidate=candidate,
                    replacement_id=replacement_id,
                )
            except ValueError:
                self._admission_rejected_count += 1
                return None
            if victims is None:
                self._admission_rejected_count += 1
                return None
            self._pending_eviction_ids.update(
                item.handle.resident_entry_id for item in victims
            )
            self._entry_serial += 1
            entry_id = f"cursor-resident:{self._entry_serial:016x}"
            handle = CursorResidentHandle(
                resident_entry_id=entry_id,
                owner_runtime_session_id=owner_runtime_session_id,
                anchor_generation=anchor_generation,
                cursor=candidate,
                charge=charge,
            )
            reservation = CursorResidentAdmissionReservation(
                reservation_id=f"cursor-admission:{uuid4().hex}",
                owner_runtime_session_id=owner_runtime_session_id,
                anchor_generation=anchor_generation,
                candidate=candidate,
                candidate_charge=charge,
                replaces_resident_entry_id=replacement_id,
                planned_eviction_entry_ids=tuple(
                    item.handle.resident_entry_id for item in victims
                ),
                provisional_handle=handle,
            )
            self._pending[reservation.reservation_id] = _PendingAdmission(
                reservation=reservation,
                phase=_AdmissionPhase.PREPARED,
                candidate_eviction_callback=_EvictionCallback.capture(
                    eviction_callback
                ),
            )
            return reservation

    def commit_admission(
        self,
        reservation: CursorResidentAdmissionReservation,
        *,
        eviction_callback: Callable[[str], bool] | None = None,
    ) -> CursorResidentHandle:
        with self._lock:
            current = self._pending.get(reservation.reservation_id)
            if current is None:
                completed_entry_id = self._completed.get(reservation.reservation_id)
                if completed_entry_id is None:
                    raise ValueError("cursor admission reservation is not active")
                entry = self._entries.get(completed_entry_id)
                if entry is not None:
                    return entry.handle
                return reservation.provisional_handle
            if current.reservation != reservation:
                raise ValueError("cursor admission reservation identity drifted")
            if current.phase is not _AdmissionPhase.PREPARED:
                raise ValueError("cursor admission commit is already in progress")
            current.phase = _AdmissionPhase.CALLBACKS
            victims = tuple(
                self._entries[entry_id]
                for entry_id in reservation.planned_eviction_entry_ids
                if entry_id in self._entries
            )
            candidate_callback = (
                _EvictionCallback.capture(eviction_callback)
                if eviction_callback is not None
                else current.candidate_eviction_callback
            )

        for victim in victims:
            try:
                victim.eviction_callback.invoke(victim.handle.resident_entry_id)
            except BaseException:
                # The exact handle is retired below. A stale owner can no longer
                # borrow it and therefore falls back to canonical evidence.
                pass

        with self._lock:
            current = self._pending.get(reservation.reservation_id)
            if (
                current is None
                or current.reservation != reservation
                or current.phase is not _AdmissionPhase.CALLBACKS
            ):
                raise ValueError("cursor admission callback phase drifted")
            before_eviction = self._resident_aggregate(
                handles=tuple(item.handle for item in self._entries.values())
            )
            evicted_count = 0
            for entry_id in reservation.planned_eviction_entry_ids:
                self._pending_eviction_ids.discard(entry_id)
                victim = self._entries.get(entry_id)
                if victim is None:
                    continue
                victim.retired = True
                if victim.borrow_count == 0:
                    self._entries.pop(entry_id, None)
                evicted_count += 1
            after_eviction = self._resident_aggregate(
                handles=tuple(item.handle for item in self._entries.values())
            )
            self._eviction_count += evicted_count
            self._evicted_charge_bytes += max(
                before_eviction.charge_bytes - after_eviction.charge_bytes,
                0,
            )
            self._tick += 1
            self._entries[reservation.provisional_handle.resident_entry_id] = (
                _ResidentEntry(
                    handle=reservation.provisional_handle,
                    eviction_callback=candidate_callback,
                    borrow_count=0,
                    lru_tick=self._tick,
                )
            )
            replacement_id = reservation.replaces_resident_entry_id
            if replacement_id is not None:
                self._retire_id(replacement_id)
            self._pending.pop(reservation.reservation_id, None)
            self._remember_completed(reservation)
            self._admission_count += 1
            return reservation.provisional_handle

    def abort_admission(
        self,
        reservation: CursorResidentAdmissionReservation,
    ) -> None:
        with self._lock:
            current = self._pending.get(reservation.reservation_id)
            if current is None:
                return
            if current.reservation != reservation:
                raise ValueError("cursor admission reservation identity drifted")
            if current.phase is not _AdmissionPhase.PREPARED:
                return
            self._pending.pop(reservation.reservation_id, None)
            self._pending_eviction_ids.difference_update(
                reservation.planned_eviction_entry_ids
            )

    def borrow(self, handle: CursorResidentHandle) -> CursorResidentLease | None:
        with self._lock:
            entry = self._entries.get(handle.resident_entry_id)
            if entry is None or entry.handle != handle or entry.retired:
                return None
            if (
                handle.resident_entry_id in self._pending_eviction_ids
                or handle.resident_entry_id in self._pending_replacement_ids()
            ):
                return None
            entry.borrow_count += 1
            self._tick += 1
            entry.lru_tick = self._tick
            return CursorResidentLease(self, handle)

    def retire(self, handle: CursorResidentHandle) -> None:
        with self._lock:
            entry = self._entries.get(handle.resident_entry_id)
            if entry is None:
                return
            if entry.handle != handle:
                raise ValueError("cursor resident handle identity drifted")
            self._retire_id(handle.resident_entry_id)

    def diagnostics(self) -> CursorResidentBudgetDiagnostic:
        with self._lock:
            aggregate = self._resident_aggregate(
                handles=self._logical_resident_handles(),
            )
            return CursorResidentBudgetDiagnostic(
                max_resident_charge_bytes=self.limits.max_resident_charge_bytes,
                max_resident_chunks=self.limits.max_resident_chunks,
                max_resident_cursors=self.limits.max_resident_cursors,
                resident_charge_bytes=aggregate.charge_bytes,
                resident_chunk_count=aggregate.chunk_count,
                resident_cursor_count=aggregate.cursor_count,
                pending_admission_count=len(self._pending),
                active_borrow_count=sum(
                    item.borrow_count for item in self._entries.values()
                ),
                admission_count=self._admission_count,
                admission_rejected_count=self._admission_rejected_count,
                eviction_count=self._eviction_count,
                evicted_charge_bytes=self._evicted_charge_bytes,
            )

    def _fits_pending(
        self,
        candidate: VerifiedTranscriptProjectionCursorSnapshot,
        *,
        replacement_id: str | None,
    ) -> bool:
        handles = self._logical_resident_handles(
            extra_candidate=candidate,
            extra_replacement_id=replacement_id,
        )
        aggregate = self._resident_aggregate(handles=handles)
        return (
            aggregate.charge_bytes <= self.limits.max_resident_charge_bytes
            and aggregate.chunk_count <= self.limits.max_resident_chunks
            and aggregate.cursor_count <= self.limits.max_resident_cursors
        )

    def _logical_resident_handles(
        self,
        *,
        extra_candidate: VerifiedTranscriptProjectionCursorSnapshot | None = None,
        extra_replacement_id: str | None = None,
    ) -> tuple[CursorResidentHandle, ...]:
        handles = {
            entry_id: item.handle for entry_id, item in self._entries.items()
        }
        for pending in self._pending.values():
            reservation = pending.reservation
            for entry_id in reservation.planned_eviction_entry_ids:
                handles.pop(entry_id, None)
            replacement_id = reservation.replaces_resident_entry_id
            if replacement_id is not None and self._replacement_can_retire(
                replacement_id
            ):
                handles.pop(replacement_id, None)
            handles[reservation.provisional_handle.resident_entry_id] = (
                reservation.provisional_handle
            )
        if extra_candidate is not None:
            if extra_replacement_id is not None and self._replacement_can_retire(
                extra_replacement_id
            ):
                handles.pop(extra_replacement_id, None)
            provisional = CursorResidentHandle(
                resident_entry_id="cursor-resident:prospective",
                owner_runtime_session_id="cursor-resident:prospective",
                anchor_generation=extra_candidate.generation,
                cursor=extra_candidate,
                charge=estimate_cursor_resident_charge(extra_candidate),
            )
            handles[provisional.resident_entry_id] = provisional
        return tuple(handles.values())

    def _replacement_can_retire(self, entry_id: str) -> bool:
        entry = self._entries.get(entry_id)
        return entry is not None and entry.borrow_count == 0

    def _pending_replacement_ids(self) -> set[str]:
        return {
            item.reservation.replaces_resident_entry_id
            for item in self._pending.values()
            if item.reservation.replaces_resident_entry_id is not None
        }

    def _remember_completed(
        self,
        reservation: CursorResidentAdmissionReservation,
    ) -> None:
        self._completed[reservation.reservation_id] = (
            reservation.provisional_handle.resident_entry_id
        )
        self._completed.move_to_end(reservation.reservation_id)
        while len(self._completed) > 1_024:
            self._completed.popitem(last=False)

    @staticmethod
    def _resident_aggregate(
        *,
        handles: tuple[CursorResidentHandle, ...],
    ) -> _ResidentAggregate:
        unique_chunks: dict[int, tuple[object, int]] = {}
        cursor_bytes = 0
        for handle in handles:
            cursor_bytes += handle.charge.cursor_object_reserve_bytes
            for chunk in handle.cursor.semantic_envelopes.chunks:
                charge = _chunk_resident_charge_bytes(chunk)
                object_identity = id(chunk)
                previous = unique_chunks.get(object_identity)
                if previous is not None:
                    if previous[0] is not chunk or previous[1] != charge:
                        raise RuntimeError("cursor resident object identity was reused")
                    continue
                unique_chunks[object_identity] = (chunk, charge)
        return _ResidentAggregate(
            charge_bytes=cursor_bytes
            + sum(item[1] for item in unique_chunks.values()),
            chunk_count=len(unique_chunks),
            cursor_count=len(handles),
        )

    def _plan_evictions(
        self,
        *,
        candidate: VerifiedTranscriptProjectionCursorSnapshot,
        replacement_id: str | None,
    ) -> tuple[_ResidentEntry, ...] | None:
        try:
            if self._fits_pending(candidate, replacement_id=replacement_id):
                return ()
        except ValueError:
            raise
        excluded = {
            *self._pending_eviction_ids,
            *self._pending_replacement_ids(),
            *({replacement_id} if replacement_id is not None else set()),
        }
        candidates = sorted(
            (
                item
                for key, item in self._entries.items()
                if key not in excluded
                and item.borrow_count == 0
                and not item.retired
            ),
            key=lambda item: (
                item.lru_tick,
                -item.handle.charge.total_charge_bytes,
                item.handle.resident_entry_id,
            ),
        )
        victims: list[_ResidentEntry] = []
        for item in candidates:
            victims.append(item)
            handles = tuple(
                handle
                for handle in self._logical_resident_handles(
                    extra_candidate=candidate,
                    extra_replacement_id=replacement_id,
                )
                if handle.resident_entry_id
                not in {
                    victim.handle.resident_entry_id for victim in victims
                }
            )
            aggregate = self._resident_aggregate(handles=handles)
            if (
                aggregate.charge_bytes <= self.limits.max_resident_charge_bytes
                and aggregate.chunk_count <= self.limits.max_resident_chunks
                and aggregate.cursor_count <= self.limits.max_resident_cursors
            ):
                return tuple(victims)
        return None

    def _retire_id(self, entry_id: str) -> None:
        entry = self._entries.get(entry_id)
        if entry is None:
            return
        entry.retired = True
        if entry.borrow_count == 0:
            self._entries.pop(entry_id, None)

    def _release(self, handle: CursorResidentHandle) -> None:
        with self._lock:
            entry = self._entries.get(handle.resident_entry_id)
            if entry is None or entry.handle != handle or entry.borrow_count < 1:
                raise ValueError("cursor resident lease release drifted")
            entry.borrow_count -= 1
            if entry.borrow_count == 0 and entry.retired:
                self._entries.pop(handle.resident_entry_id, None)


def estimate_cursor_resident_charge(
    cursor: VerifiedTranscriptProjectionCursorSnapshot,
) -> CursorResidentCharge:
    chunks = cursor.semantic_envelopes.chunks
    payload_bytes = sum(
        len(envelope.canonical_payload_bytes)
        for chunk in chunks
        for envelope in chunk.envelopes
    )
    identity_bytes = sum(
        _envelope_identity_utf8_bytes(envelope)
        for chunk in chunks
        for envelope in chunk.envelopes
    )
    envelope_reserve = sum(len(chunk.envelopes) for chunk in chunks) * 1_024
    chunk_reserve = len(cursor.semantic_envelopes.chunks) * 1_024
    cursor_reserve = 64 * 1_024
    return CursorResidentCharge(
        payload_bytes=payload_bytes,
        identity_utf8_bytes=identity_bytes,
        envelope_object_reserve_bytes=envelope_reserve,
        chunk_object_reserve_bytes=chunk_reserve,
        cursor_object_reserve_bytes=cursor_reserve,
        total_charge_bytes=(
            payload_bytes
            + identity_bytes
            + envelope_reserve
            + chunk_reserve
            + cursor_reserve
        ),
        chunk_count=len(cursor.semantic_envelopes.chunks),
    )


def _chunk_resident_charge_bytes(chunk) -> int:
    return (
        sum(len(item.canonical_payload_bytes) for item in chunk.envelopes)
        + sum(_envelope_identity_utf8_bytes(item) for item in chunk.envelopes)
        + len(chunk.envelopes) * 1_024
        + 1_024
    )


def _envelope_identity_utf8_bytes(envelope) -> int:
    string_fields = (
        "stored_envelope_version",
        "event_id",
        "runtime_session_id",
        "run_id",
        "turn_id",
        "reply_id",
        "created_at_utc",
        "event_type",
        "event_schema_version",
        "event_schema_fingerprint",
        "event_domain_contract_fingerprint",
        "payload_fingerprint",
        "envelope_fingerprint",
    )
    return sum(
        len(getattr(envelope, field).encode("utf-8")) for field in string_fields
    )


_PROCESS_CURSOR_RESIDENT_BUDGET_LOCK = Lock()
_PROCESS_CURSOR_RESIDENT_BUDGET: CursorResidentBudgetManager | None = None


def process_cursor_resident_budget_manager() -> CursorResidentBudgetManager:
    global _PROCESS_CURSOR_RESIDENT_BUDGET
    with _PROCESS_CURSOR_RESIDENT_BUDGET_LOCK:
        if _PROCESS_CURSOR_RESIDENT_BUDGET is None:
            _PROCESS_CURSOR_RESIDENT_BUDGET = CursorResidentBudgetManager()
        return _PROCESS_CURSOR_RESIDENT_BUDGET


__all__ = [
    "CursorResidentAdmissionReservation",
    "CursorResidentBudgetDiagnostic",
    "CursorResidentBudgetLimits",
    "CursorResidentBudgetManager",
    "CursorResidentCharge",
    "CursorResidentHandle",
    "CursorResidentLease",
    "estimate_cursor_resident_charge",
    "process_cursor_resident_budget_manager",
]
