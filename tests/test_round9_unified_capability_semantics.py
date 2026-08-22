from __future__ import annotations

from dataclasses import fields, replace
import ast
import json
from pathlib import Path

import pytest

from pulsara_agent.capability.contracts import (
    CapabilityKind,
    CapabilityRouteReasonCode,
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    EmptyCapabilityEpochPredecessor,
    FrozenMcpCapabilityProjectionInput,
    FrozenMcpRouteProjection,
    FrozenNativeToolWireEligibilityQuote,
    FrozenNativeToolProjectionSet,
    FrozenSkillProjectionInput,
    InstalledCapabilityEpochPredecessor,
    McpInspectEffectKind,
    McpToolCapabilityRef,
    ToolCapabilityOrigin,
    ToolCapabilityRouteKind,
    capability_identity,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
    freeze_mcp_inspectability_fact,
    freeze_tool_capability_fact,
    native_tool_projection_set_fingerprint,
    tool_capability_version_ref,
)
from pulsara_agent.capability.local_skills import LocalSkillDiscovery, LocalSkillProvider
from pulsara_agent.capability.planner import (
    CapabilityPlanningError,
    KernelToolCapabilityPlanner,
)
from pulsara_agent.capability.registry import (
    freeze_capability_dispatch_cut_and_views,
    freeze_capability_registration_set,
    freeze_tool_planning_input,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedLocalSkillCatalogSourceSnapshot,
    PreparedMcpCapabilitySourceSnapshotSet,
    freeze_capability_registry_from_owner_snapshots,
    issue_local_skill_catalog_source_snapshot,
    issue_mcp_capability_source_snapshot_set,
    issue_sealed_builtin_capability_snapshot,
)
from pulsara_agent.conversation_kernel.mcp.contracts import build_catalog_snapshot
from pulsara_agent.conversation_kernel.mcp.contracts import McpServerCatalogEntry
from pulsara_agent.conversation_kernel.mcp.directory import McpDirectoryPageFactory
from pulsara_agent.conversation_kernel.capability import (
    skill_discovery_semantic_fingerprint,
)
from pulsara_agent.conversation_kernel.mcp.meta import (
    McpToolRefCapacityExceeded,
    ProcessLocalNewMcpToolRefOwner,
)
from pulsara_agent.conversation_kernel.mcp.supervisor import McpServerState
from pulsara_agent.llm.adapters.openai.function_tools import (
    freeze_openai_native_tool_eligibility,
    materialize_openai_native_tool_projection_set,
    openai_native_function_tool_contract_fingerprint,
)
from pulsara_agent.model_input.contracts import FrozenToolSpec, ModelInputScopeKind
from pulsara_agent.primitives.context import context_fingerprint, freeze_json
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)


def _spec(name: str, schema: dict[str, object]) -> FrozenToolSpec:
    return FrozenToolSpec(
        name=name,
        description=f"Capability {name}",
        parameters=freeze_json(schema),
        descriptor_fingerprint=context_fingerprint(
            "test:round9-descriptor:v1", (name, schema)
        ),
    )


def _fact(
    *, source_kind: CapabilitySourceKind, source_id: str, name: str,
    origin: ToolCapabilityOrigin, schema: dict[str, object],
):
    source = capability_source_ref(source_kind, source_id)
    return freeze_tool_capability_fact(
        identity=capability_identity(
            kind=CapabilityKind.TOOL,
            source=source,
            stable_name=name,
        ),
        origin=origin,
        canonical_tool_spec=_spec(name, schema),
    )


def _source_snapshot(
    *,
    source_kind: CapabilitySourceKind,
    source_id: str,
    facts: tuple,
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
):
    source = capability_source_ref(source_kind, source_id)
    return freeze_capability_source_snapshot(
        registration=capability_source_registration(
            source=source,
            refresh_mode=(
                CapabilitySourceRefreshMode.IMMUTABLE
                if source_kind is CapabilitySourceKind.BUILTIN_REGISTRY
                else CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE
            ),
            source_contract_fingerprint=context_fingerprint(
                "test:round9-source-contract:v1", source_id
            ),
        ),
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
        facts=facts,
    )


