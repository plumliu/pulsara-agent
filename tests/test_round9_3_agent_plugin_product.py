"""Round 9.3 local Agent Plugin product and architecture gates."""

from __future__ import annotations

from inspect import signature
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
from pulsara_agent.plugins.contracts import (
    ExternalProcessAcceptance,
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
    SetLocalPluginEnabledRequest,
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
                                        "printf '{\"additionalContext\":"
                                        "\"plugin context\"}'"
                                    ),
                                }
                            ],
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "terminal",
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
    installed = service.install_local_plugin(
        InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    assert installed.enabled is False
    inspected = service.inspect_local_plugins(
        InspectLocalPluginsRequest(deadline, workspace_root=tmp_path)
    )
    assert inspected.disposition is PluginInspectionDisposition.COMPLETE
    assert inspected.instances[0].enabled is False
    inspected.close()
    enabled = service.set_local_plugin_enabled(
        SetLocalPluginEnabledRequest(
            PluginScopeKind.USER,
            "fixture-plugin",
            True,
            installed.package_install_id,
            deadline,
            external_process_acceptance=ExternalProcessAcceptance.ACCEPTED,
        )
    )
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
    assert (
        effective.effective_hook_trust_disposition
        is HookTrustDisposition.UNTRUSTED
    )
    enabled_inspection.close()
    removed = service.remove_local_plugin(
        RemoveLocalPluginRequest(
            PluginScopeKind.USER, "fixture-plugin", deadline
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
    installed = service.install_local_plugin(
        InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    enabled = service.set_local_plugin_enabled(
        SetLocalPluginEnabledRequest(
            PluginScopeKind.USER,
            "fixture-plugin",
            True,
            installed.package_install_id,
            deadline,
            external_process_acceptance=ExternalProcessAcceptance.ACCEPTED,
        )
    )
    assert enabled.disposition is PluginEnablementDisposition.ENABLED
    view = EnabledPluginViewOwner(
        store=store, credential_boundary=boundary
    ).observe(
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
            item.read_text(encoding="utf-8")
            for item in (root / relative).glob("*.py")
        )
        assert "pulsara_agent.plugins" not in text
    assert not any(
        name in os.environ
        for name in ("PULSARA_PLUGIN_PROFILE", "PULSARA_PLUGIN_LAYOUT")
    )
    assert "credential_boundary" in signature(KernelHostCore.production).parameters
