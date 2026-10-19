"""Public capability forms share the canonical credential destination rule."""

import asyncio

import pytest

from pulsara_agent.capability.management_form import AcceptedCapabilityFormSubmission
from pulsara_agent.capability.mcp_management import LocalMcpTarget, McpSecretMutation
from pulsara_agent.conversation_kernel.capability_management_execution import (
    CapabilityManagementCall,
)
from pulsara_agent.mcp_credentials import (
    ManagedLocalCredentialReference,
    McpCredentialBinding,
    binding_to_dict,
    secret_to_dict,
)
from pulsara_agent.plugins.connection_management import (
    ReplacePluginMcpConnectionRequest,
)
from pulsara_agent.plugins.mcp_connection import (
    overlay_from_dict,
    plugin_connection_owner,
)
from tests.test_capability_management_preparation import install_plugin, preparation


@pytest.mark.parametrize("source", ["local", "plugin"])
@pytest.mark.parametrize("replace_all", [True, False])
def test_public_form_applies_new_secrets_but_requires_confirmation_for_retained_ones(
    tmp_path, source, replace_all
):
    async def scenario():
        service = preparation(tmp_path)
        try:
            target = LocalMcpTarget("docs")
            installed = (
                await install_plugin(service, tmp_path) if source == "plugin" else None
            )
            owner = (
                plugin_connection_owner(installed.identity, "docs")
                if installed
                else target.owner
            )
            bindings = tuple(
                McpCredentialBinding(owner, "header:" + name)
                for name in ("X-First", "X-Second")
            )
            auth = {
                "type": "static_headers",
                "headers": {
                    name: secret_to_dict(ManagedLocalCredentialReference(binding))
                    for name, binding in zip(
                        ("X-First", "X-Second"), bindings, strict=True
                    )
                },
            }
            old_secrets = tuple(
                McpSecretMutation(binding, "old-" + str(index))
                for index, binding in enumerate(bindings)
            )
            if installed:
                original = {
                    "local_server_id": "docs",
                    "transport_kind": "streamable_http",
                    "endpoint": "https://old.example/mcp",
                    "public_headers": {},
                    "environment": {},
                    "secret_environment": {},
                    "auth": auth,
                }
                await service.plugins.replace_plugin_mcp_connection_overlay(
                    ReplacePluginMcpConnectionRequest(
                        installed.identity,
                        "docs",
                        installed.package_install_id,
                        None,
                        overlay_from_dict(original),
                        service.deadline(),
                        old_secrets,
                    ),
                    connections=service.mcp,
                )
                arguments = {
                    "action": "CONFIGURE_PLUGIN_MCP_CONNECTION",
                    "scope": "USER",
                    "plugin_id": installed.identity.plugin_id,
                    "server_id": "docs",
                }
                replacement = {**original, "endpoint": "https://new.example/mcp"}
                body = {"overlay": replacement}
            else:
                original = {
                    "transport": {
                        "type": "streamable_http",
                        "endpoint": "https://old.example/mcp",
                    },
                    "auth": auth,
                }
                await service.mcp.create(target, original, old_secrets)
                arguments = {
                    "action": "UPDATE_LOCAL_MCP",
                    "scope": "USER",
                    "server_id": "docs",
                }
                body = {
                    "config": {
                        **original,
                        "transport": {
                            **original["transport"],
                            "endpoint": "https://new.example/mcp",
                        },
                    }
                }
            changes = bindings if replace_all else bindings[:1]
            body["secret_changes"] = [
                {"binding": binding_to_dict(binding), "value": "new-" + str(index)}
                for index, binding in enumerate(changes)
            ]
            call = CapabilityManagementCall(service, await service.prepare(arguments))
            form = call.form("edit connection")
            if not replace_all:
                with pytest.raises(ValueError, match="确认|confirm"):
                    await form.prepare_submission(body)
                body["retain_credentials_confirmed"] = True
            values = await form.prepare_submission(body)
            # Private input validation must never publish configuration or secrets.
            assert tuple(
                service.mcp.settings.resolve_mcp_secret(binding) for binding in bindings
            ) == ("old-0", "old-1")
            call.accept(AcceptedCapabilityFormSubmission(values))
            assert (await call.execute())["status"] == "APPLIED"
            assert tuple(
                service.mcp.settings.resolve_mcp_secret(binding) for binding in bindings
            ) == (
                "new-0",
                "new-1" if replace_all else "old-1",
            )
            if installed:
                plugin = await service._plugin(None, installed.identity.plugin_id)
                server = plugin.summary.mcp.mcp_servers[0]
                current = service._connection(plugin, server)
            else:
                current = service.mcp.inspect(target)
            assert current.transport.endpoint == "https://new.example/mcp"
        finally:
            await service.mcp.aclose()

    asyncio.run(scenario())


def test_public_form_rejects_foreign_secret_before_destination_review(tmp_path):
    async def scenario():
        service = preparation(tmp_path)
        target = LocalMcpTarget("docs")
        binding = McpCredentialBinding(target.owner, "bearer")
        foreign = McpCredentialBinding(LocalMcpTarget("other").owner, "bearer")
        original = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://old.example/mcp",
            },
            "auth": {
                "type": "bearer",
                "reference": secret_to_dict(ManagedLocalCredentialReference(binding)),
            },
        }
        try:
            await service.mcp.create(
                target, original, (McpSecretMutation(binding, "old-test-token"),)
            )
            call = CapabilityManagementCall(
                service,
                await service.prepare(
                    {
                        "action": "UPDATE_LOCAL_MCP",
                        "scope": "USER",
                        "server_id": "docs",
                    }
                ),
            )
            form = call.form("edit connection")
            with pytest.raises(ValueError, match="crosses|belong"):
                await form.prepare_submission(
                    {
                        "config": {
                            **original,
                            "transport": {
                                **original["transport"],
                                "endpoint": "https://new.example/mcp",
                            },
                        },
                        "secret_changes": [
                            {
                                "binding": binding_to_dict(foreign),
                                "value": "new-test-token",
                            }
                        ],
                    }
                )
            assert service.mcp.settings.resolve_mcp_secret(binding) == "old-test-token"
            assert service.mcp.settings.resolve_mcp_secret(foreign) is None
        finally:
            await service.mcp.aclose()

    asyncio.run(scenario())