def _planning_view(
    *,
    builtin_facts: tuple,
    mcp_facts: tuple,
    inspect_overbound: frozenset[str] = frozenset(),
    predecessor=None,
    wire_api: str = "openai_chat_completions",
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
):
    builtin_snapshot = _source_snapshot(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        facts=builtin_facts,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    mcp_snapshot = _source_snapshot(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        facts=mcp_facts,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    skill_snapshot = _source_snapshot(
        source_kind=CapabilitySourceKind.LOCAL_SKILL_CATALOG,
        source_id="local-skills",
        facts=(),
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    builtin_owner = issue_sealed_builtin_capability_snapshot(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        source_snapshot=builtin_snapshot,
        executor_bindings=(),
        builtin_composition_seal=object(),
    )
    catalog = build_catalog_snapshot(owner_epoch=1, catalog_revision=1, entries=())
    mcp_owner = issue_mcp_capability_source_snapshot_set(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        source_snapshots=(mcp_snapshot,),
        catalog_snapshot=catalog,
        inspection_inputs=(),
        owner_authenticity=object(),
    )
    root_policy = LocalSkillProvider(include_user_skills=False).prepare_root_policy(
        Path.cwd(),
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    discovery = LocalSkillDiscovery(
        skills=(),
        diagnostics=(),
        root_policy=root_policy,
    )
    skill_owner = issue_local_skill_catalog_source_snapshot(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        source_snapshot=skill_snapshot,
        discovery=discovery,
        owner_authenticity=object(),
    )
    registry = freeze_capability_registry_from_owner_snapshots(
        builtin=builtin_owner,
        mcp=mcp_owner,
        skills=skill_owner,
    )
    all_facts = (*builtin_facts, *mcp_facts)
    native = freeze_openai_native_tool_eligibility(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        wire_api=wire_api,
        tool_facts=all_facts,
        retained_direct_inputs=(
            ()
            if predecessor is None
            else tuple(
                zip(
                    predecessor.direct_projection_set.tool_versions,
                    predecessor.tool_surface.tool_specs,
                    strict=True,
                )
            )
        ),
    )
    inspectability = tuple(
        freeze_mcp_inspectability_fact(
            target=McpToolCapabilityRef(
                server_id="fixture", remote_tool_name=fact.identity.stable_name
            ),
            version=tool_capability_version_ref(fact),
            descriptor_payload_fingerprint=context_fingerprint(
                "test:round9-inspect-descriptor:v1", fact.fact_semantic_fingerprint
            ),
            mcp_execution_policy_fingerprint=context_fingerprint(
                "test:round9-inspect-policy:v1", fact.identity.stable_name
            ),
            effect_kind=McpInspectEffectKind.READ_ONLY,
            conservative_logical_utf8_bytes=(
                MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES + 1
                if fact.identity.stable_name in inspect_overbound
                else 1024
            ),
        )
        for fact in mcp_facts
    )
    mcp_input = FrozenMcpCapabilityProjectionInput(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        source_snapshots=(mcp_snapshot,),
        catalog_semantic_fingerprint=catalog.semantic_fingerprint,
        inspectability_facts=inspectability,
    )
    tools = freeze_tool_planning_input(
        predecessor=predecessor or EmptyCapabilityEpochPredecessor(0),
        native_wire=native,
        mcp=mcp_input,
    )
    discovery_fingerprint = skill_discovery_semantic_fingerprint(discovery)
    skill_input = FrozenSkillProjectionInput(
        discovery_semantic_fingerprint=discovery_fingerprint,
        source_snapshot=skill_snapshot,
    )
    _cut, tool_view, _skill_view = freeze_capability_dispatch_cut_and_views(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        registry=registry,
        tools=tools,
        skills=skill_input,
    )
    return tool_view


def _finalized_plan(*, view):
    planner = KernelToolCapabilityPlanner()
    selection = planner.select(view=view)
    native = view.planning_input.native_wire
    projection_set = selection.reusable_direct_projection_set
    if projection_set is None:
        wire_api = next(
            item
            for item in (
                "openai_chat_completions",
                "openai_responses",
            )
            if openai_native_function_tool_contract_fingerprint(item)
            == native.native_function_tool_wire_contract_fingerprint
        )
        projection_set = materialize_openai_native_tool_projection_set(
            conversation_scope_kind=native.conversation_scope_kind,
            scope_subagent_task_id=native.scope_subagent_task_id,
            wire_api=wire_api,
            tool_versions=selection.direct_tool_versions,
            tool_specs=selection.direct_tool_surface.tool_specs,
            eligibility=native,
        )
    return planner.finalize(
        selection=selection,
        direct_projection_set=projection_set,
    )


def test_round9_owner_snapshot_authenticity_rejects_same_shape_forgery() -> None:
    snapshot = _source_snapshot(
        source_kind=CapabilitySourceKind.LOCAL_SKILL_CATALOG,
        source_id="local-skills",
        facts=(),
    )
    root_policy = LocalSkillProvider(include_user_skills=False).prepare_root_policy(
        Path.cwd(),
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    forged = PreparedLocalSkillCatalogSourceSnapshot(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        source_snapshot=snapshot,
        discovery=LocalSkillDiscovery(
            skills=(),
            diagnostics=(),
            root_policy=root_policy,
        ),
        owner_authenticity=object(),
        _issuer=object(),
    )
    builtin = issue_sealed_builtin_capability_snapshot(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        source_snapshot=_source_snapshot(
            source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
            source_id="builtin-registry",
            facts=(),
        ),
        executor_bindings=(),
        builtin_composition_seal=object(),
    )
    mcp = issue_mcp_capability_source_snapshot_set(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        source_snapshots=(),
        catalog_snapshot=build_catalog_snapshot(
            owner_epoch=1, catalog_revision=1, entries=()
        ),
        inspection_inputs=(),
        owner_authenticity=object(),
    )
    with pytest.raises(TypeError, match="authenticity"):
        freeze_capability_registry_from_owner_snapshots(
            builtin=builtin, mcp=mcp, skills=forged
        )


def test_round9_duplicate_source_registration_is_idempotent_or_conflict() -> None:
    builtin = _source_snapshot(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        facts=(),
    ).registration
    skill = _source_snapshot(
        source_kind=CapabilitySourceKind.LOCAL_SKILL_CATALOG,
        source_id="local-skills",
        facts=(),
    ).registration
    mcp = _source_snapshot(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        facts=(),
    ).registration
    admitted = freeze_capability_registration_set(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        builtin_registration=builtin,
        mcp_registrations=(mcp, mcp),
        local_skill_catalog_registration=skill,
    )
    assert admitted.mcp_registrations == (mcp,)

    conflicting = capability_source_registration(
        source=mcp.source,
        refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
        source_contract_fingerprint=context_fingerprint(
            "test:round9-source-contract:v1", "conflicting"
        ),
    )
    with pytest.raises(ValueError, match="conflicting registrations"):
        freeze_capability_registration_set(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            builtin_registration=builtin,
            mcp_registrations=(mcp, conflicting),
            local_skill_catalog_registration=skill,
        )


def test_round9_native_incompatible_mcp_routes_meta_without_poisoning_builtin() -> None:
    builtin = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="builtin_echo",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "object", "properties": {}},
    )
    mcp = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="remote_union",
        origin=ToolCapabilityOrigin.MCP,
        schema={
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [{"type": "string"}],
                    "anyOf": [{"type": "string"}],
                }
            },
        },
    )
    plan = _finalized_plan(
        view=_planning_view(builtin_facts=(builtin,), mcp_facts=(mcp,))
    )
    assert tuple(item.name for item in plan.direct_tool_surface.tool_specs) == (
        "builtin_echo",
    )
    assert len(plan.mcp_catalog_route_projection.routes) == 1
    route = plan.mcp_catalog_route_projection.routes[0]
    assert route.route is ToolCapabilityRouteKind.NEW_MCP_META_ONLY
    assert route.public_reason_code is CapabilityRouteReasonCode.NATIVE_WIRE_INCOMPATIBLE


