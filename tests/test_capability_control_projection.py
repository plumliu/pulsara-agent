"""Public edits preserve private owner state and settle known effects truthfully."""

import asyncio
from types import SimpleNamespace
from time import monotonic
import pytest
from pulsara_agent.capability.mcp_management import LocalMcpTarget, McpSecretMutation
from pulsara_agent.capability.management_form import AcceptedCapabilityFormSubmission
from pulsara_agent.conversation_kernel.capability_management_execution import (
    CapabilityManagementCall,
)
from pulsara_agent.mcp_credentials import (
    ManagedLocalCredentialReference,
    McpCredentialBinding,
    secret_to_dict,
)
from tests.support.capability_management_targets import mcp_target
from tests.test_capability_management_preparation import preparation, install_plugin


async def execute(service, arguments):
    return await CapabilityManagementCall(
        service, await service.prepare(arguments)
    ).execute()


def test_local_nested_public_patch_preserves_private_env_and_user_policy(tmp_path):
    async def run():
        service = preparation(tmp_path)
        target = LocalMcpTarget("docs")
        private = secret_to_dict(
            ManagedLocalCredentialReference(
                McpCredentialBinding(target.owner, "env:PRIVATE_TOKEN")
            )
        )
        original = {
            "display_name": "My display",
            "enabled": False,
            "transport": {
                "type": "stdio",
                "command": "node",
                "args": ["server.js"],
                "cwd": "project",
                "env": {"OLD": "old"},
                "secret_env": {"PRIVATE_TOKEN": private},
            },
            "scope_policy": "ROOT_ONLY",
            "default_tool_timeout_ms": 31000,
            "per_tool_timeout_ms": {"read": 3000},
            "catalog_refresh_interval_ms": "DISABLED",
        }
        await service.mcp.create(
            target,
            original,
            (
                McpSecretMutation(
                    McpCredentialBinding(target.owner, "env:PRIVATE_TOKEN"),
                    "private-fixture",
                ),
            ),
        )
        result = await execute(
            service,
            {
                "target": mcp_target(service, "docs"),
                "action": "UPDATE_MCP_CONNECTION",
                "config": {"transport": {"env": {}}},
            },
        )
        assert result.state == "SUCCESS"
        actual = service.mcp.inspect(target)
        assert actual.transport.environment == () and actual.transport.args == (
            "server.js",
        )
        assert actual.transport.secret_environment[0][0] == "PRIVATE_TOKEN"
        assert (
            actual.transport.command == "node"
            and actual.transport.cwd.relative_path == "project"
        )
        assert (
            actual.display_name == "My display"
            and not actual.enabled
            and actual.scope_policy.value == "ROOT_ONLY"
        )
        assert (
            actual.default_tool_timeout_ms == 31000
            and actual.per_tool_timeout_ms == (("read", 3000),)
        )
        assert actual.catalog_refresh_interval_ms is None
        assert result.business_value.public_fields == {
            "target": mcp_target(service, "docs")
        }
        await service.mcp.aclose()

    asyncio.run(run())


