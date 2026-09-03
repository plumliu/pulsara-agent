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
import json
import re
from typing import Iterable, Mapping, Protocol

from pulsara_agent.conversation_kernel.contracts import canonical_digest
from pulsara_agent.conversation_kernel.cancellation import stable_subagent_turn_id
from pulsara_agent.primitives.context import (
    FrozenJsonArrayFact,
    FrozenJsonObjectFact,
    FrozenJsonValue,
    canonical_json_bytes,
    freeze_json,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot


MAXIMUM_TASK_OBJECTIVE_UTF8_BYTES = 65_536
MAXIMUM_RESULT_SUMMARY_UTF8_BYTES = 16_384
MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES = 16_384
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


class SubagentTerminalReason(StrEnum):
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    DEPENDENCY_RESULT_INVARIANT = "DEPENDENCY_RESULT_INVARIANT"
    CHILD_START_FAILED = "CHILD_START_FAILED"
    CHILD_EXECUTION_FAILED = "CHILD_EXECUTION_FAILED"
    HOOK_COMPACTION_BLOCKED = "HOOK_COMPACTION_BLOCKED"
    HOST_CLOSING = "HOST_CLOSING"
    HOST_TAKEOVER = "HOST_TAKEOVER"
    USER_CANCELLED = "USER_CANCELLED"


class SubagentFailureRetryability(StrEnum):
    NEW_TASK_ONLY = "NEW_TASK_ONLY"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


SUBAGENT_COMPLETION_SCHEMA_VERSION = "pulsara.subagent-completion.v1"
SUBAGENT_COMPLETION_MEDIA_TYPE = "application/vnd.pulsara.subagent-completion+json"


_FAILURE_GUIDANCE: Mapping[
    SubagentTerminalReason, tuple[SubagentFailureRetryability, str]
] = {
    SubagentTerminalReason.DEPENDENCY_FAILED: (
        SubagentFailureRetryability.NEW_TASK_ONLY,
        "Inspect the failed dependency and create a new task if retrying is useful.",
    ),
    SubagentTerminalReason.DEPENDENCY_RESULT_INVARIANT: (
        SubagentFailureRetryability.USER_ACTION_REQUIRED,
        "Inspect the dependency result lineage before creating replacement work.",
    ),
    SubagentTerminalReason.CHILD_START_FAILED: (
        SubagentFailureRetryability.NEW_TASK_ONLY,
        "Address the launch failure and create a new task if the work is still needed.",
    ),
    SubagentTerminalReason.CHILD_EXECUTION_FAILED: (
        SubagentFailureRetryability.NEW_TASK_ONLY,
        "Use the failure detail to recover locally or create a replacement task.",
    ),
    SubagentTerminalReason.HOOK_COMPACTION_BLOCKED: (
        SubagentFailureRetryability.USER_ACTION_REQUIRED,
        "Review the blocking hook or continue the work in the main task.",
    ),
    SubagentTerminalReason.HOST_CLOSING: (
        SubagentFailureRetryability.NEW_TASK_ONLY,
        "Create a new task if this work is still required.",
    ),
    SubagentTerminalReason.HOST_TAKEOVER: (
        SubagentFailureRetryability.NEW_TASK_ONLY,
        "Create a new task under the current runtime if this work is still required.",
    ),
    SubagentTerminalReason.USER_CANCELLED: (
        SubagentFailureRetryability.NOT_APPLICABLE,
        "No retry is needed unless the user asks for the work again.",
    ),
}


_DEFAULT_TERMINAL_DETAILS: Mapping[SubagentTerminalReason, str] = {
    SubagentTerminalReason.DEPENDENCY_FAILED: (
        "An upstream delegated task did not complete, so this task was not started."
    ),
    SubagentTerminalReason.DEPENDENCY_RESULT_INVARIANT: (
        "A completed dependency did not have the required canonical result."
    ),
    SubagentTerminalReason.CHILD_START_FAILED: ("The delegated task could not start."),
    SubagentTerminalReason.CHILD_EXECUTION_FAILED: (
        "The delegated task failed while it was running."
    ),
    SubagentTerminalReason.HOOK_COMPACTION_BLOCKED: (
        "A configured hook prevented the delegated task from continuing after compaction."
    ),
    SubagentTerminalReason.HOST_CLOSING: (
        "The local runtime closed before the delegated task finished."
    ),
    SubagentTerminalReason.HOST_TAKEOVER: (
        "A newer local runtime took ownership before the delegated task finished."
    ),
    SubagentTerminalReason.USER_CANCELLED: (
        "The delegated task was cancelled by the user."
    ),
}


def build_default_terminal_public_detail(reason: str) -> str:
    return _DEFAULT_TERMINAL_DETAILS[SubagentTerminalReason(reason)]


def bounded_terminal_public_detail(value: str, *, secret: str | None = None) -> str:
    """Return useful bounded diagnostics while removing only the API key value."""

    if not isinstance(value, str):
        raise TypeError("terminal public detail must be text")
    detail = value.strip() or "The delegated task ended without additional detail."
    if secret:
        detail = detail.replace(secret, "[PULSARA_API_KEY]")
    raw = detail.encode("utf-8")
    if len(raw) <= MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES:
        return detail
    marker = "\n… detail truncated …".encode("utf-8")
    clipped = raw[: MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES - len(marker)]
    while clipped:
        try:
            return clipped.decode("utf-8") + marker.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return marker.decode("utf-8").lstrip()


def build_subagent_completion_storage_body(
    *,
    task_id: str,
    task_key: str | None,
    label: str | None,
    display_role: str | None,
    profile: str,
    status: SubagentTaskStatus,
    terminal_reason: str | None,
    terminal_public_detail: str | None,
    failed_dependency_task_ids: tuple[str, ...],
    result_id: str | None,
    result_source: str | None,
    result_summary: str | None,
) -> bytes:
    """Build the only durable ROOT completion body accepted by the reader."""

    _text(task_id, "task_id", 512)
    _text(profile, "profile", 256)
    if not status.terminal:
        raise ValueError("completion source task must be terminal")
    if len(failed_dependency_task_ids) > 16 or len(
        set(failed_dependency_task_ids)
    ) != len(failed_dependency_task_ids):
        raise ValueError("completion failed dependency set is invalid")
    if any(not item for item in failed_dependency_task_ids):
        raise ValueError("completion dependency identity is empty")
    if status is SubagentTaskStatus.COMPLETED:
        if terminal_reason is not None or terminal_public_detail is not None:
            raise ValueError("completed task cannot carry failure detail")
        _text(result_id or "", "result_id", 512)
        source = SubagentResultSource(result_source or "")
        summary = _text(
            result_summary or "", "result_summary", MAXIMUM_RESULT_SUMMARY_UTF8_BYTES
        )
        failure: object = None
        result: object = {
            "result_id": result_id,
            "source": source.value,
            "summary": summary,
        }
    else:
        reason = SubagentTerminalReason(terminal_reason or "")
        detail = _text(
            terminal_public_detail or "",
            "terminal_public_detail",
            MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES,
        )
        retryability, next_action = _FAILURE_GUIDANCE[reason]
        failure = {
            "code": reason.value,
            "detail": detail,
            "failed_dependency_task_ids": list(failed_dependency_task_ids),
            "retryability": retryability.value,
            "next_action": next_action,
        }
        result = None
        if any(
            value is not None for value in (result_id, result_source, result_summary)
        ):
            raise ValueError("failed task cannot carry a result")
    return canonical_json_bytes(
        {
            "schema_version": SUBAGENT_COMPLETION_SCHEMA_VERSION,
            "message_type": "FINAL_ANSWER",
            "task_id": task_id,
            "task_key": task_key,
            "label": label,
            "display_role": display_role,
            "profile": profile,
            "status": status.value,
            "failure": failure,
            "result": result,
        }
    )


def validate_subagent_completion_storage_body(value: bytes) -> Mapping[str, object]:
    """Validate and return a canonical completion object for provider projection."""

    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("subagent completion body is not UTF-8 JSON") from exc
    if not isinstance(decoded, dict) or set(decoded) != {
        "schema_version",
        "message_type",
        "task_id",
        "task_key",
        "label",
        "display_role",
        "profile",
        "status",
        "failure",
        "result",
    }:
        raise ValueError("subagent completion body is not closed")
    failure = decoded.get("failure")
    result = decoded.get("result")
    if failure is not None and (
        not isinstance(failure, dict)
        or set(failure)
        != {
            "code",
            "detail",
            "failed_dependency_task_ids",
            "retryability",
            "next_action",
        }
    ):
        raise ValueError("subagent completion failure is not closed")
    if result is not None and (
        not isinstance(result, dict)
        or set(result) != {"result_id", "source", "summary"}
    ):
        raise ValueError("subagent completion result is not closed")
    rebuilt = build_subagent_completion_storage_body(
        task_id=str(decoded.get("task_id") or ""),
        task_key=decoded.get("task_key"),
        label=decoded.get("label"),
        display_role=decoded.get("display_role"),
        profile=str(decoded.get("profile") or ""),
        status=SubagentTaskStatus(str(decoded.get("status") or "")),
        terminal_reason=(None if failure is None else str(failure.get("code") or "")),
        terminal_public_detail=(
            None if failure is None else str(failure.get("detail") or "")
        ),
        failed_dependency_task_ids=(
            ()
            if failure is None
            else tuple(failure.get("failed_dependency_task_ids") or ())
        ),
        result_id=(None if result is None else str(result.get("result_id") or "")),
        result_source=(None if result is None else str(result.get("source") or "")),
        result_summary=(None if result is None else str(result.get("summary") or "")),
    )
    if rebuilt != value:
        raise ValueError("subagent completion body is not canonical")
    return decoded


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
        if any(
            status.terminal and status is not SubagentTaskStatus.COMPLETED
            for status, _ in states
        ):
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
    public_detail: str
    require_absent_turn: bool
    occurred_at: datetime
    actor_id: str
    event_id: str

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
        _text(
            self.public_detail,
            "public_detail",
            MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES,
        )
        SubagentTerminalReason(self.reason)
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
            self.public_detail,
        )
        if self.event_id != expected_event:
            raise ValueError("subagent task terminal settlement identity mismatch")