def test_round9_native_incompatible_builtin_fails_entire_dispatch() -> None:
    builtin = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="invalid_builtin",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "array", "items": {"type": "string"}},
    )
    with pytest.raises(
        CapabilityPlanningError,
        match="builtin is not native-wire compatible",
    ):
        _finalized_plan(
            view=_planning_view(builtin_facts=(builtin,), mcp_facts=())
        )


def test_round9_builtin_aggregate_bound_never_diverts_to_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="builtin_echo",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "object", "properties": {}},
    )
    monkeypatch.setattr(
        "pulsara_agent.capability.planner.MAXIMUM_NATIVE_TOOL_BYTES", 1
    )
    with pytest.raises(
        CapabilityPlanningError,
        match="builtin surface exceeds native bounds",
    ):
        _finalized_plan(
            view=_planning_view(builtin_facts=(builtin,), mcp_facts=())
        )


def test_round9_parent_cut_rejects_overbound_planner_framing_before_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pulsara_agent.capability.registry."
        "MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES",
        1,
    )
    with pytest.raises(ValueError, match="planner framing bound"):
        _planning_view(builtin_facts=(), mcp_facts=())


def test_round9_inspect_overbound_is_typed_unavailable() -> None:
    mcp = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="remote_union",
        origin=ToolCapabilityOrigin.MCP,
        schema={
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [{"type": "string"}],
                    "anyOf": [{"type": "string"}],
                }
            },
        },
    )
    plan = _finalized_plan(
        view=_planning_view(
            builtin_facts=(),
            mcp_facts=(mcp,),
            inspect_overbound=frozenset({"remote_union"}),
        )
    )
    route = plan.mcp_catalog_route_projection.routes[0]
    assert route.route is ToolCapabilityRouteKind.UNAVAILABLE
    assert route.public_reason_code is CapabilityRouteReasonCode.DESCRIPTOR_OVERBOUND


