"""Settled GUI facts stay advisory and share the existing adoption/install cuts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yaml.parser import ParserError

from pulsara_agent.capability.change_observation import CapabilityUiChange
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.primitives.context import thaw_json
from tests.test_capability_management_preparation import preparation, install_plugin
from tests.test_app_capability_boundary import invoke, query_sources, skill_source


def change(name="demo"):
    return CapabilityUiChange.settled(
        "REMOVE_LOOSE_SKILL",
        "USER",
        {"kind": "SKILL", "skill_path": f"/home/skills/{name}/SKILL.md"},
        "REMOVED",
    )


def test_capability_snapshot_can_be_read_by_a_lock_owning_context_preview():
    async def run():
        host = SimpleNamespace(
            _lock=asyncio.Lock(),
            _require_open=lambda: None,
            _pending_capability_change=None,
            _capability_refresh_requested_revision=0,
            _capability_refresh_applied_revision=0,
            _capability_refresh_attention=None,
            reload_capabilities=AsyncMock(return_value={"status": "RELOADED"}),
        )
        async with host._lock:
            assert await asyncio.wait_for(
                KernelHostSession.freeze_capability_change(host), timeout=1,
            ) is None
        await KernelHostSession.request_capability_refresh(host, change())
        await KernelHostSession._adopt_capabilities_if_requested(host)
        pending = host._pending_capability_change
        async with host._lock:
            frozen = await asyncio.wait_for(
                KernelHostSession.freeze_capability_change(host), timeout=1,
            )
        assert frozen == pending
        assert host._pending_capability_change is pending

    asyncio.run(run())


def test_pending_facts_fold_at_the_existing_refresh_cut_and_keep_newer_changes():
    async def run():
        host = SimpleNamespace(
            _lock=asyncio.Lock(),
            _require_open=lambda: None,
            _pending_capability_change=None,
            _capability_refresh_requested_revision=0,
            _capability_refresh_applied_revision=0,
            _capability_refresh_attention=None,
            reload_capabilities=AsyncMock(return_value={"status": "RELOADED"}),
        )
        await KernelHostSession.request_capability_refresh(host, change())
        assert await KernelHostSession.freeze_capability_change(host) is None
        host.reload_capabilities.assert_not_awaited()
        await KernelHostSession.request_capability_refresh(host, change("last"))
        await KernelHostSession._adopt_capabilities_if_requested(host)
        frozen = await KernelHostSession.freeze_capability_change(host)
        assert frozen.changes == 2
        assert json.loads(frozen.render())["target"]["skill_path"].endswith(
            "last/SKILL.md"
        )
        await KernelHostSession.request_capability_refresh(host, change("newer"))
        await KernelHostSession.consume_capability_change(host, frozen)
        assert host._pending_capability_change.changes == 1
        assert await KernelHostSession.freeze_capability_change(host) is None
        host.reload_capabilities.side_effect = ValueError("unavailable")
        await KernelHostSession._adopt_capabilities_if_requested(host)
        latest = await KernelHostSession.freeze_capability_change(host)
        assert json.loads(latest.render())["adoption"] == "PARTIAL"
        await KernelHostSession.consume_capability_change(host, latest)
        assert host._pending_capability_change is None

    asyncio.run(run())


def test_notice_has_only_public_identity_and_no_request_body():
    with pytest.raises(ValueError):
        CapabilityUiChange.settled(
            "UPDATE_LOCAL_MCP",
            "USER",
            {"kind": "MCP_SERVER", "config": {"secret": "private"}},
            "UPDATED",
        )
    value = change()
    assert thaw_json(value.target) == {
        "kind": "SKILL",
        "skill_path": "/home/skills/demo/SKILL.md",
    }


def test_user_facts_reach_live_hosts_and_workspace_facts_stay_in_their_scope(tmp_path):
    from pulsara_agent.conversation_kernel.host import KernelHostCore

    async def run():
        first = SimpleNamespace(
            workspace=SimpleNamespace(workspace_root=tmp_path / "a"),
            request_capability_refresh=AsyncMock(),
            host_session_id="a",
        )
        second = SimpleNamespace(
            workspace=SimpleNamespace(workspace_root=tmp_path / "b"),
            request_capability_refresh=AsyncMock(),
            host_session_id="b",
        )
        core = SimpleNamespace(
            _lock=asyncio.Lock(),
            _sessions={"a": first, "b": second},
            _workspace_capability_revisions={},
        )
        fact = change()
        assert await KernelHostCore.mark_capability_change(core, change=fact) == 2
        first.request_capability_refresh.assert_awaited_once_with(fact)
        second.request_capability_refresh.assert_awaited_once_with(fact)
        first.request_capability_refresh.reset_mock()
        second.request_capability_refresh.reset_mock()
        fact = CapabilityUiChange.settled(
            "REMOVE_PLUGIN",
            "WORKSPACE",
            {"kind": "PLUGIN", "plugin_id": "demo"},
            "REMOVED",
        )
        assert (
            await KernelHostCore.mark_capability_change(
                core, first.workspace.workspace_root, change=fact
            )
            == 1
        )
        first.request_capability_refresh.assert_awaited_once_with(fact)
        second.request_capability_refresh.assert_not_awaited()

    asyncio.run(run())


def test_queued_gui_skill_cancellation_does_not_start_a_write(tmp_path, monkeypatch):
    from pulsara_agent.desktop_app.session_controller import LocalSessionController
    from pulsara_agent.capability.local_skill_management import (
        LocalSkillManagementService,
    )

    async def run():
        controller = object.__new__(LocalSessionController)
        controller._capability_mutation_lock = asyncio.Lock()
        publish = AsyncMock()
        monkeypatch.setattr(
            LocalSkillManagementService, "install_loose_local_skill", publish
        )
        async with controller._capability_mutation_lock:
            task = asyncio.create_task(
                controller.install_user_skill(source_path=str(tmp_path))
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        publish.assert_not_called()

    asyncio.run(run())


def test_capability_notice_is_one_shot_suffix_and_same_text_new_revision_is_distinct():
    from pulsara_agent.conversation_kernel.context_sources import (
        KernelContextSourceCollector,
        ContextSourceRegistry,
    )
    from pulsara_agent.model_input.contracts import ContextSourceKind, ContextRenderMode
    from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
    from pulsara_agent.model_input.continuity import decode_runtime_observation
    from pulsara_agent.llm.input import MessageRole, join_text_content
    from tests.test_round3_structured_model_input_compiler import (
        _snapshot,
        _user,
        _sources,
        _prepared_request,
        _compile_and_install_append,
        new_test_provider_input_continuity_owner,
    )
    from dataclasses import replace

    collector = object.__new__(KernelContextSourceCollector)
    collector._registry = ContextSourceRegistry()
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    initial = _user("first", sequence=1)
    previous = None
    items = []
    for index, revision in enumerate((None, 1, None, 2), 1):
        source = (
            ()
            if revision is None
            else (
                collector._candidate(
                    ContextSourceKind.CAPABILITY_CHANGE,
                    ('{"source":"CAPABILITY_UI","result":"REMOVED"}', ""),
                    domain_identity={"revision": revision},
                ),
            )
        )
        items.append(
            initial if index == 1 else _user(f"follow {index}", sequence=index)
        )
        request = replace(
            _prepared_request(_snapshot(*items), _sources(*source)),
            model_call_index=index,
        )
        _, view = _compile_and_install_append(
            compiler=compiler, owner=owner, request=request
        )
        if previous is not None:
            assert (
                view.system_prompt == previous.system_prompt
                and view.tools == previous.tools
            )
            assert view.messages[: len(previous.messages)] == previous.messages
        notices = [
            decode_runtime_observation(message)
            for message in view.messages
            if message.role is MessageRole.USER
            and "pulsara_runtime_observation" in join_text_content(message.content)
        ]
        notices = [
            notice
            for notice in notices
            if notice.source_kind is ContextSourceKind.CAPABILITY_CHANGE
        ]
        assert len(notices) == (0, 1, 1, 2)[index - 1]
        assert all(notice.body and "REMOVED" in notice.body for notice in notices)
        previous = view
    # The fallback variant must never contain a truncated target/body.
    assert source[0].variants[-1].mode is ContextRenderMode.UNAVAILABLE_MINIMAL
    assert source[0].variants[-1].text == ""


@pytest.mark.parametrize("damage", ["manifest", "package"])
def test_damaged_plugin_inspection_remove_and_reinstall_share_native_owner(
    tmp_path, damage
):
    async def run():
        import os
        import shutil

        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path)
        root = installed.package_root
        os.chmod(root, 0o700)
        if damage == "manifest":
            (root / "plugin.json").unlink()
        else:
            for folder, _, _ in os.walk(root):
                os.chmod(folder, 0o700)
            shutil.rmtree(root)
        detail = await query_sources(
            service, target={"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"}
        )
        assert detail["status"] == "UNAVAILABLE" and detail["remove_eligible"]
        assert all(c["count"] is None for c in detail["components"].values())
        removed = await invoke(
            service, action="REMOVE_PLUGIN", scope="USER", plugin_id="example"
        )
        assert removed["status"] == "APPLIED"
        assert (tmp_path / "source" / "plugin.json").exists()
        reinstalled = await invoke(
            service,
            action="INSTALL_PLUGIN",
            scope="USER",
            source_path=str(tmp_path / "source"),
        )
        assert reinstalled["status"] == "APPLIED"
        await service.mcp.aclose()

    asyncio.run(run())


def test_plugin_removal_rejects_a_changed_instance_without_package_health(tmp_path):
    async def run():
        from pulsara_agent.conversation_kernel.capability_management_execution import (
            CapabilityManagementCall,
        )

        service = preparation(tmp_path)
        await install_plugin(service, tmp_path)
        prepared = await service.prepare(
            {"action": "REMOVE_PLUGIN", "scope": "USER", "plugin_id": "example"}
        )
        await invoke(
            service,
            action="INSTALL_PLUGIN",
            scope="USER",
            source_path=str(tmp_path / "source"),
            replace=True,
        )
        result = await CapabilityManagementCall(service, prepared).execute()
        assert result["status"] != "APPLIED"
        detail = await query_sources(
            service, target={"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"}
        )
        assert detail["enabled"] is False
        await service.mcp.aclose()

    asyncio.run(run())


def test_enabled_damaged_plugin_keeps_process_effect_and_cleans_only_owned_credentials(
    tmp_path,
):
    async def run():
        import os
        from pulsara_agent.plugins.management import _request_identity
        from pulsara_agent.plugins.contracts import PluginScopeKind
        from pulsara_agent.plugins.mcp_connection import plugin_connection_owner
        from pulsara_agent.mcp_credentials import McpCredentialBinding
        from pulsara_agent.conversation_kernel.capability_management_execution import (
            CapabilityManagementCall,
        )

        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path)
        identity = installed.identity
        layout = service.plugins._store().layout(identity)
        # An external filesystem edit, entirely inside the disposable fixture.
        raw = json.loads(layout.state_path.read_text())
        raw["enabled"] = True
        layout.state_path.write_text(json.dumps(raw))
        os.chmod(installed.package_root, 0o700)
        (installed.package_root / "plugin.json").unlink()
        layout.data_root.mkdir(parents=True, exist_ok=True)
        (layout.data_root / "keep.txt").write_text("user data")
        binding = McpCredentialBinding(
            plugin_connection_owner(identity, "docs"), "bearer"
        )
        other = McpCredentialBinding(
            plugin_connection_owner(
                _request_identity(
                    PluginScopeKind.WORKSPACE, "example", service.workspace_root
                ),
                "docs",
            ),
            "bearer",
        )
        await service.mcp.settings.replace_mcp_secrets(
            binding.owner, ((binding, "fixture-user-secret"),)
        )
        await service.mcp.settings.replace_mcp_secrets(
            other.owner, ((other, "fixture-workspace-secret"),)
        )
        prepared = await service.prepare(
            {"action": "REMOVE_PLUGIN", "scope": "USER", "plugin_id": "example"}
        )
        assert prepared.effects.process_control
        result = await CapabilityManagementCall(service, prepared).execute()
        assert (
            result["status"] == "APPLIED" and not result["current"]["cleanup_attention"]
        )
        assert service.mcp.settings.read().mcp_secret(binding) is None
        assert (
            service.mcp.settings.read().mcp_secret(other) == "fixture-workspace-secret"
        )
        assert (layout.data_root / "keep.txt").read_text() == "user data"
        assert (tmp_path / "source" / "plugin.json").is_file()
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "damage", ["missing_document", "bad_document", "missing_directory"]
)
def test_loose_skill_damage_uses_directory_identity_and_ordinary_install(
    tmp_path, damage
):
    async def run():
        import shutil

        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        result = await invoke(
            service, action="INSTALL_LOOSE_SKILL", scope="USER", source_path=str(source)
        )
        from pathlib import Path

        path = Path(result["identity"]["skill_path"])
        if damage == "missing_directory":
            shutil.rmtree(path.parent)
        elif damage == "missing_document":
            path.unlink()
        else:
            path.write_text("bad document")
        detail = await query_sources(
            service, target={"kind": "SKILL", "skill_path": str(path)}
        )
        if damage == "missing_directory":
            assert not detail["directory_exists"] and not detail["remove_eligible"]
        else:
            assert (
                await invoke(
                    service,
                    action="REMOVE_LOOSE_SKILL",
                    scope="USER",
                    skill_path=str(path),
                )
            )["status"] == "APPLIED"
        assert (
            await invoke(
                service,
                action="INSTALL_LOOSE_SKILL",
                scope="USER",
                source_path=str(source),
            )
        )["status"] == "APPLIED"
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["entry", "file"])
def test_missing_mcp_config_uses_add_without_an_artificial_remove(tmp_path, missing):
    async def run():
        service = preparation(tmp_path)
        if missing == "entry":
            service.mcp.user_config_path.write_text("servers: {}\n")
        result = await invoke(
            service,
            action="ADD_LOCAL_MCP",
            scope="USER",
            server_id="demo",
            config={"transport": {"type": "stdio", "command": "/missing/demo"}},
        )
        assert result["status"] == "APPLIED"
        assert (
            await invoke(
                service, action="REMOVE_LOCAL_MCP", scope="USER", server_id="demo"
            )
        )["status"] == "APPLIED"
        service.mcp.user_config_path.write_text("servers: [")
        with pytest.raises(ParserError):
            await service.prepare(
                {
                    "action": "ADD_LOCAL_MCP",
                    "scope": "USER",
                    "server_id": "demo",
                    "config": {
                        "transport": {"type": "stdio", "command": "/missing/demo"}
                    },
                }
            )
        from pulsara_agent.capability.source_query import CapabilityQueryError

        with pytest.raises(CapabilityQueryError) as unavailable:
            await query_sources(
                service,
                target={
                    "kind": "MCP_SERVER",
                    "source_kind": "LOCAL",
                    "scope": "USER",
                    "server_id": "demo",
                },
            )
        assert unavailable.value.code == "SOURCE_OBSERVATION_UNAVAILABLE"
        assert service.mcp.user_config_path.read_text() == "servers: ["
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["missing", "bad_json", "bad_trust"])
def test_local_hook_damage_reuses_disable_and_revoke_without_granting_trust(
    tmp_path, damage
):
    async def run():
        service = preparation(tmp_path)
        path = service.hooks.user_config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if damage == "bad_json":
            path.write_text("{bad")
        elif damage == "bad_trust":
            # Discover the existing trust path from the owner, not an invented ledger.
            from pulsara_agent.hooks.contracts import HookSourceKind

            subject = service.hooks.source_subject(HookSourceKind.USER_FILE)
            trust_path = service.hooks.trust_store._state_path(subject)
            trust_path.parent.mkdir(parents=True, exist_ok=True)
            trust_path.write_text("{bad")
        for arguments in (
            {"action": "REVOKE_HOOK_TRUST"},
            {"action": "SET_HOOK_SOURCE_ENABLED", "enabled": False},
        ):
            result = await invoke(
                service, scope="USER", source_kind="LOCAL", **arguments
            )
            assert result["status"] == "APPLIED"
            if arguments["action"] == "SET_HOOK_SOURCE_ENABLED":
                assert result["current"]["enabled"] is False
            else:
                assert result["current"]["trust_revoked"] is True
        assert all(
            not source.runnable for source in service.hooks.discover().source_snapshots
        )
        if damage == "bad_json":
            detail = await query_sources(
                service,
                target={"kind": "HOOK_SOURCE", "scope": "USER", "source_kind": "LOCAL"},
            )
            assert (
                detail["enabled"] is None
                and detail["source_disposition"] == "UNAVAILABLE"
            )
            assert path.read_text() == "{bad"
        await service.mcp.aclose()

    asyncio.run(run())


def test_gui_notice_scrubs_saved_secrets_before_and_after_native_mcp_removal(tmp_path, monkeypatch):
    from pulsara_agent.capability.mcp_management import (
        LocalMcpTarget, McpSecretMutation, config_guard,
    )
    from pulsara_agent.mcp_credentials import McpCredentialBinding, ManagedLocalCredentialReference, secret_to_dict
    from tests.test_project_capability_management import _controller_with_sessions

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))

    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        controller, _, host, _, _ = _controller_with_sessions(workspace, tmp_path / "other")
        controller.inspect_user_capabilities = AsyncMock(return_value={})
        host.request_capability_refresh = AsyncMock()
        manager = controller.core.mcp_management
        # Newly saved by CREATE; removed by REMOVE before its notice is emitted.
        secret = "fixture-managed-notice-secret"
        server_id = f"demo-{secret}"
        target = LocalMcpTarget(server_id)
        binding = McpCredentialBinding(target.owner, "bearer")
        await controller.create_user_mcp_server(
            server_id=server_id,
            config={
                "enabled": False,
                "transport": {"type": "streamable_http", "endpoint": "https://example.org/mcp"},
                "auth": {"type": "bearer", "reference": secret_to_dict(ManagedLocalCredentialReference(binding))},
            },
            secret_changes=(McpSecretMutation(binding, secret),),
        )
        assert manager.settings.read().mcp_secret(binding) == secret
        created = host.request_capability_refresh.call_args.args[0]
        assert secret not in json.dumps(thaw_json(created.target))
        assert "REDACTED_CREDENTIAL" in thaw_json(created.target)["server_id"]
        await controller.remove_user_mcp_server(
            server_id=server_id, expected=config_guard(manager.inspect(target)),
        )
        assert manager.settings.read().mcp_secret(binding) is None
        removed = host.request_capability_refresh.call_args.args[0]
        assert removed.action.value == "REMOVE_LOCAL_MCP"
        assert thaw_json(removed.target) == thaw_json(created.target)
        assert secret not in json.dumps(thaw_json(removed.target))
        await manager.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["bad_definition", "bad_trust", "unreadable_trust", "untrusted", "disabled"])
def test_hook_model_projections_share_unknown_enablement(tmp_path, damage):
    from pulsara_agent.hooks.contracts import HookSourceKind
    from pulsara_agent.hooks.presentation import hook_snapshot_model_public

    async def run():
        service = preparation(tmp_path)
        path = service.hooks.user_config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad" if damage == "bad_definition" else '{"hooks":{}}')
        subject = service.hooks.source_subject(HookSourceKind.USER_FILE)
        trust_path = service.hooks.trust_store._state_path(subject)
        if damage in {"bad_trust", "unreadable_trust"}:
            trust_path.parent.mkdir(parents=True, exist_ok=True)
            if damage == "bad_trust":
                trust_path.write_text("{bad")
            else:
                trust_path.mkdir()
        elif damage == "disabled":
            await invoke(service, action="SET_HOOK_SOURCE_ENABLED", scope="USER", source_kind="LOCAL", enabled=False)
        view = service.hooks.discover()
        snapshot = next(s for s in view.source_snapshots if s.provenance.identity.kind is HookSourceKind.USER_FILE)
        expected = True if damage == "untrusted" else False if damage == "disabled" else None
        # Check the damaged state BEFORE mutation can replace its trust rows.
        detail = await query_sources(service, target={"kind": "HOOK_SOURCE", "scope": "USER", "source_kind": "LOCAL"})
        assert detail["enabled"] is expected
        assert hook_snapshot_model_public(snapshot)["enabled"] is expected
        host = SimpleNamespace(
            _hooks=SimpleNamespace(reload=AsyncMock(return_value=view)),
            _lock=asyncio.Lock(), _closing=False, _closed=False,
        )
        reloaded = await KernelHostSession.reload_hooks(host, deadline_monotonic=None)
        assert next(s for s in reloaded["sources"] if s["source"] == "USER_FILE")["enabled"] is expected
        assert not snapshot.runnable
        await service.mcp.aclose()

    asyncio.run(run())


def test_notice_scrub_uses_typed_saved_credentials_without_redacting_public_context():
    from dataclasses import replace
    from pulsara_agent.mcp_credentials import LocalMcpOAuthRecord, McpCredentialOwner
    from tests.support.model_config import test_model_runtime as runtime

    settings = runtime(api_key="fixture-model-key").settings.read()
    settings = replace(settings, mcp_oauth=(LocalMcpOAuthRecord(
        McpCredentialOwner("local", "USER", "demo"), "https://example.org/mcp",
        "https://issuer.example", "public-client", "read",
        json.dumps({"access_token": "fixture-access", "refresh_token": "fixture-refresh", "scope": "public-scope"}),
        json.dumps({"client_id": "public-client", "client_secret": "fixture-client-secret"}),
        auth_json=json.dumps({"nested": [{"id_token": "fixture-id-token"}]}),
    ),))
    scrub = settings.credential_scrub_set()
    public = "https://example.org/mcp public-client public-scope"
    secrets = ("fixture-model-key", "fixture-access", "fixture-refresh", "fixture-client-secret", "fixture-id-token")
    projected = scrub.scrub_text(public + " " + " ".join(secrets))
    assert all(secret not in projected for secret in secrets)
    assert public in projected


def test_notice_scrub_unavailable_does_not_gate_hook_mutation(tmp_path, monkeypatch):
    from pulsara_agent.desktop_app.session_controller import LocalSessionController
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
    from pulsara_agent.settings import LocalSettingsStore
    from time import monotonic

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path))
    store = LocalSettingsStore(tmp_path / "local-settings.yaml")
    # Existing Hook edit owns its own source; private settings are unreadable.
    store.path.mkdir()
    core = SimpleNamespace(
        mcp_management=SimpleNamespace(lane=asyncio.Lock(), settings=store),
        _credential_boundary=ProcessCredentialBoundary(),
        _canonical_deadline=lambda: monotonic() + 5,
        mark_capability_change=AsyncMock(return_value=0),
    )
    controller = LocalSessionController(core=core, permission_policy=None, active_skill_names=frozenset())

    async def run():
        outcome = await controller.save_user_hook_config(exists=False, expected_text="", text='{"hooks":{}}')
        assert outcome["status"] == "APPLIED"
        assert (tmp_path / "hooks.json").read_text() == '{"hooks":{}}'
        core.mark_capability_change.assert_awaited_once_with(None)

    asyncio.run(run())


def test_queued_native_remove_scrubs_credentials_added_while_waiting(tmp_path, monkeypatch):
    from pulsara_agent.capability.mcp_management import LocalMcpTarget, config_guard
    from pulsara_agent.mcp_credentials import McpCredentialBinding
    from tests.test_project_capability_management import _controller_with_sessions

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))

    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        controller, _, host, _, _ = _controller_with_sessions(workspace, tmp_path / "other")
        controller.inspect_user_capabilities = AsyncMock(return_value={})
        host.request_capability_refresh = AsyncMock()
        manager = controller.core.mcp_management
        secret = "fixture-secret-created-while-queued"
        server_id = "demo-" + secret
        target = LocalMcpTarget(server_id)
        await manager.create(target, {"enabled": False, "transport": {"type": "stdio", "command": "/missing/demo"}})
        pre_captured = asyncio.Event()
        capture = controller._capture_capability_notice_scrub

        async def observed_capture():
            value = await capture()
            pre_captured.set()
            return value

        controller._capture_capability_notice_scrub = observed_capture
        async with manager.lane:
            remove = asyncio.create_task(controller.remove_user_mcp_server(server_id=server_id, expected=config_guard(manager.inspect(target))))
            await asyncio.wait_for(pre_captured.wait(), timeout=5)
            assert not remove.done()
            # This writer is outside REMOVE's context and currently owns the
            # lane. Its new value is absent from REMOVE's earlier scrub cut.
            binding = McpCredentialBinding(target.owner, "bearer")
            await manager.settings.replace_mcp_secrets(target.owner, ((binding, secret),))
        result = await remove
        assert result["operation"]["success"]
        assert manager.settings.read().mcp_secret(binding) is None
        fact = host.request_capability_refresh.call_args.args[0]
        assert secret not in json.dumps(thaw_json(fact.target))
        assert "REDACTED_CREDENTIAL" in thaw_json(fact.target)["server_id"]
        assert manager.settings.credential_mutation_scrub is None
        await manager.aclose()

    asyncio.run(run())
