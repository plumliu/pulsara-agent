"""Pure one-shot registry and parent-dispatch-cut factories.

There is deliberately no mutable registry generation and no register/remove
API.  Physical owners freeze complete source snapshots; the Host composition
seam is the only production caller of :func:`freeze_capability_registry_snapshot`.
"""

from __future__ import annotations

from pulsara_agent.capability.contracts import (
    CapabilitySourceKind,
    EmptyCapabilityEpochPredecessor,
    FrozenCapabilityDispatchCut,
    FrozenCapabilityRegistrySnapshot,
    FrozenCapabilitySourceRegistration,
    FrozenCapabilitySourceRegistrationSet,
    FrozenCapabilitySourceSnapshot,
    FrozenMcpCapabilityProjectionInput,
    FrozenNativeToolWireEligibilitySet,
    FrozenNativeToolWireEligibilityQuote,
    FrozenSkillCapabilityDispatchView,
    FrozenSkillProjectionInput,
    FrozenToolCapabilityDispatchView,
    FrozenToolCapabilityPlanningInput,
    InstalledCapabilityEpochPredecessor,
    MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES,
    ToolCapabilityOrigin,
    capability_dispatch_cut_fingerprint,
    capability_registration_set_fingerprint,
    capability_registry_fingerprint,
    frozen_tool_spec_fingerprint,
    skill_capability_dispatch_view_fingerprint,
    tool_capability_dispatch_view_fingerprint,
    tool_capability_planning_input_fingerprint,
    tool_capability_version_ref,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind


def _require_bounded_planner_framing(
    *,
    registry: FrozenCapabilityRegistrySnapshot,
    tools: FrozenToolCapabilityPlanningInput,
    skills: FrozenSkillProjectionInput,
) -> None:
    """Quote only references carried by the pure planner, never schema bodies.

    Sixteen bytes per item conservatively cover tuple/field framing around the
    canonical SHA-256 strings.  The check is incremental, so an overbound
    inventory cannot first allocate a second multi-megabyte serialization.
    """

    total = 512

    def add(value: str) -> None:
        nonlocal total
        total += len(value.encode("utf-8")) + 16
        if total > MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES:
            raise ValueError("capability planner framing bound exceeded")

    add(registry.registration_set.registration_set_fingerprint)
    add(registry.registry_fingerprint)
    add(tools.tool_view_fingerprint)
    add(skills.snapshot_fingerprint)
    for registration in registry.registration_set.registrations:
        add(registration.registration_fingerprint)
    for snapshot in registry.source_snapshots:
        add(snapshot.source_snapshot_fingerprint)
        for fact in snapshot.facts:
            add(fact.fact_semantic_fingerprint)
    for item in tools.native_wire.entries:
        add(
            item.eligibility_fingerprint
            if isinstance(item, FrozenNativeToolWireEligibilityQuote)
            else item.decision_fingerprint
        )
    for item in tools.mcp.inspectability_facts:
        add(item.fact_fingerprint)
    predecessor = tools.predecessor
    if isinstance(predecessor, InstalledCapabilityEpochPredecessor):
        add(predecessor.direct_projection_set.projection_set_fingerprint)
        add(predecessor.mcp_route_projection.projection_fingerprint)
        for route in predecessor.mcp_route_projection.routes:
            add(route.route_fingerprint)


def freeze_capability_registration_set(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    builtin_registration: FrozenCapabilitySourceRegistration,
    mcp_registrations: tuple[FrozenCapabilitySourceRegistration, ...],
    local_skill_catalog_registration: FrozenCapabilitySourceRegistration,
) -> FrozenCapabilitySourceRegistrationSet:
    by_source: dict[str, FrozenCapabilitySourceRegistration] = {}
    for registration in mcp_registrations:
        source_id = registration.source.source_identity_fingerprint
        existing = by_source.get(source_id)
        if existing is None:
            by_source[source_id] = registration
        elif existing != registration:
            raise ValueError("MCP source has conflicting registrations")
    ordered_mcp = tuple(
        sorted(by_source.values(), key=lambda item: item.source.stable_source_id)
    )
    fingerprint = capability_registration_set_fingerprint(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        builtin_registration=builtin_registration,
        mcp_registrations=ordered_mcp,
        local_skill_catalog_registration=local_skill_catalog_registration,
    )
    return FrozenCapabilitySourceRegistrationSet(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        builtin_registration=builtin_registration,
        mcp_registrations=ordered_mcp,
        local_skill_catalog_registration=local_skill_catalog_registration,
        registration_set_fingerprint=fingerprint,
    )


def freeze_capability_registry_snapshot(
    *,
    registration_set: FrozenCapabilitySourceRegistrationSet,
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...],
) -> FrozenCapabilityRegistrySnapshot:
    """Admit one complete, deterministic registry value."""

    by_source: dict[str, FrozenCapabilitySourceSnapshot] = {}
    for snapshot in source_snapshots:
        source_id = snapshot.registration.source.source_identity_fingerprint
        existing = by_source.get(source_id)
        if existing is not None:
            if existing == snapshot:
                continue
            raise ValueError("capability source has conflicting snapshots")
        by_source[source_id] = snapshot

    ordered: list[FrozenCapabilitySourceSnapshot] = []
    for registration in registration_set.registrations:
        snapshot = by_source.pop(
            registration.source.source_identity_fingerprint, None
        )
        if snapshot is None:
            raise ValueError("capability registry source snapshot is missing")
        if snapshot.registration != registration:
            raise ValueError("capability registry registration drifted")
        ordered.append(snapshot)
    if by_source:
        raise ValueError("unregistered capability source snapshot was supplied")

    snapshots = tuple(ordered)
    return FrozenCapabilityRegistrySnapshot(
        registration_set=registration_set,
        source_snapshots=snapshots,
        registry_fingerprint=capability_registry_fingerprint(
            registration_set=registration_set,
            source_snapshots=snapshots,
        ),
    )


