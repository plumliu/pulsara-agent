"""Plugin adoption keeps usable contained content when one path is unusable."""
from __future__ import annotations

from contextlib import closing
import asyncio
import json
import subprocess
from pathlib import Path
from time import monotonic

import pytest

from pulsara_agent.plugins.contracts import (
    GcLocalPluginPackagesRequest,
    InspectLocalPluginsRequest,
    InstallLocalPluginRequest,
    PluginDiagnosticCode,
    PluginGcDisposition,
    PluginInstallDisposition,
    PluginRemovalDisposition,
    PluginScopeKind,
    PluginValidationDisposition,
    ValidateLocalPluginSourceRequest,
    RemoveLocalPluginRequest,
)
from tests.test_round9_3_plugin_physical_semantics import _install, _make_package, _owners


@pytest.fixture
def owners(tmp_path: Path):
    owners = _owners(tmp_path)
    yield owners
    _, _, service, _ = owners
    with closing(service.inspect_local_plugins(InspectLocalPluginsRequest(monotonic() + 30))) as inspected:
        instances = tuple(inspected.instances)
    for instance in instances:
        removal = asyncio.run(service.remove_local_plugin(RemoveLocalPluginRequest(
            instance.identity.scope, instance.identity.plugin_id, monotonic() + 30,
            instance.package_install_id,
        )))
        assert removal.disposition is PluginRemovalDisposition.REMOVED
    gc = service.gc_local_plugin_packages(GcLocalPluginPackagesRequest(monotonic() + 30))
    assert gc.disposition is PluginGcDisposition.COMPLETE


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _hook(command: str) -> dict:
    return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}


def test_install_preserves_contained_links_and_reopens_same_components(tmp_path: Path, owners) -> None:
    _, _, service, store = owners
    source = _make_package(tmp_path / "source")
    (source / "plugin.json").rename(source / "manifest-data.json")
    (source / "plugin.json").symlink_to("manifest-data.json")
    (source / "skills").rename(source / "skill-resources")
    (source / "skills").symlink_to("skill-resources", target_is_directory=True)
    (source / "server.py").rename(source / "server-implementation.py")
    (source / "server.py").symlink_to("server-implementation.py")
    # Path ordering and wire/path-text ordering differ at punctuation boundaries.
    # Both are valid resources and must survive the exact stage-shape check.
    (source / "lib.txt").write_text("sibling resource")
    library = source / "lib"
    library.mkdir()
    (library / "data.txt").write_text("resource next to the real script")
    (library / "run.cjs").write_text(
        "console.log(require('node:fs').readFileSync(require('node:path').join(__dirname, 'data.txt'), 'utf8'));"
    )
    (source / "launch.cjs").symlink_to(library / "run.cjs")
    root_alias = tmp_path / "selected-directory"
    root_alias.symlink_to(source, target_is_directory=True)
    installed = _install(service, InstallLocalPluginRequest(root_alias, PluginScopeKind.USER, monotonic() + 30))
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    assert installed.enabled is False
    package = store.layout(installed.identity).plugin_package_parent / installed.package_install_id
    assert (package / "skills/physical-skill/SKILL.md").read_bytes() == (source / "skills/physical-skill/SKILL.md").read_bytes()
    assert (package / "server.py").read_bytes() == (source / "server-implementation.py").read_bytes()
    execution = subprocess.run(["node", str(package / "launch.cjs")], check=True, capture_output=True, text=True)
    assert execution.stdout.strip() == "resource next to the real script"
    assert (package / "launch.cjs").resolve() == package / "lib/run.cjs"
    assert (package / "skills").resolve() == package / "skill-resources"
    with closing(service.inspect_local_plugins(InspectLocalPluginsRequest(monotonic() + 30))) as inspected:
        saved = inspected.instances[0]
        assert [item.name for item in saved.summary.skills.skills] == ["physical-skill"]
        assert saved.summary.skills.skills[0].skill_path == str(package / "skill-resources/physical-skill/SKILL.md")
        assert [item.local_server_id for item in saved.summary.mcp.mcp_servers] == ["fixture"]
        assert saved.summary.mcp.mcp_servers[0].command == "./server.py"


