"""Round 9.3 local Agent Plugin product and architecture gates."""

from __future__ import annotations

from inspect import signature
import asyncio
import json
import os
from pathlib import Path
from time import monotonic

from pulsara_agent.capability.plugin_skill_contracts import (
    PluginSkillDefinitionsDisposition,
)
from pulsara_agent.capability.pulsara_home import resolve_pulsara_home
from pulsara_agent.cli import build_parser
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.hooks.contracts import (
    FrozenHookDefinitionView,
    HookSourceKind,
    HookTrustDisposition,
)
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.mcp_config import McpScopePolicy
from pulsara_agent.plugins.contracts import (
    GcLocalPluginPackagesRequest,
    InspectLocalPluginsRequest,
    InstallLocalPluginRequest,
    PluginEnablementDisposition,
    PluginGcDisposition,
    PluginInspectionDisposition,
    PluginInstallDisposition,
    NeverCancelPluginOperation,
    PluginRemovalDisposition,
    PluginScopeKind,
    PluginValidationDisposition,
    RemoveLocalPluginRequest,
    ValidateLocalPluginSourceRequest,
)
from pulsara_agent.plugins.hook_adapter import compose_hook_definition_view
from pulsara_agent.plugins.management import PluginManagementService
from pulsara_agent.plugins.mcp_adapter import (
    framed_plugin_mcp_server_id,
    normalize_plugin_mcp_configs,
)
from pulsara_agent.plugins.package_store import ManagedPluginStore
from pulsara_agent.plugins.skill_producer import PluginSkillDefinitionProducer
from pulsara_agent.plugins.view import EnabledPluginViewOwner
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.support.plugin_review import enable_reviewed_plugin


def _package(root: Path, *, marker: str = "one") -> Path:
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "fixture-plugin",
                "version": marker,
                "description": "Round 9.3 fixture",
            }
        ),
        encoding="utf-8",
    )
    skill = root / "skills" / "fixture-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: fixture-skill\ndescription: Use the fixture Skill.\n---\n\n"
        f"# Fixture\n\nordinary body {marker}\n",
        encoding="utf-8",
    )
    server = root / "server.py"
    server.write_text("#!/usr/bin/env python3\nprint('fixture')\n", encoding="utf-8")
    server.chmod(0o755)
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "local:server": {
                        "type": "stdio",
                        "command": "./server.py",
                        "args": ["${PLUGIN_ROOT}", "${PLUGIN_DATA}", marker],
                        "cwd": "${PLUGIN_DATA}",
                        "env": {"FIXTURE_PACKAGE": "${PLUGIN_ROOT}"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    hook = root / "dev.pulsara" / "hooks"
    hook.mkdir(parents=True)
    (hook / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        'printf \'{"additionalContext":'
                                        '"plugin context"}\''
                                    ),
                                }
                            ],
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "exec_process",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "printf '{\"continue\":true}'",
                                }
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _install(service, request):
    import asyncio
    from pulsara_agent.capability.mcp_management import LocalMcpManagementService
    from pulsara_agent.settings import LocalSettingsStore, LOCAL_SETTINGS_FILE_NAME

    home = service._store().home
    connections = LocalMcpManagementService(
        LocalSettingsStore(home / LOCAL_SETTINGS_FILE_NAME),
        user_config_path=home / "mcp.yaml",
    )
    return asyncio.run(service.install_local_plugin(request, connections=connections))


def _owners(tmp_path: Path, *, credential: str = ""):
    home = resolve_pulsara_home(str(tmp_path / "home"))
    boundary = ProcessCredentialBoundary(credential)
    service = PluginManagementService(
        credential_boundary=boundary,
        pulsara_home_resolution=home,
    )
    store = ManagedPluginStore(
        pulsara_home=home,
        credential_boundary=boundary,
    )
    return boundary, service, store


