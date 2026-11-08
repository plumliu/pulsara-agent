"""Capability changes refresh the actual owners without a separate model notice."""

from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from pulsara_agent.identity import random_id as _new_management_call_id

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yaml.parser import ParserError

from pulsara_agent.conversation_kernel.host import KernelHostSession
from tests.test_capability_management_preparation import preparation, install_plugin
from tests.test_app_capability_boundary import invoke, query_sources, skill_source




@pytest.mark.parametrize('human', [True, False])
def test_context_collection_preserves_separate_human_skill_reference_surfaces(human):
    from pulsara_agent.conversation_kernel.context_sources import (
        ContextSourceRegistry, KernelContextSourceCollector,
    )
    from pulsara_agent.model_input.contracts import CapabilityActivationSubjectKind

    class ActivationReached(Exception):
        pass

    surfaces = ('````\n/quoted-skill', '/comment-skill')
    registry = ContextSourceRegistry()
    frozen = SimpleNamespace(registry_fingerprint=registry.fingerprint,
        candidates=(), absent_facts=(), diagnostics=(),
        skill_dispatch_view=object(), skill_owner_snapshot=object())
    received = []
    def activation_context(*, user_input):
        received.append(user_input)
        raise ActivationReached
    collector = object.__new__(KernelContextSourceCollector)
    collector._registry = registry
    collector._capability = SimpleNamespace(activation_context=activation_context, compose=lambda **kwargs: None)
    collector.freeze_non_trigger_sources = lambda **kwargs: frozen
    with pytest.raises(ActivationReached):
        collector.collect(
            activation_subject=(CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT if human
                else CapabilityActivationSubjectKind.ROOT_NON_HUMAN_TRIGGER),
            # This joined text deliberately has a different delimiter scope.
            # Skills must receive the original independent surfaces instead.
            activation_text='\n'.join(surfaces), skill_reference_surfaces=surfaces,
            tool_surface=object(), canonical_facts=object(), tool_exposure_plan=object(),
            skill_dispatch_view=object(), skill_owner_snapshot=object())
    assert received == [surfaces if human else ()]
    if human:
        assert received[0] is surfaces








def test_user_refresh_reaches_live_hosts_and_workspace_refresh_stays_in_scope(tmp_path):
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
        assert await KernelHostCore.mark_capability_change(core) == 2
        first.request_capability_refresh.assert_awaited_once_with()
        second.request_capability_refresh.assert_awaited_once_with()
        first.request_capability_refresh.reset_mock()
        second.request_capability_refresh.reset_mock()
        assert await KernelHostCore.mark_capability_change(
            core, first.workspace.workspace_root
        ) == 1
        first.request_capability_refresh.assert_awaited_once_with()
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
            service, action="REMOVE_PLUGIN", target={"kind":'PLUGIN', 'scope':"USER", 'plugin_id':"example"}
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
            {'action': "REMOVE_PLUGIN", 'target': {"kind": "PLUGIN", 'scope': "USER", 'plugin_id': "example"}}
        )
        await invoke(
            service,
            action="INSTALL_PLUGIN",
            scope="USER",
            source_path=str(tmp_path / "source"),
            replace=True,
        )
        result = await CapabilityManagementCall(service, prepared).execute( page_budget=ToolResultPageBudget(_new_management_call_id("call")))
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
            {'action': "REMOVE_PLUGIN", 'target': {"kind": "PLUGIN", 'scope': "USER", 'plugin_id': "example"}}
        )
        assert prepared.effects.process_control
        result = await CapabilityManagementCall(service, prepared).execute( page_budget=ToolResultPageBudget(_new_management_call_id("call")))
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

        path = Path(result["target"]["skill_path"])
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
                    action="REMOVE_LOOSE_SKILL", target={"kind":'SKILL', 'skill_path':str(path)},
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
            action="ADD_LOCAL_MCP", config={"transport": {"type": "stdio", "command": "/missing/demo"}}, target={"kind":'MCP_CONNECTION', 'scope':"USER", 'source_kind':'LOCAL', 'server_id':"demo"},
        )
        assert result["status"] == "APPLIED"
        assert (
            await invoke(
                service, action="REMOVE_LOCAL_MCP", target={"kind":'MCP_CONNECTION', 'scope':"USER", 'source_kind':'LOCAL', 'server_id':"demo"}
            )
        )["status"] == "APPLIED"
        service.mcp.user_config_path.write_text("servers: [")
        with pytest.raises(ParserError):
            await service.prepare(
                {'action': "ADD_LOCAL_MCP", 'config': {
                        "transport": {"type": "stdio", "command": "/missing/demo"}
                    }, 'target': {"kind": "MCP_CONNECTION", 'scope': "USER", 'source_kind': 'LOCAL', 'server_id': "demo"}}
            )
        from pulsara_agent.capability.source_query import CapabilityQueryError

        with pytest.raises(CapabilityQueryError) as unavailable:
            await query_sources(
                service,
                target={'kind': "MCP_CONNECTION", 'source_kind': "LOCAL", 'scope': "USER", 'server_id': "demo"},
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
                service, target={"kind": "HOOK_SOURCE", "scope": "USER", "source_kind": "LOCAL"}, **arguments
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


@pytest.mark.parametrize("kind", ["mcp", "skill"])
def test_user_capability_toggles_use_the_real_core_deadline(tmp_path, monkeypatch, kind):
    from pathlib import Path
    from pulsara_agent.capability.mcp_management import LocalMcpTarget, config_guard, config_to_entry
    from pulsara_agent.capability.user_skill_config import load_user_skill_config
    from tests.test_project_capability_management import _controller_with_sessions

    home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "pulsara"))
    skill = home / ".agents/skills/demo/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Installed user skill.\n---\nRead the supplied material.\n")

    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        controller, _, host, sibling, other = _controller_with_sessions(workspace, tmp_path / "other")
        controller.inspect_user_capabilities = AsyncMock(return_value={})
        manager = controller.core.mcp_management
        target = LocalMcpTarget("demo")
        try:
            if kind == "mcp":
                await manager.create(target, {"transport": {"type": "streamable_http", "endpoint": "https://example.org/mcp"}})
            for enabled in (False, True):
                if kind == "mcp":
                    current = manager.inspect(target)
                    entry = {**config_to_entry(current), "enabled": enabled}
                    outcome = await controller.update_user_mcp_server(
                        server_id=target.server_id, config=entry, expected=config_guard(current),
                    )
                    assert manager.inspect(target).enabled is enabled
                else:
                    outcome = await controller.set_user_skill_enabled(skill_path=str(skill), enabled=enabled)
                    assert load_user_skill_config().enabled_for(skill) is enabled
                assert outcome["operation"]["success"]
                assert outcome["adoption"]["pending_sessions"] == 3
            assert (host.refresh_requests, sibling.refresh_requests, other.refresh_requests) == (2, 2, 2)
        finally:
            await manager.aclose()

    asyncio.run(run())


