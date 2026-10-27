import asyncio
import json
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.capability_management import (
    CapabilityManagementPreparation,
)
from pulsara_agent.capability.local_skill_management import LocalSkillManagementService
from pulsara_agent.capability.pulsara_home import (
    UserHomeResolution,
    PulsaraHomeDisposition,
)
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.capability.mcp_management import (
    LocalMcpManagementService,
    LocalMcpTarget,
    McpManagementConflict,
    McpSecretMutation,
    config_guard,
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
    binding_to_dict,
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
        skills=LocalSkillManagementService(
            pulsara_home_resolution=resolve_pulsara_home(str(tmp_path / "private")),
            user_home_resolution=UserHomeResolution(
                PulsaraHomeDisposition.RESOLVED, tmp_path / "user-home"
            ),
        ),
        hooks=LocalHookSourceProvider(
            workspace_root=workspace,
            workspace_kind="project",
            workspace_state_key=str(workspace),
            pulsara_home=tmp_path / "private",
        ),
        deadline=lambda: monotonic() + 5,
    )


async def install_plugin(owner, tmp_path, *, stdio=False, hooks=None):
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
    if hooks is not None:
        hook_path = source / "dev.pulsara" / "hooks" / "hooks.json"
        hook_path.parent.mkdir(parents=True)
        hook_path.write_text(json.dumps(hooks))
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
                "config": {
                    "transport": {
                        "type": "streamable_http",
                        "endpoint": "https://example.org/mcp",
                    }
                },
                "target": {
                    "kind": "MCP_CONNECTION",
                    "scope": "WORKSPACE",
                    "source_kind": "LOCAL",
                    "server_id": "example",
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
                "target": {
                    "kind": "MCP_CONNECTION",
                    "scope": "USER",
                    "source_kind": "LOCAL",
                    "server_id": "example",
                },
            }
        )
        assert result.user_inputs == ("connection_configuration",)
        assert result.local_mutation is None
        assert thaw_json(result.public_prefill)["config"] == {}
        assert not owner.mcp.user_config_path.exists()
        await owner.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("destination_changed", [True, False])
def test_model_secret_template_update_requires_confirmation_only_for_new_destination(
    tmp_path, destination_changed
):
    from pulsara_agent.conversation_kernel.capability_management_execution import (
        CapabilityManagementCall,
    )

    async def run():
        owner = preparation(tmp_path)
        target = LocalMcpTarget("example")
        binding = McpCredentialBinding(target.owner, "bearer")
        reference = {"source": "managed", "binding": binding_to_dict(binding)}
        original = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://old.example/mcp",
            },
            "auth": {"type": "bearer", "reference": reference},
        }
        try:
            created = await owner.mcp.create(
                target,
                original,
                (McpSecretMutation(binding, "retained-test-credential"),),
            )
            arguments = json.loads(
                json.dumps(
                    {
                        "action": "UPDATE_LOCAL_MCP",
                        "expected_identity": config_guard(created.config),
                        "config": {
                            "transport": {
                                **original["transport"],
                                "endpoint": "https://new.example/mcp"
                                if destination_changed
                                else "https://old.example/mcp",
                            },
                            "auth": {
                                "type": "bearer",
                                "reference": {"parts": [reference, "x"]},
                            },
                        },
                        "target": {
                            "kind": "MCP_CONNECTION",
                            "scope": "USER",
                            "source_kind": "LOCAL",
                            "server_id": "example",
                        },
                    }
                )
            )
            prepared = await owner.prepare(arguments)
            call = CapabilityManagementCall(owner, prepared)
            if destination_changed:
                assert prepared.user_inputs == ("credential_destination_confirmation",)
                with pytest.raises(ValueError, match="requires user input"):
                    await call.execute()
                assert config_guard(owner.mcp.inspect(target)) == config_guard(
                    created.config
                )
            else:
                assert prepared.user_inputs == ()
                assert (await call.execute())["status"] == "APPLIED"
                assert owner.mcp.inspect(target).resolved_headers() == {
                    "Authorization": "Bearer retained-test-credentialx"
                }
        finally:
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
        args = {
            "action": "UPDATE_LOCAL_MCP",
            "target": {
                "kind": "MCP_CONNECTION",
                "scope": "USER",
                "source_kind": "LOCAL",
                "server_id": "example",
            },
        }
        result = await owner.prepare(args)
        assert thaw_json(result.expected_current)["expected_identity"]
        assert (
            thaw_json(result.public_prefill)["config"]["transport"]["endpoint"]
            == "https://example.org/mcp"
        )
        with pytest.raises(McpManagementConflict):
            await owner.prepare({**args, "expected_identity": "stale"})
        with pytest.raises(McpManagementConflict):
            await owner.prepare(
                {**args, "target": {**args["target"], "scope": "WORKSPACE"}}
            )
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
            {
                "action": "AUTHORIZE_MCP",
                "target": {
                    "kind": "MCP_CONNECTION",
                    "scope": "USER",
                    "source_kind": "LOCAL",
                    "server_id": "example",
                },
            }
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
            "enabled": True,
            "target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"},
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
            await owner.prepare(
                {**args, "target": {**args["target"], "scope": "WORKSPACE"}}
            )
        removal = await owner.prepare(
            {
                "action": "REMOVE_PLUGIN",
                "target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"},
            }
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
            "overlay": overlay_to_dict(overlay),
            "expected_overlay": None,
            "target": {
                "kind": "MCP_CONNECTION",
                "scope": "USER",
                "source_kind": "PLUGIN",
                "plugin_id": "example",
                "server_id": "docs",
            },
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
                "target": {
                    "kind": "MCP_CONNECTION",
                    "scope": "USER",
                    "source_kind": "PLUGIN",
                    "plugin_id": "example",
                    "server_id": "docs",
                },
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
                "enabled": True,
                "target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"},
            }
        )
        assert enabled.effects.process_control
        await owner.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("config", [{"hooks": {}}, {"hooks": {"Unsupported": []}}])