def build_subagent_task_terminal_settlement(
    *,
    session_id: str,
    workspace_id: str,
    writer_generation: int,
    task_id: str,
    expected_turn_id: str,
    status: SubagentTaskStatus,
    reason: str,
    public_detail: str,
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
        public_detail,
    )
    return PreparedSubagentTaskTerminalSettlement(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_generation=writer_generation,
        task_id=task_id,
        expected_turn_id=expected_turn_id,
        status=status,
        reason=reason,
        public_detail=public_detail,
        require_absent_turn=require_absent_turn,
        occurred_at=occurred_at,
        actor_id=actor_id,
        event_id=event_id,
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
        if self.event_id != expected_event:
            raise ValueError("subagent task start identity mismatch")


@dataclass(frozen=True, slots=True)
class PreparedSubagentLaunch:
    """Call-local launch truth frozen after the task-start winner is FULL."""

    task_start: PreparedSubagentTaskStart = dataclass_field(repr=False)
    child_turn_id: str
    configured_model_identity: str
    parent_permission_snapshot: FrozenRunPermissionSnapshot = dataclass_field(
        repr=False
    )

    def __post_init__(self) -> None:
        _text(self.child_turn_id, "child_turn_id", 512)
        _text(self.configured_model_identity, "configured_model_identity", 512)
        if self.parent_permission_snapshot.inherited_from_turn_id is not None:
            raise ValueError("subagent launch parent permission fact is invalid")
        if self.child_turn_id != stable_subagent_turn_id(
            session_id=self.task_start.session_id,
            task_id=self.task_start.task_id,
        ):
            raise ValueError("subagent launch child turn identity drifted")


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
    )