def test_outside_skill_command_and_resources_do_not_prevent_valid_install(tmp_path: Path, owners) -> None:
    _, _, service, store = owners
    source = _make_package(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.write_text("outside bytes must not be adopted")
    outside.chmod(0o755)
    bad_skill = source / "skills/bad"
    bad_skill.mkdir()
    (bad_skill / "SKILL.md").symlink_to(outside)
    (source / "outside-resource").symlink_to(outside)
    (source / "outside-command").symlink_to(outside)
    (source / "cycle").symlink_to(".", target_is_directory=True)
    mcp = json.loads((source / "mcp.json").read_text())
    mcp["mcpServers"]["outside"] = {"type": "stdio", "command": "./outside-command"}
    _write_json(source / "mcp.json", mcp)
    installed = _install(service, InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30))
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    assert [item.name for item in installed.summary.skills.skills] == ["physical-skill"]
    assert [item.local_server_id for item in installed.summary.mcp.mcp_servers] == ["fixture"]
    assert any(item.code is PluginDiagnosticCode.MCP_SERVER_INVALID for item in installed.summary.mcp.diagnostics)
    package = store.layout(installed.identity).plugin_package_parent / installed.package_install_id
    assert (package / "server.py").read_bytes() == (source / "server.py").read_bytes()
    assert not (package / "outside-resource").exists()
    assert not (package / "cycle").exists()
    with closing(service.inspect_local_plugins(InspectLocalPluginsRequest(monotonic() + 30))) as inspected:
        assert [item.local_server_id for item in inspected.instances[0].summary.mcp.mcp_servers] == ["fixture"]


def test_outside_manifest_remains_package_fatal(tmp_path: Path, owners) -> None:
    _, _, service, _ = owners
    source = _make_package(tmp_path / "source")
    outside = tmp_path / "outside-manifest.json"
    (source / "plugin.json").rename(outside)
    (source / "plugin.json").symlink_to(outside)
    result = service.validate_local_plugin_source(ValidateLocalPluginSourceRequest(source, monotonic() + 30))
    assert result.disposition is PluginValidationDisposition.INVALID


@pytest.mark.parametrize("openai", [{"hooks": []}, {"hooks": {}}])
def test_explicit_openai_empty_does_not_load_default_or_overlay(tmp_path: Path, owners, openai: dict) -> None:
    _, _, service, _ = owners
    source = _make_package(tmp_path / "source")
    manifest = json.loads((source / "plugin.json").read_text())
    manifest["extensions"] = {"com.openai": openai}
    _write_json(source / "plugin.json", manifest)
    _write_json(source / "hooks/hooks.json", _hook("echo default"))
    _write_json(source / ".codex-plugin/plugin.json", {"hooks": _hook("echo overlay")})
    result = service.validate_local_plugin_source(ValidateLocalPluginSourceRequest(source, monotonic() + 30))
    assert result.disposition is PluginValidationDisposition.VALID
    assert result.summary.hooks.hook_definitions == ()
    assert result.summary.hooks.source_relative_path == "plugin.json"


def test_native_openai_multi_file_install_keeps_actual_source_and_order(tmp_path: Path, owners) -> None:
    _, _, service, _ = owners
    source = _make_package(tmp_path / "source")
    manifest = json.loads((source / "plugin.json").read_text())
    manifest["extensions"] = {"com.openai": {"hooks": ["./hooks/a.json", "./hooks/missing.json", "./hooks/b.json"]}}
    _write_json(source / "plugin.json", manifest)
    _write_json(source / "hooks/a.json", _hook("echo first"))
    _write_json(source / "hooks/b.json", _hook("echo second"))
    installed = _install(service, InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30))
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    with closing(service.inspect_local_plugins(InspectLocalPluginsRequest(monotonic() + 30))) as inspected:
        saved = inspected.instances[0]
        assert [item.command for item in saved.summary.hooks.hook_definitions] == ["echo first", "echo second"]
        assert saved.summary.hooks.source_relative_path == "plugin.json"
        assert saved.hook_config.provenance.identity.canonical_path == saved.package_root / "plugin.json"
        assert any("missing.json" in item.message for item in installed.diagnostics)


def test_native_unknown_or_null_extensions_keep_portable_components(tmp_path: Path, owners) -> None:
    _, _, service, _ = owners
    source = _make_package(tmp_path / "source")
    manifest = json.loads((source / "plugin.json").read_text())
    manifest["extensions"] = None
    _write_json(source / "plugin.json", manifest)
    _write_json(source / "hooks/hooks.json", _hook("echo unselected"))
    result = service.validate_local_plugin_source(ValidateLocalPluginSourceRequest(source, monotonic() + 30))
    assert result.disposition is PluginValidationDisposition.VALID
    assert [item.name for item in result.summary.skills.skills] == ["physical-skill"]
    assert result.summary.hooks.hook_definitions == ()
    assert any(item.code is PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED for item in result.diagnostics)


