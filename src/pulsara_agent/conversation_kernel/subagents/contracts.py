"""Closed immutable facts for ROOT-orchestrated worker leaves.

These carriers are provider-neutral and process-local unless a repository method
mechanically projects their fields into the two canonical subagent relations.
They own no executor, callback, transport, repository, or mutable JSON value.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import re
from typing import Iterable, Mapping, Protocol

from pulsara_agent.conversation_kernel.contracts import canonical_digest
from pulsara_agent.primitives.context import (
    FrozenJsonArrayFact,
    FrozenJsonObjectFact,
    FrozenJsonValue,
    canonical_json_bytes,
    freeze_json,
)


MAXIMUM_TASK_OBJECTIVE_UTF8_BYTES = 65_536
MAXIMUM_RESULT_SUMMARY_UTF8_BYTES = 16_384
MAXIMUM_RESULT_OUTPUT_PREVIEW_UTF8_BYTES = 32_768
MAXIMUM_RESULT_DIAGNOSTICS_ITEMS = 32
MAXIMUM_RESULT_DIAGNOSTICS_UTF8_BYTES = 65_536
MAXIMUM_INTER_AGENT_MESSAGE_UTF8_BYTES = 16_384
MAXIMUM_PARENT_CONTEXT_UNITS = 3


def _text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} exceeds its UTF-8 bound")
    return value


def _fingerprint(namespace: str, payload: object) -> str:
    return canonical_digest(f"pulsara:round10:{namespace}:v1", payload)


def _stable_id(namespace: str, *parts: str) -> str:
    digest = sha256()
    digest.update(f"pulsara:round10:{namespace}:v1\0".encode())
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return f"{namespace}:" + digest.hexdigest()


class SubagentTaskStatus(StrEnum):
    PENDING_START = "PENDING_START"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    BLOCKED_DEPENDENCY_FAILED = "BLOCKED_DEPENDENCY_FAILED"

    @property
    def terminal(self) -> bool:
        return self in {
            type(self).COMPLETED,
            type(self).FAILED,
            type(self).CANCELLED,
            type(self).INTERRUPTED,
            type(self).BLOCKED_DEPENDENCY_FAILED,
        }


@dataclass(frozen=True, slots=True)
class FrozenSubagentTaskInitialDisposition:
    status: SubagentTaskStatus
    pending_reason: str | None
    terminal_reason: str | None

    def __post_init__(self) -> None:
        expected = {
            SubagentTaskStatus.PENDING_START: ("CAPACITY", None),
            SubagentTaskStatus.WAITING_DEPENDENCY: ("DEPENDENCY", None),
            SubagentTaskStatus.BLOCKED_DEPENDENCY_FAILED: (
                None,
                "DEPENDENCY_FAILED",
            ),
        }
        if expected.get(self.status) != (self.pending_reason, self.terminal_reason):
            raise ValueError("subagent initial disposition is invalid")


def derive_subagent_batch_initial_dispositions(
    *,
    ordered_tasks: Iterable[tuple[str, tuple[str, ...]]],
    external_states: Mapping[str, tuple[SubagentTaskStatus, bool]],
) -> tuple[tuple[str, FrozenSubagentTaskInitialDisposition], ...]:
    """Resolve the complete accepted DAG frontier, including transitive blocks."""

    tasks = tuple(ordered_tasks)
    dependency_map = dict(tasks)
    if len(dependency_map) != len(tasks) or any(not task_id for task_id, _ in tasks):
        raise ValueError("subagent batch task identities are invalid")
    visiting: set[str] = set()
    resolved: dict[str, FrozenSubagentTaskInitialDisposition] = {}

    def resolve(task_id: str) -> FrozenSubagentTaskInitialDisposition:
        existing = resolved.get(task_id)
        if existing is not None:
            return existing
        if task_id in visiting:
            raise ValueError("subagent dependency graph contains a cycle")
        visiting.add(task_id)
        states: list[tuple[SubagentTaskStatus, bool]] = []
        for dependency_id in dependency_map[task_id]:
            if dependency_id in dependency_map:
                disposition = resolve(dependency_id)
                states.append((disposition.status, False))
            else:
                state = external_states.get(dependency_id)
                if state is None:
                    raise ValueError("subagent dependency is unknown")
                states.append(state)
        visiting.remove(task_id)
        if any(status.terminal and status is not SubagentTaskStatus.COMPLETED for status, _ in states):
            disposition = FrozenSubagentTaskInitialDisposition(
                SubagentTaskStatus.BLOCKED_DEPENDENCY_FAILED,
                None,
                "DEPENDENCY_FAILED",
            )
        elif any(
            status is SubagentTaskStatus.COMPLETED and not has_result
            for status, has_result in states
        ):
            raise ValueError("completed dependency lacks exact result")
        elif not states or all(
            status is SubagentTaskStatus.COMPLETED and has_result
            for status, has_result in states
        ):
            disposition = FrozenSubagentTaskInitialDisposition(
                SubagentTaskStatus.PENDING_START, "CAPACITY", None
            )
        else:
            disposition = FrozenSubagentTaskInitialDisposition(
                SubagentTaskStatus.WAITING_DEPENDENCY, "DEPENDENCY", None
            )
        resolved[task_id] = disposition
        return disposition

    return tuple((task_id, resolve(task_id)) for task_id, _ in tasks)


class SubagentContextMode(StrEnum):
    NONE = "NONE"
    LAST_N = "LAST_N"


class SubagentProfileKind(StrEnum):
    GENERAL_WORKER = "general_worker"
    RESEARCH_WORKER = "research_worker"
    REVIEW_WORKER = "review_worker"
    VERIFICATION_WORKER = "verification_worker"
    SYNTHESIZER = "synthesizer"


class SubagentResultSource(StrEnum):
    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"


class SubagentBatchConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


class ExplicitSubagentResultConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


class SubagentTaskTerminalConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


class PreparedToolResultAcceptanceFact(Protocol):
    """Narrow structural view needed by the process-local result composite."""

    result_entry_id: str
    result_id: str
    attempt_id: str | None
    result_state: str
    candidate_fingerprint: str


@dataclass(frozen=True, slots=True)
class ExplicitSubagentResultConfirmation:
    kind: ExplicitSubagentResultConfirmationKind
    accepted_entry_id: str | None = None
    entry_sequence: int | None = None
    event_sequence: int | None = None

    def __post_init__(self) -> None:
        full = self.kind is ExplicitSubagentResultConfirmationKind.FULL
        if full != all(
            value is not None
            for value in (
                self.accepted_entry_id,
                self.entry_sequence,
                self.event_sequence,
            )
        ):
            raise ValueError("explicit result confirmation union is invalid")


@dataclass(frozen=True, slots=True)
class SubagentBatchConfirmation:
    kind: SubagentBatchConfirmationKind


@dataclass(frozen=True, slots=True)
class PreparedSubagentTaskTerminalSettlement:
    """Stable task-only terminal candidate for failure/stop/Host-close paths.

    A running child turn is never terminalized through this carrier.  The
    existing joint turn/task cancellation settlement owns that case.  The
    boolean only distinguishes a task that must never have admitted a turn
    from a post-run failure whose exact child turn is already terminal.
    """

    session_id: str
    workspace_id: str
    writer_generation: int
    task_id: str
    expected_turn_id: str
    status: SubagentTaskStatus
    reason: str
    require_absent_turn: bool
    occurred_at: datetime
    actor_id: str
    event_id: str
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        for field, value in (
            ("session_id", self.session_id),
            ("workspace_id", self.workspace_id),
            ("task_id", self.task_id),
            ("expected_turn_id", self.expected_turn_id),
            ("reason", self.reason),
            ("actor_id", self.actor_id),
            ("event_id", self.event_id),
        ):
            _text(value, field, 512)
        if (
            self.writer_generation < 1
            or not isinstance(self.status, SubagentTaskStatus)
            or not self.status.terminal
            or self.status is SubagentTaskStatus.COMPLETED
            or not isinstance(self.require_absent_turn, bool)
            or self.occurred_at.tzinfo is None
        ):
            raise ValueError("subagent task terminal settlement is invalid")
        expected_event = _stable_id(
            "subagent-task-terminal-event",
            self.session_id,
            self.task_id,
            str(self.writer_generation),
            self.status.value,
            self.reason,
        )
        expected = _fingerprint(
            "task-terminal-settlement",
            {
                "session": self.session_id,
                "workspace": self.workspace_id,
                "writer_generation": self.writer_generation,
                "task": self.task_id,
                "turn": self.expected_turn_id,
                "status": self.status.value,
                "reason": self.reason,
                "require_absent_turn": self.require_absent_turn,
                "occurred_at": self.occurred_at.isoformat(),
                "actor": self.actor_id,
                "event": expected_event,
            },
        )
        if self.event_id != expected_event or self.candidate_fingerprint != expected:
            raise ValueError("subagent task terminal settlement fingerprint mismatch")


def build_subagent_task_terminal_settlement(
    *,
    session_id: str,
    workspace_id: str,
    writer_generation: int,
    task_id: str,
    expected_turn_id: str,
    status: SubagentTaskStatus,
    reason: str,
    require_absent_turn: bool,
    occurred_at: datetime,
    actor_id: str,
) -> PreparedSubagentTaskTerminalSettlement:
    event_id = _stable_id(
        "subagent-task-terminal-event",
        session_id,
        task_id,
        str(writer_generation),
        status.value,
        reason,
    )
    payload = {
        "session": session_id,
        "workspace": workspace_id,
        "writer_generation": writer_generation,
        "task": task_id,
        "turn": expected_turn_id,
        "status": status.value,
        "reason": reason,
        "require_absent_turn": require_absent_turn,
        "occurred_at": occurred_at.isoformat(),
        "actor": actor_id,
        "event": event_id,
    }
    return PreparedSubagentTaskTerminalSettlement(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_generation=writer_generation,
        task_id=task_id,
        expected_turn_id=expected_turn_id,
        status=status,
        reason=reason,
        require_absent_turn=require_absent_turn,
        occurred_at=occurred_at,
        actor_id=actor_id,
        event_id=event_id,
        candidate_fingerprint=_fingerprint("task-terminal-settlement", payload),
    )


@dataclass(frozen=True, slots=True)
class PreparedSubagentTaskStart:
    session_id: str
    workspace_id: str
    writer_generation: int
    task_id: str
    parent_turn_id: str
    objective: str = dataclass_field(repr=False)
    profile: SubagentProfileKind
    parent_context: FrozenSubagentParentContextSelection = dataclass_field(repr=False)
    dependency_context: FrozenDependencyResultContext | None = dataclass_field(
        repr=False
    )
    occurred_at: datetime
    actor_id: str
    event_id: str
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        for field, value in (
            ("session_id", self.session_id),
            ("workspace_id", self.workspace_id),
            ("task_id", self.task_id),
            ("parent_turn_id", self.parent_turn_id),
            ("actor_id", self.actor_id),
            ("event_id", self.event_id),
        ):
            _text(value, field, 512)
        _text(self.objective, "objective", MAXIMUM_TASK_OBJECTIVE_UTF8_BYTES)
        if self.writer_generation < 1 or self.occurred_at.tzinfo is None:
            raise ValueError("subagent task start authority is invalid")
        if not isinstance(self.profile, SubagentProfileKind):
            raise TypeError("subagent task start profile must be closed")
        if self.dependency_context is not None and (
            self.dependency_context.target_task_id != self.task_id
        ):
            raise ValueError("subagent task start dependency context is foreign")
        expected_event = _stable_id(
            "subagent-start-event",
            self.session_id,
            self.task_id,
            str(self.writer_generation),
        )
        expected = _fingerprint(
            "task-start",
            {
                "session": self.session_id,
                "workspace": self.workspace_id,
                "writer_generation": self.writer_generation,
                "task_id": self.task_id,
                "parent_turn_id": self.parent_turn_id,
                "objective": self.objective,
                "profile": self.profile.value,
                "parent_context": self.parent_context.selection_fingerprint,
                "dependency_context": (
                    None
                    if self.dependency_context is None
                    else self.dependency_context.context_fingerprint
                ),
                "occurred_at": self.occurred_at.isoformat(),
                "actor": self.actor_id,
                "event": expected_event,
            },
        )
        if self.event_id != expected_event or self.candidate_fingerprint != expected:
            raise ValueError("subagent task start fingerprint mismatch")


def build_subagent_task_start(
    *,
    session_id: str,
    workspace_id: str,
    writer_generation: int,
    task_id: str,
    parent_turn_id: str,
    objective: str,
    profile: SubagentProfileKind,
    parent_context: FrozenSubagentParentContextSelection,
    dependency_context: FrozenDependencyResultContext | None,
    occurred_at: datetime,
    actor_id: str,
) -> PreparedSubagentTaskStart:
    event_id = _stable_id(
        "subagent-start-event", session_id, task_id, str(writer_generation)
    )
    payload = {
        "session": session_id,
        "workspace": workspace_id,
        "writer_generation": writer_generation,
        "task_id": task_id,
        "parent_turn_id": parent_turn_id,
        "objective": objective,
        "profile": profile.value,
        "parent_context": parent_context.selection_fingerprint,
        "dependency_context": (
            None if dependency_context is None else dependency_context.context_fingerprint
        ),
        "occurred_at": occurred_at.isoformat(),
        "actor": actor_id,
        "event": event_id,
    }
    return PreparedSubagentTaskStart(
        session_id,
        workspace_id,
        writer_generation,
        task_id,
        parent_turn_id,
        objective,
        profile,
        parent_context,
        dependency_context,
        occurred_at,
        actor_id,
        event_id,
        _fingerprint("task-start", payload),
    )


@dataclass(frozen=True, slots=True)
class FrozenRootConversationContextUnitFact:
    ordered_entry_ids: tuple[str, ...]
    ordered_public_items: tuple[str, ...]
    unit_fingerprint: str

    def __post_init__(self) -> None:
        if not self.ordered_entry_ids or not self.ordered_public_items:
            raise ValueError("ROOT context unit must contain entries and public items")
        if any(not item for item in self.ordered_entry_ids + self.ordered_public_items):
            raise ValueError("ROOT context unit values must be non-empty")
        expected = _fingerprint(
            "parent-context-unit",
            {"entries": self.ordered_entry_ids, "items": self.ordered_public_items},
        )
        if self.unit_fingerprint != expected:
            raise ValueError("ROOT context unit fingerprint mismatch")


def build_root_context_unit(
    *, ordered_entry_ids: Iterable[str], ordered_public_items: Iterable[str]
) -> FrozenRootConversationContextUnitFact:
    entries = tuple(ordered_entry_ids)
    items = tuple(ordered_public_items)
    return FrozenRootConversationContextUnitFact(
        entries,
        items,
        _fingerprint("parent-context-unit", {"entries": entries, "items": items}),
    )


@dataclass(frozen=True, slots=True)
class FrozenSubagentParentContextCallSubject:
    session_id: str
    caller_turn_id: str
    provider_input_cut_fingerprint: str
    continuity_epoch_nonce: str
    continuity_epoch_revision: int
    compiled_semantic_input_fingerprint: str
    compiled_message_placements_fingerprint: str
    ordered_eligible_units: tuple[FrozenRootConversationContextUnitFact, ...]
    eligible_context_units_fingerprint: str
    subject_fingerprint: str

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (
                self.session_id,
                self.caller_turn_id,
                self.provider_input_cut_fingerprint,
                self.continuity_epoch_nonce,
                self.compiled_semantic_input_fingerprint,
                self.compiled_message_placements_fingerprint,
            )
        ):
            raise ValueError("parent context call subject identity is incomplete")
        if self.continuity_epoch_revision < 0:
            raise ValueError("continuity epoch revision must be non-negative")
        if len(self.ordered_eligible_units) > MAXIMUM_PARENT_CONTEXT_UNITS:
            raise ValueError("parent context subject exceeds its unit bound")
        units = tuple(item.unit_fingerprint for item in self.ordered_eligible_units)
        expected_units = _fingerprint("parent-context-units", units)
        if self.eligible_context_units_fingerprint != expected_units:
            raise ValueError("eligible parent context fingerprint mismatch")
        expected = _fingerprint(
            "parent-context-call-subject",
            {
                "session_id": self.session_id,
                "caller_turn_id": self.caller_turn_id,
                "cut": self.provider_input_cut_fingerprint,
                "epoch_nonce": self.continuity_epoch_nonce,
                "epoch_revision": self.continuity_epoch_revision,
                "semantic": self.compiled_semantic_input_fingerprint,
                "placements": self.compiled_message_placements_fingerprint,
                "units": expected_units,
            },
        )
        if self.subject_fingerprint != expected:
            raise ValueError("parent context call subject fingerprint mismatch")


def build_parent_context_call_subject(
    *,
    session_id: str,
    caller_turn_id: str,
    provider_input_cut_fingerprint: str,
    continuity_epoch_nonce: str,
    continuity_epoch_revision: int,
    compiled_semantic_input_fingerprint: str,
    compiled_message_placements_fingerprint: str,
    ordered_eligible_units: tuple[FrozenRootConversationContextUnitFact, ...],
) -> FrozenSubagentParentContextCallSubject:
    units = _fingerprint(
        "parent-context-units",
        tuple(item.unit_fingerprint for item in ordered_eligible_units),
    )
    payload = {
        "session_id": session_id,
        "caller_turn_id": caller_turn_id,
        "cut": provider_input_cut_fingerprint,
        "epoch_nonce": continuity_epoch_nonce,
        "epoch_revision": continuity_epoch_revision,
        "semantic": compiled_semantic_input_fingerprint,
        "placements": compiled_message_placements_fingerprint,
        "units": units,
    }
    return FrozenSubagentParentContextCallSubject(
        session_id,
        caller_turn_id,
        provider_input_cut_fingerprint,
        continuity_epoch_nonce,
        continuity_epoch_revision,
        compiled_semantic_input_fingerprint,
        compiled_message_placements_fingerprint,
        ordered_eligible_units,
        units,
        _fingerprint("parent-context-call-subject", payload),
    )


@dataclass(frozen=True, slots=True)
class FrozenSubagentParentContextSelection:
    mode: SubagentContextMode
    last_n_turns: int | None
    selected_units: tuple[FrozenRootConversationContextUnitFact, ...]
    rendered_body: str | None
    source_fingerprint: str | None
    selection_fingerprint: str

    def __post_init__(self) -> None:
        if self.mode is SubagentContextMode.NONE:
            if any(
                value is not None
                for value in (
                    self.last_n_turns,
                    self.rendered_body,
                    self.source_fingerprint,
                )
            ) or self.selected_units:
                raise ValueError("NONE parent context must be absent")
        elif self.mode is SubagentContextMode.LAST_N:
            if self.last_n_turns is None or not 1 <= self.last_n_turns <= 3:
                raise ValueError("LAST_N parent context requires 1..3 turns")
            if self.selected_units and (
                self.rendered_body is None or self.source_fingerprint is None
            ):
                raise ValueError("non-empty LAST_N selection requires a source")
            if not self.selected_units and (
                self.rendered_body is not None or self.source_fingerprint is not None
            ):
                raise ValueError("empty LAST_N selection must remain absent")
        else:
            raise TypeError("unknown parent context mode")


def _render_parent_units(
    units: tuple[FrozenRootConversationContextUnitFact, ...],
) -> str:
    return canonical_json_bytes(
        {
            "pulsara_parent_context": {
                "trust": "UNTRUSTED_OBSERVATION",
                "units": [
                    {
                        "ordinal": ordinal,
                        "items": list(unit.ordered_public_items),
                    }
                    for ordinal, unit in enumerate(units)
                ],
            }
        }
    ).decode("utf-8")


def build_parent_context_selection(
    subject: FrozenSubagentParentContextCallSubject,
    *,
    mode: SubagentContextMode,
    last_n_turns: int | None,
) -> FrozenSubagentParentContextSelection:
    if mode is SubagentContextMode.NONE:
        selected: tuple[FrozenRootConversationContextUnitFact, ...] = ()
        body = None
        source = None
    else:
        if last_n_turns is None or not 1 <= last_n_turns <= 3:
            raise ValueError("LAST_N parent context requires 1..3 turns")
        selected = subject.ordered_eligible_units[-last_n_turns:]
        body = _render_parent_units(selected) if selected else None
        source = (
            _fingerprint(
                "parent-context-source",
                {
                    "subject": subject.subject_fingerprint,
                    "units": tuple(item.unit_fingerprint for item in selected),
                    "body": body,
                },
            )
            if body is not None
            else None
        )
    selection = _fingerprint(
        "parent-context-selection",
        {
            "subject": subject.subject_fingerprint,
            "mode": mode.value,
            "last_n": last_n_turns,
            "units": tuple(item.unit_fingerprint for item in selected),
            "source": source,
        },
    )
    return FrozenSubagentParentContextSelection(
        mode, last_n_turns, selected, body, source, selection
    )


@dataclass(frozen=True, slots=True)
class PreparedSubagentTaskDraft:
    task_id: str
    task_key: str | None
    label: str | None
    profile: SubagentProfileKind
    display_role: str | None
    objective: str
    context: FrozenSubagentParentContextSelection
    dependency_task_ids: tuple[str, ...]
    initial_status: SubagentTaskStatus
    pending_reason: str | None
    terminal_reason: str | None
    draft_fingerprint: str

    def __post_init__(self) -> None:
        _text(self.task_id, "task_id", 512)
        _text(self.objective, "objective", MAXIMUM_TASK_OBJECTIVE_UTF8_BYTES)
        if not isinstance(self.profile, SubagentProfileKind):
            raise TypeError("subagent task profile must be closed")
        if not isinstance(self.context, FrozenSubagentParentContextSelection):
            raise TypeError("subagent task context selection must be frozen")
        if not isinstance(self.initial_status, SubagentTaskStatus):
            raise TypeError("subagent task status must be closed")
        if self.task_key is not None:
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.task_key) is None:
                raise ValueError("task_key is invalid")
        for field, value in (("label", self.label), ("display_role", self.display_role)):
            if value is not None:
                _text(value, field, 256)
        if len(self.dependency_task_ids) > 16 or len(set(self.dependency_task_ids)) != len(
            self.dependency_task_ids
        ):
            raise ValueError("dependency set is invalid")
        if any(not isinstance(item, str) or not item for item in self.dependency_task_ids):
            raise ValueError("dependency identity is invalid")
        if self.task_id in self.dependency_task_ids:
            raise ValueError("task cannot depend on itself")
        if self.initial_status.terminal != (self.terminal_reason is not None):
            raise ValueError("task terminal reason does not match initial status")
        if (self.initial_status in {
            SubagentTaskStatus.PENDING_START,
            SubagentTaskStatus.WAITING_DEPENDENCY,
        }) != (self.pending_reason is not None):
            raise ValueError("task pending reason does not match initial status")
        valid_initial = {
            SubagentTaskStatus.PENDING_START: ("CAPACITY", None),
            SubagentTaskStatus.WAITING_DEPENDENCY: ("DEPENDENCY", None),
            SubagentTaskStatus.BLOCKED_DEPENDENCY_FAILED: (
                None,
                "DEPENDENCY_FAILED",
            ),
        }
        if valid_initial.get(self.initial_status) != (
            self.pending_reason,
            self.terminal_reason,
        ):
            raise ValueError("subagent task initial disposition is invalid")
        expected = _fingerprint(
            "task-draft",
            {
                "task_id": self.task_id,
                "task_key": self.task_key,
                "label": self.label,
                "profile": self.profile.value,
                "display_role": self.display_role,
                "objective": self.objective,
                "context": self.context.selection_fingerprint,
                "dependencies": self.dependency_task_ids,
                "initial_status": self.initial_status.value,
                "pending_reason": self.pending_reason,
                "terminal_reason": self.terminal_reason,
            },
        )
        if self.draft_fingerprint != expected:
            raise ValueError("subagent task draft fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PreparedSubagentTaskBatchAdmission:
    session_id: str
    workspace_id: str
    writer_generation: int
    parent_turn_id: str
    source_tool_attempt_id: str
    permission_snapshot_fingerprint: str
    parent_call_subject: FrozenSubagentParentContextCallSubject
    batch_id: str
    ordered_tasks: tuple[PreparedSubagentTaskDraft, ...]
    occurred_at: datetime
    actor_id: str
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.ordered_tasks) <= 16:
            raise ValueError("subagent task batch must contain 1..16 tasks")
        if sum(len(item.dependency_task_ids) for item in self.ordered_tasks) > 64:
            raise ValueError("subagent task batch exceeds edge bound")
        if self.parent_call_subject.session_id != self.session_id:
            raise ValueError("parent call subject belongs to another session")
        if self.parent_call_subject.caller_turn_id != self.parent_turn_id:
            raise ValueError("parent call subject belongs to another turn")
        if self.writer_generation < 1:
            raise ValueError("subagent batch writer generation is invalid")
        if self.occurred_at.tzinfo is None:
            raise ValueError("subagent batch occurrence time must be timezone-aware")
        _text(self.batch_id, "batch_id", 512)
        _text(self.source_tool_attempt_id, "source_tool_attempt_id", 512)
        _text(
            self.permission_snapshot_fingerprint,
            "permission_snapshot_fingerprint",
            512,
        )
        _text(self.actor_id, "actor_id", 512)
        expected_batch_id = _stable_id(
            "subagent-batch", self.source_tool_attempt_id, "batch"
        )
        if self.batch_id != expected_batch_id:
            raise ValueError("subagent batch stable identity mismatch")
        task_ids = tuple(item.task_id for item in self.ordered_tasks)
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("subagent batch task identities are duplicated")
        if task_ids != tuple(
            _stable_id("subagent-task", self.source_tool_attempt_id, str(ordinal))
            for ordinal in range(len(self.ordered_tasks))
        ):
            raise ValueError("subagent task stable identities mismatch")
        keys = tuple(
            item.task_key for item in self.ordered_tasks if item.task_key is not None
        )
        if len(set(keys)) != len(keys):
            raise ValueError("subagent batch task keys are duplicated")
        if any(
            item.context
            != build_parent_context_selection(
                self.parent_call_subject,
                mode=item.context.mode,
                last_n_turns=item.context.last_n_turns,
            )
            for item in self.ordered_tasks
        ):
            raise ValueError("subagent task context is not derived from the exact call")
        expected = _fingerprint(
            "task-batch",
            {
                "session": self.session_id,
                "workspace": self.workspace_id,
                "writer_generation": self.writer_generation,
                "turn": self.parent_turn_id,
                "attempt": self.source_tool_attempt_id,
                "permission": self.permission_snapshot_fingerprint,
                "subject": self.parent_call_subject.subject_fingerprint,
                "batch": self.batch_id,
                "tasks": tuple(item.draft_fingerprint for item in self.ordered_tasks),
                "occurred_at": self.occurred_at.isoformat(),
                "actor": self.actor_id,
            },
        )
        if self.candidate_fingerprint != expected:
            raise ValueError("subagent batch candidate fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenSubagentResultPublicFact:
    task_id: str
    result_id: str
    source: SubagentResultSource
    producer_entry_id: str
    summary: str
    output_preview: str | None
    diagnostics: FrozenJsonValue
    source_assistant_content_digest: str | None
    result_fingerprint: str

    def __post_init__(self) -> None:
        _text(self.task_id, "task_id", 512)
        _text(self.result_id, "result_id", 512)
        _text(self.producer_entry_id, "producer_entry_id", 512)
        if not isinstance(self.source, SubagentResultSource):
            raise TypeError("subagent result source must be closed")
        _text(self.summary, "summary", MAXIMUM_RESULT_SUMMARY_UTF8_BYTES)
        if self.output_preview is not None:
            _text(
                self.output_preview,
                "output_preview",
                MAXIMUM_RESULT_OUTPUT_PREVIEW_UTF8_BYTES,
            )
        if self.source is SubagentResultSource.EXPLICIT:
            if self.source_assistant_content_digest is not None:
                raise ValueError("explicit result cannot cite assistant content")
        elif not self.source_assistant_content_digest:
            raise ValueError("inferred result requires assistant content digest")
        if (
            self.source_assistant_content_digest is not None
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}", self.source_assistant_content_digest
            )
            is None
        ):
            raise ValueError("subagent result assistant digest is invalid")
        if not isinstance(self.diagnostics, FrozenJsonArrayFact):
            raise TypeError("subagent result diagnostics must be a frozen array")
        if len(self.diagnostics.items) > MAXIMUM_RESULT_DIAGNOSTICS_ITEMS:
            raise ValueError("subagent result diagnostics exceed their item bound")
        if any(
            not isinstance(item, FrozenJsonObjectFact)
            for item in self.diagnostics.items
        ):
            raise TypeError("subagent result diagnostics items must be frozen objects")
        if any(
            len(canonical_json_bytes(item)) > 8_192
            for item in self.diagnostics.items
        ):
            raise ValueError("subagent result diagnostic item exceeds its bound")
        if (
            len(canonical_json_bytes(self.diagnostics))
            > MAXIMUM_RESULT_DIAGNOSTICS_UTF8_BYTES
        ):
            raise ValueError("subagent result diagnostics exceed their bound")
        expected = _fingerprint(
            "result-public-fact",
            {
                "task_id": self.task_id,
                "result_id": self.result_id,
                "source": self.source.value,
                "producer_entry_id": self.producer_entry_id,
                "summary": self.summary,
                "output_preview": self.output_preview,
                "diagnostics": self.diagnostics,
                "source_assistant_content_digest": self.source_assistant_content_digest,
            },
        )
        if self.result_fingerprint != expected:
            raise ValueError("subagent result fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PreparedExplicitSubagentResultSettlement:
    """One immutable explicit result and its canonical ToolResult acknowledgement."""

    task_id: str
    tool_result: PreparedToolResultAcceptanceFact
    result: FrozenSubagentResultPublicFact
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.task_id
            or self.result.task_id != self.task_id
            or self.result.source is not SubagentResultSource.EXPLICIT
            or self.result.producer_entry_id != self.tool_result.result_entry_id
            or self.result.result_id == self.tool_result.result_id
            or self.tool_result.attempt_id is None
            or self.tool_result.result_state != "SUCCESS"
        ):
            raise ValueError("explicit subagent result composite is invalid")
        expected = _fingerprint(
            "explicit-result-settlement",
            {
                "task_id": self.task_id,
                "tool_result": self.tool_result.candidate_fingerprint,
                "result": self.result.result_fingerprint,
            },
        )
        if self.candidate_fingerprint != expected:
            raise ValueError("explicit subagent result fingerprint mismatch")


def build_explicit_subagent_result_settlement(
    *,
    task_id: str,
    tool_result: PreparedToolResultAcceptanceFact,
    result: FrozenSubagentResultPublicFact,
) -> PreparedExplicitSubagentResultSettlement:
    return PreparedExplicitSubagentResultSettlement(
        task_id=task_id,
        tool_result=tool_result,
        result=result,
        candidate_fingerprint=_fingerprint(
            "explicit-result-settlement",
            {
                "task_id": task_id,
                "tool_result": tool_result.candidate_fingerprint,
                "result": result.result_fingerprint,
            },
        ),
    )


def build_subagent_result_public_fact(
    *,
    task_id: str,
    result_id: str,
    source: SubagentResultSource,
    producer_entry_id: str,
    summary: str,
    output_preview: str | None = None,
    diagnostics: object = (),
    source_assistant_content_digest: str | None = None,
) -> FrozenSubagentResultPublicFact:
    frozen = freeze_json(diagnostics)
    payload = {
        "task_id": task_id,
        "result_id": result_id,
        "source": source.value,
        "producer_entry_id": producer_entry_id,
        "summary": summary,
        "output_preview": output_preview,
        "diagnostics": frozen,
        "source_assistant_content_digest": source_assistant_content_digest,
    }
    return FrozenSubagentResultPublicFact(
        task_id,
        result_id,
        source,
        producer_entry_id,
        summary,
        output_preview,
        frozen,
        source_assistant_content_digest,
        _fingerprint("result-public-fact", payload),
    )


@dataclass(frozen=True, slots=True)
class FrozenDependencyResultContextItem:
    dependency_ordinal: int
    dependency_task_id: str
    task_key: str | None
    label: str | None
    result_id: str
    result_source: SubagentResultSource
    summary: str
    result_fingerprint: str
    item_fingerprint: str

    def __post_init__(self) -> None:
        if self.dependency_ordinal < 0:
            raise ValueError("dependency result ordinal is invalid")
        _text(self.dependency_task_id, "dependency_task_id", 512)
        _text(self.result_id, "result_id", 512)
        _text(self.summary, "summary", MAXIMUM_RESULT_SUMMARY_UTF8_BYTES)
        if not self.result_fingerprint.startswith("sha256:"):
            raise ValueError("dependency result fingerprint is invalid")
        expected = _fingerprint(
            "dependency-result-item",
            {
                "ordinal": self.dependency_ordinal,
                "task_id": self.dependency_task_id,
                "task_key": self.task_key,
                "label": self.label,
                "result_id": self.result_id,
                "source": self.result_source.value,
                "summary": self.summary,
                "result_fingerprint": self.result_fingerprint,
            },
        )
        if self.item_fingerprint != expected:
            raise ValueError("dependency result item fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenDependencyResultContext:
    target_task_id: str
    ordered_items: tuple[FrozenDependencyResultContextItem, ...]
    rendered_body: str
    context_fingerprint: str

    def __post_init__(self) -> None:
        if not self.ordered_items or len(self.ordered_items) > 16:
            raise ValueError("dependency result context requires 1..16 items")
        if tuple(item.dependency_ordinal for item in self.ordered_items) != tuple(
            range(len(self.ordered_items))
        ):
            raise ValueError("dependency result ordinals are not contiguous")
        expected_body = _render_dependency_results(self.ordered_items)
        expected = _fingerprint(
            "dependency-result-context",
            {
                "target_task_id": self.target_task_id,
                "items": tuple(item.item_fingerprint for item in self.ordered_items),
                "body": expected_body,
            },
        )
        if self.rendered_body != expected_body or self.context_fingerprint != expected:
            raise ValueError("dependency result context fingerprint mismatch")


def _render_dependency_results(
    items: tuple[FrozenDependencyResultContextItem, ...],
) -> str:
    return canonical_json_bytes(
        {
            "pulsara_dependency_results": {
                "trust": "UNTRUSTED_OBSERVATION",
                "results": [
                    {
                        "dependency_ordinal": item.dependency_ordinal,
                        "task_id": item.dependency_task_id,
                        "task_key": item.task_key,
                        "label": item.label,
                        "result_id": item.result_id,
                        "result_source": item.result_source.value,
                        "summary": item.summary,
                    }
                    for item in items
                ],
            }
        }
    ).decode("utf-8")


def build_dependency_result_context(
    *,
    target_task_id: str,
    rows: Iterable[Mapping[str, object]],
) -> FrozenDependencyResultContext | None:
    ordered_rows = tuple(rows)
    if not ordered_rows:
        return None
    items: list[FrozenDependencyResultContextItem] = []
    for expected_ordinal, row in enumerate(ordered_rows):
        ordinal = int(row["dependency_ordinal"])
        if ordinal != expected_ordinal or str(row["status"]) != "COMPLETED":
            raise ValueError("dependency result set is not ready in exact order")
        source = SubagentResultSource(str(row["result_source"]))
        payload = {
            "ordinal": ordinal,
            "task_id": str(row["dependency_task_id"]),
            "task_key": row.get("task_key"),
            "label": row.get("label"),
            "result_id": str(row["result_id"]),
            "source": source.value,
            "summary": str(row["summary"]),
            "result_fingerprint": str(row["result_fingerprint"]),
        }
        items.append(
            FrozenDependencyResultContextItem(
                dependency_ordinal=ordinal,
                dependency_task_id=payload["task_id"],
                task_key=payload["task_key"],
                label=payload["label"],
                result_id=payload["result_id"],
                result_source=source,
                summary=payload["summary"],
                result_fingerprint=payload["result_fingerprint"],
                item_fingerprint=_fingerprint("dependency-result-item", payload),
            )
        )
    frozen_items = tuple(items)
    body = _render_dependency_results(frozen_items)
    payload = {
        "target_task_id": target_task_id,
        "items": tuple(item.item_fingerprint for item in frozen_items),
        "body": body,
    }
    return FrozenDependencyResultContext(
        target_task_id,
        frozen_items,
        body,
        _fingerprint("dependency-result-context", payload),
    )


@dataclass(frozen=True, slots=True)
class PreparedInterAgentMailboxItem:
    session_id: str
    sender_turn_id: str
    sender_tool_attempt_id: str
    sender_tool_call_id: str
    recipient_task_id: str
    recipient_turn_id: str
    ordinal: int
    message: str
    message_digest: str
    entry_id: str
    event_id: str
    item_fingerprint: str

    def __post_init__(self) -> None:
        _text(self.message, "message", MAXIMUM_INTER_AGENT_MESSAGE_UTF8_BYTES)
        if self.ordinal < 0:
            raise ValueError("mailbox ordinal must be non-negative")
        for field, value in (
            ("session_id", self.session_id),
            ("sender_turn_id", self.sender_turn_id),
            ("sender_tool_attempt_id", self.sender_tool_attempt_id),
            ("sender_tool_call_id", self.sender_tool_call_id),
            ("recipient_task_id", self.recipient_task_id),
            ("recipient_turn_id", self.recipient_turn_id),
        ):
            _text(value, field, 512)
        digest = "sha256:" + sha256(self.message.encode("utf-8")).hexdigest()
        entry_id = _stable_id(
            "inter-agent-entry", self.sender_tool_attempt_id, self.recipient_task_id
        )
        event_id = _stable_id(
            "inter-agent-event", self.sender_tool_attempt_id, self.recipient_task_id
        )
        expected = _fingerprint(
            "inter-agent-item",
            {
                "session": self.session_id,
                "sender_turn": self.sender_turn_id,
                "attempt": self.sender_tool_attempt_id,
                "call": self.sender_tool_call_id,
                "recipient": self.recipient_task_id,
                "recipient_turn": self.recipient_turn_id,
                "ordinal": self.ordinal,
                "digest": digest,
                "entry": entry_id,
                "event": event_id,
            },
        )
        if (
            self.message_digest != digest
            or self.entry_id != entry_id
            or self.event_id != event_id
            or self.item_fingerprint != expected
        ):
            raise ValueError("inter-agent mailbox item identity mismatch")


@dataclass(frozen=True, slots=True)
class PreparedInterAgentMailboxBatch:
    items: tuple[PreparedInterAgentMailboxItem, ...]
    actor_id: str
    occurred_at: datetime
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.items) <= 16:
            raise ValueError("inter-agent mailbox batch must contain 1..16 items")
        _text(self.actor_id, "actor_id", 512)
        if self.occurred_at.tzinfo is None:
            raise ValueError("mailbox occurrence time must be timezone-aware")
        first = self.items[0]
        if any(
            item.session_id != first.session_id
            or item.recipient_task_id != first.recipient_task_id
            or item.recipient_turn_id != first.recipient_turn_id
            for item in self.items
        ):
            raise ValueError("inter-agent mailbox batch identity is mixed")
        if tuple(item.ordinal for item in self.items) != tuple(
            range(first.ordinal, first.ordinal + len(self.items))
        ):
            raise ValueError("inter-agent mailbox batch ordinals are not contiguous")
        if sum(len(item.message.encode("utf-8")) for item in self.items) > 65_536:
            raise ValueError("inter-agent mailbox batch exceeds its UTF-8 bound")
        expected = _fingerprint(
            "inter-agent-mailbox-batch",
            {
                "items": tuple(item.item_fingerprint for item in self.items),
                "actor": self.actor_id,
                "occurred_at": self.occurred_at.isoformat(),
            },
        )
        if self.candidate_fingerprint != expected:
            raise ValueError("inter-agent mailbox batch fingerprint mismatch")


def build_inter_agent_mailbox_batch(
    *,
    items: tuple[PreparedInterAgentMailboxItem, ...],
    actor_id: str,
    occurred_at: datetime,
) -> PreparedInterAgentMailboxBatch:
    return PreparedInterAgentMailboxBatch(
        items=items,
        actor_id=actor_id,
        occurred_at=occurred_at,
        candidate_fingerprint=_fingerprint(
            "inter-agent-mailbox-batch",
            {
                "items": tuple(item.item_fingerprint for item in items),
                "actor": actor_id,
                "occurred_at": occurred_at.isoformat(),
            },
        ),
    )


__all__ = [name for name in globals() if name.startswith(("Frozen", "Prepared", "Subagent", "build_", "MAXIMUM_"))]
