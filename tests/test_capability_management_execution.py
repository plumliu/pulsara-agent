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
from pulsara_agent.mcp_config import McpScopePolicy
from pulsara_agent.plugins.connection_management import (
    ReplacePluginMcpConnectionRequest,
)
from pulsara_agent.plugins.mcp_connection import PluginMcpConnectionOverlay
from pulsara_agent.primitives.context import thaw_json


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


def test_plugin_connection_form_preserves_scope_on_patch_and_removes_override_on_reset(
    tmp_path,
):
    async def run():
        service = preparation(tmp_path)
        try:
            installed = await install_plugin(service, tmp_path)
            old_endpoint = "https://saved.example/mcp"
            edited_endpoint = "https://edited.example/mcp"
            await service.plugins.replace_plugin_mcp_connection_overlay(
                ReplacePluginMcpConnectionRequest(
                    installed.identity,
                    "docs",
                    installed.package_install_id,
                    None,
                    PluginMcpConnectionOverlay(
                        "docs",
                        "streamable_http",
                        endpoint=old_endpoint,
                        scope_policy=McpScopePolicy.ROOT_ONLY,
                    ),
                    service.deadline(),
                ),
                connections=service.mcp,
            )
            args = {
                "action": "UPDATE_MCP_CONNECTION",
                "target": mcp_target(service, "docs", scope="USER", plugin="example"),
            }
            patch = CapabilityManagementCall(
                service,
                await service.prepare(
                    {**args, "config": {"transport": {"endpoint": edited_endpoint}}}
                ),
            )
            patch_form = patch.form("review")
            shown = thaw_json(patch_form.public_projection)
            assert shown["prefill"]["saved_destination"] == {
                "type": "streamable_http",
                "endpoint": old_endpoint,
            }
            assert (
                shown["connection"]["config"]["transport"]["endpoint"]
                == edited_endpoint
            )
            assert shown["connection"]["config"]["scope_policy"] == "ROOT_ONLY"
            values = await patch_form.prepare_submission(
                {"overlay": shown["connection"]["overlay"]}
            )
            patch.accept(AcceptedCapabilityFormSubmission(values))
            assert (await patch.execute()).state == "SUCCESS"
            plugin = await service._plugin(None, "example")
            assert (
                plugin.mcp_connection_overlays[0].scope_policy
                is McpScopePolicy.ROOT_ONLY
            )

            restore = CapabilityManagementCall(
                service, await service.prepare({**args, "config": None})
            )
            restore_form = restore.form("review")
            shown = thaw_json(restore_form.public_projection)
            assert shown["prefill"]["saved_destination"]["endpoint"] == edited_endpoint
            assert shown["prefill"]["restore_defaults"] is True
            assert shown["connection"]["overlay"] is None
            assert shown["connection"]["config"]["scope_policy"] == "ROOT_AND_SUBAGENTS"
            values = await restore_form.prepare_submission({"overlay": None})
            restore.accept(AcceptedCapabilityFormSubmission(values))
            result = await restore.execute()
            assert result.state == "SUCCESS"
            assert (
                result.business_value.content[0].value
                == "已恢复 MCP 连接 docs 的默认配置。"
            )
            plugin = await service._plugin(None, "example")
            assert plugin.mcp_connection_overlays == ()
            server = plugin.summary.mcp.mcp_servers[0]
            current = service._connection(plugin, server)
            assert current.transport.endpoint == "https://example.org/mcp"
            assert current.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
        finally:
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
                verb = "替换" if replace else "安装"
                assert (
                    result.business_value.content[0].value
                    == f"已{verb}插件 published-plugin，当前未启用。"
                )
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
            assert "target" not in remove.business_value.public_fields
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


@pytest.mark.parametrize(
    "action,target",
    [
        (
            "REMOVE_LOOSE_SKILL",
            {"kind": "SKILL", "skill_path": "/private/tmp/example/SKILL.md"},
        ),
        (
            "REMOVE_LOCAL_MCP",
            {
                "kind": "MCP_CONNECTION",
                "config_path": "/private/tmp/mcp.json",
                "server_id": "docs",
            },
        ),
        ("REMOVE_PLUGIN", {"kind": "PLUGIN", "plugin_path": "/private/tmp/example"}),
    ],
)
@pytest.mark.parametrize("status", ["APPLIED", "REJECTED", "CANCELLED"])
def test_remove_public_projection_omits_target_for_all_outcomes(action, target, status):
    from types import SimpleNamespace
    from pulsara_agent.capability.management_intent import (
        parse_capability_management_intent,
    )
    from pulsara_agent.conversation_kernel.capability_management_execution import (
        management_outcome,
    )

    prepared = SimpleNamespace(
        intent=parse_capability_management_intent({"action": action, "target": target})
    )
    result = management_outcome(
        prepared, {"status": status, "target": target, "current": {}}
    )
    assert "target" not in result.business_value.public_fields
    assert (
        result.state
        == {
            "APPLIED": "SUCCESS",
            "REJECTED": "APPLICATION_ERROR",
            "CANCELLED": "CANCELLED",
        }[status]
    )
    assert result.changed == (status == "APPLIED")


