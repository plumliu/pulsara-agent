"""Exact ROOT work lifetime for TODOs and disposable media read observations."""

from __future__ import annotations

from pulsara_agent.identity import derive_id, random_id, require_id


from collections.abc import Iterable
from pathlib import Path
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock

from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.tools.builtins.todo import FrozenTodoCandidate, FrozenTodoItem


MAXIMUM_PENDING_TODO_SETTLEMENTS_PER_RUN = 64


class CurrentWorkPhase(StrEnum):
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    CLOSING = "CLOSING"


class TodoLiveDisposition(StrEnum):
    ACTIVE = "ACTIVE"
    CLEARED = "CLEARED"


@dataclass(frozen=True, slots=True)
class RootWorkIdentity:
    session_id: str
    scope_kind: ModelInputScopeKind
    root_run_id: str | None
    owner_epoch: str

    @property
    def work_run_id(self) -> str:
        value = self.root_run_id
        if value is None:
            raise RuntimeError("ROOT work identity has no public run ID")
        return value

    def __post_init__(self) -> None:
        if not self.session_id or not self.owner_epoch or not self.root_run_id:
            raise ValueError("ROOT work identity is invalid")


@dataclass(frozen=True, slots=True)
class FrozenTodoSnapshot:
    run_identity: RootWorkIdentity
    revision: int
    ordered_items: tuple[FrozenTodoItem, ...]
    pending_count: int
    in_progress_count: int
    completed_count: int

    def __post_init__(self) -> None:
        counts = {"pending": 0, "in_progress": 0, "completed": 0}
        for ordinal, item in enumerate(self.ordered_items):
            if item.ordinal != ordinal:
                raise ValueError("TODO snapshot item order is invalid")
            counts[item.status.value] += 1
        if (
            self.revision < 0
            or counts["pending"] != self.pending_count
            or counts["in_progress"] != self.in_progress_count
            or counts["completed"] != self.completed_count
            or self.in_progress_count > 1
        ):
            raise ValueError("TODO snapshot is invalid")


@dataclass(frozen=True, slots=True)
class PreparedRootWorkActivation:
    session_id: str
    admission_kind: str
    command_id: str | None
    queue_item_id: str | None
    queue_sequence: int | None
    exact_turn_id: str
    exact_initial_entry_id: str
    exact_context_binding_revision_id: str
    proposed_root_run_id: str


@dataclass(frozen=True, slots=True)
class PreparedTodoReplacement:
    run_identity: RootWorkIdentity
    attempt_id: str
    proposed_result_entry_id: str
    candidate: FrozenTodoCandidate
    acknowledgement: bytes
    token_id: str


@dataclass(frozen=True, slots=True)
class TodoInstallation:
    installed_snapshot: FrozenTodoSnapshot
    turn_id: str
    disposition: TodoLiveDisposition


@dataclass(frozen=True, slots=True)
class FrozenCurrentWorkCloseProjection:
    run_identity: RootWorkIdentity
    last_turn_id: str


@dataclass(frozen=True, slots=True)
class MediaReadRange:
    canonical_path: str
    file_mtime_ns: str
    from_seconds: float
    to_seconds: float
    attempt_id: str

    def __post_init__(self) -> None:
        if (not Path(self.canonical_path).is_absolute() or re.fullmatch(r"(?:0|-?[1-9][0-9]*)", self.file_mtime_ns) is None
                or isinstance(self.from_seconds, bool) or isinstance(self.to_seconds, bool)
                or not self.attempt_id or not math.isfinite(self.from_seconds)
                or not math.isfinite(self.to_seconds)
                or not 0 <= self.from_seconds < self.to_seconds):
            raise ValueError("media read range is invalid")
        require_id(self.attempt_id, "attempt")


@dataclass(frozen=True, slots=True)
class FrozenCurrentWorkSnapshot:
    todos: tuple[FrozenTodoSnapshot, ...]
    media_ranges: tuple[MediaReadRange, ...]


@dataclass(slots=True)
class _CurrentWorkRecord:
    run_identity: RootWorkIdentity
    activation: PreparedRootWorkActivation
    phase: CurrentWorkPhase
    last_turn_id: str
    current_snapshot: FrozenTodoSnapshot
    pending_settlements: dict[str, PreparedTodoReplacement]
    media_ranges: dict[tuple[str, str], MediaReadRange]
    latest_media_starts: dict[str, tuple[str, str]]


