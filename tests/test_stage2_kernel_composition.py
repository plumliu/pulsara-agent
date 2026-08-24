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
from pulsara_agent.capability.local_skills import (
    LocalSkillDiscovery,
    SkillDiscoveryDisposition,
)
from pulsara_agent.capability.local_skills import LocalSkillProvider
from pulsara_agent.capability.provider import SkillProjectionOutput
from pulsara_agent.conversation_kernel.capability import (
    KernelSkillProjectionComposer,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.workspace_identity import HostWorkspaceInput


class _SkillProjectionProvider:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.provider = LocalSkillProvider(include_user_skills=False)

    def snapshot_projection_input(self, *, root_policy, deadline_monotonic=None):
        del deadline_monotonic
        self.snapshot_calls += 1
        return LocalSkillDiscovery(
            root_policy=root_policy,
            disposition=SkillDiscoveryDisposition.COMPLETE,
        )

    def resolve_projection_from_snapshot(self, context, *, discovery):
        assert context.active_skill_names == frozenset({"review"})
        assert discovery.disposition.value == "COMPLETE"
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


def _composer(tmp_path, provider: _SkillProjectionProvider):
    return KernelSkillProjectionComposer(
        workspace_root=tmp_path,
        configured_active_skill_names=frozenset({"review"}),
        provider=provider,  # type: ignore[arg-type]
    )


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
    composer = _composer(tmp_path, provider)
    owner, view = _skill_view(composer)
    projection = composer.compose(
        view=view,
        owner=owner,
        activation_subject=composer.activation_context(user_input="please review"),
    )
    assert projection.catalog_prompt == "<skills>review</skills>"
    assert projection.active_skill_prompt == "<active-skill>review body</active-skill>"
    assert tuple(item.name for item in projection.catalog_entries) == ("review",)
    assert tuple(item.name for item in projection.active_injections) == ("review",)
    assert tuple(item.code for item in projection.diagnostics) == ("skill_ready",)


def test_round3_1_capability_input_is_sampled_once_for_multiple_prefix_trials(
    tmp_path,
) -> None:
    provider = _SkillProjectionProvider()
    composer = _composer(tmp_path, provider)
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

    assert provider.snapshot_calls == 1
    assert first.catalog_prompt == second.catalog_prompt == "<skills>review</skills>"


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
    core = KernelHostCore(settings=SimpleNamespace())  # type: ignore[arg-type]

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="resource activation observed"):
            await core.open_session(
                HostWorkspaceInput(
                    workspace_root=tmp_path,
                    workspace_kind="project",
                )
            )

    asyncio.run(exercise())
    assert activated