@pytest.mark.parametrize("component", ["mcp.json", "skills"])
def test_outside_fixed_component_is_isolated_at_install(tmp_path: Path, owners, component: str) -> None:
    _, _, service, store = owners
    source = _make_package(tmp_path / "source")
    outside = tmp_path / "outside-component"
    (source / component).rename(outside)
    (source / component).symlink_to(outside, target_is_directory=component == "skills")
    installed = _install(service, InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30))
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    if component == "mcp.json":
        assert [item.name for item in installed.summary.skills.skills] == ["physical-skill"]
        assert installed.summary.mcp.mcp_servers == ()
    else:
        assert [item.local_server_id for item in installed.summary.mcp.mcp_servers] == ["fixture"]
        assert installed.summary.skills.skills == ()
    package = store.layout(installed.identity).plugin_package_parent / installed.package_install_id
    assert not (package / component).exists()


def test_pulsara_hook_entry_wins_over_openai_and_overlay(tmp_path: Path, owners) -> None:
    _, _, service, _ = owners
    source = _make_package(tmp_path / "source")
    manifest = json.loads((source / "plugin.json").read_text())
    manifest["extensions"] = {"com.openai": {"hooks": _hook("echo openai")}}
    _write_json(source / "plugin.json", manifest)
    _write_json(source / "dev.pulsara/hooks/hooks.json", _hook("echo pulsara"))
    _write_json(source / ".codex-plugin/plugin.json", {"hooks": _hook("echo overlay")})
    result = service.validate_local_plugin_source(ValidateLocalPluginSourceRequest(source, monotonic() + 30))
    assert [item.command for item in result.summary.hooks.hook_definitions] == ["echo pulsara"]
    assert result.summary.hooks.source_relative_path == "dev.pulsara/hooks/hooks.json"


def test_selected_openai_default_and_unsupported_features_are_independent(tmp_path: Path, owners) -> None:
    _, _, service, _ = owners
    source = _make_package(tmp_path / "source")
    manifest = json.loads((source / "plugin.json").read_text())
    manifest["extensions"] = {"com.openai": {"apps": "./.app.json"}}
    _write_json(source / "plugin.json", manifest)
    _write_json(source / "hooks/hooks.json", _hook("echo selected-default"))
    result = service.validate_local_plugin_source(ValidateLocalPluginSourceRequest(source, monotonic() + 30))
    assert [item.command for item in result.summary.hooks.hook_definitions] == ["echo selected-default"]
    assert any("apps" in item.message for item in result.summary.hooks.diagnostics)


@pytest.mark.parametrize("blocked", ["leaf", "namespace", "linked-parent"])
def test_rejected_pulsara_source_does_not_activate_openai_fallback(tmp_path: Path, owners, blocked: str) -> None:
    _, _, service, _ = owners
    source = _make_package(tmp_path / "source")
    manifest = json.loads((source / "plugin.json").read_text())
    manifest["extensions"] = {"com.openai": {"hooks": _hook("echo must-not-activate")}}
    _write_json(source / "plugin.json", manifest)
    outside = tmp_path / "outside-hook"
    _write_json(outside, _hook("echo outside"))
    if blocked == "namespace":
        (source / "dev.pulsara").symlink_to(tmp_path, target_is_directory=True)
    else:
        hook_root = source / "dev.pulsara/hooks"
        hook_root.mkdir(parents=True)
        (hook_root / "hooks.json").symlink_to(outside)
        if blocked == "linked-parent":
            (source / "dev.pulsara").rename(source / "config-resources")
            (source / "dev.pulsara").symlink_to("config-resources", target_is_directory=True)
    installed = _install(service, InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30))
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    assert [item.name for item in installed.summary.skills.skills] == ["physical-skill"]
    assert installed.summary.hooks.hook_definitions == ()
    with closing(service.inspect_local_plugins(InspectLocalPluginsRequest(monotonic() + 30))) as inspected:
        saved = inspected.instances[0]
        assert saved.summary.hooks.hook_definitions == ()
        assert saved.summary.hooks.source_relative_path == "dev.pulsara/hooks/hooks.json"
