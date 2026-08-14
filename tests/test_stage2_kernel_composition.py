from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_descriptors
from pulsara_agent.conversation_kernel.capability import KernelCapabilityComposer
from pulsara_agent.conversation_kernel.host import (
    KernelHostCore,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


class _CapabilityProvider:
    def __init__(self) -> None:
        self.snapshot_calls = 0

    def resolve_projection_for_available_tools(self, context, *, available_tool_names):
        assert context.active_skill_names == frozenset({"review"})
        assert available_tool_names == frozenset({"read_file"})
        return SimpleNamespace(
            catalog_entries=(SimpleNamespace(name="review"),),
            active_injections=(SimpleNamespace(name="review"),),
            diagnostics=(SimpleNamespace(code="skill_ready"),),
            catalog_prompt="<skills>review</skills>",
            active_skill_prompt="<active-skill>review body</active-skill>",
        )

    def snapshot_projection_input(self, *, workspace_root, available_tool_names):
        del workspace_root
        self.snapshot_calls += 1
        assert available_tool_names == frozenset({"read_file"})
        return SimpleNamespace(skills=(), diagnostics=())

    def resolve_projection_from_snapshot(
        self, context, *, available_tool_names, discovery
    ):
        assert discovery.skills == ()
        return self.resolve_projection_for_available_tools(
            context, available_tool_names=available_tool_names
        )


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
    composer = KernelCapabilityComposer(
        workspace_root=tmp_path,
        workspace_kind="project",
        memory_domain=None,  # type: ignore[arg-type]
        available_tool_names=frozenset({"read_file"}),
        configured_active_skill_names=frozenset({"review"}),
        provider=_CapabilityProvider(),  # type: ignore[arg-type]
    )
    projection = composer.resolve_projection(
        user_input="please review",
        available_tool_names=frozenset({"read_file"}),
    )
    assert projection.catalog_prompt == "<skills>review</skills>"
    assert projection.active_skill_prompt == "<active-skill>review body</active-skill>"
    assert tuple(item.name for item in projection.catalog_entries) == ("review",)
    assert tuple(item.name for item in projection.active_injections) == ("review",)
    assert tuple(item.code for item in projection.diagnostics) == ("skill_ready",)


def test_round3_1_capability_input_is_sampled_once_for_multiple_prefix_trials(
    tmp_path,
) -> None:
    provider = _CapabilityProvider()
    composer = KernelCapabilityComposer(
        workspace_root=tmp_path,
        workspace_kind="project",
        memory_domain=None,  # type: ignore[arg-type]
        available_tool_names=frozenset({"read_file"}),
        configured_active_skill_names=frozenset({"review"}),
        provider=provider,  # type: ignore[arg-type]
    )
    frozen = composer.freeze_projection_input(
        available_tool_names=frozenset({"read_file"})
    )
    first = composer.resolve_projection_from_frozen(frozen, user_input="first")
    second = composer.resolve_projection_from_frozen(frozen, user_input="second")

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
                    workspace_kind="project",
                    workspace_root=tmp_path,
                )
            )

    asyncio.run(exercise())
    assert activated is True
