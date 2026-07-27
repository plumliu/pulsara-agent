"""Closed process-local command boundary for subagent orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import StrEnum
from collections.abc import Mapping
from typing import Literal, Protocol, TypeAlias

from pulsara_agent.event import EventContext
from pulsara_agent.ports.tool_execution import (
    ToolInvocationOwnerKind,
    ToolPermissionInvocation,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.subagent import (
    SubagentCommandKind,
    SubagentContextMode,
    SubagentEdgeKind,
    SubagentResultSource,
    SubagentRole,
    SubagentStatus,
    SubagentTaskProfileName,
    SubagentTaskStatus,
)


@dataclass(frozen=True, slots=True)
class SubagentCommandOwner:
    runtime_session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    event_context: EventContext
    parent_context_id: str | None
    parent_model_call_index: int | None
    invocation_owner_kind: ToolInvocationOwnerKind
    permission: ToolPermissionInvocation
    bound_child_subagent_run_id: str | None
    owner_fingerprint: str

    def __post_init__(self) -> None:
        if self.event_context.run_id != self.run_id:
            raise ValueError("subagent command owner run identity mismatch")
        _validate_fingerprint(
            self,
            "owner_fingerprint",
            "subagent-command-owner:v1",
        )


@dataclass(frozen=True, slots=True)
class SpawnAgentCommand:
    command_kind: Literal["spawn_agent"]
    owner: SubagentCommandOwner
    task: str
    label: str | None
    role: SubagentRole
    context_mode: SubagentContextMode
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class WaitAgentCommand:
    command_kind: Literal["wait_agent"]
    owner: SubagentCommandOwner
    subagent_run_id: str
    timeout_seconds: float | None
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class StopAgentCommand:
    command_kind: Literal["stop_agent"]
    owner: SubagentCommandOwner
    subagent_run_id: str
    reason: str | None
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class ListAgentsCommand:
    command_kind: Literal["list_agents"]
    owner: SubagentCommandOwner
    maximum_items: int
    include_edges: bool
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class CreateAgentTaskSpec:
    task: str
    profile: SubagentTaskProfileName
    task_key: str | None
    label: str | None
    display_role: str | None
    depends_on: tuple[str, ...]
    spec_fingerprint: str


@dataclass(frozen=True, slots=True)
class CreateAgentTasksCommand:
    command_kind: Literal["create_agent_tasks"]
    owner: SubagentCommandOwner
    ordered_tasks: tuple[CreateAgentTaskSpec, ...]
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class WaitAgentTasksCommand:
    command_kind: Literal["wait_agent_tasks"]
    owner: SubagentCommandOwner
    task_ids: tuple[str, ...]
    settle: Literal["all", "first"]
    timeout_seconds: float | None
    include_consumed: bool
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class StopAgentTaskCommand:
    command_kind: Literal["stop_agent_task"]
    owner: SubagentCommandOwner
    task_id: str
    reason: str | None
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReportAgentPhaseCommand:
    command_kind: Literal["report_agent_phase"]
    owner: SubagentCommandOwner
    subagent_run_id: str
    phase: str
    message: str | None
    progress: FrozenJsonObjectFact | None
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReportAgentResultCommand:
    command_kind: Literal["report_agent_result"]
    owner: SubagentCommandOwner
    subagent_run_id: str
    summary: str
    output_preview: str | None
    diagnostics: tuple[FrozenJsonObjectFact, ...]
    command_fingerprint: str


SubagentToolCommand: TypeAlias = (
    SpawnAgentCommand
    | WaitAgentCommand
    | StopAgentCommand
    | ListAgentsCommand
    | CreateAgentTasksCommand
    | WaitAgentTasksCommand
    | StopAgentTaskCommand
    | ReportAgentPhaseCommand
    | ReportAgentResultCommand
)


@dataclass(frozen=True, slots=True)
class SubagentSpawnedOutcome:
    outcome_kind: Literal["spawned"]
    subagent_run_id: str
    child_runtime_session_id: str
    label: str | None
    role: SubagentRole
    context_mode: SubagentContextMode
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentCollectedResultView:
    subagent_run_id: str
    task_id: str | None
    status: Literal["completed", "failed", "cancelled"]
    result_id: str
    summary: str
    output_preview: str | None
    result_artifact_id: str | None
    artifact_ids: tuple[str, ...]
    result_source: Literal["explicit", "inferred"]
    diagnostics: tuple[FrozenJsonObjectFact, ...]
    view_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentWaitCompletedOutcome:
    outcome_kind: Literal["wait_completed"]
    result: SubagentCollectedResultView
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentRunTerminalWithoutResultOutcome:
    outcome_kind: Literal["terminal_without_result"]
    subagent_run_id: str
    task_id: str | None
    status: Literal["failed", "cancelled"]
    reason_code: str
    terminal_event_id: str
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentRunStoppedOutcome:
    outcome_kind: Literal["run_stopped"]
    subagent_run_id: str
    status: Literal["cancelled", "completed", "failed"]
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentTaskProjectionView:
    item_kind: Literal["task"]
    task_id: str
    subagent_run_id: str | None
    child_runtime_session_id: str | None
    status: SubagentTaskStatus
    pending_state: str | None
    label: str | None
    task_key: str | None
    profile_id: str
    display_role: str | None
    objective_preview: str
    depends_on: tuple[str, ...]
    has_child_run: bool
    run_index: int | None
    phase: str | None
    result_id: str | None
    result_artifact_id: str | None
    delivered: bool
    consumed_by_wait: bool
    item_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentRunProjectionView:
    item_kind: Literal["run"]
    subagent_run_id: str
    child_runtime_session_id: str
    status: SubagentStatus
    label: str | None
    role: SubagentRole
    phase: str | None
    result_id: str | None
    result_artifact_id: str | None
    delivered: bool
    consumed_by_wait: bool
    item_fingerprint: str


SubagentProjectionItemView: TypeAlias = (
    SubagentTaskProjectionView | SubagentRunProjectionView
)


@dataclass(frozen=True, slots=True)
class SubagentGraphEdgeView:
    edge_id: str
    edge_kind: SubagentEdgeKind
    subagent_run_id: str
    source_tool_call_id: str | None
    source_tool_name: str | None
    result_id: str | None
    returned_to_tool_call_id: str | None
    created_at: str
    edge_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentInventoryOutcome:
    outcome_kind: Literal["inventory"]
    parent_runtime_session_id: str
    items: tuple[SubagentProjectionItemView, ...]
    edges: tuple[SubagentGraphEdgeView, ...]
    total_items: int
    total_edges: int
    items_truncated: bool
    edges_truncated: bool
    diagnostics: tuple[FrozenJsonObjectFact, ...]
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentTaskStartedView:
    task_id: str
    task_key: str | None
    label: str | None
    profile: SubagentTaskProfileName
    status: SubagentTaskStatus
    subagent_run_id: str | None
    child_runtime_session_id: str | None
    view_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentTaskBatchAcceptedOutcome:
    outcome_kind: Literal["task_batch_accepted"]
    batch_id: str
    started_count: int
    tasks: tuple[SubagentTaskStartedView, ...]
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentTaskWaitResultView:
    task_id: str
    task_key: str | None
    status: Literal["completed", "failed", "cancelled", "blocked_dependency_failed"]
    subagent_run_id: str | None
    child_runtime_session_id: str | None
    result_id: str | None
    summary: str | None
    output_preview: str | None
    result_artifact_id: str | None
    artifact_ids: tuple[str, ...]
    result_source: SubagentResultSource
    consumed: bool
    view_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentTasksWaitedOutcome:
    outcome_kind: Literal["tasks_waited"]
    settle: Literal["all", "first"]
    results: tuple[SubagentTaskWaitResultView, ...]
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentTaskStoppedOutcome:
    outcome_kind: Literal["task_stopped"]
    task_id: str
    status: SubagentTaskStatus
    subagent_run_id: str | None
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentPhaseReportedOutcome:
    outcome_kind: Literal["phase_reported"]
    subagent_run_id: str
    phase: str
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentResultSubmittedOutcome:
    outcome_kind: Literal["result_submitted"]
    subagent_run_id: str
    result_id: str
    summary: str
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class SubagentToolNotReadyOutcome:
    outcome_kind: Literal["not_ready"]
    command_kind: Literal["wait_agent"]
    subagent_run_ids: tuple[str, ...]
    outcome_fingerprint: str


class SubagentToolRejectCode(StrEnum):
    MALFORMED_ARGUMENTS = "malformed_arguments"
    NOT_FOUND = "not_found"
    LIMIT_EXCEEDED = "limit_exceeded"
    INVALID_TRANSITION = "invalid_transition"
    BATCH_PREFLIGHT_FAILED = "batch_preflight_failed"
    BATCH_START_FAILED = "batch_start_failed"
    OWNER_MISMATCH = "owner_mismatch"
    CONTRACT_MISMATCH = "contract_mismatch"


@dataclass(frozen=True, slots=True)
class SubagentToolRejectedOutcome:
    outcome_kind: Literal["rejected"]
    command_kind: SubagentCommandKind
    reject_code: SubagentToolRejectCode
    sanitized_message: str
    batch_id: str | None
    failed_stage: Literal["preflight", "post_commit_start"] | None
    failed_task_keys: tuple[str, ...]
    diagnostics: tuple[FrozenJsonObjectFact, ...]
    outcome_fingerprint: str


SubagentToolOutcome: TypeAlias = (
    SubagentSpawnedOutcome
    | SubagentWaitCompletedOutcome
    | SubagentRunTerminalWithoutResultOutcome
    | SubagentRunStoppedOutcome
    | SubagentInventoryOutcome
    | SubagentTaskBatchAcceptedOutcome
    | SubagentTasksWaitedOutcome
    | SubagentTaskStoppedOutcome
    | SubagentPhaseReportedOutcome
    | SubagentResultSubmittedOutcome
    | SubagentToolNotReadyOutcome
    | SubagentToolRejectedOutcome
)


class SubagentControlPort(Protocol):
    async def execute(self, command: SubagentToolCommand) -> SubagentToolOutcome: ...


def build_subagent_command_owner(
    *,
    runtime_session_id: str,
    tool_call_id: str,
    tool_name: str,
    event_context: EventContext,
    parent_context_id: str | None,
    parent_model_call_index: int | None,
    invocation_owner_kind: ToolInvocationOwnerKind,
    permission: ToolPermissionInvocation,
    bound_child_subagent_run_id: str | None,
) -> SubagentCommandOwner:
    payload = {
        "runtime_session_id": runtime_session_id,
        "run_id": event_context.run_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "event_context": asdict(event_context),
        "parent_context_id": parent_context_id,
        "parent_model_call_index": parent_model_call_index,
        "invocation_owner_kind": invocation_owner_kind.value,
        "permission": asdict(permission),
        "bound_child_subagent_run_id": bound_child_subagent_run_id,
    }
    return SubagentCommandOwner(
        runtime_session_id=runtime_session_id,
        run_id=event_context.run_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        event_context=event_context,
        parent_context_id=parent_context_id,
        parent_model_call_index=parent_model_call_index,
        invocation_owner_kind=invocation_owner_kind,
        permission=permission,
        bound_child_subagent_run_id=bound_child_subagent_run_id,
        owner_fingerprint=context_fingerprint("subagent-command-owner:v1", payload),
    )


def build_subagent_tool_command(
    *,
    owner: SubagentCommandOwner,
    arguments: Mapping[str, object],
) -> SubagentToolCommand:
    """Parse one model call into the closed command union.

    Public JSON Schema is catalog-owned; this factory is the execution-boundary
    validator and therefore rejects unknown keys instead of silently ignoring
    them.
    """

    kind = owner.tool_name
    if kind == "spawn_agent":
        _require_keys(arguments, allowed={"task", "label", "role", "context"})
        payload = {
            "command_kind": kind,
            "owner": owner,
            "task": _required_str(arguments.get("task"), "task"),
            "label": _optional_str(arguments.get("label"), "label"),
            "role": _closed_value(
                arguments.get("role"),
                name="role",
                default="worker",
                allowed={"worker", "verifier", "synthesizer", "orchestrator"},
            ),
            "context_mode": _closed_value(
                arguments.get("context"),
                name="context",
                default="isolated",
                allowed={"isolated", "fork"},
            ),
        }
        return SpawnAgentCommand(
            **payload,
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    if kind == "wait_agent":
        _require_keys(arguments, allowed={"subagent_run_id", "timeout_seconds"})
        payload = {
            "command_kind": kind,
            "owner": owner,
            "subagent_run_id": _required_str(
                arguments.get("subagent_run_id"), "subagent_run_id"
            ),
            "timeout_seconds": _optional_positive_float(
                arguments.get("timeout_seconds"), "timeout_seconds"
            ),
        }
        return WaitAgentCommand(
            **payload,
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    if kind == "stop_agent":
        _require_keys(arguments, allowed={"subagent_run_id", "reason"})
        payload = {
            "command_kind": kind,
            "owner": owner,
            "subagent_run_id": _required_str(
                arguments.get("subagent_run_id"), "subagent_run_id"
            ),
            "reason": _optional_str(arguments.get("reason"), "reason"),
        }
        return StopAgentCommand(
            **payload,
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    if kind == "list_agents":
        _require_keys(arguments, allowed={"max_items", "include_edges"})
        payload = {
            "command_kind": kind,
            "owner": owner,
            "maximum_items": _bounded_int(
                arguments.get("max_items"),
                name="max_items",
                default=50,
                minimum=1,
                maximum=100,
            ),
            "include_edges": _optional_bool(
                arguments.get("include_edges"),
                name="include_edges",
                default=False,
            ),
        }
        return ListAgentsCommand(
            **payload,
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    if kind == "create_agent_tasks":
        _require_keys(arguments, allowed={"tasks"})
        raw_tasks = arguments.get("tasks")
        if not isinstance(raw_tasks, (tuple, list)) or not raw_tasks:
            raise ValueError("tasks must be a non-empty array")
        tasks = tuple(
            _build_task_spec(item, index=index) for index, item in enumerate(raw_tasks)
        )
        task_keys = tuple(item.task_key for item in tasks if item.task_key is not None)
        if len(set(task_keys)) != len(task_keys):
            raise ValueError("task_key values must be unique within a batch")
        payload = {
            "command_kind": kind,
            "owner": owner,
            "ordered_tasks": tasks,
        }
        return CreateAgentTasksCommand(
            **payload,
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    if kind == "wait_agent_tasks":
        _require_keys(
            arguments,
            allowed={"task_ids", "settle", "timeout_seconds", "include_consumed"},
        )
        raw_ids = arguments.get("task_ids")
        if not isinstance(raw_ids, (tuple, list)) or not raw_ids:
            raise ValueError("task_ids must be a non-empty array")
        task_ids = tuple(
            _required_str(item, f"task_ids[{index}]")
            for index, item in enumerate(raw_ids)
        )
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("task_ids must be unique")
        payload = {
            "command_kind": kind,
            "owner": owner,
            "task_ids": task_ids,
            "settle": _closed_value(
                arguments.get("settle"),
                name="settle",
                default="all",
                allowed={"all", "first"},
            ),
            "timeout_seconds": _optional_positive_float(
                arguments.get("timeout_seconds"), "timeout_seconds"
            ),
            "include_consumed": _optional_bool(
                arguments.get("include_consumed"),
                name="include_consumed",
                default=False,
            ),
        }
        return WaitAgentTasksCommand(
            **payload,
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    if kind == "stop_agent_task":
        _require_keys(arguments, allowed={"task_id", "reason"})
        payload = {
            "command_kind": kind,
            "owner": owner,
            "task_id": _required_str(arguments.get("task_id"), "task_id"),
            "reason": _optional_str(arguments.get("reason"), "reason"),
        }
        return StopAgentTaskCommand(
            **payload,
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    if kind == "report_agent_phase":
        _require_bound_child(owner)
        _require_keys(arguments, allowed={"phase", "message", "progress"})
        progress = _optional_frozen_object(arguments.get("progress"), "progress")
        payload = {
            "command_kind": kind,
            "owner": owner,
            "subagent_run_id": owner.bound_child_subagent_run_id,
            "phase": _required_str(arguments.get("phase"), "phase"),
            "message": _optional_str(arguments.get("message"), "message"),
            "progress": progress,
        }
        return ReportAgentPhaseCommand(
            **payload,  # type: ignore[arg-type]
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    if kind == "report_agent_result":
        _require_bound_child(owner)
        _require_keys(
            arguments,
            allowed={"summary", "output_preview", "diagnostics"},
        )
        diagnostics = _optional_frozen_objects(
            arguments.get("diagnostics"), "diagnostics"
        )
        payload = {
            "command_kind": kind,
            "owner": owner,
            "subagent_run_id": owner.bound_child_subagent_run_id,
            "summary": _required_str(arguments.get("summary"), "summary"),
            "output_preview": _optional_str(
                arguments.get("output_preview"), "output_preview"
            ),
            "diagnostics": diagnostics,
        }
        return ReportAgentResultCommand(
            **payload,  # type: ignore[arg-type]
            command_fingerprint=_command_fingerprint(kind, payload),
        )
    raise ValueError(f"unsupported subagent command: {kind}")


def process_local_subagent_fingerprint(namespace: str, **payload: object) -> str:
    return context_fingerprint(namespace, payload)


def _command_fingerprint(kind: str, payload: Mapping[str, object]) -> str:
    normalized = _normalize_command_fingerprint_value(dict(payload))
    return context_fingerprint(f"subagent-{kind}-command:v1", normalized)


def _normalize_command_fingerprint_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _normalize_command_fingerprint_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_command_fingerprint_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return tuple(_normalize_command_fingerprint_value(item) for item in value)
    return value


def _build_task_spec(value: object, *, index: int) -> CreateAgentTaskSpec:
    if not isinstance(value, Mapping):
        raise ValueError(f"tasks[{index}] must be an object")
    _require_keys(
        value,
        allowed={"task", "profile", "task_key", "label", "display_role", "depends_on"},
    )
    raw_dependencies = value.get("depends_on", ())
    if not isinstance(raw_dependencies, (tuple, list)):
        raise ValueError(f"tasks[{index}].depends_on must be an array")
    dependencies = tuple(
        _required_str(item, f"tasks[{index}].depends_on[{dep_index}]")
        for dep_index, item in enumerate(raw_dependencies)
    )
    if len(set(dependencies)) != len(dependencies):
        raise ValueError(f"tasks[{index}].depends_on must be unique")
    task_key = _optional_str(value.get("task_key"), f"tasks[{index}].task_key")
    if task_key is not None and task_key.startswith("task:"):
        raise ValueError("task_key must not use the reserved task: prefix")
    payload = {
        "task": _required_str(value.get("task"), f"tasks[{index}].task"),
        "profile": _closed_value(
            value.get("profile"),
            name=f"tasks[{index}].profile",
            default=None,
            allowed={
                "research_worker",
                "review_worker",
                "verification_worker",
                "general_worker",
            },
        ),
        "task_key": task_key,
        "label": _optional_str(value.get("label"), f"tasks[{index}].label"),
        "display_role": _optional_str(
            value.get("display_role"), f"tasks[{index}].display_role"
        ),
        "depends_on": dependencies,
    }
    return CreateAgentTaskSpec(
        **payload,  # type: ignore[arg-type]
        spec_fingerprint=context_fingerprint("subagent-task-spec:v1", payload),
    )


def _require_keys(value: Mapping[str, object], *, allowed: set[str]) -> None:
    extras = set(value).difference(allowed)
    if extras:
        raise ValueError("unexpected fields: " + ", ".join(sorted(extras)))


def _required_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip() or None


def _optional_positive_float(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    parsed = float(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _bounded_int(
    value: object,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _optional_bool(value: object, *, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _closed_value(
    value: object,
    *,
    name: str,
    default: str | None,
    allowed: set[str],
) -> str:
    resolved = default if value is None else value
    if not isinstance(resolved, str) or resolved not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return resolved


def _optional_frozen_object(value: object, name: str) -> FrozenJsonObjectFact | None:
    if value is None:
        return None
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise ValueError(f"{name} must be an object")
    return frozen


def _optional_frozen_objects(
    value: object, name: str
) -> tuple[FrozenJsonObjectFact, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} must be an array")
    result: list[FrozenJsonObjectFact] = []
    for index, item in enumerate(value):
        frozen = freeze_json(item)
        if not isinstance(frozen, FrozenJsonObjectFact):
            raise ValueError(f"{name}[{index}] must be an object")
        result.append(frozen)
    return tuple(result)


def _require_bound_child(owner: SubagentCommandOwner) -> None:
    if not owner.bound_child_subagent_run_id:
        raise ValueError("child reporting command lacks bound subagent run identity")


def _validate_fingerprint(value: object, field_name: str, namespace: str) -> None:
    payload = asdict(value)
    actual = payload.pop(field_name)
    if actual != context_fingerprint(namespace, payload):
        raise ValueError(f"{field_name} mismatch")


__all__ = [name for name in globals() if name.startswith("Subagent")]
__all__ += [
    "build_subagent_command_owner",
    "build_subagent_tool_command",
    "CreateAgentTaskSpec",
    "CreateAgentTasksCommand",
    "ListAgentsCommand",
    "ReportAgentPhaseCommand",
    "ReportAgentResultCommand",
    "SpawnAgentCommand",
    "StopAgentCommand",
    "StopAgentTaskCommand",
    "WaitAgentCommand",
    "WaitAgentTasksCommand",
    "process_local_subagent_fingerprint",
]