def test_round9_installed_epoch_keeps_native_surface_and_routes_late_mcp_meta() -> None:
    builtin = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="builtin_echo",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "object", "properties": {}},
    )
    first = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="first",
        origin=ToolCapabilityOrigin.MCP,
        schema={"type": "object", "properties": {}},
    )
    cold = _finalized_plan(
        view=_planning_view(builtin_facts=(builtin,), mcp_facts=(first,))
    )
    predecessor = InstalledCapabilityEpochPredecessor(
        expected_continuity_revision=1,
        continuity_epoch_nonce="epoch:installed",
        tool_surface=cold.direct_tool_surface,
        direct_projection_set=cold.direct_projection_set,
        mcp_route_projection=cold.mcp_catalog_route_projection,
    )
    late = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="late",
        origin=ToolCapabilityOrigin.MCP,
        schema={"type": "object", "properties": {}},
    )
    installed = _finalized_plan(
        view=_planning_view(
            builtin_facts=(builtin,),
            mcp_facts=(first, late),
            predecessor=predecessor,
        )
    )
    assert installed.direct_tool_surface == cold.direct_tool_surface
    assert installed.direct_projection_set == cold.direct_projection_set
    routes = {
        item.target.remote_tool_name: item
        for item in installed.mcp_catalog_route_projection.routes
    }
    assert routes["first"].route is ToolCapabilityRouteKind.DIRECT
    assert routes["late"].route is ToolCapabilityRouteKind.NEW_MCP_META_ONLY
    assert (
        routes["late"].public_reason_code
        is CapabilityRouteReasonCode.NEW_NOT_IN_NATIVE_SURFACE
    )


