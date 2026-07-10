"""Typed DTOs for Pulsara-owned subagent runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

from pulsara_agent.runtime.subagent.immutable import (
    freeze_json_mapping,
    thaw_json_mapping,
)


SubagentStatus = Literal["running", "suspended", "completed", "failed", "cancelled"]
SubagentTaskStatus = Literal[
    "created",
    "waiting_dependency",
    "running",
    "blocked_dependency_failed",
    "completed",
    "failed",
    "cancelled",
]
SubagentRole = Literal["worker", "verifier", "synthesizer", "orchestrator"]
SubagentEdgeKind = Literal["spawn", "send", "followup", "wait", "cancel", "result", "suspend", "resume"]
SubagentResultSource = Literal["explicit", "inferred", "none"]
SubagentSpawnInitiatorKind = Literal["tool_call", "scheduler", "dependency_satisfied"]
SubagentCapabilityProfileName = Literal[
    "general_worker",
    "research_worker",
    "review_worker",
    "verification_worker",
    "synthesizer",
    "orchestrator",
]


@dataclass(frozen=True, slots=True)
class SubagentContextPolicy:
    mode: Literal["isolated", "fork"] = "isolated"
    include_parent_summary: bool = False
    include_parent_current_task: bool = True
    include_parent_memory_projection: bool = False
    include_parent_artifact_refs: bool = False
    max_parent_context_chars: int | None = None
    fork_source_context_id: str | None = None

    def to_event_value(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "include_parent_summary": self.include_parent_summary,
            "include_parent_current_task": self.include_parent_current_task,
            "include_parent_memory_projection": self.include_parent_memory_projection,
            "include_parent_artifact_refs": self.include_parent_artifact_refs,
            "max_parent_context_chars": self.max_parent_context_chars,
            "fork_source_context_id": self.fork_source_context_id,
        }


@dataclass(frozen=True, slots=True)
class SubagentBudget:
    max_concurrent_children_per_parent_run: int = 4
    max_concurrent_children_per_host_session: int = 8
    max_spawn_depth_from_root: int = 0
    child_timeout_seconds: float | None = None
    max_total_child_runs_per_parent_run: int = 16
    max_result_summary_chars_per_child: int = 4_000
    max_subagent_results_per_parent_compile: int = 8

    @classmethod
    def from_event_snapshot(cls, snapshot: object) -> SubagentBudget:
        """Hydrate a convenience value from one immutable event snapshot."""

        return cls(
            max_concurrent_children_per_parent_run=int(
                getattr(snapshot, "max_concurrent_children_per_parent_run")
            ),
            max_concurrent_children_per_host_session=int(
                getattr(snapshot, "max_concurrent_children_per_host_session")
            ),
            max_spawn_depth_from_root=int(
                getattr(snapshot, "max_spawn_depth_from_root")
            ),
            child_timeout_seconds=getattr(snapshot, "child_timeout_seconds"),
            max_total_child_runs_per_parent_run=int(
                getattr(snapshot, "max_total_child_runs_per_parent_run")
            ),
            max_result_summary_chars_per_child=int(
                getattr(snapshot, "max_result_summary_chars_per_child")
            ),
            max_subagent_results_per_parent_compile=int(
                getattr(snapshot, "max_subagent_results_per_parent_compile")
            ),
        )

    def to_event_value(self) -> dict[str, Any]:
        return {
            "max_concurrent_children_per_parent_run": self.max_concurrent_children_per_parent_run,
            "max_concurrent_children_per_host_session": self.max_concurrent_children_per_host_session,
            "max_spawn_depth_from_root": self.max_spawn_depth_from_root,
            "child_timeout_seconds": self.child_timeout_seconds,
            "max_total_child_runs_per_parent_run": self.max_total_child_runs_per_parent_run,
            "max_result_summary_chars_per_child": self.max_result_summary_chars_per_child,
            "max_subagent_results_per_parent_compile": self.max_subagent_results_per_parent_compile,
        }


@dataclass(frozen=True, slots=True)
class SubagentCapabilityProfile:
    profile_id: str
    profile_name: SubagentCapabilityProfileName = "general_worker"
    inherited_from_parent_context_id: str | None = None
    permission_mode: str | None = None
    permission_policy: Mapping[str, object] = field(default_factory=dict)
    allowed_tool_names: tuple[str, ...] = ()
    allowed_descriptor_ids: tuple[str, ...] = ()
    allowed_skill_names: tuple[str, ...] = ()
    allowed_mcp_server_ids: tuple[str, ...] = ()
    can_spawn_subagents: bool = False
    max_spawn_depth_from_root: int = 0
    memory_enabled: bool = False
    computed_from_parent_exposure_generation: int | None = None
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "permission_policy",
            freeze_json_mapping(self.permission_policy),
        )
        object.__setattr__(
            self,
            "diagnostics",
            tuple(freeze_json_mapping(item) for item in self.diagnostics),
        )

    def to_event_value(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "inherited_from_parent_context_id": self.inherited_from_parent_context_id,
            "permission_mode": self.permission_mode,
            "permission_policy": thaw_json_mapping(self.permission_policy),
            "allowed_tool_names": sorted(set(self.allowed_tool_names)),
            "allowed_descriptor_ids": sorted(set(self.allowed_descriptor_ids)),
            "allowed_skill_names": sorted(set(self.allowed_skill_names)),
            "allowed_mcp_server_ids": sorted(set(self.allowed_mcp_server_ids)),
            "can_spawn_subagents": self.can_spawn_subagents,
            "max_spawn_depth_from_root": self.max_spawn_depth_from_root,
            "memory_enabled": self.memory_enabled,
            "computed_from_parent_exposure_generation": self.computed_from_parent_exposure_generation,
            "diagnostics": [thaw_json_mapping(diagnostic) for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class SubagentEdge:
    edge_id: str
    edge_kind: SubagentEdgeKind
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None
    subagent_run_id: str
    child_runtime_session_id: str
    child_run_id: str | None = None
    source_context_id: str | None = None
    source_model_call_index: int | None = None
    source_tool_call_id: str | None = None
    source_tool_name: str | None = None
    target_context_id: str | None = None
    created_at: datetime | None = None
    payload_artifact_id: str | None = None
    result_id: str | None = None
    result_artifact_id: str | None = None
    returned_to_tool_call_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubagentResult:
    subagent_run_id: str
    result_id: str
    status: Literal["completed", "failed", "cancelled"]
    summary: str
    output_preview: str | None
    final_message_artifact_id: str | None
    artifact_ids: tuple[str, ...]
    diagnostics: tuple[Mapping[str, object], ...]
    token_usage: Mapping[str, object] | None
    tool_call_count: int | None
    completed_at: datetime
    task_id: str | None = None
    result_source: Literal["explicit", "inferred"] = "inferred"


@dataclass(frozen=True, slots=True)
class SubagentRunTerminalOutcome:
    subagent_run_id: str
    status: Literal["failed", "cancelled"]
    reason_code: str
    terminal_event_id: str
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentGraphNode:
    subagent_run_id: str
    child_runtime_session_id: str
    status: SubagentStatus
    label: str | None
    role: str
    phase: str | None = None
    result_id: str | None = None
    result_artifact_id: str | None = None
    delivered: bool = False
    consumed_by_wait: bool = False


@dataclass(frozen=True, slots=True)
class SubagentTaskProjection:
    task_id: str
    batch_id: str | None
    create_tool_call_id: str | None
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None
    task_key: str | None
    label: str | None
    profile_id: str
    display_role: str | None
    objective_preview: str
    status: SubagentTaskStatus
    depends_on: tuple[str, ...]
    current_run_id: str | None = None
    has_child_run: bool = False
    run_index: int | None = None
    phase: str | None = None
    result_id: str | None = None
    primary_result_artifact_id: str | None = None
    delivered: bool = False
    consumed_by_wait: bool = False
    pending_state: str | None = None
    blocked_reason: str | None = None
    blocked_by_task_ids: tuple[str, ...] = ()
    dependency_status_snapshot: Mapping[str, str] = field(default_factory=dict)
    dependency_terminal_event_ids: Mapping[str, str] = field(default_factory=dict)
    dependency_generation: int | None = None


@dataclass(frozen=True, slots=True)
class SubagentGraphProjection:
    parent_runtime_session_id: str
    nodes: tuple[SubagentGraphNode, ...]
    edges: tuple[SubagentEdge, ...]
    tasks: tuple[SubagentTaskProjection, ...] = ()
    diagnostics: tuple[Mapping[str, object], ...] = ()
