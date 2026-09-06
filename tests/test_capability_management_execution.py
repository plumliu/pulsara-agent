import asyncio

import pytest

from tests.test_capability_management_preparation import preparation, install_plugin
from pulsara_agent.capability.management_form import AcceptedCapabilityFormSubmission
from pulsara_agent.capability.mcp_management import (
    LocalMcpTarget,
    McpManagementConflict,
)
from pulsara_agent.conversation_kernel.capability_management_execution import (
    CapabilityManagementCall,
)


def test_direct_call_is_consumed_once_and_uses_canonical_local_owner(tmp_path):
    async def run():
        service = preparation(tmp_path)
        args = {
            "action": "ADD_LOCAL_MCP",
            "scope": "WORKSPACE",
            "server_id": "docs",
            "config": {
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.org/mcp",
                }
            },
        }
        call = CapabilityManagementCall(service, await service.prepare(args))
        assert not service.mcp.path(
            LocalMcpTarget("docs", service.workspace_root)
        ).exists()
        result = await call.execute()
        assert result["status"] == "APPLIED"
        assert (
            service.mcp.inspect(LocalMcpTarget("docs", service.workspace_root))
            is not None
        )
        with pytest.raises(RuntimeError):
            await call.execute()
        with pytest.raises(RuntimeError):
            call.form("again")
        await service.mcp.aclose()

    asyncio.run(run())


def test_form_validation_does_not_write_and_executes_user_candidate(tmp_path):
    async def run():
        service = preparation(tmp_path)
        args = {"action": "ADD_LOCAL_MCP", "scope": "USER", "server_id": "docs"}
        call = CapabilityManagementCall(service, await service.prepare(args))
        form = call.form("需要用户配置")
        with pytest.raises(RuntimeError):
            await call.execute()
        with pytest.raises(ValueError):
            await form.prepare_submission({"server_id": "foreign"})
        config = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://user.example.org/mcp",
            }
        }
        values = await form.prepare_submission({"config": config})
        assert not service.mcp.user_config_path.exists()
        accepted = AcceptedCapabilityFormSubmission(values)
        call.accept(accepted)
        outcome = await call.execute()
        assert (
            outcome["current"]["config"]["transport"]["endpoint"]
            == "https://user.example.org/mcp"
        )
        with pytest.raises(RuntimeError):
            accepted.take()
        await service.mcp.aclose()

    asyncio.run(run())


def test_form_never_replaces_old_expected_guard_with_fresh_inspection(tmp_path):
    async def run():
        service = preparation(tmp_path)
        target = LocalMcpTarget("docs")
        config = {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://example.org/mcp",
            }
        }
        original = await service.mcp.create(target, config)
        call = CapabilityManagementCall(
            service,
            await service.prepare(
                {"action": "UPDATE_LOCAL_MCP", "scope": "USER", "server_id": "docs"}
            ),
        )
        form = call.form("确认")
        from pulsara_agent.capability.mcp_management import config_guard

        await service.mcp.update(
            target, {**config, "enabled": False}, expected=config_guard(original.config)
        )
        with pytest.raises(McpManagementConflict):
            await form.prepare_submission({"config": config})
        assert not service.mcp.inspect(target).enabled
        call.discard()
        await service.mcp.aclose()

    asyncio.run(run())


def test_plugin_enable_always_uses_user_review_then_guarded_existing_owner(tmp_path):
    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path)
        prepared = await service.prepare(
            {
                "action": "SET_PLUGIN_ENABLED",
                "scope": "USER",
                "plugin_id": "example",
                "enabled": True,
            }
        )
        direct = CapabilityManagementCall(service, prepared)
        with pytest.raises(ValueError, match="user input"):
            await direct.execute()
        call = CapabilityManagementCall(service, prepared)
        form = call.form("review")
        from pulsara_agent.primitives.context import thaw_json
        shown = thaw_json(form.public_projection)["plugin"]["mcp"][0]
        assert shown["server_id"] == "docs"
        assert shown["config"]["transport"]["endpoint"] == "https://example.org/mcp"
        assert shown["credentials"] == []
        with pytest.raises(ValueError, match="review"):
            await form.prepare_submission({})
        values = await form.prepare_submission({"enable_review_accepted": True})
        call.accept(AcceptedCapabilityFormSubmission(values))
        outcome = await call.execute()
        assert outcome["status"] == "APPLIED", outcome
        assert outcome["current"]["operation_status"] == "ENABLED"
        await service.mcp.aclose()

    asyncio.run(run())