@pytest.mark.parametrize(
    "disposition,verb", [("INSTALLED", "安装"), ("REPLACED", "替换")]
)
def test_plugin_install_cleanup_failure_preserves_published_effect(disposition, verb):
    from types import SimpleNamespace
    from pulsara_agent.capability.management_intent import (
        parse_capability_management_intent,
    )
    from pulsara_agent.conversation_kernel.capability_management_execution import (
        management_outcome,
    )

    prepared = SimpleNamespace(
        intent=parse_capability_management_intent(
            {
                "action": "INSTALL_PLUGIN",
                "scope": "USER",
                "target": {"kind": "PATH", "path": "/private/tmp/source"},
            }
        )
    )
    target = {"kind": "PLUGIN", "plugin_path": "/private/tmp/example"}
    result = management_outcome(
        prepared,
        {
            "status": "APPLIED",
            "target": target,
            "current": {
                "operation_status": "CLEANUP_UNAVAILABLE",
                "prior_operation_status": disposition,
                "cleanup_attention": True,
            },
            "diagnostics": [
                {"path": "/private/tmp/staging", "message": "cleanup failed"}
            ],
        },
    )
    assert result.state == "APPLICATION_ERROR" and result.changed
    assert result.business_value.public_fields["target"] == target
    assert (
        result.business_value.content[0].value == f"已{verb}插件 example，当前未启用。"
    )
    assert any("cleanup failed" in part.value for part in result.business_value.content)


@pytest.mark.parametrize(
    "action,target",
    [
        (
            "REMOVE_LOOSE_SKILL",
            {"kind": "SKILL", "skill_path": "/private/tmp/example/SKILL.md"},
        ),
        (
            "REMOVE_LOCAL_MCP",
            {
                "kind": "MCP_CONNECTION",
                "config_path": "/private/tmp/mcp.json",
                "server_id": "docs",
            },
        ),
        ("REMOVE_PLUGIN", {"kind": "PLUGIN", "plugin_path": "/private/tmp/example"}),
    ],
)
def test_caught_removal_error_does_not_expose_target(action, target):
    from types import SimpleNamespace
    from pulsara_agent.capability.management_intent import (
        parse_capability_management_intent,
    )
    from pulsara_agent.conversation_kernel.tool_runtime_management import (
        _ToolManagementOperations,
    )

    async def run():
        service = object()
        intent = parse_capability_management_intent(
            {"action": action, "target": target}
        )
        prepared = SimpleNamespace(
            intent=intent, effects=SimpleNamespace(read_only=False)
        )

        class Call:
            subject = ("session", "turn", "entry", "call")

            def __init__(self):
                self.service, self.prepared = service, prepared

            async def execute(self, **kwargs):
                raise ValueError("actual failure")

            def _public_scrub(self, prepared):
                return SimpleNamespace(scrub_text=lambda text: text)

        runtime = _ToolManagementOperations()
        runtime._session_id, runtime._capability_management = "session", service
        context = SimpleNamespace(
            capability_call=Call(),
            conversation_scope_kind="ROOT",
            cell_cancel_event=None,
        )
        result = await runtime._invoke_capability_management(
            turn_id="turn",
            assistant_entry_id="entry",
            tool_call_id="call",
            invocation_context=context,
            invocation_started=0,
            observation_origin="test",
        )
        assert result.state == "APPLICATION_ERROR"
        assert "target" not in result.content.public_fields
        assert result.content.content[0].value == "actual failure"

    asyncio.run(run())