def freeze_tool_planning_input(
    *,
    predecessor: EmptyCapabilityEpochPredecessor
    | InstalledCapabilityEpochPredecessor,
    native_wire: FrozenNativeToolWireEligibilitySet,
    mcp: FrozenMcpCapabilityProjectionInput,
) -> FrozenToolCapabilityPlanningInput:
    fingerprint = tool_capability_planning_input_fingerprint(
        predecessor=predecessor,
        native_wire=native_wire,
        mcp=mcp,
    )
    return FrozenToolCapabilityPlanningInput(
        predecessor=predecessor,
        native_wire=native_wire,
        mcp=mcp,
        tool_view_fingerprint=fingerprint,
    )


def freeze_capability_dispatch_cut_and_views(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    registry: FrozenCapabilityRegistrySnapshot,
    tools: FrozenToolCapabilityPlanningInput,
    skills: FrozenSkillProjectionInput,
) -> tuple[
    FrozenCapabilityDispatchCut,
    FrozenToolCapabilityDispatchView,
    FrozenSkillCapabilityDispatchView,
]:
    registration_set = registry.registration_set
    if (
        registration_set.conversation_scope_kind is not conversation_scope_kind
        or registration_set.scope_subagent_task_id != scope_subagent_task_id
        or tools.native_wire.conversation_scope_kind is not conversation_scope_kind
        or tools.native_wire.scope_subagent_task_id != scope_subagent_task_id
        or tools.mcp.conversation_scope_kind is not conversation_scope_kind
        or tools.mcp.scope_subagent_task_id != scope_subagent_task_id
    ):
        raise ValueError("capability dispatch cut scope conflicts")

    _require_bounded_planner_framing(
        registry=registry,
        tools=tools,
        skills=skills,
    )

    tool_facts = registry.tool_facts
    skill_facts = registry.skill_facts
    expected_versions = {
        tool_capability_version_ref(fact).version_fingerprint for fact in tool_facts
    }
    actual_versions = {
        item.capability_version_fingerprint for item in tools.native_wire.entries
    }
    if not expected_versions <= actual_versions:
        raise ValueError("native eligibility does not cover registry tools")
    if isinstance(tools.predecessor, EmptyCapabilityEpochPredecessor):
        if expected_versions != actual_versions:
            raise ValueError("cold native eligibility contains foreign tools")
    else:
        retained_versions = {
            item.version_fingerprint
            for item in tools.predecessor.direct_projection_set.tool_versions
        }
        if actual_versions != expected_versions | retained_versions:
            raise ValueError("installed native eligibility coverage conflicts")

    expected_spec_fingerprints = {
        tool_capability_version_ref(fact).version_fingerprint: (
            frozen_tool_spec_fingerprint(fact.canonical_tool_spec)
        )
        for fact in tool_facts
    }
    if isinstance(tools.predecessor, InstalledCapabilityEpochPredecessor):
        for version, spec in zip(
            tools.predecessor.direct_projection_set.tool_versions,
            tools.predecessor.tool_surface.tool_specs,
            strict=True,
        ):
            fingerprint = frozen_tool_spec_fingerprint(spec)
            existing = expected_spec_fingerprints.setdefault(
                version.version_fingerprint, fingerprint
            )
            if existing != fingerprint:
                raise ValueError("retained native Tool spec conflicts")
    if any(
        item.canonical_tool_spec_fingerprint
        != expected_spec_fingerprints[item.capability_version_fingerprint]
        for item in tools.native_wire.entries
    ):
        raise ValueError("native eligibility canonical Tool spec drifted")

    mcp_snapshots = tuple(
        item
        for item in registry.source_snapshots
        if item.registration.source.kind is CapabilitySourceKind.MCP_SERVER
    )
    if tuple(
        sorted(item.source_snapshot_fingerprint for item in mcp_snapshots)
    ) != tools.mcp.source_snapshot_fingerprints:
        raise ValueError("MCP projection does not exact-join registry sources")
    expected_mcp_inspectability = {
        (
            fact.identity.source.stable_source_id,
            fact.identity.stable_name,
        ): tool_capability_version_ref(fact)
        for fact in tool_facts
        if fact.origin is ToolCapabilityOrigin.MCP
    }
    actual_mcp_inspectability = {
        (item.target.server_id, item.target.remote_tool_name): item.version
        for item in tools.mcp.inspectability_facts
    }
    if actual_mcp_inspectability != expected_mcp_inspectability:
        raise ValueError(
            "MCP inspectability does not exact-join registry Tool versions"
        )
    skill_snapshot = tuple(
        item
        for item in registry.source_snapshots
        if item.registration.source.kind
        is CapabilitySourceKind.LOCAL_SKILL_CATALOG
    )
    if (
        len(skill_snapshot) != 1
        or skill_snapshot[0].source_snapshot_fingerprint
        != skills.source_snapshot_fingerprint
    ):
        raise ValueError("skill projection does not exact-join registry source")

    parent_fingerprint = capability_dispatch_cut_fingerprint(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        registry=registry,
        tools=tools,
        skills=skills,
    )
    parent = FrozenCapabilityDispatchCut(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        registry=registry,
        tools=tools,
        skills=skills,
        dispatch_cut_fingerprint=parent_fingerprint,
    )
    tool_view_fingerprint = tool_capability_dispatch_view_fingerprint(
        parent_dispatch_cut_fingerprint=parent_fingerprint,
        registry_fingerprint=registry.registry_fingerprint,
        registry_tool_facts=tool_facts,
        planning_input=tools,
    )
    tool_view = FrozenToolCapabilityDispatchView(
        parent_dispatch_cut_fingerprint=parent_fingerprint,
        registry_fingerprint=registry.registry_fingerprint,
        registry_tool_facts=tool_facts,
        planning_input=tools,
        view_fingerprint=tool_view_fingerprint,
    )
    skill_view_fingerprint = skill_capability_dispatch_view_fingerprint(
        parent_dispatch_cut_fingerprint=parent_fingerprint,
        registry_fingerprint=registry.registry_fingerprint,
        registry_skill_facts=skill_facts,
        projection_input=skills,
    )
    skill_view = FrozenSkillCapabilityDispatchView(
        parent_dispatch_cut_fingerprint=parent_fingerprint,
        registry_fingerprint=registry.registry_fingerprint,
        registry_skill_facts=skill_facts,
        projection_input=skills,
        view_fingerprint=skill_view_fingerprint,
    )
    return parent, tool_view, skill_view


__all__ = [
    "freeze_capability_dispatch_cut_and_views",
    "freeze_capability_registration_set",
    "freeze_capability_registry_snapshot",
    "freeze_tool_planning_input",
]