class CurrentWorkStateOwner:
    """The sole same-Host ROOT work owner; never reconstructed from rows."""

    def __init__(self, *, session_id: str, owner_epoch: str) -> None:
        if not session_id or not owner_epoch:
            raise ValueError("current work owner identity is required")
        self._session_id = session_id
        self._owner_epoch = owner_epoch
        self._root: _CurrentWorkRecord | None = None
        self._lock = RLock()

    def activate_root_run(
        self,
        prepared: PreparedRootWorkActivation,
        *,
        allow_closing_predecessor: bool = False,
    ) -> FrozenCurrentWorkCloseProjection | None:
        with self._lock:
            self._require_session(prepared.session_id)
            if self._root is not None and (
                self._root.run_identity.root_run_id == prepared.proposed_root_run_id
                and self._root.last_turn_id == prepared.exact_turn_id
            ):
                if self._root.activation != prepared:
                    raise RuntimeError("ROOT work activation identity conflicts")
                return None
            old = self._root
            if old is not None and old.pending_settlements:
                raise RuntimeError("ROOT work changed with pending settlement")
            if (
                old is not None
                and old.phase is not CurrentWorkPhase.IDLE
                and not (
                    allow_closing_predecessor and old.phase is CurrentWorkPhase.CLOSING
                )
            ):
                raise RuntimeError("ROOT work changed before becoming idle")
            closed = None if old is None else self._close_projection(old)
            identity = _root_identity(
                session_id=self._session_id,
                root_run_id=prepared.proposed_root_run_id,
                owner_epoch=self._owner_epoch,
            )
            self._root = _new_record(identity, prepared.exact_turn_id, prepared)
            return closed

    def bind_continuation(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        turn_id: str,
    ) -> None:
        with self._lock:
            record = self._record(scope_kind)
            if record is None or record.phase is CurrentWorkPhase.CLOSING:
                raise RuntimeError("ROOT work continuation has no active run")
            record.last_turn_id = turn_id
            record.phase = CurrentWorkPhase.ACTIVE

    def bind_continuation_if_present(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        turn_id: str,
    ) -> bool:
        """Bind an external continuation without reconstructing lost current work state."""

        with self._lock:
            record = self._record(scope_kind)
            if record is None:
                return False
            if record.phase is CurrentWorkPhase.CLOSING:
                raise RuntimeError("ROOT work continuation run is closing")
            record.last_turn_id = turn_id
            record.phase = CurrentWorkPhase.ACTIVE
            return True

    def mark_root_idle(self, *, exact_turn_id: str) -> FrozenCurrentWorkSnapshot | None:
        with self._lock:
            record = self._root
            if record is None or record.last_turn_id != exact_turn_id:
                return
            if record.pending_settlements:
                raise RuntimeError("ROOT work became idle before settlement drain")
            if record.phase is CurrentWorkPhase.ACTIVE:
                record.phase = CurrentWorkPhase.IDLE
                had_media = bool(record.media_ranges)
                record.media_ranges.clear()
                record.latest_media_starts.clear()
                had_todos = bool(record.current_snapshot.ordered_items)
                if had_todos:
                    # Keep only the empty run identity for a later external
                    # continuation. Idle never retains a current checklist.
                    snapshot = FrozenTodoSnapshot(
                        run_identity=record.run_identity,
                        revision=record.current_snapshot.revision + 1,
                        ordered_items=(),
                        pending_count=0,
                        in_progress_count=0,
                        completed_count=0,
                    )
                    record.current_snapshot = snapshot
                if had_todos or had_media:
                    return self.current_work_snapshot()

    def require_active(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        exact_turn_id: str,
    ) -> RootWorkIdentity:
        with self._lock:
            record = self._record(scope_kind)
            if (
                record is None
                or record.phase is not CurrentWorkPhase.ACTIVE
                or record.last_turn_id != exact_turn_id
            ):
                raise LookupError("todo scope is no longer active")
            return record.run_identity

    def reserve_media_read_start(
        self, *, exact_turn_id: str, attempt_id: str, canonical_path: str, file_mtime_ns: str,
    ) -> bool:
        require_id(attempt_id, "attempt")
        with self._lock:
            record = self._root
            if (record is None or record.phase is not CurrentWorkPhase.ACTIVE
                    or record.last_turn_id != exact_turn_id):
                return False
            record.latest_media_starts[canonical_path] = (file_mtime_ns, attempt_id)
            return True

    def observe_media_read_start(
        self, *, exact_turn_id: str, attempt_id: str, canonical_path: str,
        file_mtime_ns: str, from_seconds: float, to_seconds: float,
    ) -> bool:
        observation = MediaReadRange(canonical_path, file_mtime_ns, from_seconds, to_seconds, attempt_id)
        with self._lock:
            record = self._root
            if (record is None or record.phase is not CurrentWorkPhase.ACTIVE
                    or record.last_turn_id != exact_turn_id):
                return False
            if record.latest_media_starts.get(canonical_path) != (file_mtime_ns, attempt_id):
                return False
            key = (canonical_path, file_mtime_ns)
            for old_key in tuple(record.media_ranges):
                if old_key[0] == canonical_path and old_key != key:
                    del record.media_ranges[old_key]
            record.media_ranges[key] = observation
            return True

    def current_work_snapshot(self) -> FrozenCurrentWorkSnapshot:
        with self._lock:
            return FrozenCurrentWorkSnapshot(self.current_snapshots(), self.current_media_ranges())

    def current_media_ranges(self) -> tuple[MediaReadRange, ...]:
        with self._lock:
            record = self._root
            if record is None or record.phase is CurrentWorkPhase.IDLE:
                return ()
            for key in tuple(record.media_ranges):
                try:
                    valid = str(Path(key[0]).stat().st_mtime_ns) == key[1]
                except OSError:
                    valid = False
                if not valid:
                    del record.media_ranges[key]
            return tuple(record.media_ranges.values())

    def prepare_replace(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        exact_turn_id: str,
        attempt_id: str,
        proposed_result_entry_id: str,
        candidate: FrozenTodoCandidate,
        acknowledgement: bytes,
    ) -> PreparedTodoReplacement:
        with self._lock:
            record = self._record(scope_kind)
            if (
                record is None
                or record.phase is not CurrentWorkPhase.ACTIVE
                or record.last_turn_id != exact_turn_id
            ):
                raise LookupError("todo scope is no longer active")
            if (
                len(record.pending_settlements)
                >= MAXIMUM_PENDING_TODO_SETTLEMENTS_PER_RUN
            ):
                raise RuntimeError("TODO settlement capacity is exhausted")
            token_id = random_id("todo-settlement")
            while token_id in record.pending_settlements:
                token_id = random_id("todo-settlement")
            prepared = PreparedTodoReplacement(
                run_identity=record.run_identity,
                attempt_id=attempt_id,
                proposed_result_entry_id=proposed_result_entry_id,
                candidate=candidate,
                acknowledgement=acknowledgement,
                token_id=token_id,
            )
            existing = record.pending_settlements.get(token_id)
            if existing is not None and existing != prepared:
                raise RuntimeError("TODO settlement token conflicts")
            record.pending_settlements[token_id] = prepared
            return prepared

    def commit(self, prepared: PreparedTodoReplacement) -> TodoInstallation:
        with self._lock:
            record = self._record_for_identity(prepared.run_identity)
            if record is None or record.phase not in {
                CurrentWorkPhase.ACTIVE,
                CurrentWorkPhase.CLOSING,
            }:
                raise RuntimeError("accepted TODO replacement lost its run")
            retained = record.pending_settlements.get(prepared.token_id)
            if retained != prepared:
                raise RuntimeError("accepted TODO settlement token conflicts")
            revision = record.current_snapshot.revision + 1
            snapshot = _snapshot(prepared.run_identity, revision, prepared.candidate)
            record.current_snapshot = snapshot
            record.pending_settlements.pop(prepared.token_id)
            return TodoInstallation(
                installed_snapshot=snapshot,
                turn_id=record.last_turn_id,
                disposition=(
                    TodoLiveDisposition.CLEARED
                    if not snapshot.ordered_items
                    else TodoLiveDisposition.ACTIVE
                ),
            )

    def discard(self, prepared: PreparedTodoReplacement) -> None:
        with self._lock:
            record = self._record_for_identity(prepared.run_identity)
            if record is None:
                return
            retained = record.pending_settlements.get(prepared.token_id)
            if retained is None:
                return
            if retained != prepared:
                raise RuntimeError("discarded TODO settlement token conflicts")
            record.pending_settlements.pop(prepared.token_id)

    def mark_closing(
        self,
        *,
        scope_kind: ModelInputScopeKind,
    ) -> bool:
        with self._lock:
            record = self._record(scope_kind)
            if record is None:
                return False
            record.phase = CurrentWorkPhase.CLOSING
            return True

    def close_run(
        self,
        *,
        scope_kind: ModelInputScopeKind,
    ) -> FrozenCurrentWorkCloseProjection | None:
        with self._lock:
            record = self._record(scope_kind)
            if record is None:
                return None
            if record.pending_settlements:
                raise RuntimeError("ROOT work closed before settlement drain")
            projection = self._close_projection(record)
            self._root = None
            return projection

    def snapshot(
        self,
        *,
        scope_kind: ModelInputScopeKind,
    ) -> FrozenTodoSnapshot | None:
        with self._lock:
            record = self._record(scope_kind)
            return None if record is None else record.current_snapshot

    def current_snapshots(self) -> tuple[FrozenTodoSnapshot, ...]:
        with self._lock:
            records: Iterable[_CurrentWorkRecord] = (
                *((self._root,) if self._root is not None else ()),
            )
            return tuple(record.current_snapshot for record in records)

    def freeze_compaction_handoff(
        self,
        *,
        scope_kind: ModelInputScopeKind,
    ) -> FrozenTodoSnapshot | None:
        return self.snapshot(
            scope_kind=scope_kind,
        )

    def _record(
        self,
        scope_kind: ModelInputScopeKind,
    ) -> _CurrentWorkRecord | None:
        return self._root

    def _record_for_identity(self, identity: RootWorkIdentity) -> _CurrentWorkRecord | None:
        record = self._record(identity.scope_kind)
        if record is None or record.run_identity != identity:
            return None
        return record

    @staticmethod
    def _close_projection(record: _CurrentWorkRecord) -> FrozenCurrentWorkCloseProjection:
        return FrozenCurrentWorkCloseProjection(
            run_identity=record.run_identity,
            last_turn_id=record.last_turn_id,
        )

    def _require_session(self, session_id: str) -> None:
        if session_id != self._session_id:
            raise ValueError("ROOT work activation belongs to another session")