def test_round9_provider_contract_reset_reprojects_only_installed_cohort() -> None:
    builtin = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="builtin_echo",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "object", "properties": {}},
    )
    direct = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="direct",
        origin=ToolCapabilityOrigin.MCP,
        schema={"type": "object", "properties": {}},
    )
    cold = _finalized_plan(
        view=_planning_view(builtin_facts=(builtin,), mcp_facts=(direct,))
    )
    predecessor = InstalledCapabilityEpochPredecessor(
        expected_continuity_revision=1,
        continuity_epoch_nonce="epoch:chat",
        tool_surface=cold.direct_tool_surface,
        direct_projection_set=cold.direct_projection_set,
        mcp_route_projection=cold.mcp_catalog_route_projection,
    )
    late = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="late",
        origin=ToolCapabilityOrigin.MCP,
        schema={"type": "object", "properties": {}},
    )
    reset = _finalized_plan(
        view=_planning_view(
            builtin_facts=(builtin,),
            mcp_facts=(direct, late),
            predecessor=predecessor,
            wire_api="openai_responses",
        )
    )
    assert reset.direct_tool_surface == cold.direct_tool_surface
    assert tuple(
        item.provider_name for item in reset.direct_projection_set.tool_versions
    ) == tuple(
        item.provider_name for item in cold.direct_projection_set.tool_versions
    )
    assert (
        reset.direct_projection_set.native_function_tool_wire_contract_fingerprint
        != cold.direct_projection_set.native_function_tool_wire_contract_fingerprint
    )
    late_route = next(
        item
        for item in reset.mcp_catalog_route_projection.routes
        if item.target.remote_tool_name == "late"
    )
    assert late_route.route is ToolCapabilityRouteKind.NEW_MCP_META_ONLY


def test_round9_installed_schema_replacement_cannot_bypass_native_via_meta() -> None:
    old = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="replace_me",
        origin=ToolCapabilityOrigin.MCP,
        schema={"type": "object", "properties": {"old": {"type": "string"}}},
    )
    cold = _finalized_plan(
        view=_planning_view(builtin_facts=(), mcp_facts=(old,))
    )
    predecessor = InstalledCapabilityEpochPredecessor(
        expected_continuity_revision=1,
        continuity_epoch_nonce="epoch:installed",
        tool_surface=cold.direct_tool_surface,
        direct_projection_set=cold.direct_projection_set,
        mcp_route_projection=cold.mcp_catalog_route_projection,
    )
    replacement = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="replace_me",
        origin=ToolCapabilityOrigin.MCP,
        schema={"type": "object", "properties": {"new": {"type": "integer"}}},
    )
    plan = _finalized_plan(
        view=_planning_view(
            builtin_facts=(),
            mcp_facts=(replacement,),
            predecessor=predecessor,
        )
    )
    assert plan.direct_tool_surface == cold.direct_tool_surface
    assert plan.direct_projection_set == cold.direct_projection_set
    assert len(plan.mcp_catalog_route_projection.routes) == 1
    route = plan.mcp_catalog_route_projection.routes[0]
    assert route.route is ToolCapabilityRouteKind.UNAVAILABLE
    assert route.public_reason_code is CapabilityRouteReasonCode.SCHEMA_REPLACED


def test_round9_cold_mcp_native_aggregate_is_all_direct_or_all_meta() -> None:
    builtin = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="builtin_echo",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "object", "properties": {}},
    )
    mcp_facts = tuple(
        _fact(
            source_kind=CapabilitySourceKind.MCP_SERVER,
            source_id="fixture",
            name=f"remote_{index:03d}",
            origin=ToolCapabilityOrigin.MCP,
            schema={"type": "object", "properties": {}},
        )
        for index in range(64)
    )
    plan = _finalized_plan(
        view=_planning_view(builtin_facts=(builtin,), mcp_facts=mcp_facts)
    )
    assert tuple(item.name for item in plan.direct_tool_surface.tool_specs) == (
        "builtin_echo",
    )
    assert len(plan.mcp_catalog_route_projection.routes) == 64
    assert {
        item.route for item in plan.mcp_catalog_route_projection.routes
    } == {ToolCapabilityRouteKind.NEW_MCP_META_ONLY}
    assert {
        item.public_reason_code for item in plan.mcp_catalog_route_projection.routes
    } == {CapabilityRouteReasonCode.NEW_COLD_COHORT_META_FALLBACK}

    predecessor = InstalledCapabilityEpochPredecessor(
        expected_continuity_revision=1,
        continuity_epoch_nonce="epoch:cold-meta",
        tool_surface=plan.direct_tool_surface,
        direct_projection_set=plan.direct_projection_set,
        mcp_route_projection=plan.mcp_catalog_route_projection,
    )
    successor = _finalized_plan(
        view=_planning_view(
            builtin_facts=(builtin,),
            mcp_facts=mcp_facts,
            predecessor=predecessor,
        )
    )
    # A ref issued by inspect on the cold aggregate-fallback route remains
    # usable on the next call in the exact same continuity epoch.  The route
    # reason is identity-bearing and must not be recomputed as "late".
    assert successor.mcp_catalog_route_projection.routes == (
        plan.mcp_catalog_route_projection.routes
    )


