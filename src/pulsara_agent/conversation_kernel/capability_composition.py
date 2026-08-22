"""Owner-issued process-local carriers and the one Round 9 registry seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from pulsara_agent.capability.contracts import (
    CapabilitySourceKind,
    CapabilitySourceSnapshotDisposition,
    FrozenCapabilityRegistrySnapshot,
    FrozenCapabilitySourceSnapshot,
    FrozenToolCapabilityFact,
)
from pulsara_agent.capability.local_skills import LocalSkillDiscovery
from pulsara_agent.capability.registry import (
    freeze_capability_registration_set,
    freeze_capability_registry_snapshot,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.mcp.contracts import (
        McpCatalogSnapshot,
        McpToolSemanticFact,
    )
    from pulsara_agent.conversation_kernel.tool_runtime import (
        ProductionBuiltinExecutorBinding,
    )
    from pulsara_agent.conversation_kernel.tool_surface import (
        McpToolExecutionPolicyFact,
    )


_BUILTIN_ISSUER = object()
_MCP_ISSUER = object()
_SKILL_ISSUER = object()


class BuiltinCompositionState(StrEnum):
    PREPARING = "PREPARING"
    SEALED = "SEALED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class SealedBuiltinCapabilitySnapshot:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshot: FrozenCapabilitySourceSnapshot
    executor_bindings: tuple["ProductionBuiltinExecutorBinding", ...] = field(
        repr=False
    )
    builtin_composition_seal: object = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedMcpCapabilitySourceSnapshotSet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...]
    catalog_snapshot: "McpCatalogSnapshot" = field(repr=False)
    inspection_inputs: tuple["PreparedMcpInspectionInput", ...] = field(
        repr=False
    )
    owner_authenticity: object = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedMcpInspectionInput:
    source_snapshot: FrozenCapabilitySourceSnapshot = field(repr=False)
    tool_fact: FrozenToolCapabilityFact = field(repr=False)
    semantic: "McpToolSemanticFact" = field(repr=False)
    policy: "McpToolExecutionPolicyFact" = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedLocalSkillCatalogSourceSnapshot:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshot: FrozenCapabilitySourceSnapshot
    discovery: LocalSkillDiscovery = field(repr=False)
    owner_authenticity: object = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


def issue_sealed_builtin_capability_snapshot(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    source_snapshot: FrozenCapabilitySourceSnapshot,
    executor_bindings: tuple["ProductionBuiltinExecutorBinding", ...],
    builtin_composition_seal: object,
) -> SealedBuiltinCapabilitySnapshot:
    return SealedBuiltinCapabilitySnapshot(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        source_snapshot=source_snapshot,
        executor_bindings=executor_bindings,
        builtin_composition_seal=builtin_composition_seal,
        _issuer=_BUILTIN_ISSUER,
    )


def issue_mcp_capability_source_snapshot_set(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...],
    catalog_snapshot: "McpCatalogSnapshot",
    inspection_inputs: tuple[PreparedMcpInspectionInput, ...],
    owner_authenticity: object,
) -> PreparedMcpCapabilitySourceSnapshotSet:
    return PreparedMcpCapabilitySourceSnapshotSet(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        source_snapshots=source_snapshots,
        catalog_snapshot=catalog_snapshot,
        inspection_inputs=inspection_inputs,
        owner_authenticity=owner_authenticity,
        _issuer=_MCP_ISSUER,
    )


def issue_local_skill_catalog_source_snapshot(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    source_snapshot: FrozenCapabilitySourceSnapshot,
    discovery: LocalSkillDiscovery,
    owner_authenticity: object,
) -> PreparedLocalSkillCatalogSourceSnapshot:
    root_policy = discovery.root_policy
    if (
        root_policy.conversation_scope_kind is not conversation_scope_kind
        or root_policy.scope_subagent_task_id != scope_subagent_task_id
    ):
        raise ValueError("Skill discovery scope conflicts")
    return PreparedLocalSkillCatalogSourceSnapshot(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        source_snapshot=source_snapshot,
        discovery=discovery,
        owner_authenticity=owner_authenticity,
        _issuer=_SKILL_ISSUER,
    )


def freeze_capability_registry_from_owner_snapshots(
    *,
    builtin: SealedBuiltinCapabilitySnapshot,
    mcp: PreparedMcpCapabilitySourceSnapshotSet,
    skills: PreparedLocalSkillCatalogSourceSnapshot,
) -> FrozenCapabilityRegistrySnapshot:
    """The sole production merge of the three physical source owners."""

    if (
        type(builtin) is not SealedBuiltinCapabilitySnapshot
        or builtin._issuer is not _BUILTIN_ISSUER
        or type(mcp) is not PreparedMcpCapabilitySourceSnapshotSet
        or mcp._issuer is not _MCP_ISSUER
        or type(skills) is not PreparedLocalSkillCatalogSourceSnapshot
        or skills._issuer is not _SKILL_ISSUER
    ):
        raise TypeError("capability owner snapshot authenticity conflicts")
    scope = builtin.conversation_scope_kind
    task_id = builtin.scope_subagent_task_id
    if (
        mcp.conversation_scope_kind is not scope
        or skills.conversation_scope_kind is not scope
        or mcp.scope_subagent_task_id != task_id
        or skills.scope_subagent_task_id != task_id
    ):
        raise ValueError("capability owner snapshot scopes conflict")
    builtin_snapshot = builtin.source_snapshot
    skill_snapshot = skills.source_snapshot
    if (
        builtin_snapshot.registration.source.kind
        is not CapabilitySourceKind.BUILTIN_REGISTRY
        or builtin_snapshot.disposition
        is not CapabilitySourceSnapshotDisposition.COMPLETE
        or skill_snapshot.registration.source.kind
        is not CapabilitySourceKind.LOCAL_SKILL_CATALOG
        or any(
            item.registration.source.kind is not CapabilitySourceKind.MCP_SERVER
            for item in mcp.source_snapshots
        )
    ):
        raise ValueError("capability owner snapshot matrix conflicts")

    registrations = freeze_capability_registration_set(
        conversation_scope_kind=scope,
        scope_subagent_task_id=task_id,
        builtin_registration=builtin_snapshot.registration,
        mcp_registrations=tuple(
            item.registration for item in mcp.source_snapshots
        ),
        local_skill_catalog_registration=skill_snapshot.registration,
    )
    return freeze_capability_registry_snapshot(
        registration_set=registrations,
        source_snapshots=(
            builtin_snapshot,
            *tuple(
                sorted(
                    mcp.source_snapshots,
                    key=lambda item: item.registration.source.stable_source_id,
                )
            ),
            skill_snapshot,
        ),
    )


__all__ = [
    "BuiltinCompositionState",
    "PreparedLocalSkillCatalogSourceSnapshot",
    "PreparedMcpInspectionInput",
    "PreparedMcpCapabilitySourceSnapshotSet",
    "SealedBuiltinCapabilitySnapshot",
    "freeze_capability_registry_from_owner_snapshots",
    "issue_local_skill_catalog_source_snapshot",
    "issue_mcp_capability_source_snapshot_set",
    "issue_sealed_builtin_capability_snapshot",
]
