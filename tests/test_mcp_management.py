from __future__ import annotations

import asyncio

import pytest

from pulsara_agent.capability.mcp_management import (
    LocalMcpManagementService,
    LocalMcpTarget,
    McpSecretMutation,
    McpManagementConflict,
    config_guard,
    config_to_entry,
)
from pulsara_agent.mcp_credentials import (
    McpCredentialBinding,
    ManagedLocalCredentialReference,
    secret_to_dict,
)
from pulsara_agent.settings import LocalSettingsStore


def test_connection_reports_schema_limit_separately_from_authentication(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from pulsara_agent.conversation_kernel.mcp import sdk_facade, supervisor
    from pulsara_agent.conversation_kernel.mcp.wire import McpSchemaBoundExceeded

    async def run():
        client = AsyncMock()
        monkeypatch.setattr(sdk_facade, "BoundedMcpSdkClient", lambda *args, **kwargs: client)
        monkeypatch.setattr(supervisor, "discover_mcp_catalog", AsyncMock(
            side_effect=McpSchemaBoundExceeded("remote text must not be reflected")
        ))
        service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"), user_config_path=tmp_path / "mcp.yaml")
        try:
            result = await service.test(LocalMcpTarget("schema-test"), {
                "transport": {"type": "streamable_http", "endpoint": "https://example.org/mcp"},
            }, workspace_root=tmp_path)
            assert result.status == "schema_bound_exceeded"
            assert result.tools == 0
            assert "remote text" not in repr(result)
            client.open.assert_awaited_once()
            client.aclose.assert_awaited_once()
            assert not (tmp_path / "mcp.yaml").exists()
        finally:
            await service.aclose()

    asyncio.run(run())


def test_preparation_derives_physical_effects_and_does_not_hold_private_values(
    tmp_path,
):
    from pulsara_agent.primitives.context import thaw_json

    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        service = LocalMcpManagementService(
            LocalSettingsStore(tmp_path / "private" / "local-settings.yaml"),
            user_config_path=tmp_path / "mcp.yaml",
        )
        entry = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://example.org/mcp",
            }
        }
        target = LocalMcpTarget("test", workspace)
        prepared = await service.prepare_mutation("ADD_LOCAL_MCP", target, entry)
        assert prepared.expected is None and prepared.effects.workspace_write
        assert (
            not prepared.effects.outside_workspace_write
            and not prepared.effects.process_control
        )
        assert not service.path(target).exists()
        binding = McpCredentialBinding(target.owner, "bearer")
        private_entry = {
            **entry,
            "auth": {
                "type": "bearer",
                "reference": secret_to_dict(ManagedLocalCredentialReference(binding)),
            },
        }
        prepared = await service.prepare_mutation(
            "ADD_LOCAL_MCP", target, private_entry
        )
        assert (
            prepared.effects.outside_workspace_write
            and prepared.missing_credentials == ("bearer",)
        )
        created = await service.create(
            target,
            private_entry,
            (McpSecretMutation(binding, "preparation-private-token"),),
        )
        edited = await service.prepare_mutation(
            "UPDATE_LOCAL_MCP", target, {**private_entry, "display_name": "New title"}
        )
        assert edited.expected == config_guard(created.config)
        assert not edited.effects.outside_workspace_write
        assert not edited.missing_credentials
        assert "preparation-private-token" not in repr(edited)
        assert thaw_json(edited.entry)["display_name"] == "New title"
        changed_destination = await service.prepare_mutation(
            "UPDATE_LOCAL_MCP",
            target,
            {
                **private_entry,
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://other.example.org/mcp",
                },
            },
        )
        assert changed_destination.requires_destination_confirmation
        removed = await service.prepare_mutation("REMOVE_LOCAL_MCP", target)
        assert removed.effects.outside_workspace_write and removed.entry is None
        with pytest.raises(McpManagementConflict):
            await service.prepare_mutation(
                "UPDATE_LOCAL_MCP", target, private_entry, expected="stale"
            )
        with pytest.raises(McpManagementConflict):
            await service.prepare_mutation("ADD_LOCAL_MCP", target, entry)
        with pytest.raises(ValueError):
            await service.prepare_mutation("REMOVE_LOCAL_MCP", target, entry)
        await service.aclose()

    asyncio.run(run())


def test_keyless_workspace_removal_does_not_write_user_settings(tmp_path):
    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        def fail_writer(*_):
            pytest.fail("pure workspace edit must not write private user settings")

        service = LocalMcpManagementService(
            LocalSettingsStore(
                tmp_path / "private" / "local-settings.yaml", writer=fail_writer
            ),
            user_config_path=tmp_path / "mcp.yaml",
        )
        target = LocalMcpTarget("test", workspace)
        created = await service.create(
            target,
            {
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.org/mcp",
                }
            },
        )
        prepared = await service.prepare_mutation("REMOVE_LOCAL_MCP", target)
        assert not prepared.effects.outside_workspace_write
        outcome = await service.remove(target, expected=config_guard(created.config))
        assert outcome.applied and not outcome.cleanup_attention
        await service.aclose()

    asyncio.run(run())