@dataclass(frozen=True, slots=True)
class FrozenRootConversationContextUnitFact:
    ordered_entry_ids: tuple[str, ...]
    ordered_public_items: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.ordered_entry_ids or not self.ordered_public_items:
            raise ValueError("ROOT context unit must contain entries and public items")
        if any(not item for item in self.ordered_entry_ids + self.ordered_public_items):
            raise ValueError("ROOT context unit values must be non-empty")


def build_root_context_unit(
    *, ordered_entry_ids: Iterable[str], ordered_public_items: Iterable[str]
) -> FrozenRootConversationContextUnitFact:
    entries = tuple(ordered_entry_ids)
    items = tuple(ordered_public_items)
    return FrozenRootConversationContextUnitFact(entries, items)


def _root_context_unit_identity_digest(
    unit: FrozenRootConversationContextUnitFact,
) -> str:
    """Reproduce the historical semantic identity only where a stable ID needs it."""

    return _fingerprint(
        "parent-context-unit",
        {"entries": unit.ordered_entry_ids, "items": unit.ordered_public_items},
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


def parent_context_call_subject_identity_digest(
    subject: FrozenSubagentParentContextCallSubject,
) -> str:
    """Derive the pre-hard-cut subject identity without storing duplicate state."""

    units = _fingerprint(
        "parent-context-units",
        tuple(
            _root_context_unit_identity_digest(item)
            for item in subject.ordered_eligible_units
        ),
    )
    return _fingerprint(
        "parent-context-call-subject",
        {
            "session_id": subject.session_id,
            "caller_turn_id": subject.caller_turn_id,
            "cut": subject.provider_input_cut_fingerprint,
            "epoch_nonce": subject.continuity_epoch_nonce,
            "epoch_revision": subject.continuity_epoch_revision,
            "semantic": subject.compiled_semantic_input_fingerprint,
            "placements": subject.compiled_message_placements_fingerprint,
            "units": units,
        },
    )


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
    return FrozenSubagentParentContextCallSubject(
        session_id,
        caller_turn_id,
        provider_input_cut_fingerprint,
        continuity_epoch_nonce,
        continuity_epoch_revision,
        compiled_semantic_input_fingerprint,
        compiled_message_placements_fingerprint,
        ordered_eligible_units,
    )


@dataclass(frozen=True, slots=True)
class FrozenSubagentParentContextSelection:
    mode: SubagentContextMode
    last_n_turns: int | None
    selected_units: tuple[FrozenRootConversationContextUnitFact, ...]
    rendered_body: str | None

    def __post_init__(self) -> None:
        if self.mode is SubagentContextMode.NONE:
            if (
                any(
                    value is not None
                    for value in (
                        self.last_n_turns,
                        self.rendered_body,
                    )
                )
                or self.selected_units
            ):
                raise ValueError("NONE parent context must be absent")
        elif self.mode is SubagentContextMode.LAST_N:
            if self.last_n_turns is None or not 1 <= self.last_n_turns <= 3:
                raise ValueError("LAST_N parent context requires 1..3 turns")
            if self.selected_units and self.rendered_body is None:
                raise ValueError("non-empty LAST_N selection requires a source")
            if not self.selected_units and self.rendered_body is not None:
                raise ValueError("empty LAST_N selection must remain absent")
        else:
            raise TypeError("unknown parent context mode")


def _render_parent_units(
    units: tuple[FrozenRootConversationContextUnitFact, ...],
) -> str:
    return canonical_json_bytes(
        {
            "pulsara_parent_context": {
                "content_semantics": "ADVISORY_COLLABORATION_DATA",
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
    else:
        if last_n_turns is None or not 1 <= last_n_turns <= 3:
            raise ValueError("LAST_N parent context requires 1..3 turns")
        selected = subject.ordered_eligible_units[-last_n_turns:]
        body = _render_parent_units(selected) if selected else None
    return FrozenSubagentParentContextSelection(mode, last_n_turns, selected, body)


def parent_context_source_identity_digest(
    subject: FrozenSubagentParentContextCallSubject,
    selection: FrozenSubagentParentContextSelection,
) -> str | None:
    if selection.rendered_body is None:
        return None
    return _fingerprint(
        "parent-context-source",
        {
            "subject": parent_context_call_subject_identity_digest(subject),
            "units": tuple(
                _root_context_unit_identity_digest(item)
                for item in selection.selected_units
            ),
            "body": selection.rendered_body,
        },
    )


def parent_context_selection_identity_digest(
    subject: FrozenSubagentParentContextCallSubject,
    selection: FrozenSubagentParentContextSelection,
) -> str:
    """Local derivation retained solely to preserve existing stable identities."""

    source = parent_context_source_identity_digest(subject, selection)
    return _fingerprint(
        "parent-context-selection",
        {
            "subject": parent_context_call_subject_identity_digest(subject),
            "mode": selection.mode.value,
            "last_n": selection.last_n_turns,
            "units": tuple(
                _root_context_unit_identity_digest(item)
                for item in selection.selected_units
            ),
            "source": source,
        },
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
        for field, value in (
            ("label", self.label),
            ("display_role", self.display_role),
        ):
            if value is not None:
                _text(value, field, 256)
        if len(self.dependency_task_ids) > 16 or len(
            set(self.dependency_task_ids)
        ) != len(self.dependency_task_ids):
            raise ValueError("dependency set is invalid")
        if any(
            not isinstance(item, str) or not item for item in self.dependency_task_ids
        ):
            raise ValueError("dependency identity is invalid")
        if self.task_id in self.dependency_task_ids:
            raise ValueError("task cannot depend on itself")
        if self.initial_status.terminal != (self.terminal_reason is not None):
            raise ValueError("task terminal reason does not match initial status")
        if (
            self.initial_status
            in {
                SubagentTaskStatus.PENDING_START,
                SubagentTaskStatus.WAITING_DEPENDENCY,
            }
        ) != (self.pending_reason is not None):
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


def _subagent_task_draft_identity_digest(
    subject: FrozenSubagentParentContextCallSubject,
    draft: PreparedSubagentTaskDraft,
) -> str:
    return _fingerprint(
        "task-draft",
        {
            "task_id": draft.task_id,
            "task_key": draft.task_key,
            "label": draft.label,
            "profile": draft.profile.value,
            "display_role": draft.display_role,
            "objective": draft.objective,
            "context": parent_context_selection_identity_digest(subject, draft.context),
            "dependencies": draft.dependency_task_ids,
            "initial_status": draft.initial_status.value,
            "pending_reason": draft.pending_reason,
            "terminal_reason": draft.terminal_reason,
        },
    )


def subagent_task_batch_identity_digest(
    candidate: PreparedSubagentTaskBatchAdmission,
) -> str:
    """Preserve accepted event IDs without storing an aggregate candidate hash."""

    return _fingerprint(
        "task-batch",
        {
            "session": candidate.session_id,
            "workspace": candidate.workspace_id,
            "writer_generation": candidate.writer_generation,
            "turn": candidate.parent_turn_id,
            "attempt": candidate.source_tool_attempt_id,
            "permission": candidate.permission_snapshot_fingerprint,
            "subject": parent_context_call_subject_identity_digest(
                candidate.parent_call_subject
            ),
            "batch": candidate.batch_id,
            "tasks": tuple(
                _subagent_task_draft_identity_digest(
                    candidate.parent_call_subject, item
                )
                for item in candidate.ordered_tasks
            ),
            "occurred_at": candidate.occurred_at.isoformat(),
            "actor": candidate.actor_id,
        },
    )


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
            len(canonical_json_bytes(item)) > 8_192 for item in self.diagnostics.items
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

    def __post_init__(self) -> None:
        if self.dependency_ordinal < 0:
            raise ValueError("dependency result ordinal is invalid")
        _text(self.dependency_task_id, "dependency_task_id", 512)
        _text(self.result_id, "result_id", 512)
        _text(self.summary, "summary", MAXIMUM_RESULT_SUMMARY_UTF8_BYTES)
        if not self.result_fingerprint.startswith("sha256:"):
            raise ValueError("dependency result fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class FrozenDependencyResultContext:
    target_task_id: str
    ordered_items: tuple[FrozenDependencyResultContextItem, ...]
    rendered_body: str

    def __post_init__(self) -> None:
        if not self.ordered_items or len(self.ordered_items) > 16:
            raise ValueError("dependency result context requires 1..16 items")
        if tuple(item.dependency_ordinal for item in self.ordered_items) != tuple(
            range(len(self.ordered_items))
        ):
            raise ValueError("dependency result ordinals are not contiguous")
        expected_body = _render_dependency_results(self.ordered_items)
        if self.rendered_body != expected_body:
            raise ValueError("dependency result context body mismatch")


def dependency_result_context_identity_digest(
    context: FrozenDependencyResultContext,
) -> str:
    def item_digest(item: FrozenDependencyResultContextItem) -> str:
        return _fingerprint(
            "dependency-result-item",
            {
                "ordinal": item.dependency_ordinal,
                "task_id": item.dependency_task_id,
                "task_key": item.task_key,
                "label": item.label,
                "result_id": item.result_id,
                "source": item.result_source.value,
                "summary": item.summary,
                "result_fingerprint": item.result_fingerprint,
            },
        )

    return _fingerprint(
        "dependency-result-context",
        {
            "target_task_id": context.target_task_id,
            "items": tuple(item_digest(item) for item in context.ordered_items),
            "body": context.rendered_body,
        },
    )


def _render_dependency_results(
    items: tuple[FrozenDependencyResultContextItem, ...],
) -> str:
    return canonical_json_bytes(
        {
            "pulsara_dependency_results": {
                "content_semantics": "ADVISORY_COLLABORATION_DATA",
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
        items.append(
            FrozenDependencyResultContextItem(
                dependency_ordinal=ordinal,
                dependency_task_id=str(row["dependency_task_id"]),
                task_key=row.get("task_key"),
                label=row.get("label"),
                result_id=str(row["result_id"]),
                result_source=source,
                summary=str(row["summary"]),
                result_fingerprint=str(row["result_fingerprint"]),
            )
        )
    frozen_items = tuple(items)
    body = _render_dependency_results(frozen_items)
    return FrozenDependencyResultContext(target_task_id, frozen_items, body)


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
        if (
            self.message_digest != digest
            or self.entry_id != entry_id
            or self.event_id != event_id
        ):
            raise ValueError("inter-agent mailbox item identity mismatch")


@dataclass(frozen=True, slots=True)
class PreparedInterAgentMailboxBatch:
    items: tuple[PreparedInterAgentMailboxItem, ...]
    actor_id: str
    occurred_at: datetime

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
    )


__all__ = [
    name
    for name in globals()
    if name.startswith(("Frozen", "Prepared", "Subagent", "build_", "MAXIMUM_"))
]
