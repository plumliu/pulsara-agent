from tests.support.capability_management_targets import (
    mcp_target,
    plugin_target,
)

import asyncio
import json

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
            "target": mcp_target(service, "docs", scope="WORKSPACE"),
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
        assert result.state == "SUCCESS"
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
        args = {
            "action": "ADD_LOCAL_MCP",
            "target": mcp_target(service, "docs", scope="USER"),
        }
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
        assert outcome.business_value.public_fields["target"] == mcp_target(
            service, "docs"
        )
        assert (
            service.mcp.inspect(LocalMcpTarget("docs")).transport.endpoint
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
                {
                    "action": "UPDATE_MCP_CONNECTION",
                    "target": mcp_target(service, "docs", scope="USER"),
                }
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
                "target": plugin_target(service, "example", scope="USER"),
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
        assert outcome.state == "SUCCESS", outcome
        assert (await service._plugin(None, "example")).enabled
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("scope", ["USER", "WORKSPACE"])
def test_plugin_install_returns_published_identity_for_followup_management(
    tmp_path, scope
):
    async def run():
        service = preparation(tmp_path)
        source = tmp_path / "directory-name-is-not-plugin-id"
        source.mkdir()
        (source / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": "published-plugin",
                }
            )
        )
        args = {
            "action": "INSTALL_PLUGIN",
            "scope": scope,
            "target": {"kind": "PATH", "path": str(source)},
        }
        try:
            for replace in (False, True):
                call = CapabilityManagementCall(
                    service, await service.prepare({**args, "replace": replace})
                )
                result = await call.execute()
                assert result.state == "SUCCESS", result
                assert result.changed
                assert result.business_value.public_fields["target"] == plugin_target(
                    service, "published-plugin", scope=scope
                )

            conflict = await CapabilityManagementCall(
                service, await service.prepare(args)
            ).execute()
            assert conflict.state == "APPLICATION_ERROR", conflict
            assert "target" not in conflict.business_value.public_fields

            remove = await CapabilityManagementCall(
                service,
                await service.prepare(
                    {
                        "action": "REMOVE_PLUGIN",
                        "target": result.business_value.public_fields["target"],
                    }
                ),
            ).execute()
            assert remove.state == "SUCCESS", remove
            assert remove.changed
        finally:
            await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("scope", ["USER", "WORKSPACE"])
def test_model_oauth_completion_carries_exact_owner_only_after_settlement(
    tmp_path, monkeypatch, scope
):
    from dataclasses import asdict
    from pulsara_agent.capability.mcp_management import config_guard

    async def run():
        service = preparation(tmp_path)
        target = LocalMcpTarget(
            "docs", service.workspace_root if scope == "WORKSPACE" else None
        )
        created = await service.mcp.create(
            target,
            {
                "transport": {
                    "type": "streamable_http",
                    "endpoint": "https://example.org/mcp",
                },
                "auth": {"type": "oauth"},
            },
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def exchange(*args, **kwargs):
            entered.set()
            await release.wait()
            return None

        monkeypatch.setattr(service.mcp.oauth, "_exchange", exchange)
        call = CapabilityManagementCall(
            service,
            await service.prepare(
                {
                    "action": "AUTHORIZE_MCP",
                    "target": mcp_target(service, "docs", scope=scope),
                }
            ),
        )
        form = call.form("authorize")
        call.accept(AcceptedCapabilityFormSubmission(await form.prepare_submission({})))
        work = asyncio.create_task(call.execute())
        await entered.wait()
        assert not work.done()
        release.set()
        result = await work
        assert result.state == "SUCCESS"
        assert result.result_facts["authorization"] == {
            "scope": scope,
            "owner": asdict(target.owner),
            "config_identity": created.config.resolved_config_identity,
            "display_identity": config_guard(created.config),
            "package_install_id": None,
        }
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("token_error", [False, True])
def test_oauth_cell_stop_detaches_only_conversation_wait_and_shared_sdk_flow_completes(
    tmp_path, token_error
):
    import httpx
    from tests.test_mcp_oauth import oauth_server

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        trace = {}
        async with oauth_server(
            tmp_path,
            auto_callback=False,
            token_gate=(entered, release),
            trace=trace,
            token_error=token_error,
        ) as (
            manager,
            target,
            config,
            _,
            _,
            callbacks,
            *_,
        ):
            service = preparation(tmp_path)
            unused_owner = service.mcp
            service.mcp = trace["management"]
            await unused_owner.aclose()
            call = CapabilityManagementCall(
                service,
                await service.prepare(
                    {
                        "action": "AUTHORIZE_MCP",
                        "target": mcp_target(service, target.server_id, scope="USER"),
                    }
                ),
            )
            form = call.form("authorize")
            call.accept(
                AcceptedCapabilityFormSubmission(await form.prepare_submission({}))
            )
            redirected = asyncio.Event()
            work = asyncio.create_task(call.execute(cell_cancel_event=redirected))
            callback = None
            try:
                async with asyncio.timeout(10):
                    while not callbacks:
                        await asyncio.sleep(0.005)

                async def visit():
                    async with httpx.AsyncClient(trust_env=False) as client:
                        return await client.get(callbacks[0])

                callback = asyncio.create_task(visit())
                await asyncio.wait_for(entered.wait(), 10)
                shared = await manager.begin(target.owner, lambda: config)
                assert not shared.task.done()
                redirected.set()
                result = await asyncio.wait_for(work, 5)
                assert result.state == "CANCELLED" and not result.changed
                assert result.business_value.content[0].value == "Cell 已停止等待登录"
                assert result.result_facts == {}
                assert not shared.task.cancelled()
                assert not shared.task.done()
                assert manager.settings.read().mcp_authorization(target.owner) is None
                release.set()
                if token_error:
                    with pytest.raises(ValueError, match="登录未完成"):
                        await asyncio.wait_for(asyncio.shield(shared.task), 10)
                    assert (
                        manager.settings.read().mcp_authorization(target.owner) is None
                    )
                else:
                    await asyncio.wait_for(asyncio.shield(shared.task), 10)
                    assert (
                        manager.settings.read().mcp_authorization(target.owner)
                        is not None
                    )
                response = await callback
                assert ("授权未完成" if token_error else "授权成功") in response.text
                assert len(trace["authorizations"]) == 1
                assert result.state == "CANCELLED"
            finally:
                release.set()
                await asyncio.gather(
                    *(task for task in (work, callback) if task is not None),
                    return_exceptions=True,
                )

    asyncio.run(run())