def test_prepared_dispatch_rejects_new_private_cleanup_effect_before_writing(tmp_path):
    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        service = LocalMcpManagementService(
            LocalSettingsStore(tmp_path / "private" / "local-settings.yaml"),
            user_config_path=tmp_path / "mcp.yaml",
        )
        target = LocalMcpTarget("test", workspace)
        entry = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://example.org/mcp",
            }
        }
        created = await service.create(target, entry)
        prepared = await service.prepare_mutation("REMOVE_LOCAL_MCP", target)
        assert not prepared.effects.outside_workspace_write
        binding = McpCredentialBinding(target.owner, "bearer")
        await service.settings.replace_mcp_secrets(
            target.owner, ((binding, "private-value"),)
        )
        with pytest.raises(McpManagementConflict, match="preparation changed"):
            await service.execute_prepared_mutation(prepared)
        assert config_guard(service.inspect(target)) == config_guard(created.config)
        assert service.settings.resolve_mcp_secret(binding) == "private-value"
        fresh = await service.prepare_mutation("REMOVE_LOCAL_MCP", target)
        assert fresh.effects.outside_workspace_write
        assert (await service.execute_prepared_mutation(fresh)).applied
        assert service.settings.resolve_mcp_secret(binding) is None
        await service.aclose()

    asyncio.run(run())


def test_repeated_caller_cancellation_still_joins_invalidated_login(
    tmp_path, monkeypatch
):
    async def run():
        service = LocalMcpManagementService(
            LocalSettingsStore(tmp_path / "private" / "local-settings.yaml"),
            user_config_path=tmp_path / "mcp.yaml",
        )
        target = LocalMcpTarget("test")
        created = await service.create(
            target,
            {
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.org/mcp",
                }
            },
        )
        started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def login():
            try:
                started.set()
                await asyncio.Future()
            finally:
                closing.set()
                await release.wait()

        login_task = asyncio.create_task(login())
        await started.wait()

        def invalidate(owner):
            assert owner == target.owner
            login_task.cancel()
            return (login_task,)

        monkeypatch.setattr(service.oauth, "invalidate", invalidate)
        update = asyncio.create_task(
            service.update(
                target,
                {
                    "transport": {
                        "type": "streamable_http",
                        "endpoint": "https://next.example.org/mcp",
                    }
                },
                expected=config_guard(created.config),
            )
        )
        await closing.wait()
        # The write can finish while the old browser/HTTP owner is draining.
        async with asyncio.timeout(2):
            while (
                service.inspect(target).transport.endpoint
                != "https://next.example.org/mcp"
            ):
                await asyncio.sleep(0)
        for _ in range(2):
            update.cancel()
            await asyncio.sleep(0)
            assert not update.done() and not login_task.done()
        release.set()
        result = await update
        assert result.applied and not result.cleanup_attention
        assert login_task.done()
        assert not service.lane.locked()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [True, False])
def test_preparation_projects_only_real_stdio_adoption_effects(tmp_path, enabled):
    async def run():
        service = LocalMcpManagementService(
            LocalSettingsStore(tmp_path / "private" / "local-settings.yaml"),
            user_config_path=tmp_path / "mcp.yaml",
        )
        prepared = await service.prepare_mutation(
            "ADD_LOCAL_MCP",
            LocalMcpTarget("test"),
            {
                "transport": {
                    "type": "stdio",
                    "command": "node",
                    "args": ["server.js"],
                },
                "enabled": enabled,
            },
        )
        assert prepared.effects.process_control == enabled
        assert prepared.effects.outside_workspace_write
        await service.aclose()

    asyncio.run(run())


def test_whole_entry_edit_delete_and_private_cleanup(tmp_path):
    async def scenario():
        service = LocalMcpManagementService(
            LocalSettingsStore(tmp_path / "private" / "local-settings.yaml"),
            user_config_path=tmp_path / "mcp.yaml",
        )
        target = LocalMcpTarget("test")
        binding = McpCredentialBinding(target.owner, "bearer")
        entry = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://example.org/mcp",
            },
            "auth": {
                "type": "bearer",
                "reference": secret_to_dict(ManagedLocalCredentialReference(binding)),
            },
        }
        created = await service.create(
            target, entry, (McpSecretMutation(binding, "private-token"),)
        )
        assert created.applied and not created.cleanup_attention
        assert "private-token" not in service.user_config_path.read_text()
        assert (
            created.config.resolved_headers()["Authorization"] == "Bearer private-token"
        )
        first_guard = config_guard(created.config)
        edited = await service.update(
            target,
            {**config_to_entry(created.config), "display_name": "Edited"},
            expected=first_guard,
        )
        assert service.inspect(target).display_name == "Edited"
        assert service.settings.resolve_mcp_secret(binding) == "private-token"
        with pytest.raises(McpManagementConflict):
            await service.remove(target, expected=first_guard)
        anonymous = await service.update(
            target,
            {**config_to_entry(edited.config), "auth": {"type": "none"}},
            expected=config_guard(edited.config),
        )
        assert service.settings.resolve_mcp_secret(binding) is None
        removed = await service.remove(target, expected=config_guard(anonymous.config))
        assert removed.applied and removed.config is None
        assert service.inspect(target) is None

    asyncio.run(scenario())


