"""Event-safe cross-ledger subagent terminal and result handoff facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives.model_call import (
    ModelTokenUsageFact,
    canonical_json_bytes,
    sha256_fingerprint,
)
from pulsara_agent.primitives.run_lifecycle import (
    RunStopReason,
    RunTerminalizationKind,
)
from pulsara_agent.primitives.subagent_json import (
    freeze_json_mapping,
    thaw_json_mapping,
)

if TYPE_CHECKING:
    from pulsara_agent.primitives.long_horizon import ChildRolloutReservationPolicyFact


SubagentStatus: TypeAlias = Literal[
    "running", "suspended", "completed", "failed", "cancelled"
]
SubagentTaskStatus: TypeAlias = Literal[
    "created",
    "waiting_dependency",
    "running",
    "blocked_dependency_failed",
    "completed",
    "failed",
    "cancelled",
]
SubagentRole: TypeAlias = Literal["worker", "verifier", "synthesizer", "orchestrator"]
SubagentContextMode: TypeAlias = Literal["isolated", "fork"]
SubagentEdgeKind: TypeAlias = Literal[
    "spawn", "send", "followup", "wait", "cancel", "result", "suspend", "resume"
]
SubagentResultSource: TypeAlias = Literal["explicit", "inferred", "none"]
SubagentSpawnInitiatorKind: TypeAlias = Literal[
    "tool_call", "scheduler", "dependency_satisfied"
]
SubagentCapabilityProfileName: TypeAlias = Literal[
    "general_worker",
    "research_worker",
    "review_worker",
    "verification_worker",
    "synthesizer",
    "orchestrator",
]
SubagentTaskProfileName: TypeAlias = Literal[
    "general_worker", "research_worker", "review_worker", "verification_worker"
]
SubagentCommandKind: TypeAlias = Literal[
    "spawn_agent",
    "wait_agent",
    "stop_agent",
    "list_agents",
    "create_agent_tasks",
    "wait_agent_tasks",
    "stop_agent_task",
    "report_agent_phase",
    "report_agent_result",
]


def _default_child_rollout_policy() -> "ChildRolloutReservationPolicyFact":
    from pulsara_agent.primitives.long_horizon import default_child_rollout_policy

    return default_child_rollout_policy()


@dataclass(frozen=True, slots=True)
class SubagentContextPolicy:
    mode: SubagentContextMode = "isolated"
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
    max_result_artifact_refs_per_child: int = 32
    max_subagent_results_per_parent_compile: int = 8
    child_rollout_policy: ChildRolloutReservationPolicyFact = field(
        default_factory=_default_child_rollout_policy
    )

    @classmethod
    def from_event_snapshot(cls, snapshot: object) -> "SubagentBudget":
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
            max_result_artifact_refs_per_child=int(
                getattr(snapshot, "max_result_artifact_refs_per_child")
            ),
            max_subagent_results_per_parent_compile=int(
                getattr(snapshot, "max_subagent_results_per_parent_compile")
            ),
            child_rollout_policy=getattr(snapshot, "child_rollout_policy"),
        )

    def to_event_value(self) -> dict[str, Any]:
        return {
            "max_concurrent_children_per_parent_run": self.max_concurrent_children_per_parent_run,
            "max_concurrent_children_per_host_session": self.max_concurrent_children_per_host_session,
            "max_spawn_depth_from_root": self.max_spawn_depth_from_root,
            "child_timeout_seconds": self.child_timeout_seconds,
            "max_total_child_runs_per_parent_run": self.max_total_child_runs_per_parent_run,
            "max_result_summary_chars_per_child": self.max_result_summary_chars_per_child,
            "max_result_artifact_refs_per_child": self.max_result_artifact_refs_per_child,
            "max_subagent_results_per_parent_compile": self.max_subagent_results_per_parent_compile,
            "child_rollout_policy": self.child_rollout_policy.model_dump(mode="json"),
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
            self, "permission_policy", freeze_json_mapping(self.permission_policy)
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
            "diagnostics": [thaw_json_mapping(item) for item in self.diagnostics],
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
    role: SubagentRole
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


class ChildResultRenderPolicyFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    renderer_version: str = Field(min_length=1, max_length=128)
    max_summary_chars: int = Field(ge=0)
    max_artifact_refs: int = Field(ge=0)
    policy_fingerprint: str

    @model_validator(mode="after")
    def _validate_fingerprint(self) -> "ChildResultRenderPolicyFact":
        expected = child_result_render_policy_fingerprint(
            renderer_version=self.renderer_version,
            max_summary_chars=self.max_summary_chars,
            max_artifact_refs=self.max_artifact_refs,
        )
        if self.policy_fingerprint != expected:
            raise ValueError("child result render policy fingerprint mismatch")
        return self


def child_result_render_policy_fingerprint(
    *,
    renderer_version: str,
    max_summary_chars: int,
    max_artifact_refs: int,
) -> str:
    return sha256_fingerprint(
        "child-result-render-policy:v1",
        [renderer_version, max_summary_chars, max_artifact_refs],
    )


def build_child_result_render_policy(
    *,
    renderer_version: str,
    max_summary_chars: int,
    max_artifact_refs: int,
) -> ChildResultRenderPolicyFact:
    return ChildResultRenderPolicyFact(
        renderer_version=renderer_version,
        max_summary_chars=max_summary_chars,
        max_artifact_refs=max_artifact_refs,
        policy_fingerprint=child_result_render_policy_fingerprint(
            renderer_version=renderer_version,
            max_summary_chars=max_summary_chars,
            max_artifact_refs=max_artifact_refs,
        ),
    )


def validate_child_render_policy_against_budget(
    policy: ChildResultRenderPolicyFact,
    budget_snapshot: object,
) -> None:
    if policy.max_summary_chars != int(
        getattr(budget_snapshot, "max_result_summary_chars_per_child")
    ):
        raise ValueError("child result summary cap does not match parent budget")
    if policy.max_artifact_refs != int(
        getattr(budget_snapshot, "max_result_artifact_refs_per_child")
    ):
        raise ValueError("child result artifact cap does not match parent budget")


class ChildNativeTerminalReferenceFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    child_runtime_session_id: str = Field(min_length=1)
    child_run_id: str = Field(min_length=1)
    terminal_event_id: str = Field(min_length=1)
    terminal_sequence: int = Field(ge=1)
    terminal_status: Literal["finished", "failed", "aborted"]
    terminalization_kind: RunTerminalizationKind
    stop_reason: RunStopReason

    @model_validator(mode="after")
    def _validate_terminal_matrix(self) -> "ChildNativeTerminalReferenceFact":
        kind = self.terminalization_kind
        if kind is RunTerminalizationKind.NORMAL:
            valid = (
                self.terminal_status == "finished"
                and self.stop_reason is RunStopReason.FINAL
            )
        elif kind in {
            RunTerminalizationKind.USER_STOP,
            RunTerminalizationKind.HOST_TEARDOWN,
            RunTerminalizationKind.RECOVERED_INTERRUPTED,
        }:
            valid = (
                self.terminal_status == "aborted"
                and self.stop_reason is RunStopReason.ABORTED
            )
        else:
            valid = self.terminal_status == "failed" and self.stop_reason not in {
                RunStopReason.FINAL,
                RunStopReason.WAITING_USER,
                RunStopReason.ABORTED,
            }
        if not valid:
            raise ValueError("child terminal reference violates run terminal matrix")
        return self


class ChildExplicitResultEvidenceFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_result_submitted_event_id: str = Field(min_length=1)
    source_result_submitted_event_sequence: int = Field(ge=1)
    child_runtime_session_id: str = Field(min_length=1)
    child_run_id: str = Field(min_length=1)
    source_tool_call_id: str = Field(min_length=1)
    tool_call_start_event_id: str = Field(min_length=1)
    tool_call_start_sequence: int = Field(ge=1)
    tool_result_end_event_id: str = Field(min_length=1)
    tool_result_end_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_order(self) -> "ChildExplicitResultEvidenceFact":
        if self.tool_result_end_sequence < self.tool_call_start_sequence:
            raise ValueError("explicit result tool result precedes tool call")
        return self


class ChildResultHandoffFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_kind: Literal["explicit", "inferred"]
    renderer_version: str = Field(min_length=1)
    render_policy_fingerprint: str = Field(min_length=1)
    child_terminal_reference: ChildNativeTerminalReferenceFact
    explicit_evidence: ChildExplicitResultEvidenceFact | None
    result_id: str = Field(min_length=1)
    summary: str
    result_artifact_id: str = Field(min_length=1)
    artifact_ids: tuple[str, ...]
    rendered_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    token_usage: ModelTokenUsageFact | None
    usage_status: Literal["complete", "partial", "missing"]
    tool_call_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_handoff(self) -> "ChildResultHandoffFact":
        if self.handoff_kind == "explicit":
            if self.explicit_evidence is None:
                raise ValueError("explicit handoff requires explicit evidence")
            if (
                self.explicit_evidence.child_runtime_session_id
                != self.child_terminal_reference.child_runtime_session_id
                or self.explicit_evidence.child_run_id
                != self.child_terminal_reference.child_run_id
            ):
                raise ValueError("explicit evidence child attribution mismatch")
            terminal_sequence = self.child_terminal_reference.terminal_sequence
            evidence_sequences = (
                self.explicit_evidence.tool_call_start_sequence,
                self.explicit_evidence.tool_result_end_sequence,
            )
            if any(sequence >= terminal_sequence for sequence in evidence_sequences):
                raise ValueError("explicit evidence must precede child terminal")
        elif self.explicit_evidence is not None:
            raise ValueError("inferred handoff cannot contain explicit evidence")

        if self.usage_status == "missing":
            if self.token_usage is not None:
                raise ValueError("missing usage cannot contain token usage")
        elif self.token_usage is None:
            raise ValueError("complete/partial usage requires token usage")

        if not self.artifact_ids:
            raise ValueError("handoff requires the primary result artifact")
        if self.artifact_ids != tuple(sorted(set(self.artifact_ids))):
            raise ValueError("handoff artifact_ids must be sorted and unique")
        if self.result_artifact_id not in self.artifact_ids:
            raise ValueError("result_artifact_id must be present in artifact_ids")
        return self


def build_child_result_handoff(
    *,
    handoff_kind: Literal["explicit", "inferred"],
    policy: ChildResultRenderPolicyFact,
    child_terminal_reference: ChildNativeTerminalReferenceFact,
    explicit_evidence: ChildExplicitResultEvidenceFact | None,
    result_id: str,
    summary: str,
    result_artifact_id: str,
    artifact_ids: tuple[str, ...],
    token_usage: ModelTokenUsageFact | None,
    usage_status: Literal["complete", "partial", "missing"],
    tool_call_count: int,
) -> ChildResultHandoffFact:
    if len(summary) > policy.max_summary_chars:
        raise ValueError("child result summary exceeds frozen render policy")
    canonical_artifact_ids = tuple(sorted(set(artifact_ids)))
    if len(canonical_artifact_ids) > policy.max_artifact_refs:
        raise ValueError("child result artifact refs exceed frozen render policy")
    payload = {
        "handoff_kind": handoff_kind,
        "renderer_version": policy.renderer_version,
        "render_policy_fingerprint": policy.policy_fingerprint,
        "child_terminal_reference": child_terminal_reference.model_dump(mode="json"),
        "explicit_evidence": (
            explicit_evidence.model_dump(mode="json")
            if explicit_evidence is not None
            else None
        ),
        "result_id": result_id,
        "summary": summary,
        "result_artifact_id": result_artifact_id,
        "artifact_ids": list(canonical_artifact_ids),
        "token_usage": (
            token_usage.model_dump(mode="json") if token_usage is not None else None
        ),
        "usage_status": usage_status,
        "tool_call_count": tool_call_count,
    }
    return ChildResultHandoffFact(
        **payload,
        rendered_payload_sha256=rendered_payload_sha256(payload),
    )


def deterministic_child_result_id(
    *, subagent_run_id: str, terminal_event_id: str, policy_fingerprint: str
) -> str:
    digest = sha256_fingerprint(
        "child-result-id:v1",
        [subagent_run_id, terminal_event_id, policy_fingerprint],
    ).removeprefix("sha256:")
    return f"subagent_result:{digest}"


def deterministic_child_result_artifact_id(
    *, subagent_run_id: str, terminal_event_id: str, policy_fingerprint: str
) -> str:
    digest = sha256_fingerprint(
        "child-result-artifact-id:v1",
        [subagent_run_id, terminal_event_id, policy_fingerprint],
    ).removeprefix("sha256:")
    return f"artifact:subagent_result:{digest}"


def deterministic_parent_subagent_terminal_event_id(
    *,
    parent_runtime_session_id: str,
    subagent_run_id: str,
    child_terminal_event_id: str,
    parent_terminal_event_type: str,
) -> str:
    """Derive the parent-ledger terminal identity from durable child truth."""

    digest = sha256_fingerprint(
        "parent-subagent-terminal-event-id:v1",
        [
            parent_runtime_session_id,
            subagent_run_id,
            child_terminal_event_id,
            parent_terminal_event_type,
        ],
    ).removeprefix("sha256:")
    return f"{parent_terminal_event_type}:{digest}"


def rendered_payload_sha256(payload: object) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


__all__ = [
    "ChildExplicitResultEvidenceFact",
    "ChildNativeTerminalReferenceFact",
    "ChildResultHandoffFact",
    "ChildResultRenderPolicyFact",
    "SubagentBudget",
    "SubagentCapabilityProfile",
    "SubagentCapabilityProfileName",
    "SubagentCommandKind",
    "SubagentContextMode",
    "SubagentContextPolicy",
    "SubagentEdge",
    "SubagentEdgeKind",
    "SubagentGraphNode",
    "SubagentGraphProjection",
    "SubagentResult",
    "SubagentResultSource",
    "SubagentRole",
    "SubagentRunTerminalOutcome",
    "SubagentSpawnInitiatorKind",
    "SubagentStatus",
    "SubagentTaskProfileName",
    "SubagentTaskProjection",
    "SubagentTaskStatus",
    "build_child_result_render_policy",
    "build_child_result_handoff",
    "child_result_render_policy_fingerprint",
    "deterministic_child_result_artifact_id",
    "deterministic_child_result_id",
    "deterministic_parent_subagent_terminal_event_id",
    "rendered_payload_sha256",
    "validate_child_render_policy_against_budget",
]