def build_root_activation(
    *,
    session_id: str,
    admission_kind: str,
    exact_turn_id: str,
    exact_initial_entry_id: str,
    exact_context_binding_revision_id: str,
    command_id: str | None = None,
    queue_item_id: str | None = None,
    queue_sequence: int | None = None,
) -> PreparedRootWorkActivation:
    if (
        not session_id
        or not exact_turn_id
        or not exact_initial_entry_id
        or not exact_context_binding_revision_id
    ):
        raise ValueError("ROOT work admission identity is incomplete")
    if admission_kind not in {"DIRECT", "QUEUED"}:
        raise ValueError("ROOT work admission kind is invalid")
    direct = command_id is not None and queue_item_id is None and queue_sequence is None
    queued = (
        command_id is None
        and queue_item_id is not None
        and queue_sequence is not None
        and queue_sequence > 0
    )
    if (admission_kind == "DIRECT" and not direct) or (
        admission_kind == "QUEUED" and not queued
    ):
        raise ValueError("ROOT work admission identity union is invalid")
    proposed = derive_id("todo-run", "root", session_id, exact_turn_id)
    return PreparedRootWorkActivation(
        session_id=session_id,
        admission_kind=admission_kind,
        command_id=command_id,
        queue_item_id=queue_item_id,
        queue_sequence=queue_sequence,
        exact_turn_id=exact_turn_id,
        exact_initial_entry_id=exact_initial_entry_id,
        exact_context_binding_revision_id=exact_context_binding_revision_id,
        proposed_root_run_id=proposed,
    )