def test_disposable_connection_test_uses_unsaved_secret_and_never_publishes(tmp_path):
    from aiohttp import web

    async def scenario():
        calls = []

        async def serve(request):
            if request.method != "POST":
                return web.Response(status=405)
            assert request.headers["Authorization"] == "Bearer transient-private"
            value = await request.json()
            calls.append(value["method"])
            if "id" not in value:
                return web.Response(status=202)
            if value["method"] == "server/discover":
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": value["id"],
                        "error": {"code": -32601, "message": "legacy initialize"},
                    }
                )
            result = {
                "initialize": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "disposable", "version": "1"},
                },
                "tools/list": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Test tool",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }[value["method"]]
            return web.json_response(
                {"jsonrpc": "2.0", "id": value["id"], "result": result}
            )

        app = web.Application()
        app.router.add_route("*", "/mcp", serve)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        service = LocalMcpManagementService(
            LocalSettingsStore(tmp_path / "private.yaml"),
            user_config_path=tmp_path / "mcp.yaml",
        )
        target = LocalMcpTarget("test")
        binding = McpCredentialBinding(target.owner, "bearer")
        entry = {
            "enabled": False,
            "transport": {
                "type": "streamable_http",
                "endpoint": f"http://127.0.0.1:{port}/mcp",
                "allow_http_localhost": True,
            },
            "auth": {
                "type": "bearer",
                "reference": secret_to_dict(ManagedLocalCredentialReference(binding)),
            },
        }
        try:
            outcome = await service.test(
                target,
                entry,
                workspace_root=tmp_path,
                secrets=(McpSecretMutation(binding, "transient-private"),),
            )
            assert outcome.status == "ready"
            assert outcome.tools == 1
            assert calls == [
                "server/discover",
                "initialize",
                "notifications/initialized",
                "tools/list",
            ]
            assert not (tmp_path / "mcp.yaml").exists()
            assert not (tmp_path / "private.yaml").exists()
            assert service.settings.resolve_mcp_secret(binding) is None
        finally:
            await service.aclose()
            await runner.cleanup()

    asyncio.run(scenario())


def test_retained_credentials_new_destination_needs_confirmation_in_shared_owner(
    tmp_path,
):
    async def scenario():
        service = LocalMcpManagementService(
            LocalSettingsStore(tmp_path / "private.yaml"),
            user_config_path=tmp_path / "mcp.yaml",
        )
        target = LocalMcpTarget("test")
        binding = McpCredentialBinding(target.owner, "bearer")
        entry = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://old.example/mcp",
            },
            "auth": {
                "type": "bearer",
                "reference": secret_to_dict(ManagedLocalCredentialReference(binding)),
            },
        }
        initial = await service.create(
            target, entry, (McpSecretMutation(binding, "private"),)
        )
        entry["transport"]["endpoint"] = "https://new.example/mcp"
        with pytest.raises(ValueError, match="确认"):
            await service.update(target, entry, expected=config_guard(initial.config))
        with pytest.raises(ValueError, match="确认"):
            await service.test(target, entry, workspace_root=tmp_path)
        assert service.inspect(target).transport.endpoint == "https://old.example/mcp"
        updated = await service.update(
            target,
            entry,
            expected=config_guard(initial.config),
            retain_credentials_confirmed=True,
        )
        assert updated.config.transport.endpoint == "https://new.example/mcp"
        assert service.settings.resolve_mcp_secret(binding) == "private"
        await service.aclose()

    asyncio.run(scenario())


def test_secret_reference_cannot_read_another_connection(tmp_path):
    async def scenario():
        service = LocalMcpManagementService(
            LocalSettingsStore(tmp_path / "local-settings.yaml"),
            user_config_path=tmp_path / "mcp.yaml",
        )
        target = LocalMcpTarget("first")
        binding = McpCredentialBinding(LocalMcpTarget("other").owner, "bearer")
        with pytest.raises(ValueError, match="crosses"):
            await service.create(
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
            )
        assert not service.user_config_path.exists()

    asyncio.run(scenario())
