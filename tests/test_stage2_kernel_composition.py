from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_descriptors
from pulsara_agent.conversation_kernel.capability import KernelCapabilityComposer
from pulsara_agent.conversation_kernel.host import (
    KernelCompositionUnavailable,
    KernelHostCore,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


class _CapabilityProvider:
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
        base_system_prompt="ROOT SYSTEM",
        provider=_CapabilityProvider(),  # type: ignore[arg-type]
    )
    projection = composer.compose(user_input="please review")
    assert projection.system_prompt == (
        "ROOT SYSTEM\n\n<skills>review</skills>\n\n"
        "<active-skill>review body</active-skill>"
    )
    assert projection.catalog_skill_names == ("review",)
    assert projection.active_skill_names == ("review",)
    assert projection.diagnostic_codes == ("skill_ready",)


def test_enabled_mcp_is_rejected_before_kernel_resource_activation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    activated = False

    async def forbidden_activation(_self):
        nonlocal activated
        activated = True
        raise AssertionError("resource activation must not run")

    monkeypatch.setattr(
        kernel_host,
        "load_mcp_server_configs",
        lambda **_: (SimpleNamespace(server_id="mcp:test", enabled=True),),
    )
    monkeypatch.setattr(KernelHostCore, "_ensure_resources", forbidden_activation)
    core = KernelHostCore(settings=SimpleNamespace())  # type: ignore[arg-type]

    async def exercise() -> None:
        with pytest.raises(KernelCompositionUnavailable, match="mcp:test"):
            await core.open_session(
                HostWorkspaceInput(
                    workspace_kind="project",
                    workspace_root=tmp_path,
                )
            )

    asyncio.run(exercise())
    assert activated is False