def test_public_headers_replace_only_public_map_and_same_auth_keeps_oauth_fields(
    tmp_path,
):
    async def run():
        service = preparation(tmp_path)
        target = LocalMcpTarget("docs")
        original = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://example.org/mcp",
            },
            "public_headers": {"X-Old": "old"},
            "auth": {
                "type": "oauth",
                "client_id": "original-client",
                "scope": "original-scope",
                "redirect_uri": "http://127.0.0.1:34567/callback",
            },
            "scope_policy": "ROOT_ONLY",
            "supports_parallel_tool_calls": True,
        }
        await service.mcp.create(target, original)
        result = await execute(
            service,
            {
                "target": mcp_target(service, "docs"),
                "action": "UPDATE_MCP_CONNECTION",
                "config": {"public_headers": {}, "auth": {"type": "oauth"}},
            },
        )
        assert result.state == "SUCCESS"
        actual = service.mcp.inspect(target)
        assert (
            actual.public_headers == ()
            and actual.auth.client_id == "original-client"
            and actual.auth.scope == "original-scope"
        )
        assert (
            actual.scope_policy.value == "ROOT_ONLY"
            and actual.supports_parallel_tool_calls
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_transport_switch_requires_new_branch_fields_and_does_not_keep_process_secrets(
    tmp_path,
):
    async def run():
        service = preparation(tmp_path)
        target = LocalMcpTarget("docs")
        await service.mcp.create(
            target,
            {"transport": {"type": "stdio", "command": "cat", "env": {"A": "old"}}},
        )
        arguments = {
            "target": mcp_target(service, "docs"),
            "action": "UPDATE_MCP_CONNECTION",
        }
        with pytest.raises(ValueError, match="endpoint"):
            await service.prepare(
                {**arguments, "config": {"transport": {"type": "streamable_http"}}}
            )
        result = await execute(
            service,
            {
                **arguments,
                "config": {
                    "transport": {
                        "type": "streamable_http",
                        "endpoint": "https://example.org/mcp",
                    }
                },
            },
        )
        assert (
            result.state == "SUCCESS"
            and service.mcp.inspect(target).transport.endpoint
            == "https://example.org/mcp"
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_plugin_patch_and_restore_use_the_same_native_overlay_owner(tmp_path):
    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path, stdio=True)
        target = mcp_target(service, "docs", plugin="example")
        initial = await service.prepare(
            {"target": target, "action": "UPDATE_MCP_CONNECTION"}
        )
        call = CapabilityManagementCall(service, initial)
        form = call.form("configure")
        owner = initial.plugin.identity
        from pulsara_agent.plugins.mcp_connection import (
            plugin_connection_owner,
            PluginMcpConnectionOverlay,
            overlay_to_dict,
        )

        binding = McpCredentialBinding(
            plugin_connection_owner(owner, "docs"), "env:TOKEN"
        )
        overlay = PluginMcpConnectionOverlay(
            "docs",
            "stdio",
            environment=(("OLD", "old"),),
            secret_environment=(("TOKEN", ManagedLocalCredentialReference(binding)),),
            scope_policy=__import__(
                "pulsara_agent.mcp_config", fromlist=["McpScopePolicy"]
            ).McpScopePolicy.ROOT_ONLY,
        )
        from pulsara_agent.mcp_credentials import binding_to_dict

        body = {
            "overlay": overlay_to_dict(overlay),
            "secret_changes": [
                {"binding": binding_to_dict(binding), "value": "private-fixture-token"}
            ],
        }
        values = await form.prepare_submission(body)
        call.accept(AcceptedCapabilityFormSubmission(values))
        assert (await call.execute()).state == "SUCCESS"
        result = await execute(
            service,
            {
                "target": target,
                "action": "UPDATE_MCP_CONNECTION",
                "config": {"transport": {"env": {"NEW": "new"}}},
            },
        )
        assert result.state == "SUCCESS"
        actual = (await service._plugin(None, "example")).mcp_connection_overlays[0]
        assert (
            actual.environment == (("NEW", "new"),)
            and actual.secret_environment == overlay.secret_environment
        )
        assert actual.scope_policy.value == "ROOT_ONLY"
        result = await execute(
            service,
            {"target": target, "action": "UPDATE_MCP_CONNECTION", "config": None},
        )
        assert (
            result.state == "SUCCESS"
            and "默认配置" in result.business_value.content[0].value
        )
        plugin = await service._plugin(None, "example")
        assert plugin.mcp_connection_overlays == ()
        assert (
            service._connection(
                plugin, plugin.summary.mcp.mcp_servers[0]
            ).scope_policy.value
            == "ROOT_AND_SUBAGENTS"
        )
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": True},
        {"transport": {"type": "stdio"}},
        {"transport": {"command": "other"}},
        {"transport": {"args": []}},
        {"transport": {"cwd": "."}},
    ],
)
def test_plugin_public_update_cannot_change_package_definition(tmp_path, config):
    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path, stdio=True)
        with pytest.raises(ValueError):
            await service.prepare(
                {
                    "target": mcp_target(service, "docs", plugin="example"),
                    "action": "UPDATE_MCP_CONNECTION",
                    "config": config,
                }
            )
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("adoption_failed", [False, True])
def test_cleanup_failure_keeps_the_actual_change_and_still_adopts_the_runtime(
    tmp_path, monkeypatch, adoption_failed
):
    from pulsara_agent.conversation_kernel.tool_runtime_management import (
        _ToolManagementOperations,
    )
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from pulsara_agent.identity import random_id
    from unittest.mock import AsyncMock

    async def run():
        service = preparation(tmp_path)
        target = LocalMcpTarget("docs")
        binding = McpCredentialBinding(target.owner, "bearer")
        secret = "actual-private-token"
        await service.mcp.create(
            target,
            {
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.org/mcp",
                },
                "auth": {
                    "type": "bearer",
                    "reference": secret_to_dict(
                        ManagedLocalCredentialReference(binding)
                    ),
                },
            },
            (McpSecretMutation(binding, secret),),
        )
        call = CapabilityManagementCall(
            service,
            await service.prepare(
                {
                    "target": mcp_target(service, "docs"),
                    "action": "UPDATE_MCP_CONNECTION",
                    "config": {"auth": {"type": "none"}},
                }
            ),
        )

        async def cleanup(*args, **kwargs):
            raise OSError("cleanup failed for " + secret)

        monkeypatch.setattr(service.mcp.settings, "replace_mcp_secrets", cleanup)
        session, turn, entry, provider = (
            random_id("session"),
            random_id("turn"),
            random_id("entry"),
            "provider-call",
        )
        call.subject = (session, turn, entry, provider)
        adoption = AsyncMock(return_value={
            "status": "PARTIAL" if adoption_failed else "RELOADED",
            "errors": ["actual adoption error " + secret] if adoption_failed else [],
        })
        port = SimpleNamespace(
            _session_id=session,
            _capability_management=service,
            _capability_reload=SimpleNamespace(
                adopt_capability_management_change=adoption
            ),
        )
        context = SimpleNamespace(
            capability_call=call, conversation_scope_kind="ROOT", cell_cancel_event=None
        )
        result = await _ToolManagementOperations._invoke_capability_management(
            port,
            turn_id=turn,
            assistant_entry_id=entry,
            tool_call_id=provider,
            invocation_context=context,
            invocation_started=monotonic(),
            observation_origin=ToolObservationOrigin.BUILTIN,
        )
        assert result.state == "APPLICATION_ERROR"
        assert service.mcp.inspect(target).auth.kind == "none"
        adoption.assert_awaited_once_with(workspace_root=None)
        text = "\n".join(part.value for part in result.content.content)
        assert "已更新" in text and "cleanup failed" in text and secret not in text
        if adoption_failed:
            assert "actual adoption error" in text and "后续能力刷新未完成" in text
        await service.mcp.aclose()

    asyncio.run(run())
