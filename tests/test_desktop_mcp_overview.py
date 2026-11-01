"""The device inventory observes loaded owners, without choosing a conversation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from pulsara_agent import mcp_config
from pulsara_agent.desktop_app import session_controller as controller_module
from pulsara_agent.desktop_app.session_controller import LocalSessionController


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "mcp.yaml"
    path.write_text(
        "servers:\n  docs:\n    transport:\n"
        "      type: streamable_http\n      endpoint: https://docs.example/mcp\n"
    )
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path))
    monkeypatch.setattr(controller_module, "_user_plugins_payload", lambda *_: {"status": "ready", "items": [], "details": []})
    return mcp_config.load_mcp_server_configs(user_config_path=path)[0]


def inspection(config, status="READY", names=("search",), *, resources=0,
               source="USER", matches=True, identity=None, instructions=""):
    return SimpleNamespace(
        mcp_catalog=SimpleNamespace(servers=(SimpleNamespace(
            server_id=config.server_id,
            status=SimpleNamespace(value=status),
            exposed_tool_count=len(names),
            resource_count=resources,
            resource_template_count=0,
            prompt_count=0,
            sanitized_instructions=instructions,
            stable_failure_category="MCPError" if status.startswith("FAILED") else None,
        ),)),
        mcp_configured_servers=(SimpleNamespace(
            server_id=config.server_id,
            resolved_config_identity=identity or config.resolved_config_identity,
            source_kind=source,
            status_matches_config=matches,
        ),),
        mcp_tools=tuple(SimpleNamespace(
            server_id=config.server_id, provider_name=name, remote_name=name,
            description=name, effect="READ_ONLY", available_to_subagents=False,
            parallel_safe=False,
        ) for name in names),
    )


def project(config, *observations):
    return controller_module._user_capability_payload(
        skills={}, mcp_configs=(config,), plugins=object(),
        secret_resolver=lambda _: None, live_inspections=observations,
    )["mcp"]["servers"][0]


def test_ready_owner_wins_and_directory_stays_coherent(config):
    ready = inspection(config, names=("search", "read"), resources=2,
                       instructions="ready owner")
    failed = inspection(config, "FAILED_RETRYABLE", ("failed-only",), resources=12)
    discovering = inspection(config, "DISCOVERING", ("pending-only",))
    smaller = inspection(config, names=("other-ready-only",), resources=20)
    forward = project(config, failed, discovering, ready, smaller)
    reverse = project(config, smaller, ready, discovering, failed)
    assert forward == reverse
    assert forward["status"] == "READY"
    assert forward["tool_count"] == 2
    assert [tool["name"] for tool in forward["tools"]] == ["search", "read"]
    assert forward["resource_count"] == 2
    assert forward["instructions"] == "ready owner"
    assert forward["has_failure"] is False


@pytest.mark.parametrize("foreign", [
    {"source": "WORKSPACE"},
    {"source": "MANAGED_PACKAGE"},
    {"identity": "old-user-config"},
    {"matches": False},
])
def test_foreign_or_stale_owner_cannot_override_current_user_config(config, foreign):
    failed = inspection(config, "FAILED_RETRYABLE", ())
    result = project(config, inspection(config, **foreign), failed)
    assert result["status"] == "FAILED_RETRYABLE"
    assert result["tool_count"] == 0
    assert result["tools"] == []
    assert project(config, inspection(config, **foreign))["status"] == "CONFIGURED"


def test_disabled_current_config_wins_over_loaded_observation(config, tmp_path):
    path = tmp_path / "disabled.yaml"
    entry = controller_module.config_to_entry(config)
    entry["enabled"] = False
    path.write_text(yaml.safe_dump({"servers": {config.server_id: entry}}))
    disabled = mcp_config.load_mcp_server_configs(user_config_path=path)[0]
    result = project(disabled, inspection(config))
    assert result["status"] == "DISABLED"
    assert result["tools"] == []


def test_inventory_reads_all_loaded_owners_and_drops_retired_owner(config, monkeypatch):
    async def exercise():
        monkeypatch.setattr(controller_module, "_inspect_user_skills", lambda: {"status": "ready", "items": [], "issues": [], "roots": [], "details": []})
        controller = object.__new__(LocalSessionController)
        controller._lock = asyncio.Lock()
        controller._operations = {}
        controller.core = SimpleNamespace(
            mcp_management=SimpleNamespace(
                load_configs=lambda: (config,),
                settings=SimpleNamespace(read=lambda: SimpleNamespace(mcp_secret=lambda _: None)),
            ),
            inspect_user_plugins=AsyncMock(return_value=object()),
        )
        current = inspection(config, "FAILED_RETRYABLE", ())
        ready = inspection(config, names=("search", "read"))
        controller._by_session = {
            "session:old": SimpleNamespace(session=SimpleNamespace(inspect_capability_catalog=lambda: current)),
            "session:new": SimpleNamespace(session=SimpleNamespace(inspect_capability_catalog=lambda: ready)),
        }

        async def read():
            payload = await controller.inspect_user_capabilities()
            return payload["mcp"]["servers"][0]

        assert (await read())["status"] == "READY"
        # Map insertion/selection order cannot choose the observed conversation.
        controller._by_session = dict(reversed(tuple(controller._by_session.items())))
        assert (await read())["tool_count"] == 2
        # Lifecycle owners already quiescing are unavailable even before unpublish.
        controller._operations["session:new"] = object()
        result = await read()
        assert result["status"] == "FAILED_RETRYABLE"
        assert result["tools"] == []
        controller._by_session.clear()
        result = await read()
        assert result["status"] == "CONFIGURED"
        assert result["tools"] == []

    asyncio.run(exercise())


def test_user_inventory_keeps_real_loose_skill_removal_identity(config, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(controller_module.Path, "home", lambda: home)
    skill = tmp_path / "skills" / "inventory-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: inventory-skill\ndescription: Inventory regression fixture.\n---\n"
        "Read the supplied material.\n"
    )

    async def exercise():
        controller = object.__new__(LocalSessionController)
        controller._lock = asyncio.Lock()
        controller._operations = {}
        controller._by_session = {}
        controller.core = SimpleNamespace(
            mcp_management=SimpleNamespace(
                load_configs=lambda: (config,),
                settings=SimpleNamespace(read=lambda: SimpleNamespace(mcp_secret=lambda _: None)),
            ),
            inspect_user_plugins=AsyncMock(return_value=object()),
        )
        result = await controller.inspect_user_capabilities()
        item, = result["skills"]["items"]
        assert item["name"] == "inventory-skill"
        root = skill.parent.stat()
        directory = skill.stat()
        assert item["removal_identity"] == {
            "root_device": str(root.st_dev),
            "root_inode": str(root.st_ino),
            "directory_device": str(directory.st_dev),
            "directory_inode": str(directory.st_ino),
        }
        assert result["mcp"]["servers"][0]["id"] == config.server_id

    asyncio.run(exercise())


def test_workspace_skill_target_is_exact_and_collisions_fail(tmp_path, monkeypatch):
    from pulsara_agent.identity import derive_id
    from pulsara_agent.capability.local_skill_publisher import LocalSkillInstallScope
    items = [{"root": "agents", "name": "one", "path": str(tmp_path / "one")},
             {"root": "pulsara", "name": "two", "path": str(tmp_path / "two")}]
    class Inventory:
        def inspect_loose_skills(self, **_kwargs):
            return {"status": "COMPLETE", "config_available": True, "items": [dict(item) for item in items]}, None, None
    monkeypatch.setattr(controller_module, "LocalSkillManagementService", Inventory)
    monkeypatch.setattr(controller_module, "_skill_removal_identity", lambda *_: None)
    observed = controller_module._inspect_loose_skill_management(LocalSkillInstallScope.WORKSPACE, tmp_path)
    assert observed["items"][0]["id"] == derive_id("skill", "ui-target", "agents", "one")
    assert controller_module._workspace_skill_path(observed, observed["items"][0]["id"]) == (tmp_path / "one").resolve()
    with pytest.raises(ValueError):
        controller_module._workspace_skill_path(observed, "agents:one")
    monkeypatch.setattr(controller_module, "derive_id", lambda *_: "skill:" + "a" * 26)
    with pytest.raises(ValueError, match="identity collision"):
        controller_module._inspect_loose_skill_management(LocalSkillInstallScope.WORKSPACE, tmp_path)


def test_readonly_skill_winners_use_source_name_and_reject_collisions(tmp_path, monkeypatch):
    from pulsara_agent.identity import derive_id
    class Catalog:
        def __init__(self):
            self.candidate_issues = ()
            self.root_policy = SimpleNamespace(roots=())
            self.winners = tuple(SimpleNamespace(
                path=tmp_path / name / "SKILL.md", source=SimpleNamespace(value=source),
                name=name, description="", skill_path=str(tmp_path / name / "SKILL.md"),
                authoring_diagnostic_codes=(),
            ) for source, name in (("user", "one"), ("plugin", "two")))
    monkeypatch.setattr(controller_module, "CompleteEffectiveSkillCatalogInspection", Catalog)
    inspection = SimpleNamespace(mcp_tools=(), mcp_catalog=SimpleNamespace(servers=(), provider_name_collision_facts=()),
        mcp_configured_servers=(), skill_catalog=Catalog(), configured_active_skill_names=frozenset())
    kwargs = dict(session_id="session:" + "a" * 26, workspace_root=tmp_path, workspace_kind="project",
        workspace_skills={"items": []}, workspace_mcp_configs=(), trust_all_workspace_mcp=False,
        workspace_mcp_approvals={}, refresh_pending=False, refresh_attention=None, inspection=inspection)
    result = controller_module._capability_payload(**kwargs)
    assert [item["id"] for item in result["skills"]["items"]] == [
        derive_id("skill", "ui-target", "user", "one"), derive_id("skill", "ui-target", "plugin", "two")]
    monkeypatch.setattr(controller_module, "derive_id", lambda *_: "skill:" + "a" * 26)
    with pytest.raises(ValueError, match="identity collision"):
        controller_module._capability_payload(**kwargs)


def test_plugin_operation_preserves_cut_facts_and_withholds_secret_id(tmp_path):
    from pulsara_agent.plugins.contracts import (
        CleanupUnavailablePluginInstallOutcome, FailedPluginEnablementOutcome, FailedPluginInstallOutcome, FailedPluginRemovalOutcome,
        PluginCleanupLocationStatus, PluginDiagnostic, PluginDiagnosticCode,
        PluginEnablementDisposition, PluginInstallDisposition, PluginRemovalDisposition,
        PluginInstanceIdentity, PluginScopeKind,
    )
    from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet
    secret_id = "package:" + "a" * 26
    visible_id = "package:" + "b" * 25 + "a"
    scrub = ProcessCredentialScrubSet()
    scrub.observe(secret_id)
    identity = PluginInstanceIdentity(PluginScopeKind.USER, "demo")
    install = FailedPluginInstallOutcome(PluginInstallDisposition.ACK_UNKNOWN, tmp_path,
        attempted_package_install_id=secret_id, intended_enabled=False, last_known_cut="written", scrub_set=scrub)
    enabled = FailedPluginEnablementOutcome(PluginEnablementDisposition.STALE, identity, True,
        expected_package_install_id=secret_id, observed_package_install_id=visible_id, scrub_set=scrub)
    removed = FailedPluginRemovalOutcome(PluginRemovalDisposition.ACK_UNKNOWN, identity,
        prior_package_install_id=secret_id, last_known_cut="removed", scrub_set=scrub)
    for outcome, builder, field in ((install, controller_module._plugin_install_operation, "attempted_package_install_id"),
                                    (enabled, controller_module._plugin_enablement_operation, "expected_package_install_id"),
                                    (removed, controller_module._plugin_removal_operation, "prior_package_install_id")):
        public = controller_module._public_plugin_operation(outcome, builder(outcome))
        assert public[field] == {"visibility": "WITHHELD"}
        assert public["status"] == outcome.disposition.value
        assert secret_id not in str(public)
    public = controller_module._public_plugin_operation(enabled, controller_module._plugin_enablement_operation(enabled))
    assert public["observed_package_install_id"] == {"visibility": "VISIBLE", "value": visible_id}
    assert controller_module._public_plugin_operation(install, controller_module._plugin_install_operation(install))["last_known_cut"] == "written"
    wrapped = CleanupUnavailablePluginInstallOutcome(install, tmp_path, PluginCleanupLocationStatus.UNKNOWN,
        PluginDiagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE, "cleanup"), scrub_set=scrub)
    public = controller_module._public_plugin_operation(wrapped, controller_module._plugin_install_operation(wrapped))
    assert public["status"] == "CLEANUP_UNAVAILABLE"
    assert public["prior_operation_status"] == "ACK_UNKNOWN"
    assert public["attempted_package_install_id"] == {"visibility": "WITHHELD"}