def test_plugin_replacement_cleanup_failure_reports_real_published_result(
    tmp_path, monkeypatch
):
    from pulsara_agent.plugins.contracts import (
        CleanupUnavailablePluginInstallOutcome,
        PluginCleanupLocationStatus,
        PluginDiagnostic,
        PluginDiagnosticCode,
    )

    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path)
        install = service.plugins.install_local_plugin

        async def install_with_cleanup_failure(*args, **kwargs):
            prior = await install(*args, **kwargs)
            return CleanupUnavailablePluginInstallOutcome(
                prior=prior,
                attempted_path=tmp_path / "staging",
                location_status=PluginCleanupLocationStatus.UNKNOWN,
                diagnostic=PluginDiagnostic(
                    PluginDiagnosticCode.SOURCE_UNAVAILABLE,
                    "staging cleanup failed",
                    str(tmp_path / "staging"),
                ),
            )

        monkeypatch.setattr(
            service.plugins, "install_local_plugin", install_with_cleanup_failure
        )
        try:
            result = await CapabilityManagementCall(
                service,
                await service.prepare(
                    {
                        "action": "INSTALL_PLUGIN",
                        "scope": "USER",
                        "replace": True,
                        "target": {"kind": "PATH", "path": str(tmp_path / "source")},
                    }
                ),
            ).execute()
            assert result.state == "APPLICATION_ERROR" and result.changed
            assert result.business_value.public_fields["target"] == plugin_target(
                service, "example", scope="USER"
            )
            assert (
                result.business_value.content[0].value
                == "已替换插件 example，当前未启用。"
            )
            assert any(
                "staging cleanup failed" in part.value
                for part in result.business_value.content
            )
            assert not (await service._plugin(None, "example")).enabled
        finally:
            await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["credentials", "lock_exit", "materialize", "source_close"]
)
def test_real_plugin_replacement_post_publication_failure_keeps_effect_and_adopts(
    tmp_path, monkeypatch, failure
):
    from time import monotonic
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pulsara_agent.conversation_kernel.tool_runtime_management import (
        _ToolManagementOperations,
    )
    from pulsara_agent.mcp_credentials import McpCredentialBinding
    from pulsara_agent.plugins import connection_management
    from pulsara_agent.plugins.contracts import PluginScopeKind
    from pulsara_agent.plugins.management import _request_identity
    from pulsara_agent.plugins.mcp_connection import plugin_connection_owner
    from pulsara_agent.plugins.package_store import ManagedPluginStore

    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path)
        identity = _request_identity(PluginScopeKind.USER, "example", None)
        owner = plugin_connection_owner(identity, "docs")
        binding = McpCredentialBinding(owner, "bearer")
        secret = "private-test-credential"
        await service.mcp.settings.replace_mcp_secrets(owner, ((binding, secret),))
        prepared = await service.prepare(
            {
                "action": "INSTALL_PLUGIN",
                "scope": "USER",
                "replace": True,
                "target": {"kind": "PATH", "path": str(tmp_path / "source")},
            }
        )
        call = CapabilityManagementCall(service, prepared)
        call.subject = ("session", "turn", "entry", "call")
        if failure == "credentials":

            async def fail_cleanup(*args, **kwargs):
                raise OSError(f"credential cleanup failure {secret}")

            monkeypatch.setattr(
                service.mcp.settings, "replace_mcp_secrets", fail_cleanup
            )
        elif failure == "materialize":

            def fail_materialize(**kwargs):
                raise OSError(f"MCP materialize failure {secret}")

            monkeypatch.setattr(
                connection_management,
                "materialize_plugin_mcp_definition",
                fail_materialize,
            )
        elif failure == "source_close":
            from pulsara_agent.plugins.package_core import HeldPluginPackageObservation

            original_install = connection_management.install_plugin_package
            original_close = HeldPluginPackageObservation.close
            published = False

            async def install_then_mark(*args, **kwargs):
                nonlocal published
                result = await original_install(*args, **kwargs)
                published = True
                return result

            def fail_source_close(observation):
                original_close(observation)
                if published and observation.source_path == tmp_path / "source":
                    raise OSError(f"source close failure {secret}")

            monkeypatch.setattr(
                connection_management, "install_plugin_package", install_then_mark
            )
            monkeypatch.setattr(
                HeldPluginPackageObservation, "close", fail_source_close
            )
        else:
            original_edit = ManagedPluginStore.edit_instance

            def failing_edit(self, *args, **kwargs):
                wrapped = original_edit(self, *args, **kwargs)

                class Lock:
                    def __enter__(self):
                        return wrapped.__enter__()

                    def __exit__(self, *exc):
                        wrapped.__exit__(*exc)
                        raise OSError(f"lock release failure {secret}")

                return Lock()

            monkeypatch.setattr(ManagedPluginStore, "edit_instance", failing_edit)
        runtime = _ToolManagementOperations()
        runtime._session_id, runtime._capability_management = "session", service
        adoption = AsyncMock(return_value={"status": "RELOADED"})
        runtime._capability_reload = SimpleNamespace(
            adopt_capability_management_change=adoption
        )
        context = SimpleNamespace(
            capability_call=call, conversation_scope_kind="ROOT", cell_cancel_event=None
        )
        try:
            result = await runtime._invoke_capability_management(
                turn_id="turn",
                assistant_entry_id="entry",
                tool_call_id="call",
                invocation_context=context,
                invocation_started=monotonic(),
                observation_origin="test",
            )
            assert result.state == "APPLICATION_ERROR"
            assert result.content.public_fields["target"] == plugin_target(
                service, "example", scope="USER"
            )
            text = "\n".join(part.value for part in result.content.content)
            assert "已替换插件 example，当前未启用。" in text
            assert {
                "credentials": "credential cleanup failure",
                "lock_exit": "lock release failure",
                "materialize": "MCP materialize failure",
                "source_close": "source close failure",
            }[failure] in text
            assert secret not in text
            adoption.assert_awaited_once_with(workspace_root=None)
            assert not (await service._plugin(None, "example")).enabled
            assert service.mcp.settings.read().mcp_secret(binding) == (
                secret if failure in {"credentials", "materialize"} else None
            )
        finally:
            await service.mcp.aclose()

    asyncio.run(run())
