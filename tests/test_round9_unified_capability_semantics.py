from __future__ import annotations
from tests.support.model_config import scheduled_task_service

from pathlib import Path
from types import SimpleNamespace

import pytest


from pulsara_agent.capability.contracts import (
    CapabilityKind,
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    EmptyCapabilityEpochPredecessor,
    FrozenMcpCapabilityProjectionInput,
    FrozenSkillProjectionInput,
    InstalledCapabilityEpochPredecessor,
    ToolCapabilityOrigin,
    capability_identity,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
    freeze_tool_capability_fact,
    tool_capability_version_ref,
)
from pulsara_agent.capability.local_skills import (
    LooseSkillDefinitionProducer,
)
from pulsara_agent.capability.resolver import CompleteEffectiveSkillCatalogInspection
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
    PreparedSkillCatalogSourceSnapshot,
    freeze_capability_registry_from_owner_snapshots,
    issue_skill_catalog_source_snapshot,
    issue_mcp_capability_source_snapshot_set,
    issue_sealed_builtin_capability_snapshot,
)
from pulsara_agent.conversation_kernel.mcp.contracts import (
    build_catalog_snapshot,
)

from pulsara_agent.llm.adapters.openai.function_tools import (
    freeze_openai_native_tool_eligibility,
    materialize_openai_native_tool_projection_set,
)
from pulsara_agent.model_input.contracts import FrozenToolSpec, ModelInputScopeKind
from pulsara_agent.primitives.context import context_fingerprint, freeze_json


def test_remember_is_main_only_in_frozen_and_model_surfaces(tmp_path):
    import asyncio
    from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import (
        DefaultToolDispatchAuthorizationPolicy,
    )
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from tests.support.round3 import prepare_test_direct_tool_surface
    from pulsara_agent.primitives.permission import PermissionMode

    async def exercise():
        port = DirectKernelToolPort(
            scheduled_tasks=scheduled_task_service(),
            workspace_root=tmp_path,
            host_owner_id="host:jx2n72tacopn3if4goyyza5ztm",
            session_id="session:2wae56asyel532v3i7tvactdee",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_memory_port(
            SimpleNamespace(tool_names=frozenset({"remember", "query_memory"}))
        )
        try:
            for scope, task, expected in (
                (ModelInputScopeKind.ROOT, None, True),
                (
                    ModelInputScopeKind.SUBAGENT,
                    "task:t2ptfli5koch6nfrihpkwl3kvi",
                    False,
                ),
            ):
                surface = prepare_test_direct_tool_surface(
                    port,
                    conversation_scope_kind=scope,
                )
                assert (
                    "remember" in {s.name for s in surface.model_surface.tool_specs}
                ) is expected
                snapshot = port.sealed_builtin_capability_snapshot(
                    conversation_scope_kind=scope,
                    effective_permission_mode=PermissionMode.ASK_PERMISSIONS,
                )
                assert (
                    "remember" in {b.tool_name for b in snapshot.executor_bindings}
                ) is expected
            assert (
                len(
                    builtin_tool_catalog_entry(
                        "remember"
                    ).availability_requirement.allowed_invocation_owners
                )
                == 1
            )
        finally:
            await port.aclose()

    asyncio.run(exercise())


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
    *,
    source_kind: CapabilitySourceKind,
    source_id: str,
    name: str,
    origin: ToolCapabilityOrigin,
    schema: dict[str, object],
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
        disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
        facts=facts,
    )


