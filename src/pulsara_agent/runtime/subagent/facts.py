"""Immutable durable facts for the parent-owned subagent graph."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping

from types import MappingProxyType

from pulsara_agent.runtime.subagent.immutable import (
    freeze_json_mapping,
    thaw_json_mapping,
)
from pulsara_agent.runtime.subagent.types import (
    SubagentBudget,
    SubagentCapabilityProfile,
    SubagentContextPolicy,
    SubagentEdgeKind,
    SubagentRole,
    SubagentSpawnInitiatorKind,
    SubagentTaskStatus,
)


DurableSubagentRunStatus = Literal["running", "suspended", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class SubagentFactProvenance:
    created_event_id: str
    created_sequence: int
    last_event_id: str
    last_sequence: int
    created_at: datetime
    updated_at: datetime
    terminal_event_id: str | None = None
    terminal_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class SubagentTaskFact:
    task_id: str
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None
    batch_id: str | None
    create_tool_call_id: str | None
    task_key: str | None
    label: str | None
    profile_id: str
    display_role: str | None
    objective_preview: str
    objective_artifact_id: str
    depends_on: tuple[str, ...]
    status: SubagentTaskStatus
    current_run_id: str | None
    run_index: int | None
    scheduled_at: datetime | None
    schedule_reason: Literal["immediate", "dependency_satisfied", "manual"] | None
    phase: str | None
    result_id: str | None
    blocked_reason: str | None
    blocked_by_task_ids: tuple[str, ...]
    dependency_status_snapshot: Mapping[str, str]
    dependency_terminal_event_ids: Mapping[str, str]
    dependency_generation: int | None
    failure_reason_code: str | None
    cancellation_reason_code: str | None
    provenance: SubagentFactProvenance

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dependency_status_snapshot",
            MappingProxyType(dict(self.dependency_status_snapshot)),
        )
        object.__setattr__(
            self,
            "dependency_terminal_event_ids",
            MappingProxyType(dict(self.dependency_terminal_event_ids)),
        )

    @property
    def has_child_run(self) -> bool:
        return self.current_run_id is not None

    @property
    def created_at(self) -> datetime:
        return self.provenance.created_at

    @property
    def updated_at(self) -> datetime:
        return self.provenance.updated_at

    @property
    def completed_at(self) -> datetime | None:
        if self.status not in {
            "completed",
            "failed",
            "cancelled",
            "blocked_dependency_failed",
        }:
            return None
        return self.provenance.updated_at


@dataclass(frozen=True, slots=True)
class SubagentRunFact:
    subagent_run_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None
    parent_context_id: str | None
    parent_model_call_index: int | None
    edge_id: str
    spawning_tool_name: str | None
    spawn_initiator_kind: SubagentSpawnInitiatorKind | None
    spawn_initiator_id: str | None
    child_runtime_session_id: str
    reported_child_run_id: str | None
    task_id: str | None
    batch_id: str | None
    create_tool_call_id: str | None
    run_index: int | None
    label: str | None
    role: SubagentRole
    profile_id: str | None
    task_preview: str
    task_artifact_id: str | None
    context_policy: SubagentContextPolicy
    capability_profile: SubagentCapabilityProfile
    budget_snapshot: SubagentBudget
    status: DurableSubagentRunStatus
    phase: str | None
    pending_kind: str | None
    pending_reason_code: str | None
    result_id: str | None
    failure_reason_code: str | None
    cancellation_reason_code: str | None
    provenance: SubagentFactProvenance

    @property
    def child_run_id(self) -> str | None:
        return self.reported_child_run_id

    @property
    def created_at(self) -> datetime:
        return self.provenance.created_at

    @property
    def updated_at(self) -> datetime:
        return self.provenance.updated_at

    @property
    def context_policy_value(self) -> SubagentContextPolicy:
        return self.context_policy

    @property
    def capability_profile_value(self) -> SubagentCapabilityProfile:
        payload = self.capability_profile
        return SubagentCapabilityProfile(
            profile_id=payload.profile_id,
            profile_name=payload.profile_name,
            inherited_from_parent_context_id=payload.inherited_from_parent_context_id,
            permission_mode=payload.permission_mode,
            permission_policy=thaw_json_mapping(payload.permission_policy),
            allowed_tool_names=payload.allowed_tool_names,
            allowed_descriptor_ids=payload.allowed_descriptor_ids,
            allowed_skill_names=payload.allowed_skill_names,
            allowed_mcp_server_ids=payload.allowed_mcp_server_ids,
            can_spawn_subagents=payload.can_spawn_subagents,
            max_spawn_depth_from_root=payload.max_spawn_depth_from_root,
            memory_enabled=payload.memory_enabled,
            computed_from_parent_exposure_generation=(
                payload.computed_from_parent_exposure_generation
            ),
            diagnostics=tuple(
                thaw_json_mapping(diagnostic)
                for diagnostic in payload.diagnostics
            ),
        )

    @property
    def budget(self) -> SubagentBudget:
        return self.budget_snapshot


@dataclass(frozen=True, slots=True)
class SubagentResultFact:
    result_id: str
    subagent_run_id: str
    task_id: str | None
    status: Literal["submitted", "completed"]
    result_source: Literal["explicit", "inferred"]
    summary: str
    output_preview: str | None
    final_message_artifact_id: str
    artifact_ids: tuple[str, ...]
    diagnostics: tuple[Mapping[str, object], ...]
    token_usage: Mapping[str, object] | None
    tool_call_count: int | None
    provenance: SubagentFactProvenance

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "diagnostics",
            tuple(freeze_json_mapping(item) for item in self.diagnostics),
        )
        if self.token_usage is not None:
            object.__setattr__(
                self,
                "token_usage",
                freeze_json_mapping(self.token_usage),
            )


@dataclass(frozen=True, slots=True)
class SubagentEdgeFact:
    edge_id: str
    edge_kind: SubagentEdgeKind
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None
    subagent_run_id: str
    child_runtime_session_id: str
    child_run_id: str | None
    source_context_id: str | None
    source_model_call_index: int | None
    source_tool_call_id: str | None
    source_tool_name: str | None
    target_context_id: str | None
    payload_artifact_id: str | None
    result_id: str | None
    result_artifact_id: str | None
    returned_to_tool_call_id: str | None
    provenance: SubagentFactProvenance


@dataclass(frozen=True, slots=True)
class SubagentConsumptionFact:
    consumption_id: str
    kind: Literal["wait_run", "wait_task"]
    consumer_tool_call_id: str
    task_id: str | None
    subagent_run_id: str | None
    result_id: str | None
    consumed_status: Literal["completed", "failed", "cancelled", "blocked_dependency_failed"]
    terminal_event_id: str | None
    diagnostics: tuple[Mapping[str, object], ...]
    provenance: SubagentFactProvenance

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "diagnostics",
            tuple(freeze_json_mapping(item) for item in self.diagnostics),
        )


@dataclass(frozen=True, slots=True)
class SubagentDeliveryFact:
    result_id: str
    subagent_run_id: str
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None
    context_id: str
    model_call_index: int
    section_id: str
    result_artifact_id: str
    provenance: SubagentFactProvenance


@dataclass(frozen=True, slots=True)
class SubagentGraphDiagnostic:
    code: str
    severity: Literal["warning", "error"]
    event_id: str
    sequence: int
    entity_kind: Literal["task", "run", "result", "edge", "graph"]
    entity_id: str | None
    message: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class SubagentGraphState:
    tasks: Mapping[str, SubagentTaskFact] = field(default_factory=dict)
    runs: Mapping[str, SubagentRunFact] = field(default_factory=dict)
    results: Mapping[str, SubagentResultFact] = field(default_factory=dict)
    edges: Mapping[str, SubagentEdgeFact] = field(default_factory=dict)
    consumptions: Mapping[str, SubagentConsumptionFact] = field(default_factory=dict)
    deliveries: Mapping[str, SubagentDeliveryFact] = field(default_factory=dict)
    diagnostics: tuple[SubagentGraphDiagnostic, ...] = ()
    consistent: bool = True
    through_sequence: int = 0
    applied_subagent_event_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for field_name in (
            "tasks",
            "runs",
            "results",
            "edges",
            "consumptions",
            "deliveries",
        ):
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(dict(getattr(self, field_name))),
            )

    @classmethod
    def empty(cls) -> SubagentGraphState:
        return cls()


def subagent_dependency_generation(
    terminal_event_ids: Mapping[str, str],
) -> int | None:
    """Return the stable 48-bit version token for dependency terminal facts."""

    if not terminal_event_ids:
        return None
    canonical = json.dumps(
        dict(sorted(terminal_event_ids.items())),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return int(hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12], 16)
