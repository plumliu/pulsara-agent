from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_descriptors
from pulsara_agent.capability.contracts import (
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    EmptyCapabilityEpochPredecessor,
    FrozenMcpCapabilityProjectionInput,
    FrozenNativeToolWireEligibilitySet,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
)
from pulsara_agent.capability.registry import (
    freeze_capability_dispatch_cut_and_views,
    freeze_capability_registration_set,
    freeze_capability_registry_snapshot,
    freeze_tool_planning_input,
)
from pulsara_agent.capability.bundled_skills import (
    BundledSkillDistributionBindingOwner,
    EXPECTED_BUNDLED_SKILL_NAMES,
)
from pulsara_agent.capability.local_skills import (
    AGENT_SKILLS_CONTRACT_ID,
    LOOSE_SKILL_ROOT_ORDER,
    SKILL_PLACEMENT_CONTRACT_ID,
    LooseSkillDefinitionProducer,
)
from pulsara_agent.capability.provider import SkillProjectionOutput
from pulsara_agent.capability.plugin_skill_contracts import (
    FrozenPluginSkillDefinitions,
    PluginSkillDefinitionsDisposition,
)
from pulsara_agent.conversation_kernel.capability import (
    KernelSkillProjectionComposer,
)
from pulsara_agent.conversation_kernel.host import (
    KernelHostCore,
    KernelHostCoreClosing,
)
from pulsara_agent.capability.types import BundledSkillOrigin
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.hooks.contracts import FrozenHookDefinitionView
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_runtime


class _SkillProjectionProvider:
    def __init__(self) -> None:
        self.resolve_calls = 0

    def resolve_projection_from_snapshot(self, context, *, inspection):
        self.resolve_calls += 1
        assert context.active_skill_names == frozenset({"review"})
        assert inspection.disposition.value == "COMPLETE"
        return SkillProjectionOutput(
            catalog_entries=(
                SimpleNamespace(
                    name="review", description="Review changes", location="test"
                ),
            ),
            active_injections=(
                SimpleNamespace(
                    name="review",
                    location="test",
                    body="Review body",
                    reason="configured",
                ),
            ),
            diagnostics=(SimpleNamespace(code="skill_ready"),),
            catalog_prompt="<skills>review</skills>",
            active_skill_prompt="<active-skill>review body</active-skill>",
        )


class _TrackingLooseProducer(LooseSkillDefinitionProducer):
    def __init__(self, *, user_root):
        super().__init__(
            user_product_skills_root=user_root,
            user_agents_skills_root=user_root.parent / "agents-skills",
        )
        self.observe_calls = 0

    def observe(self, policy, *, deadline_monotonic=None):
        self.observe_calls += 1
        return super().observe(policy, deadline_monotonic=deadline_monotonic)


def _composer(tmp_path, provider: _SkillProjectionProvider):
    skill_dir = tmp_path / ".pulsara" / "skills" / "review"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: review