def test_gui_mcp_create_and_remove_preserves_credential_ownership(tmp_path, monkeypatch):
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
        # A newly saved secret remains private and is removed with its owner.
        secret = "fixture-managed-notice-secret"
        server_id = f"demo-{secret}"
        target = LocalMcpTarget(server_id)
        binding = McpCredentialBinding(target.owner, "bearer")
        created = await controller.create_user_mcp_server(
            server_id=server_id,
            config={
                "enabled": False,
                "transport": {"type": "streamable_http", "endpoint": "https://example.org/mcp"},
                "auth": {"type": "bearer", "reference": secret_to_dict(ManagedLocalCredentialReference(binding))},
            },
            secret_changes=(McpSecretMutation(binding, secret),),
        )
        assert manager.settings.read().mcp_secret(binding) == secret
        assert secret not in json.dumps(created)
        host.request_capability_refresh.assert_awaited_once_with()
        removed = await controller.remove_user_mcp_server(
            server_id=server_id, expected=config_guard(manager.inspect(target)),
        )
        assert secret not in json.dumps(removed)
        assert manager.settings.read().mcp_secret(binding) is None
        assert host.request_capability_refresh.await_count == 2
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
            await invoke(service, action="SET_HOOK_SOURCE_ENABLED", enabled=False, target={"kind":'HOOK_SOURCE', 'scope':"USER", 'source_kind':"LOCAL"})
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


@pytest.mark.parametrize("secret", ["status", "RELOADED", "USER_FILE"])
def test_reload_status_protocol_is_fixed_and_source_paths_are_dynamic(tmp_path, monkeypatch, secret):
    import pulsara_agent.conversation_kernel.host as host_module
    from pulsara_agent.hooks.executor import HookSecretScrubSet

    async def run():
        service = preparation(tmp_path)
        scrub = HookSecretScrubSet()
        scrub.observe(secret)
        monkeypatch.setattr(host_module.HookSecretScrubSet, "capture", lambda: scrub)
        host = SimpleNamespace(_hooks=SimpleNamespace(reload=AsyncMock(return_value=service.hooks.discover())),
                               _lock=asyncio.Lock(), _closing=False, _closed=False)
        result = await KernelHostSession.reload_hooks(host, deadline_monotonic=None)
        assert result["status"] == "RELOADED"
        assert next(source for source in result["sources"] if source["source"] == "USER_FILE")
        assert all(secret not in source["path"] for source in result["sources"])
        await service.mcp.aclose()
    asyncio.run(run())


def test_saved_credential_projection_preserves_public_context():
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


def test_unavailable_settings_do_not_gate_hook_mutation(tmp_path, monkeypatch):
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


def test_queued_native_remove_cleans_credentials_added_while_waiting(tmp_path, monkeypatch):
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
        async with manager.lane:
            remove = asyncio.create_task(controller.remove_user_mcp_server(server_id=server_id, expected=config_guard(manager.inspect(target))))
            while not manager.lane._waiters:
                await asyncio.sleep(0)
            assert not remove.done()
            # A secret installed while REMOVE is queued still belongs to the
            # exact owner selected by the native removal.
            binding = McpCredentialBinding(target.owner, "bearer")
            await manager.settings.replace_mcp_secrets(target.owner, ((binding, secret),))
        result = await remove
        assert result["operation"]["success"]
        assert secret not in json.dumps(result)
        assert manager.settings.read().mcp_secret(binding) is None
        host.request_capability_refresh.assert_awaited_once_with()
        await manager.aclose()

    asyncio.run(run())