def test_round9_3_six_operations_close_local_lifecycle(tmp_path: Path) -> None:
    source = _package(tmp_path / "source")
    _boundary, service, _store = _owners(tmp_path)
    deadline = monotonic() + 30
    validation = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(source, deadline)
    )
    assert validation.disposition is PluginValidationDisposition.VALID
    installed = _install(
        service, InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    assert installed.enabled is False
    inspected = service.inspect_local_plugins(
        InspectLocalPluginsRequest(deadline, workspace_root=tmp_path)
    )
    assert inspected.disposition is PluginInspectionDisposition.COMPLETE
    assert inspected.instances[0].enabled is False
    inspected.close()
    enabled = enable_reviewed_plugin(service, installed, deadline)
    assert enabled.disposition is PluginEnablementDisposition.ENABLED
    enabled_inspection = service.inspect_local_plugins(
        InspectLocalPluginsRequest(deadline, workspace_root=tmp_path)
    )
    assert enabled_inspection.disposition is PluginInspectionDisposition.COMPLETE
    effective = enabled_inspection.instances[0]
    assert effective.effective_skill_names == ("fixture-skill",)
    assert effective.effective_mcp_server_ids == (
        framed_plugin_mcp_server_id("fixture-plugin", "local:server"),
    )
    assert effective.effective_hook is True
    assert effective.effective_hook_definition_count == 2
    assert effective.effective_hook_trust_disposition is HookTrustDisposition.UNTRUSTED
    enabled_inspection.close()
    removed = asyncio.run(
        service.remove_local_plugin(
            RemoveLocalPluginRequest(
                PluginScopeKind.USER,
                "fixture-plugin",
                deadline,
                installed.package_install_id,
            )
        )
    )
    assert removed.disposition is PluginRemovalDisposition.REMOVED
    gc = service.gc_local_plugin_packages(
        GcLocalPluginPackagesRequest(deadline, workspace_root=tmp_path)
    )
    assert gc.disposition is PluginGcDisposition.COMPLETE


def test_round9_3_one_view_feeds_skill_mcp_and_hook_native_owners(
    tmp_path: Path,
) -> None:
    source = _package(tmp_path / "source")
    boundary, service, store = _owners(tmp_path)
    deadline = monotonic() + 30
    installed = _install(
        service,
        InstallLocalPluginRequest(
            source,
            PluginScopeKind.USER,
            deadline,
            mcp_scope_policies=(("local:server", McpScopePolicy.ROOT_AND_SUBAGENTS),),
        ),
    )
    enabled = enable_reviewed_plugin(service, installed, deadline)
    assert enabled.disposition is PluginEnablementDisposition.ENABLED
    view = EnabledPluginViewOwner(store=store, credential_boundary=boundary).observe(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    try:
        skills = PluginSkillDefinitionProducer().observe(view)
        assert skills.disposition is PluginSkillDefinitionsDisposition.COMPLETE
        assert [item.name for item in skills.candidates] == ["fixture-skill"]
        mcp = normalize_plugin_mcp_configs(existing_configs=(), view=view)
        assert len(mcp.configs) == 1
        config = mcp.configs[0]
        assert config.server_id == framed_plugin_mcp_server_id(
            "fixture-plugin", "local:server"
        )
        assert config.required is False
        assert config.scope_policy.value == "ROOT_AND_SUBAGENTS"
        assert config.effect_policy.default_effect.value == "AUTO"
        transport = config.transport
        assert str(view.user_instances[0].package_root) in transport.args[0]
        assert str(view.user_instances[0].data_root) in transport.args[1]

        local_provider = LocalHookSourceProvider(
            workspace_root=tmp_path,
            workspace_kind="project",
            workspace_state_key="workspace-test",
            pulsara_home=store.home,
        )
        hooks = compose_hook_definition_view(
            local_view=local_provider.discover(),
            plugin_view=view,
            trust_store=local_provider.trust_store,
        )
        plugin = next(
            item
            for item in hooks.source_snapshots
            if item.provenance.identity.kind is HookSourceKind.PLUGIN
        )
        assert plugin.trust.disposition is HookTrustDisposition.UNTRUSTED
        assert dict(plugin.provenance.declaration_environment) == {
            "PLUGIN_DATA": str(view.user_instances[0].data_root),
            "PLUGIN_ROOT": str(view.user_instances[0].package_root),
            "CLAUDE_PLUGIN_DATA": str(view.user_instances[0].data_root),
            "CLAUDE_PLUGIN_ROOT": str(view.user_instances[0].package_root),
        }
        local_provider.trust_store.trust(
            plugin.provenance.trust_subject,
            expected_digest=plugin.trust.current_definition_digest or "",
        )
        trusted = compose_hook_definition_view(
            local_view=FrozenHookDefinitionView(
                tuple(
                    item
                    for item in hooks.source_snapshots
                    if item.provenance.identity.kind is not HookSourceKind.PLUGIN
                )
            ),
            plugin_view=view,
            trust_store=local_provider.trust_store,
        )
        trusted_plugin = next(
            item
            for item in trusted.source_snapshots
            if item.provenance.identity.kind is HookSourceKind.PLUGIN
        )
        assert trusted_plugin.trust.disposition is HookTrustDisposition.TRUSTED
    finally:
        view.close()


def test_round9_3_current_borrowed_credential_is_rejected_from_name_and_bytes(
    tmp_path: Path,
) -> None:
    secret = "round-9-3-exact-secret"
    source = _package(tmp_path / f"source-{secret}")
    boundary, service, _store = _owners(tmp_path, credential=secret)
    assert boundary.last_boundary_snapshot == secret
    outcome = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(source, monotonic() + 30)
    )
    assert outcome.disposition is PluginValidationDisposition.INVALID
    assert secret not in repr(outcome)


def test_round9_3_cli_and_dependency_direction_are_single_path() -> None:
    parser = build_parser()
    for command in (
        ("validate", "/tmp/package"),
        ("add", "--scope", "user", "/tmp/package"),
        ("enable", "--scope", "user", "--yes", "fixture-plugin"),
        ("disable", "--scope", "user", "fixture-plugin"),
        ("remove", "--scope", "user", "fixture-plugin"),
        ("list",),
        ("doctor",),
        ("gc",),
    ):
        parsed = parser.parse_args(("plugins", *command))
        assert parsed.plugins_command == command[0]
    root = Path(__file__).parents[1] / "src" / "pulsara_agent"
    for relative in (
        "capability",
        "hooks",
        "conversation_kernel/mcp",
    ):
        text = "\n".join(
            item.read_text(encoding="utf-8") for item in (root / relative).glob("*.py")
        )
        assert "pulsara_agent.plugins" not in text
    assert not any(
        name in os.environ
        for name in ("PULSARA_PLUGIN_PROFILE", "PULSARA_PLUGIN_LAYOUT")
    )
    assert "credential_boundary" in signature(KernelHostCore.production).parameters


def test_validation_parses_hook_declarations_without_plugin_provenance(tmp_path, monkeypatch):
    from pulsara_agent.plugins import package_core

    source = _package(tmp_path / "source")
    boundary, service, _ = _owners(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("prevalidation must not construct Hook provenance")
    with monkeypatch.context() as patch:
        patch.setattr(package_core, "PluginHookSourceIdentity", forbidden)
        result = service.validate_local_plugin_source(
            ValidateLocalPluginSourceRequest(source, monotonic() + 30)
        )
    assert result.disposition is PluginValidationDisposition.VALID
    assert len(result.summary.hooks.hook_definitions) == 2
    installed = _install(service, InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30))
    inspected = service.inspect_local_plugins(InspectLocalPluginsRequest(monotonic() + 30))
    try:
        bound = inspected.instances[0].hook_config
        assert bound is not None
        assert bound.provenance.identity.package_install_id == installed.package_install_id
        assert all(item.provenance is bound.provenance for item in bound.definitions)
    finally:
        inspected.close()


def test_secret_package_id_stays_raw_for_cas_and_is_withheld_in_public_output(tmp_path, monkeypatch):
    from pulsara_agent.cli import _plugin_install_payload, _plugin_inspection_payload, _plugin_enablement_payload
    from pulsara_agent.identity import random_id
    from pulsara_agent.plugins import package_store
    from pulsara_agent.plugins.contracts import SetLocalPluginEnabledRequest
    from pulsara_agent.cli import _public_plugin_cli_payload

    source = _package(tmp_path / "source")
    package = random_id("package")
    monkeypatch.setattr(package_store, "random_id", lambda kind: package if kind == "package" else random_id(kind))
    boundary, service, _ = _owners(tmp_path, credential=package)
    deadline = monotonic() + 30
    installed = _install(service, InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline))
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    assert installed.package_install_id == package
    public = _public_plugin_cli_payload(_plugin_install_payload(installed), installed.scrub_set)
    assert public["package_install_id"] == {"visibility": "WITHHELD"}
    assert package not in json.dumps(public)

    inspection = service.inspect_local_plugins(InspectLocalPluginsRequest(deadline))
    try:
        assert inspection.instances[0].package_install_id == package
        assert inspection.instances[0].package_root.name == package
        assert inspection.instances[0].hook_config.provenance.identity.package_install_id == package
        public = _public_plugin_cli_payload(_plugin_inspection_payload(inspection, doctor=True, projection="doctor"), inspection.scrub_set)
        assert public["instances"][0]["package_install_id"] == {"visibility": "WITHHELD"}
        assert package not in json.dumps(public)
    finally:
        inspection.close()
    changed = service.set_local_plugin_enabled(SetLocalPluginEnabledRequest(
        PluginScopeKind.USER, "fixture-plugin", False, package, deadline, connection_review=(),
    ))
    assert changed.disposition is PluginEnablementDisposition.ALREADY_DISABLED
    assert changed.package_install_id == package
    public = _public_plugin_cli_payload(_plugin_enablement_payload(changed, reviewed={}), changed.scrub_set)
    assert public["package_install_id"] == {"visibility": "WITHHELD"}
    assert package not in json.dumps(public)
    stale_id = random_id("package")
    stale = service.set_local_plugin_enabled(SetLocalPluginEnabledRequest(
        PluginScopeKind.USER, "fixture-plugin", False, stale_id, deadline, connection_review=(),
    ))
    assert stale.disposition is PluginEnablementDisposition.STALE
    public = _public_plugin_cli_payload(
        _plugin_enablement_payload(stale, reviewed={}), stale.scrub_set)
    assert public["expected_package_install_id"] == {"visibility": "VISIBLE", "value": stale_id}
    assert public["observed_package_install_id"] == {"visibility": "WITHHELD"}
    from pulsara_agent.cli import _plugin_removal_payload
    removed = asyncio.run(service.remove_local_plugin(RemoveLocalPluginRequest(
        PluginScopeKind.USER, "fixture-plugin", deadline, package,
    )))
    assert removed.disposition is PluginRemovalDisposition.REMOVED
    public = _public_plugin_cli_payload(
        _plugin_removal_payload(removed), removed.scrub_set)
    assert public["prior_package_install_id"] == {"visibility": "WITHHELD"}
    assert package not in json.dumps(public)