def _empty_skill_inspection() -> CompleteEffectiveSkillCatalogInspection:
    producer = LooseSkillDefinitionProducer(
        user_product_skills_root=Path.cwd() / ".test-round9-user-product",
        user_agents_skills_root=Path.cwd() / ".test-round9-user-agents",
    )
    return CompleteEffectiveSkillCatalogInspection(
        root_policy=producer.prepare_root_policy(Path.cwd()),
        winners=(),
        readable_skills=(),
        candidate_issues=(),
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
    )
    mcp_snapshots = tuple(
        _source_snapshot(
            source_kind=CapabilitySourceKind.MCP_SERVER,
            source_id=source_id,
            facts=tuple(
                fact
                for fact in mcp_facts
                if fact.identity.source.stable_source_id == source_id
            ),
            conversation_scope_kind=conversation_scope_kind,
        )
        for source_id in sorted(
            {fact.identity.source.stable_source_id for fact in mcp_facts} or {"fixture"}
        )
    )
    skill_snapshot = _source_snapshot(
        source_kind=CapabilitySourceKind.LOCAL_SKILL_CATALOG,
        source_id="local-skills",
        facts=(),
        conversation_scope_kind=conversation_scope_kind,
    )
    builtin_owner = issue_sealed_builtin_capability_snapshot(
        conversation_scope_kind=conversation_scope_kind,
        source_snapshot=builtin_snapshot,
        executor_bindings=(),
        builtin_composition_seal=object(),
    )
    catalog = build_catalog_snapshot(
        owner_epoch="epoch:aaaaaaaaaaaaaaaaaaaaaaaaaa", catalog_revision=1, entries=()
    )
    mcp_owner = issue_mcp_capability_source_snapshot_set(
        conversation_scope_kind=conversation_scope_kind,
        source_snapshots=mcp_snapshots,
        catalog_snapshot=catalog,
        inspection_inputs=(),
        owner_authenticity=object(),
    )
    inspection = _empty_skill_inspection()
    skill_owner = issue_skill_catalog_source_snapshot(
        conversation_scope_kind=conversation_scope_kind,
        source_snapshot=skill_snapshot,
        inspection=inspection,
        owner_authenticity=object(),
    )
    registry = freeze_capability_registry_from_owner_snapshots(
        builtin=builtin_owner,
        mcp=mcp_owner,
        skills=skill_owner,
    )
    all_facts = builtin_facts
    native = freeze_openai_native_tool_eligibility(
        conversation_scope_kind=conversation_scope_kind,
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
    mcp_input = FrozenMcpCapabilityProjectionInput(
        conversation_scope_kind=conversation_scope_kind,
        source_snapshots=mcp_snapshots,
    )
    tools = freeze_tool_planning_input(
        predecessor=predecessor or EmptyCapabilityEpochPredecessor(0),
        native_wire=native,
        mcp=mcp_input,
    )
    skill_input = FrozenSkillProjectionInput(
        source_snapshot=skill_snapshot,
        inspection=inspection,
    )
    _cut, tool_view, _skill_view = freeze_capability_dispatch_cut_and_views(
        conversation_scope_kind=conversation_scope_kind,
        registry=registry,
        tools=tools,
        skills=skill_input,
    )
    return tool_view


def _finalized_plan(*, view, wire_api="openai_chat_completions"):
    planner = KernelToolCapabilityPlanner()
    selection = planner.select(view=view)
    native = view.planning_input.native_wire
    projection_set = selection.reusable_direct_projection_set
    if projection_set is None:
        projection_set = materialize_openai_native_tool_projection_set(
            conversation_scope_kind=native.conversation_scope_kind,
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
    forged = PreparedSkillCatalogSourceSnapshot(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        source_snapshot=snapshot,
        inspection=_empty_skill_inspection(),
        owner_authenticity=object(),
        _issuer=object(),
    )
    builtin = issue_sealed_builtin_capability_snapshot(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
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
        source_snapshots=(),
        catalog_snapshot=build_catalog_snapshot(
            owner_epoch="epoch:aaaaaaaaaaaaaaaaaaaaaaaaaa",
            catalog_revision=1,
            entries=(),
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
            builtin_registration=builtin,
            mcp_registrations=(mcp, conflicting),
            local_skill_catalog_registration=skill,
        )


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
        _finalized_plan(view=_planning_view(builtin_facts=(builtin,), mcp_facts=()))


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
    monkeypatch.setattr("pulsara_agent.capability.planner.MAXIMUM_NATIVE_TOOL_BYTES", 1)
    with pytest.raises(
        CapabilityPlanningError,
        match="builtin surface exceeds native bounds",
    ):
        _finalized_plan(view=_planning_view(builtin_facts=(builtin,), mcp_facts=()))


def test_round9_parent_cut_rejects_overbound_planner_framing_before_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pulsara_agent.capability.registry.MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES",
        1,
    )
    with pytest.raises(ValueError, match="planner framing bound"):
        _planning_view(builtin_facts=(), mcp_facts=())


def test_round9_installed_predecessor_rejects_foreign_scope() -> None:
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
        conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
        scope_subagent_task_id="task:xf2km7nsw6mnh2uevbbikzaseu",
    )
    first = _finalized_plan(view=child_a)
    predecessor = InstalledCapabilityEpochPredecessor(
        expected_continuity_revision=1,
        continuity_epoch_nonce="epoch:jab4555mywnmhhu3736nwansui",
        tool_surface=first.direct_tool_surface,
        direct_projection_set=first.direct_projection_set,
    )
    with pytest.raises(ValueError, match="predecessor scope"):
        _planning_view(
            builtin_facts=(fact,),
            mcp_facts=(),
            predecessor=predecessor,
            conversation_scope_kind=ModelInputScopeKind.ROOT,
        )


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
    tool_runtime = (root / "conversation_kernel" / "tool_runtime.py").read_text(
        encoding="utf-8"
    )
    direct_model = (root / "conversation_kernel" / "direct_model.py").read_text(
        encoding="utf-8"
    )

    # Every production surface must now pass through owner snapshots, the
    # sole registry composition seam, adapter-native preflight, and the parent
    # dispatch cut.  These former convenience APIs constructed only one side
    # of that proof and are intentionally test-support-only now.
    assert "def snapshot_tool_surface(" not in tool_runtime
    assert "def prepare_call(" not in direct_model
    assert "_compatibility_native_projection_set" not in direct_model


def test_capability_identity_digest_vectors_sort_and_provider_wire_are_stable():
    from pulsara_agent.capability.contracts import (
        capability_identity_digest,
        capability_source_identity_digest,
        tool_capability_version_identity_digest,
    )

    facts = tuple(
        _fact(
            source_kind=CapabilitySourceKind.MCP_SERVER,
            source_id="fixture",
            name=name,
            origin=ToolCapabilityOrigin.MCP,
            schema={"type": "object", "properties": {}},
        )
        for name in ("a", "b")
    )
    assert (
        capability_source_identity_digest(facts[0].identity.source)
        == "sha256:97f68523f26809d00f0448fe9d8fc89417b5b36aa1c2bf5d093845f7ffedbccd"
    )
    assert (
        capability_identity_digest(facts[0].identity)
        == "sha256:49a503a891efce74f13931075cb1782ee58ef521b5f7443922dd83006c6738da"
    )
    assert (
        tool_capability_version_identity_digest(tool_capability_version_ref(facts[0]))
        == "sha256:fcc5ed38f35a417010c305bb73c4a9439d56c3d1c5418d9d19c749b8c58c848c"
    )
    assert facts[0].identity.stable_name < facts[1].identity.stable_name
    assert capability_identity_digest(facts[0].identity) > capability_identity_digest(
        facts[1].identity
    )
    plan = _finalized_plan(view=_planning_view(builtin_facts=(), mcp_facts=facts))
    assert [f.identity.stable_name for f in plan.dispatch_view.registry_tool_facts] == [
        "b",
        "a",
    ]
    assert plan.direct_projection_set.projections == ()


def test_capability_multisource_framing_uses_original_digest_bytes(monkeypatch):
    from pulsara_agent.capability import registry as registry_module

    facts = tuple(
        _fact(
            source_kind=CapabilitySourceKind.MCP_SERVER,
            source_id=source,
            name=name,
            origin=ToolCapabilityOrigin.MCP,
            schema={"type": "object", "properties": {}},
        )
        for source, name in (("server-a", "a"), ("server-b", "b"))
    )
    view = _planning_view(builtin_facts=(), mcp_facts=facts)
    cut = view.parent_dispatch_cut
    kwargs = dict(registry=cut.registry, tools=cut.tools, skills=cut.skills)
    low, high = 0, registry_module.MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES
    while low < high:
        middle = (low + high) // 2
        monkeypatch.setattr(
            registry_module, "MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES", middle
        )
        try:
            registry_module._require_bounded_planner_framing(**kwargs)
        except ValueError:
            low = middle + 1
        else:
            high = middle
    monkeypatch.setattr(
        registry_module, "MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES", low
    )
    registry_module._require_bounded_planner_framing(**kwargs)
    monkeypatch.setattr(
        registry_module, "MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES", low - 1
    )
    with pytest.raises(ValueError, match="framing"):
        registry_module._require_bounded_planner_framing(**kwargs)
    assert low == 2174


def test_mcp_discovery_never_changes_the_builtin_provider_tools_prefix():
    builtin = _fact(
        source_kind=CapabilitySourceKind.BUILTIN_REGISTRY,
        source_id="builtin-registry",
        name="builtin_echo",
        origin=ToolCapabilityOrigin.BUILTIN,
        schema={"type": "object", "properties": {}},
    )
    first = _finalized_plan(view=_planning_view(builtin_facts=(builtin,), mcp_facts=()))
    remote = tuple(
        _fact(
            source_kind=CapabilitySourceKind.MCP_SERVER,
            source_id="server",
            name=f"remote_{index}",
            origin=ToolCapabilityOrigin.MCP,
            schema={"type": "array", "items": {"type": "string"}},
        )
        for index in range(140)
    )
    predecessor = InstalledCapabilityEpochPredecessor(
        expected_continuity_revision=1,
        continuity_epoch_nonce="epoch:jab4555mywnmhhu3736nwansui",
        tool_surface=first.direct_tool_surface,
        direct_projection_set=first.direct_projection_set,
    )
    successor = _finalized_plan(
        view=_planning_view(
            builtin_facts=(builtin,), mcp_facts=remote, predecessor=predecessor
        )
    )
    assert successor.direct_tool_surface == first.direct_tool_surface
    assert successor.direct_projection_set is first.direct_projection_set
    assert [spec.name for spec in successor.direct_tool_surface.tool_specs] == [
        "builtin_echo"
    ]
    assert len(successor.dispatch_view.registry_tool_facts) == 141