def test_round9_new_mcp_ref_requires_exact_full_install_and_epoch() -> None:
    owner = ProcessLocalNewMcpToolRefOwner()
    prepared = owner.prepare(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        continuity_epoch_nonce="epoch:one",
        capability_identity_fingerprint="sha256:identity",
        tool_semantic_fingerprint="sha256:semantic",
        mcp_execution_policy_fingerprint="sha256:policy",
        tool_route_fingerprint="sha256:route",
        result_entry_id="entry:inspect-result",
    )
    owner.settle(
        prepared=prepared,
        committed=True,
    )
    with pytest.raises(LookupError, match="STALE_OR_DORMANT"):
        owner.resolve_callable(
            prepared.ref.opaque_token,
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            continuity_epoch_nonce="epoch:one",
        )
    assert owner.install_full_result(
        result_entry_id="entry:inspect-result",
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        continuity_epoch_nonce="epoch:one",
    )
    resolved = owner.resolve_callable(
        prepared.ref.opaque_token,
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        continuity_epoch_nonce="epoch:one",
    )
    assert resolved is prepared.ref
    with pytest.raises(LookupError, match="STALE_OR_DORMANT"):
        owner.resolve_callable(
            prepared.ref.opaque_token,
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            continuity_epoch_nonce="epoch:two",
        )


def test_round9_new_mcp_ref_capacity_is_closed_and_does_not_evict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.mcp.meta."
        "MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH",
        1,
    )
    owner = ProcessLocalNewMcpToolRefOwner()
    first = owner.prepare(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        continuity_epoch_nonce="epoch:one",
        capability_identity_fingerprint="sha256:identity-one",
        tool_semantic_fingerprint="sha256:semantic-one",
        mcp_execution_policy_fingerprint="sha256:policy",
        tool_route_fingerprint="sha256:route-one",
        result_entry_id="entry:first",
    )
    with pytest.raises(McpToolRefCapacityExceeded):
        owner.prepare(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            continuity_epoch_nonce="epoch:one",
            capability_identity_fingerprint="sha256:identity-two",
            tool_semantic_fingerprint="sha256:semantic-two",
            mcp_execution_policy_fingerprint="sha256:policy",
            tool_route_fingerprint="sha256:route-two",
            result_entry_id="entry:second",
        )
    owner.settle(
        prepared=first,
        committed=True,
    )
    assert owner.install_full_result(
        result_entry_id="entry:first",
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        continuity_epoch_nonce="epoch:one",
    )
    assert owner.resolve_callable(
        first.ref.opaque_token,
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        continuity_epoch_nonce="epoch:one",
    ) == first.ref