def test_hook_approval_requires_nonempty_normalized_definitions(tmp_path, config):
    async def run():
        owner = preparation(tmp_path)
        path = owner.hooks.user_config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config))
        try:
            with pytest.raises(ValueError, match="complete current Hook definitions"):
                await owner.prepare(
                    {
                        "action": "TRUST_HOOK_SOURCE",
                        "target": {
                            "kind": "HOOK_SOURCE",
                            "scope": "USER",
                            "source_kind": "LOCAL",
                        },
                    }
                )
        finally:
            await owner.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("reset_overlay", [False, True])
def test_plugin_connection_form_keeps_target_and_reprepares_native_overlay(
    tmp_path, reset_overlay
):
    from pulsara_agent.capability.management_form import (
        AcceptedCapabilityFormSubmission,
    )
    from pulsara_agent.conversation_kernel.capability_management_execution import (
        CapabilityManagementCall,
    )

    async def run():
        owner = preparation(tmp_path)
        await install_plugin(owner, tmp_path)
        target = {
            "kind": "MCP_CONNECTION",
            "scope": "USER",
            "source_kind": "PLUGIN",
            "plugin_id": "example",
            "server_id": "docs",
        }
        call = CapabilityManagementCall(
            owner,
            await owner.prepare(
                {"action": "CONFIGURE_PLUGIN_MCP_CONNECTION", "target": target}
            ),
        )
        form = call.form("configure")
        assert thaw_json(form.public_projection)["prefill"]["target"] == target
        with pytest.raises(ValueError, match="unsupported capability submission"):
            await form.prepare_submission(
                {"target": {**target, "server_id": "foreign"}, "overlay": None}
            )
        overlay = (
            None
            if reset_overlay
            else overlay_to_dict(
                PluginMcpConnectionOverlay(
                    "docs", "streamable_http", endpoint="https://other.example/mcp"
                )
            )
        )
        values = await form.prepare_submission({"overlay": overlay})
        assert thaw_json(values.public_fields)["target"] == target
        assert thaw_json(values.public_fields)["expected_overlay"] is None
        call.accept(AcceptedCapabilityFormSubmission(values))
        result = await call.execute()
        assert result["status"] == "APPLIED" and result["target"] == target
        assert "scope" not in result and "identity" not in result
        plugin = await owner._plugin(None, "example")
        assert (
            (not plugin.mcp_connection_overlays)
            if reset_overlay
            else plugin.mcp_connection_overlays[0].endpoint
            == "https://other.example/mcp"
        )
        await owner.mcp.aclose()

    asyncio.run(run())