def _root_identity(
    *, session_id: str, root_run_id: str, owner_epoch: str
) -> RootWorkIdentity:
    return RootWorkIdentity(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        root_run_id=root_run_id,
        owner_epoch=owner_epoch,
    )


def _new_record(
    identity: RootWorkIdentity,
    turn_id: str,
    activation: PreparedRootWorkActivation,
) -> _CurrentWorkRecord:
    empty = FrozenTodoCandidate(
        ordered_items=(),
        pending_count=0,
        in_progress_count=0,
        completed_count=0,
        canonical_json_utf8_bytes=len(b'{"items":[]}'),
    )
    return _CurrentWorkRecord(
        run_identity=identity,
        activation=activation,
        phase=CurrentWorkPhase.ACTIVE,
        last_turn_id=turn_id,
        current_snapshot=_snapshot(identity, 0, empty),
        pending_settlements={},
        media_ranges={},
        latest_media_starts={},
    )


def _snapshot(
    identity: RootWorkIdentity, revision: int, candidate: FrozenTodoCandidate
) -> FrozenTodoSnapshot:
    return FrozenTodoSnapshot(
        run_identity=identity,
        revision=revision,
        ordered_items=candidate.ordered_items,
        pending_count=candidate.pending_count,
        in_progress_count=candidate.in_progress_count,
        completed_count=candidate.completed_count,
    )


__all__ = [
    "FrozenCurrentWorkSnapshot",
    "MediaReadRange",
    "FrozenCurrentWorkCloseProjection",
    "FrozenTodoSnapshot",
    "PreparedTodoReplacement",
    "PreparedRootWorkActivation",
    "TodoInstallation",
    "TodoLiveDisposition",
    "RootWorkIdentity",
    "CurrentWorkPhase",
    "CurrentWorkStateOwner",
    "build_root_activation",
]
