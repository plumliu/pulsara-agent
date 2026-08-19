"""Exact-run process-local authority for the lightweight TODO checklist."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock

from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.tools.builtins.todo import FrozenTodoCandidate, FrozenTodoItem


MAXIMUM_PENDING_TODO_SETTLEMENTS_PER_RUN = 64


class TodoRunPhase(StrEnum):
    ACTIVE = "ACTIVE"
    IDLE_RETAINED = "IDLE_RETAINED"
    CLOSING = "CLOSING"


class TodoLiveDisposition(StrEnum):
    ACTIVE = "ACTIVE"
    CLEARED = "CLEARED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class TodoRunIdentity:
    session_id: str
    scope_kind: ModelInputScopeKind
    root_run_id: str | None
    subagent_task_id: str | None
    owner_epoch: str
    identity_fingerprint: str

    @property
    def todo_run_id(self) -> str:
        value = self.root_run_id or self.subagent_task_id
        if value is None:
            raise RuntimeError("TODO run identity has no public run ID")
        return value

    def __post_init__(self) -> None:
        root = (
            self.scope_kind is ModelInputScopeKind.ROOT
            and self.root_run_id is not None
            and self.subagent_task_id is None
        )
        child = (
            self.scope_kind is ModelInputScopeKind.SUBAGENT_TASK
            and self.root_run_id is None
            and self.subagent_task_id is not None
        )
        if (
            not self.session_id
            or not self.owner_epoch
            or not self.identity_fingerprint.startswith("sha256:")
            or root == child
        ):
            raise ValueError("TODO run identity is invalid")


@dataclass(frozen=True, slots=True)
class FrozenTodoSnapshot:
    run_identity: TodoRunIdentity
    revision: int
    ordered_items: tuple[FrozenTodoItem, ...]
    pending_count: int
    in_progress_count: int
    completed_count: int
    snapshot_fingerprint: str

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
            or not self.snapshot_fingerprint.startswith("sha256:")
        ):
            raise ValueError("TODO snapshot is invalid")


@dataclass(frozen=True, slots=True)
class PreparedTodoRootRunActivation:
    session_id: str
    admission_kind: str
    command_id: str | None
    queue_item_id: str | None
    queue_sequence: int | None
    exact_turn_id: str
    exact_initial_entry_id: str
    exact_context_binding_revision_id: str
    proposed_root_run_id: str
    exact_admission_candidate_fingerprint: str
    activation_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedTodoChildRunActivation:
    session_id: str
    subagent_task_id: str
    exact_turn_id: str
    exact_initial_entry_id: str
    exact_context_binding_revision_id: str
    exact_admission_candidate_fingerprint: str
    activation_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedTodoReplacement:
    run_identity: TodoRunIdentity
    attempt_id: str
    proposed_result_entry_id: str
    candidate_fingerprint: str
    acknowledgement_fingerprint: str
    candidate: FrozenTodoCandidate
    acknowledgement: bytes
    token_id: str
    token_fingerprint: str


@dataclass(frozen=True, slots=True)
class TodoInstallation:
    installed_snapshot: FrozenTodoSnapshot
    turn_id: str
    disposition: TodoLiveDisposition


@dataclass(frozen=True, slots=True)
class FrozenTodoCloseProjection:
    run_identity: TodoRunIdentity
    last_turn_id: str
    closing_revision: int
    disposition: TodoLiveDisposition = TodoLiveDisposition.CLOSED


@dataclass(frozen=True, slots=True)
class FrozenTodoCompactionHandoff:
    run_identity: TodoRunIdentity
    source_snapshot_fingerprint: str
    actionable_items: tuple[FrozenTodoItem, ...]
    completed_omitted: int
    handoff_fingerprint: str


@dataclass(slots=True)
class _TodoRunRecord:
    run_identity: TodoRunIdentity
    activation_fingerprint: str
    phase: TodoRunPhase
    last_turn_id: str
    current_snapshot: FrozenTodoSnapshot
    pending_settlements: dict[str, PreparedTodoReplacement]


class TodoRunStateOwner:
    """The sole same-Host current TODO owner; never reconstructed from rows."""

    def __init__(self, *, session_id: str, owner_epoch: str) -> None:
        if not session_id or not owner_epoch:
            raise ValueError("TODO owner identity is required")
        self._session_id = session_id
        self._owner_epoch = owner_epoch
        self._root: _TodoRunRecord | None = None
        self._children: dict[str, _TodoRunRecord] = {}
        self._lock = RLock()

    def activate_root_run(
        self,
        prepared: PreparedTodoRootRunActivation,
        *,
        allow_closing_predecessor: bool = False,
    ) -> FrozenTodoCloseProjection | None:
        with self._lock:
            self._require_session(prepared.session_id)
            if self._root is not None and (
                self._root.run_identity.root_run_id == prepared.proposed_root_run_id
                and self._root.last_turn_id == prepared.exact_turn_id
            ):
                if self._root.activation_fingerprint != prepared.activation_fingerprint:
                    raise RuntimeError("ROOT TODO activation identity conflicts")
                return None
            old = self._root
            if old is not None and old.pending_settlements:
                raise RuntimeError("ROOT TODO run changed with pending settlement")
            if old is not None and old.phase is not TodoRunPhase.IDLE_RETAINED and not (
                allow_closing_predecessor and old.phase is TodoRunPhase.CLOSING
            ):
                raise RuntimeError("ROOT TODO run changed before becoming idle")
            closed = None if old is None else self._close_projection(old)
            identity = _root_identity(
                session_id=self._session_id,
                root_run_id=prepared.proposed_root_run_id,
                owner_epoch=self._owner_epoch,
            )
            self._root = _new_record(
                identity, prepared.exact_turn_id, prepared.activation_fingerprint
            )
            return closed

    def activate_child_run(self, prepared: PreparedTodoChildRunActivation) -> None:
        with self._lock:
            self._require_session(prepared.session_id)
            current = self._children.get(prepared.subagent_task_id)
            if current is not None:
                if current.last_turn_id == prepared.exact_turn_id:
                    if current.activation_fingerprint != prepared.activation_fingerprint:
                        raise RuntimeError("child TODO activation identity conflicts")
                    return
                raise RuntimeError("child TODO run identity conflicts")
            if len(self._children) >= STAGE2_LIMITS.nonterminal_subagent_hard_items:
                raise RuntimeError("child TODO run capacity is exhausted")
            identity = _child_identity(
                session_id=self._session_id,
                task_id=prepared.subagent_task_id,
                owner_epoch=self._owner_epoch,
            )
            self._children[prepared.subagent_task_id] = _new_record(
                identity, prepared.exact_turn_id, prepared.activation_fingerprint
            )

    def bind_continuation(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        turn_id: str,
    ) -> None:
        with self._lock:
            record = self._record(scope_kind, scope_subagent_task_id)
            if record is None or record.phase is TodoRunPhase.CLOSING:
                raise RuntimeError("TODO continuation has no active run")
            record.last_turn_id = turn_id
            record.phase = TodoRunPhase.ACTIVE

    def bind_continuation_if_present(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        turn_id: str,
    ) -> bool:
        """Bind an external continuation without reconstructing lost TODO state."""

        with self._lock:
            record = self._record(scope_kind, scope_subagent_task_id)
            if record is None:
                return False
            if record.phase is TodoRunPhase.CLOSING:
                raise RuntimeError("TODO continuation run is closing")
            record.last_turn_id = turn_id
            record.phase = TodoRunPhase.ACTIVE
            return True

    def mark_root_idle(self, *, exact_turn_id: str) -> None:
        with self._lock:
            record = self._root
            if record is None or record.last_turn_id != exact_turn_id:
                return
            if record.pending_settlements:
                raise RuntimeError("ROOT TODO run became idle before settlement drain")
            if record.phase is TodoRunPhase.ACTIVE:
                record.phase = TodoRunPhase.IDLE_RETAINED

    def require_active(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        exact_turn_id: str,
    ) -> TodoRunIdentity:
        with self._lock:
            record = self._record(scope_kind, scope_subagent_task_id)
            if (
                record is None
                or record.phase is not TodoRunPhase.ACTIVE
                or record.last_turn_id != exact_turn_id
            ):
                raise LookupError("todo scope is no longer active")
            return record.run_identity

    def prepare_replace(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        exact_turn_id: str,
        attempt_id: str,
        proposed_result_entry_id: str,
        candidate: FrozenTodoCandidate,
        acknowledgement: bytes,
    ) -> PreparedTodoReplacement:
        with self._lock:
            record = self._record(scope_kind, scope_subagent_task_id)
            if (
                record is None
                or record.phase is not TodoRunPhase.ACTIVE
                or record.last_turn_id != exact_turn_id
            ):
                raise LookupError("todo scope is no longer active")
            if (
                len(record.pending_settlements)
                >= MAXIMUM_PENDING_TODO_SETTLEMENTS_PER_RUN
            ):
                raise RuntimeError("TODO settlement capacity is exhausted")
            ack_fingerprint = context_fingerprint(
                "pulsara:todo-acknowledgement:v1",
                acknowledgement.decode("utf-8"),
            )
            token_fingerprint = context_fingerprint(
                "pulsara:todo-settlement-token:v1",
                {
                    "run": record.run_identity.identity_fingerprint,
                    "attempt_id": attempt_id,
                    "result_entry_id": proposed_result_entry_id,
                    "candidate": candidate.candidate_fingerprint,
                    "ack": ack_fingerprint,
                },
            )
            token_id = (
                "todo-settlement:" + token_fingerprint.removeprefix("sha256:")
            )
            prepared = PreparedTodoReplacement(
                run_identity=record.run_identity,
                attempt_id=attempt_id,
                proposed_result_entry_id=proposed_result_entry_id,
                candidate_fingerprint=candidate.candidate_fingerprint,
                acknowledgement_fingerprint=ack_fingerprint,
                candidate=candidate,
                acknowledgement=acknowledgement,
                token_id=token_id,
                token_fingerprint=token_fingerprint,
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
                TodoRunPhase.ACTIVE,
                TodoRunPhase.CLOSING,
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
        scope_subagent_task_id: str | None,
    ) -> bool:
        with self._lock:
            record = self._record(scope_kind, scope_subagent_task_id)
            if record is None:
                return False
            record.phase = TodoRunPhase.CLOSING
            return True

    def close_run(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> FrozenTodoCloseProjection | None:
        with self._lock:
            record = self._record(scope_kind, scope_subagent_task_id)
            if record is None:
                return None
            if record.pending_settlements:
                raise RuntimeError("TODO run closed before settlement drain")
            projection = self._close_projection(record)
            if scope_kind is ModelInputScopeKind.ROOT:
                self._root = None
            else:
                assert scope_subagent_task_id is not None
                self._children.pop(scope_subagent_task_id, None)
            return projection

    def snapshot(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> FrozenTodoSnapshot | None:
        with self._lock:
            record = self._record(scope_kind, scope_subagent_task_id)
            return None if record is None else record.current_snapshot

    def current_snapshots(self) -> tuple[FrozenTodoSnapshot, ...]:
        with self._lock:
            records: Iterable[_TodoRunRecord] = (
                *((self._root,) if self._root is not None else ()),
                *(self._children[key] for key in sorted(self._children)),
            )
            return tuple(record.current_snapshot for record in records)

    def freeze_compaction_handoff(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> FrozenTodoCompactionHandoff | None:
        snapshot = self.snapshot(
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        )
        if snapshot is None:
            return None
        actionable = tuple(
            item
            for item in snapshot.ordered_items
            if item.status.value != "completed"
        )
        fingerprint = context_fingerprint(
            "pulsara:todo-compaction-handoff:v1",
            {
                "snapshot": snapshot.snapshot_fingerprint,
                "items": tuple(
                    {
                        "ordinal": item.ordinal,
                        "status": item.status.value,
                        "text": item.text,
                    }
                    for item in actionable
                ),
                "completed_omitted": snapshot.completed_count,
                "trust": "UNTRUSTED_OBSERVATION",
            },
        )
        return FrozenTodoCompactionHandoff(
            run_identity=snapshot.run_identity,
            source_snapshot_fingerprint=snapshot.snapshot_fingerprint,
            actionable_items=actionable,
            completed_omitted=snapshot.completed_count,
            handoff_fingerprint=fingerprint,
        )

    def _record(
        self,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> _TodoRunRecord | None:
        if scope_kind is ModelInputScopeKind.ROOT:
            if scope_subagent_task_id is not None:
                raise ValueError("ROOT TODO scope cannot carry a child task")
            return self._root
        if (
            scope_kind is not ModelInputScopeKind.SUBAGENT_TASK
            or not scope_subagent_task_id
        ):
            raise ValueError("child TODO scope requires its exact task")
        return self._children.get(scope_subagent_task_id)

    def _record_for_identity(
        self, identity: TodoRunIdentity
    ) -> _TodoRunRecord | None:
        record = self._record(identity.scope_kind, identity.subagent_task_id)
        if record is None or record.run_identity != identity:
            return None
        return record

    @staticmethod
    def _close_projection(record: _TodoRunRecord) -> FrozenTodoCloseProjection:
        return FrozenTodoCloseProjection(
            run_identity=record.run_identity,
            last_turn_id=record.last_turn_id,
            closing_revision=record.current_snapshot.revision + 1,
        )

    def _require_session(self, session_id: str) -> None:
        if session_id != self._session_id:
            raise ValueError("TODO activation belongs to another session")


def build_root_activation(
    *,
    session_id: str,
    admission_kind: str,
    exact_turn_id: str,
    exact_initial_entry_id: str,
    exact_context_binding_revision_id: str,
    exact_admission_candidate_fingerprint: str,
    command_id: str | None = None,
    queue_item_id: str | None = None,
    queue_sequence: int | None = None,
) -> PreparedTodoRootRunActivation:
    if (
        not session_id
        or not exact_turn_id
        or not exact_initial_entry_id
        or not exact_context_binding_revision_id
        or not exact_admission_candidate_fingerprint.startswith("sha256:")
    ):
        raise ValueError("TODO ROOT admission identity is incomplete")
    if admission_kind not in {"DIRECT", "QUEUED"}:
        raise ValueError("TODO ROOT admission kind is invalid")
    direct = (
        command_id is not None
        and queue_item_id is None
        and queue_sequence is None
    )
    queued = (
        command_id is None
        and queue_item_id is not None
        and queue_sequence is not None
        and queue_sequence > 0
    )
    if (admission_kind == "DIRECT" and not direct) or (
        admission_kind == "QUEUED" and not queued
    ):
        raise ValueError("TODO ROOT admission identity union is invalid")
    proposed = "todo-root-run:" + context_fingerprint(
        "pulsara:todo-root-run-id:v1",
        {"session_id": session_id, "turn_id": exact_turn_id},
    ).removeprefix("sha256:")
    payload = {
        "session_id": session_id,
        "admission_kind": admission_kind,
        "command_id": command_id,
        "queue_item_id": queue_item_id,
        "queue_sequence": queue_sequence,
        "turn_id": exact_turn_id,
        "entry_id": exact_initial_entry_id,
        "context_binding_revision_id": exact_context_binding_revision_id,
        "root_run_id": proposed,
        "admission_candidate": exact_admission_candidate_fingerprint,
    }
    return PreparedTodoRootRunActivation(
        session_id=session_id,
        admission_kind=admission_kind,
        command_id=command_id,
        queue_item_id=queue_item_id,
        queue_sequence=queue_sequence,
        exact_turn_id=exact_turn_id,
        exact_initial_entry_id=exact_initial_entry_id,
        exact_context_binding_revision_id=exact_context_binding_revision_id,
        proposed_root_run_id=proposed,
        exact_admission_candidate_fingerprint=(
            exact_admission_candidate_fingerprint
        ),
        activation_fingerprint=context_fingerprint(
            "pulsara:prepared-todo-root-run-activation:v1", payload
        ),
    )


def build_child_activation(
    *,
    session_id: str,
    subagent_task_id: str,
    exact_turn_id: str,
    exact_initial_entry_id: str,
    exact_context_binding_revision_id: str,
    exact_admission_candidate_fingerprint: str,
) -> PreparedTodoChildRunActivation:
    if (
        not session_id
        or not subagent_task_id
        or not exact_turn_id
        or not exact_initial_entry_id
        or not exact_context_binding_revision_id
        or not exact_admission_candidate_fingerprint.startswith("sha256:")
    ):
        raise ValueError("TODO child admission identity is incomplete")
    payload = {
        "session_id": session_id,
        "subagent_task_id": subagent_task_id,
        "turn_id": exact_turn_id,
        "entry_id": exact_initial_entry_id,
        "context_binding_revision_id": exact_context_binding_revision_id,
        "admission_candidate": exact_admission_candidate_fingerprint,
    }
    return PreparedTodoChildRunActivation(
        session_id=session_id,
        subagent_task_id=subagent_task_id,
        exact_turn_id=exact_turn_id,
        exact_initial_entry_id=exact_initial_entry_id,
        exact_context_binding_revision_id=exact_context_binding_revision_id,
        exact_admission_candidate_fingerprint=(
            exact_admission_candidate_fingerprint
        ),
        activation_fingerprint=context_fingerprint(
            "pulsara:prepared-todo-child-run-activation:v1", payload
        ),
    )


def _root_identity(
    *, session_id: str, root_run_id: str, owner_epoch: str
) -> TodoRunIdentity:
    return TodoRunIdentity(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        root_run_id=root_run_id,
        subagent_task_id=None,
        owner_epoch=owner_epoch,
        identity_fingerprint=context_fingerprint(
            "pulsara:todo-run-identity:v1",
            {
                "session_id": session_id,
                "scope_kind": "ROOT",
                "root_run_id": root_run_id,
                "owner_epoch": owner_epoch,
            },
        ),
    )


def _child_identity(
    *, session_id: str, task_id: str, owner_epoch: str
) -> TodoRunIdentity:
    return TodoRunIdentity(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
        root_run_id=None,
        subagent_task_id=task_id,
        owner_epoch=owner_epoch,
        identity_fingerprint=context_fingerprint(
            "pulsara:todo-run-identity:v1",
            {
                "session_id": session_id,
                "scope_kind": "SUBAGENT_TASK",
                "subagent_task_id": task_id,
                "owner_epoch": owner_epoch,
            },
        ),
    )


def _new_record(
    identity: TodoRunIdentity, turn_id: str, activation_fingerprint: str
) -> _TodoRunRecord:
    empty = FrozenTodoCandidate(
        ordered_items=(),
        pending_count=0,
        in_progress_count=0,
        completed_count=0,
        canonical_json_utf8_bytes=len(b'{"items":[]}'),
        candidate_fingerprint=context_fingerprint(
            "pulsara:todo-replacement:v1", {"items": ()}
        ),
    )
    return _TodoRunRecord(
        run_identity=identity,
        activation_fingerprint=activation_fingerprint,
        phase=TodoRunPhase.ACTIVE,
        last_turn_id=turn_id,
        current_snapshot=_snapshot(identity, 0, empty),
        pending_settlements={},
    )


def _snapshot(
    identity: TodoRunIdentity, revision: int, candidate: FrozenTodoCandidate
) -> FrozenTodoSnapshot:
    return FrozenTodoSnapshot(
        run_identity=identity,
        revision=revision,
        ordered_items=candidate.ordered_items,
        pending_count=candidate.pending_count,
        in_progress_count=candidate.in_progress_count,
        completed_count=candidate.completed_count,
        snapshot_fingerprint=context_fingerprint(
            "pulsara:todo-run-snapshot:v1",
            {
                "run": identity.identity_fingerprint,
                "revision": revision,
                "candidate": candidate.candidate_fingerprint,
            },
        ),
    )


__all__ = [
    "FrozenTodoCloseProjection",
    "FrozenTodoCompactionHandoff",
    "FrozenTodoSnapshot",
    "PreparedTodoChildRunActivation",
    "PreparedTodoReplacement",
    "PreparedTodoRootRunActivation",
    "TodoInstallation",
    "TodoLiveDisposition",
    "TodoRunIdentity",
    "TodoRunPhase",
    "TodoRunStateOwner",
    "build_child_activation",
    "build_root_activation",
]
