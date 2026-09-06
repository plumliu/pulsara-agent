import asyncio
import json
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.capability_management import (
    CapabilityManagementPreparation,
)
from pulsara_agent.capability.mcp_management import (
    LocalMcpManagementService,
    LocalMcpTarget,
    McpManagementConflict,
)
from pulsara_agent.plugins.management import PluginManagementService
from pulsara_agent.plugins.contracts import InstallLocalPluginRequest, PluginScopeKind
from pulsara_agent.capability.pulsara_home import resolve_pulsara_home
from pulsara_agent.plugins.mcp_connection import (
    plugin_connection_owner,
    PluginMcpConnectionOverlay,
    overlay_to_dict,
)
from pulsara_agent.mcp_config import BearerSecret
from pulsara_agent.mcp_credentials import (
    ManagedLocalCredentialReference,
    McpCredentialBinding,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.primitives.context import thaw_json


def preparation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp = LocalMcpManagementService(
        LocalSettingsStore(tmp_path / "private" / "local-settings.yaml"),
        user_config_path=tmp_path / "mcp.yaml",
    )
    return CapabilityManagementPreparation(
        mcp=mcp,
        plugins=PluginManagementService(
            credential_boundary=ProcessCredentialBoundary(),
            pulsara_home_resolution=resolve_pulsara_home(str(tmp_path / "private")),
        ),
        workspace_root=workspace,
        deadline=lambda: monotonic() + 5,
    )


async def install_plugin(owner, tmp_path, *, stdio=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "example",
            }
        )
    )
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "docs": {"type": "stdio", "command": "node", "args": ["server.js"]}
                    if stdio
                    else {"type": "streamable-http", "url": "https://example.org/mcp"}
                },
            }
        )
    )
    installed = await owner.plugins.install_local_plugin(
        InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 10),
        connections=owner.mcp,
    )
    assert installed.disposition.value == "INSTALLED", installed
    return installed


def test_local_model_preparation_uses_host_workspace_and_never_writes(tmp_path):
    async def run():
        owner = preparation(tmp_path)
        result = await owner.prepare(
            {
                "action": "ADD_LOCAL_MCP",
                "scope": "WORKSPACE",
                "server_id": "example",
                "config": {
                    "transport": {
                        "type": "streamable_http",
                        "endpoint": "https://example.org/mcp",
                    }
                },
            }
        )
        assert result.local_mutation.target.workspace_root == owner.workspace_root
        assert not result.user_inputs and not result.effects.outside_workspace_write
        assert not owner.mcp.path(result.local_mutation.target).exists()
        assert thaw_json(result.expected_current) == {}
        await owner.mcp.aclose()

    asyncio.run(run())


def test_missing_local_configuration_is_a_form_draft_not_a_fake_endpoint(tmp_path):
    async def run():
        owner = preparation(tmp_path)
        result = await owner.prepare(
            {
                "action": "ADD_LOCAL_MCP",
                "scope": "USER",
                "server_id": "example",
            }
        )
        assert result.user_inputs == ("connection_configuration",)
        assert result.local_mutation is None
        assert thaw_json(result.public_prefill)["config"] == {}
        assert not owner.mcp.user_config_path.exists()
        await owner.mcp.aclose()

    asyncio.run(run())


def test_edit_draft_freezes_current_guard_and_rejects_explicit_stale_value(tmp_path):
    async def run():
        owner = preparation(tmp_path)
        target = LocalMcpTarget("example")
        await owner.mcp.create(
            target,
            {
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.org/mcp",
                }
            },
        )
        args = {"action": "UPDATE_LOCAL_MCP", "scope": "USER", "server_id": "example"}
        result = await owner.prepare(args)
        assert thaw_json(result.expected_current)["expected_identity"]
        assert (
            thaw_json(result.public_prefill)["config"]["transport"]["endpoint"]
            == "https://example.org/mcp"
        )
        with pytest.raises(McpManagementConflict):
            await owner.prepare({**args, "expected_identity": "stale"})
        with pytest.raises(McpManagementConflict):
            await owner.prepare({**args, "scope": "WORKSPACE"})
        await owner.mcp.aclose()

    asyncio.run(run())