def test_round9_mcp_directory_cursor_is_scope_and_catalog_bound() -> None:
    entries = tuple(
        McpServerCatalogEntry(
            server_id=f"server-{index}",
            display_name=f"Server {index}",
            status=McpServerState.READY,
            required=False,
            exposed_tool_count=0,
            discovered_tool_count=0,
            resource_count=0,
            resource_template_count=0,
            prompt_count=0,
            bounded_tool_name_overview=(),
            sanitized_instructions=None,
            stable_failure_category=None,
            tool_surface_semantic_fingerprint=context_fingerprint(
                "test:round9-empty-tool-surface:v1", index
            ),
            catalog_semantic_fingerprint=context_fingerprint(
                "test:round9-server-catalog:v1", index
            ),
            scope_subagents=True,
        )
        for index in range(3)
    )
    catalog = build_catalog_snapshot(
        owner_epoch=1, catalog_revision=1, entries=entries
    )
    contract = context_fingerprint("test:round9-wire-contract:v1", "chat")
    direct = FrozenNativeToolProjectionSet(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        native_function_tool_wire_contract_fingerprint=contract,
        tool_versions=(),
        projections=(),
        projection_set_fingerprint=native_tool_projection_set_fingerprint(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            native_function_tool_wire_contract_fingerprint=contract,
            tool_versions=(),
            projections=(),
        ),
    )
    routes = FrozenMcpRouteProjection(
        routes=(),
        joined_catalog_semantic_fingerprint=catalog.semantic_fingerprint,
        projection_fingerprint=context_fingerprint(
            "mcp-route-projection:v1",
            {"routes": (), "catalog": catalog.semantic_fingerprint},
        ),
    )
    factory = McpDirectoryPageFactory()
    first = factory.render(
        arguments={"limit": 1},
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        catalog=catalog,
        candidates={},
        routes=routes,
        direct_projection_set=direct,
    )
    assert first.state == "SUCCESS"
    payload = json.loads(first.content)
    assert len(payload["servers"]) == 1
    assert payload["next_cursor"]
    second = factory.render(
        arguments={"limit": 1, "cursor": payload["next_cursor"]},
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        catalog=catalog,
        candidates={},
        routes=routes,
        direct_projection_set=direct,
    )
    assert json.loads(second.content)["servers"][0]["server_id"] == "server-1"
    child = factory.render(
        arguments={"limit": 1, "cursor": payload["next_cursor"]},
        scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
        scope_subagent_task_id="task:child",
        catalog=catalog.for_scope(ModelInputScopeKind.SUBAGENT_TASK),
        candidates={},
        routes=routes,
        direct_projection_set=direct,
    )
    assert child.state == "APPLICATION_ERROR"
    assert b"STALE_CURSOR" in child.content

    drifted_routes = replace(
        routes,
        joined_catalog_semantic_fingerprint="sha256:foreign-catalog",
        projection_fingerprint=context_fingerprint(
            "mcp-route-projection:v1",
            {"routes": (), "catalog": "sha256:foreign-catalog"},
        ),
    )
    stale_cut = factory.render(
        arguments={"limit": 1},
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        catalog=catalog,
        candidates={},
        routes=drifted_routes,
        direct_projection_set=direct,
    )
    assert stale_cut.state == "APPLICATION_ERROR"
    assert b"MCP_CATALOG_STALE" in stale_cut.content


def test_round9_projection_wire_bytes_are_derived_not_caller_supplied() -> None:
    fact = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="builtin_echo",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "object", "properties": {}},
    )
    eligibility = freeze_openai_native_tool_eligibility(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        wire_api="openai_chat_completions",
        tool_facts=(fact,),
    )
    version = tool_capability_version_ref(fact)
    projection_set = materialize_openai_native_tool_projection_set(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        wire_api="openai_chat_completions",
        tool_versions=(version,),
        tool_specs=(fact.canonical_tool_spec,),
        eligibility=eligibility,
    )
    assert projection_set.projections[0].wire_utf8_bytes > 0
    with pytest.raises(TypeError):
        replace(projection_set.projections[0], wire_utf8_bytes=1)


def test_round9_large_mcp_cohort_quotes_before_meta_fallback() -> None:
    facts = tuple(
        _fact(
            source_kind=CapabilitySourceKind.MCP_SERVER,
            source_id="fixture",
            name=f"bulk_{index:03d}",
            origin=ToolCapabilityOrigin.MCP,
            schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "x" * 4_096,
                    }
                },
            },
        )
        for index in range(65)
    )
    view = _planning_view(builtin_facts=(), mcp_facts=facts)
    eligibility = view.planning_input.native_wire
    assert all(
        isinstance(item, FrozenNativeToolWireEligibilityQuote)
        and not hasattr(item, "wire_tool")
        for item in eligibility.entries
    )

    selection = KernelToolCapabilityPlanner().select(view=view)
    assert selection.direct_tool_versions == ()
    assert len(selection.mcp_catalog_route_projection.routes) == 65
    assert {
        item.route for item in selection.mcp_catalog_route_projection.routes
    } == {ToolCapabilityRouteKind.NEW_MCP_META_ONLY}
    projection_set = materialize_openai_native_tool_projection_set(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        wire_api="openai_chat_completions",
        tool_versions=selection.direct_tool_versions,
        tool_specs=selection.direct_tool_surface.tool_specs,
        eligibility=eligibility,
    )
    assert projection_set.projections == ()