def test_model_omitted_guard_freezes_raw_id_while_form_withholds_it(tmp_path):
    from pulsara_agent.capability.management_form import AcceptedCapabilityFormSubmission
    from pulsara_agent.capability.mcp_management import McpManagementConflict
    from pulsara_agent.conversation_kernel.capability_management_execution import CapabilityManagementCall
    from pulsara_agent.primitives.context import thaw_json
    from tests.test_capability_management_preparation import preparation
    import pytest

    async def run():
        service = preparation(tmp_path)
        source = _package(tmp_path / "source")
        installed = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 20),
            connections=service.mcp,
        )
        package = installed.package_install_id
        service.plugins._credential_boundary.rotate_sync(package[10:20])
        intent = {"action": "SET_PLUGIN_ENABLED", "enabled": False,
                  "target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "fixture-plugin"}}
        prepared = await service.prepare(intent)
        assert thaw_json(prepared.expected_current)["expected_package_install_id"] == package
        form_call = CapabilityManagementCall(service, prepared)
        form = form_call.form("review")
        public = thaw_json(form.public_projection)
        assert public["plugin"]["package_install_id"] == {"visibility": "WITHHELD"}
        assert public["expected_current"]["expected_package_install_id"] == {"visibility": "WITHHELD"}
        assert package[10:20] not in json.dumps(public)
        form_call.accept(AcceptedCapabilityFormSubmission(await form.prepare_submission({})))
        assert (await form_call.execute())["status"] == "APPLIED"
        explicit = await service.prepare({**intent, "expected_package_install_id": package})
        assert thaw_json(explicit.expected_current)["expected_package_install_id"] == package
        with pytest.raises(McpManagementConflict):
            await service.prepare({**intent, "expected_package_install_id": "package:aaaaaaaaaaaaaaaaaaaaaaaaaa"})
        await service.mcp.aclose()
    asyncio.run(run())


def test_install_ack_unknown_keeps_real_id_and_public_cut_when_secret_matches(tmp_path, monkeypatch):
    from pulsara_agent.cli import _plugin_install_payload
    from pulsara_agent.identity import random_id
    from pulsara_agent.plugins import package_store
    from pulsara_agent.cli import _public_plugin_cli_payload

    source = _package(tmp_path / "source")
    package = random_id("package")
    monkeypatch.setattr(package_store, "random_id", lambda kind: package if kind == "package" else random_id(kind))
    original_read = ManagedPluginStore.read_state
    def read_until_committed(store, layout):
        if layout.state_path.exists():
            raise OSError("confirmation unavailable")
        return original_read(store, layout)
    monkeypatch.setattr(ManagedPluginStore, "read_state", read_until_committed)
    _, service, _ = _owners(tmp_path, credential=package)
    result = _install(service, InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30))
    assert result.disposition is PluginInstallDisposition.ACK_UNKNOWN
    assert result.attempted_package_install_id == package
    assert result.last_known_cut == "STATE_RENAME_FULL_CONFIRMATION_UNAVAILABLE"
    public = _public_plugin_cli_payload(_plugin_install_payload(result), result.scrub_set)
    assert public["attempted_package_install_id"] == {"visibility": "WITHHELD"}
    assert public["last_known_cut"] == result.last_known_cut
    assert package not in json.dumps(public)