def test_authorization_requires_saved_oauth_but_never_launches_browser_in_prepare(
    tmp_path, monkeypatch
):
    async def run():
        owner = preparation(tmp_path)
        target = LocalMcpTarget("example")
        await owner.mcp.create(
            target,
            {
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.org/mcp",
                },
                "auth": {"type": "oauth"},
            },
        )

        async def forbidden(*args, **kwargs):
            pytest.fail("preparation must not start OAuth")

        monkeypatch.setattr(owner.mcp, "authorize", forbidden)
        result = await owner.prepare(
            {"action": "AUTHORIZE_MCP", "scope": "USER", "server_id": "example"}
        )
        assert result.user_inputs == ("browser_authorization",)
        assert (
            result.effects.outside_workspace_write
            and not result.effects.process_control
        )
        await owner.mcp.aclose()

    asyncio.run(run())


def test_plugin_prepare_freezes_exact_package_and_cannot_select_inherited_instance(
    tmp_path,
):
    async def run():
        owner = preparation(tmp_path)
        installed = await install_plugin(owner, tmp_path)
        args = {
            "action": "SET_PLUGIN_ENABLED",
            "scope": "USER",
            "plugin_id": "example",
            "enabled": True,
        }
        prepared = await owner.prepare(args)
        assert prepared.plugin.package_install_id == installed.package_install_id
        assert not prepared.plugin.enabled
        assert prepared.user_inputs == ("plugin_enable_review",)
        assert (
            prepared.effects.outside_workspace_write
            and not prepared.effects.process_control
        )
        assert thaw_json(prepared.expected_current) == {
            "expected_package_install_id": installed.package_install_id
        }
        with pytest.raises(McpManagementConflict):
            await owner.prepare({**args, "expected_package_install_id": "stale"})
        with pytest.raises(McpManagementConflict):
            await owner.prepare({**args, "scope": "WORKSPACE"})
        removal = await owner.prepare(
            {"action": "REMOVE_PLUGIN", "scope": "USER", "plugin_id": "example"}
        )
        assert removal.effects.destructive and not removal.user_inputs
        await owner.mcp.aclose()

    asyncio.run(run())


def test_plugin_connection_missing_key_is_user_input_not_package_rejection(tmp_path):
    async def run():
        owner = preparation(tmp_path)
        installed = await install_plugin(owner, tmp_path)
        binding = McpCredentialBinding(
            plugin_connection_owner(installed.identity, "docs"), "bearer"
        )
        overlay = PluginMcpConnectionOverlay(
            "docs",
            "streamable_http",
            auth=BearerSecret(ManagedLocalCredentialReference(binding)),
        )
        args = {
            "action": "CONFIGURE_PLUGIN_MCP_CONNECTION",
            "scope": "USER",
            "plugin_id": "example",
            "server_id": "docs",
            "overlay": overlay_to_dict(overlay),
            "expected_overlay": None,
        }
        prepared = await owner.prepare(args)
        assert prepared.user_inputs == ("credential:bearer",)
        assert prepared.effects.outside_workspace_write
        assert thaw_json(prepared.expected_current)["expected_overlay"] is None
        assert prepared.plugin.mcp_connection_overlays == ()
        with pytest.raises(McpManagementConflict):
            await owner.prepare({**args, "expected_overlay": overlay_to_dict(overlay)})
        with pytest.raises(ValueError):
            await owner.prepare({**args, "server_id": "foreign"})
        await owner.mcp.aclose()

    asyncio.run(run())


def test_stdio_plugin_connection_draft_keeps_immutable_executable_definition(tmp_path):
    async def run():
        owner = preparation(tmp_path)
        await install_plugin(owner, tmp_path, stdio=True)
        prepared = await owner.prepare(
            {
                "action": "CONFIGURE_PLUGIN_MCP_CONNECTION",
                "scope": "USER",
                "plugin_id": "example",
                "server_id": "docs",
            }
        )
        assert prepared.user_inputs == ("connection_configuration",)
        assert (
            thaw_json(prepared.public_prefill)["config"]["transport"]["command"]
            == "node"
        )
        assert (
            not prepared.effects.process_control
        )  # Installed disabled; no process will be restarted.
        enabled = await owner.prepare(
            {
                "action": "SET_PLUGIN_ENABLED",
                "scope": "USER",
                "plugin_id": "example",
                "enabled": True,
            }
        )
        assert enabled.effects.process_control
        await owner.mcp.aclose()

    asyncio.run(run())