def test_round9_native_quote_loop_obeys_dispatch_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fact = _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="deadline_probe",
        origin=ToolCapabilityOrigin.MCP,
        schema={"type": "object", "properties": {}},
    )
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(
        "pulsara_agent.llm.adapters.openai.function_tools.monotonic",
        lambda: next(ticks),
    )
    with pytest.raises(TimeoutError, match="planning deadline"):
        freeze_openai_native_tool_eligibility(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            wire_api="openai_chat_completions",
            tool_facts=(fact,),
            deadline_monotonic=1.0,
        )


def test_round9_installed_predecessor_rejects_foreign_child_task() -> None:
    fact = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="builtin_echo",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "object", "properties": {}},
    )
    child_a = _planning_view(
        builtin_facts=(fact,),
        mcp_facts=(),
        conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
        scope_subagent_task_id="task:child-a",
    )
    first = _finalized_plan(view=child_a)
    predecessor = InstalledCapabilityEpochPredecessor(
        expected_continuity_revision=1,
        continuity_epoch_nonce="epoch:child-a",
        tool_surface=first.direct_tool_surface,
        direct_projection_set=first.direct_projection_set,
        mcp_route_projection=first.mcp_catalog_route_projection,
    )
    with pytest.raises(ValueError, match="predecessor scope"):
        _planning_view(
            builtin_facts=(fact,),
            mcp_facts=(),
            predecessor=predecessor,
            conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id="task:child-b",
        )


def test_round9_owner_carriers_do_not_retain_decorative_proof_fields() -> None:
    assert "resolved_config_inventory_fingerprint" not in {
        item.name for item in fields(PreparedMcpCapabilitySourceSnapshotSet)
    }
    assert "root_policy_fingerprint" not in {
        item.name for item in fields(PreparedLocalSkillCatalogSourceSnapshot)
    }


def test_round9_generic_capability_package_has_no_physical_authority_imports() -> None:
    root = Path(__file__).parents[1] / "src" / "pulsara_agent" / "capability"
    forbidden = (
        "conversation_kernel.mcp",
        "conversation_kernel.repository",
        "llm.adapters",
        "storage",
        "compaction",
    )
    for name in ("contracts.py", "registry.py", "planner.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert not any(item in source for item in forbidden), name


def test_round9_production_has_no_pre_registry_tool_surface_bypass() -> None:
    root = Path(__file__).parents[1] / "src" / "pulsara_agent"
    tool_runtime = (
        root / "conversation_kernel" / "tool_runtime.py"
    ).read_text(encoding="utf-8")
    direct_model = (
        root / "conversation_kernel" / "direct_model.py"
    ).read_text(encoding="utf-8")

    # Every production surface must now pass through owner snapshots, the
    # sole registry composition seam, adapter-native preflight, and the parent
    # dispatch cut.  These former convenience APIs constructed only one side
    # of that proof and are intentionally test-support-only now.
    assert "def snapshot_tool_surface(" not in tool_runtime
    assert "def prepare_call(" not in direct_model
    assert "_compatibility_native_projection_set" not in direct_model


def test_round9_owner_snapshot_issuers_and_registry_merge_have_closed_callers() -> None:
    src = Path(__file__).parents[1] / "src" / "pulsara_agent"
    allowed = {
        "issue_sealed_builtin_capability_snapshot": {
            "conversation_kernel/tool_runtime.py",
        },
        "issue_mcp_capability_source_snapshot_set": {
            "conversation_kernel/mcp/supervisor.py",
        },
        "issue_local_skill_catalog_source_snapshot": {
            "conversation_kernel/capability.py",
        },
        "freeze_capability_registry_from_owner_snapshots": {
            "conversation_kernel/runner.py",
        },
    }
    observed = {name: set() for name in allowed}
    for path in src.rglob("*.py"):
        relative = path.relative_to(src).as_posix()
        if relative == "conversation_kernel/capability_composition.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            if name in observed:
                observed[name].add(relative)
    assert observed == allowed