description: Review changes
---
Review body
""",
        encoding="utf-8",
    )
    binding = BundledSkillDistributionBindingOwner()
    loose = _TrackingLooseProducer(user_root=tmp_path / "user-skills")
    composer = KernelSkillProjectionComposer(
        workspace_root=tmp_path,
        bundled_binding_owner=binding,
        plugin_definitions_provider=lambda: FrozenPluginSkillDefinitions(
            PluginSkillDefinitionsDisposition.COMPLETE
        ),
        configured_active_skill_names=frozenset({"review"}),
        loose_producer=loose,
        projection_provider=provider,  # type: ignore[arg-type]
    )
    return composer, binding, loose


def _skill_view(composer: KernelSkillProjectionComposer):
    owner = composer.freeze_owner_snapshot(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    projection = composer.freeze_projection_input(owner)
    builtin_source = capability_source_ref(
        CapabilitySourceKind.BUILTIN_REGISTRY, "test-builtin"
    )
    builtin_registration = capability_source_registration(
        source=builtin_source,
        refresh_mode=CapabilitySourceRefreshMode.IMMUTABLE,
        source_contract_fingerprint=context_fingerprint(
            "test:builtin-contract:v1", "empty"
        ),
    )
    builtin_snapshot = freeze_capability_source_snapshot(
        registration=builtin_registration,
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
        facts=(),
    )
    registration_set = freeze_capability_registration_set(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        builtin_registration=builtin_registration,
        mcp_registrations=(),
        local_skill_catalog_registration=owner.source_snapshot.registration,
    )
    registry = freeze_capability_registry_snapshot(
        registration_set=registration_set,
        source_snapshots=(builtin_snapshot, owner.source_snapshot),
    )
    native_contract = context_fingerprint("test:native-contract:v1", "empty")
    native = FrozenNativeToolWireEligibilitySet(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        native_function_tool_wire_contract_fingerprint=native_contract,
        entries=(),
    )
    mcp = FrozenMcpCapabilityProjectionInput(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        source_snapshots=(),
        catalog_semantic_fingerprint=context_fingerprint("test:mcp-catalog:v1", ()),
        inspectability_facts=(),
    )
    tools = freeze_tool_planning_input(
        predecessor=EmptyCapabilityEpochPredecessor(0),
        native_wire=native,
        mcp=mcp,
    )
    _parent, _tool_view, view = freeze_capability_dispatch_cut_and_views(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        registry=registry,
        tools=tools,
        skills=projection,
    )
    return owner, view


def test_every_model_callable_builtin_has_provider_object_schema() -> None:
    invalid = {
        descriptor.name: descriptor.input_schema.get("type")
        for descriptor in builtin_tool_descriptors()
        if descriptor.is_model_callable
        and descriptor.input_schema.get("type") != "object"
    }
    assert invalid == {}


def test_kernel_composition_preserves_root_catalog_and_active_skill_prompt(
    tmp_path,
) -> None:
    provider = _SkillProjectionProvider()
    composer, binding, _loose = _composer(tmp_path, provider)
    try:
        owner, view = _skill_view(composer)
        assert owner.source_snapshot.registration.source_contract_fingerprint == (
            context_fingerprint(
                "skill-source-contract:v6-local-enablement",
                {
                    "parser_contract": AGENT_SKILLS_CONTRACT_ID,
                    "placement_contract": SKILL_PLACEMENT_CONTRACT_ID,
                    "producer_kinds": ("LOOSE", "PLUGIN", "BUNDLED"),
                    "precedence": (
                        *(item.value for item in LOOSE_SKILL_ROOT_ORDER),
                        "WORKSPACE_PLUGIN",
                        "USER_PLUGIN",
                        "BUNDLED",
                    ),
                    "bundled_names": EXPECTED_BUNDLED_SKILL_NAMES,
                    "local_enablement": (
                        "exact-path-user-config",
                        "exact-path-workspace-config",
                    ),
                },
            )
        )
        bundled_fact = next(
            item
            for item in view.registry_skill_facts
            if isinstance(item.origin, BundledSkillOrigin)
        )
        origin_digest = context_fingerprint(
            "bundled-skill-origin:v1-agent-skills",
            {
                "package": "pulsara_agent",
                "relative_skill_directory": (
                    bundled_fact.origin.package_relative_skill_directory
                ),
            },
        )
        assert bundled_fact.fact_semantic_fingerprint == context_fingerprint(
            "skill-capability-fact:v1",
            {
                "identity_fingerprint": bundled_fact.identity.identity_fingerprint,
                "catalog_semantic_fingerprint": (
                    bundled_fact.catalog_semantic_fingerprint
                ),
                "activation_semantic_fingerprint": (
                    bundled_fact.activation_semantic_fingerprint
                ),
                "winning_root_provenance_fingerprint": origin_digest,
            },
        )
        assert not hasattr(bundled_fact, "winning_root_provenance_fingerprint")
        projection = composer.compose(
            view=view,
            owner=owner,
            activation_subject=composer.activation_context(user_input="please review"),
        )
        assert projection.catalog_prompt == "<skills>review</skills>"
        assert projection.active_skill_prompt == (
            "<active-skill>review body</active-skill>"
        )
        assert tuple(item.name for item in projection.catalog_entries) == ("review",)
        assert tuple(item.name for item in projection.active_injections) == ("review",)
        assert tuple(item.code for item in projection.diagnostics) == ("skill_ready",)
    finally:
        binding.close()


def test_round3_1_capability_input_is_sampled_once_for_multiple_prefix_trials(
    tmp_path,
) -> None:
    provider = _SkillProjectionProvider()
    composer, binding, loose = _composer(tmp_path, provider)
    try:
        owner, frozen_view = _skill_view(composer)
        first = composer.compose(
            view=frozen_view,
            owner=owner,
            activation_subject=composer.activation_context(user_input="first"),
        )
        second = composer.compose(
            view=frozen_view,
            owner=owner,
            activation_subject=composer.activation_context(user_input="second"),
        )

        assert loose.observe_calls == 1
        assert first.catalog_prompt == second.catalog_prompt == (
            "<skills>review</skills>"
        )
    finally:
        binding.close()


def test_enabled_mcp_enters_kernel_resource_activation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    activated = False

    async def observed_activation(_self):
        nonlocal activated
        activated = True
        raise RuntimeError("resource activation observed")

    monkeypatch.setattr(
        kernel_host,
        "load_mcp_server_configs",
        lambda **_: (SimpleNamespace(server_id="mcp:test", enabled=True),),
    )
    monkeypatch.setattr(KernelHostCore, "_ensure_resources", observed_activation)
    core = KernelHostCore(model_runtime=test_model_runtime())

    async def exercise() -> None:
        try:
            with pytest.raises(RuntimeError, match="resource activation observed"):
                await core.open_session(
                    HostWorkspaceInput(
                        workspace_root=tmp_path,
                        workspace_kind="project",
                    )
                )
        finally:
            await core.shutdown()

    asyncio.run(exercise())
    assert activated


def test_shutdown_fences_and_joins_unregistered_session_open(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    start_mcp_entered = asyncio.Event()
    release_start_mcp = asyncio.Event()
    constructed_sessions: list[object] = []
    home_observations = 0
    resolve_user_home = kernel_host.resolve_user_home

    def observed_user_home():
        nonlocal home_observations
        home_observations += 1
        return resolve_user_home()

    class FakeHookSourceProvider:
        def __init__(self, **_kwargs: object) -> None:
            self.trust_store = object()

        def discover(self, **_kwargs: object) -> object:
            return FrozenHookDefinitionView(())

    class FakeRepository:
        @staticmethod
        def acquire_host_writer(**_kwargs: object) -> object:
            return object()

    class BlockingSession:
        def __init__(self, **kwargs: object) -> None:
            self.session_id = str(kwargs["session_id"])
            self.extensions = object()
            self.closed = False
            self.binding = kwargs["bundled_skill_binding"]
            constructed_sessions.append(self)

        async def start_mcp(self) -> None:
            start_mcp_entered.set()
            await release_start_mcp.wait()

        async def aclose(self, **_kwargs: object) -> None:
            self.closed = True

    async def fake_resources(_self: KernelHostCore) -> FakeRepository:
        return FakeRepository()

    monkeypatch.setattr(kernel_host, "LocalHookSourceProvider", FakeHookSourceProvider)
    monkeypatch.setattr(kernel_host, "load_mcp_server_configs", lambda **_: ())
    monkeypatch.setattr(kernel_host, "KernelHostSession", BlockingSession)
    monkeypatch.setattr(kernel_host, "resolve_user_home", observed_user_home)
    monkeypatch.setattr(KernelHostCore, "_ensure_resources", fake_resources)
    core = KernelHostCore(model_runtime=test_model_runtime())

    async def exercise() -> None:
        opening = asyncio.create_task(
            core.open_session(
                HostWorkspaceInput(
                    workspace_root=tmp_path,
                    workspace_kind="project",
                )
            )
        )
        await asyncio.wait_for(start_mcp_entered.wait(), timeout=1)
        assert core._sessions == {}  # noqa: SLF001
        assert len(core._open_attempts) == 1  # noqa: SLF001

        shutdown = asyncio.create_task(core.shutdown())
        while not core._closing:  # noqa: SLF001
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert core._bundled_skill_binding.is_bound  # noqa: SLF001

        release_start_mcp.set()
        with pytest.raises(KernelHostCoreClosing):
            await opening
        await shutdown

        assert len(constructed_sessions) == 1
        assert home_observations == 1
        session = constructed_sessions[0]
        assert isinstance(session, BlockingSession)
        assert session.closed
        assert core._sessions == {}  # noqa: SLF001
        assert core._open_attempts == set()  # noqa: SLF001
        assert not core._bundled_skill_binding.is_bound  # noqa: SLF001
        with pytest.raises(KernelHostCoreClosing):
            await core.open_session(
                HostWorkspaceInput(
                    workspace_root=tmp_path,
                    workspace_kind="project",
                )
            )

    asyncio.run(exercise())
