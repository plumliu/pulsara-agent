"""Round 9.3 physical, precedence, trust, and cancellation regression gates."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
import stat
from threading import Event, Thread
from time import monotonic

import httpx
import pytest

from pulsara_agent.capability.bundled_skills import (
    BundledSkillDefinitionProducer,
    BundledSkillDistributionBindingOwner,
)
from pulsara_agent.capability.local_skills import (
    LooseSkillDefinitionProducer,
    SkillObservationCancelled,
)
from pulsara_agent.capability.pulsara_home import resolve_pulsara_home
from pulsara_agent.capability.resolver import SkillCatalogResolver
from pulsara_agent.capability.types import (
    BundledSkillOrigin,
    ConflictingSkillCandidateIssue,
)
from pulsara_agent.cli import _plugins_command, build_parser
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.hooks.contracts import FrozenHookDefinitionView, HookSourceKind
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.plugins.contracts import (
    ExternalProcessAcceptance,
    GcLocalPluginPackagesRequest,
    InspectLocalPluginsRequest,
    InstallLocalPluginRequest,
    NeverCancelPluginOperation,
    PluginComponentObservationDisposition,
    PluginDiagnosticCode,
    PluginDiagnosticSeverity,
    PluginGcDisposition,
    PluginInspectionDisposition,
    PluginInstallDisposition,
    PluginScopeKind,
    PluginValidationDisposition,
    RemoveLocalPluginRequest,
    SetLocalPluginEnabledRequest,
    ValidateLocalPluginSourceRequest,
    plugin_diagnostic_severity,
)
from pulsara_agent.plugins.hook_adapter import compose_hook_definition_view
from pulsara_agent.plugins.management import PluginManagementService
from pulsara_agent.plugins.mcp_adapter import normalize_plugin_mcp_configs
from pulsara_agent.plugins.package_core import COPY_CHUNK_BYTES, PluginPackageTimedOut
import pulsara_agent.plugins.package_store as package_store_module
from pulsara_agent.plugins.package_store import ManagedPluginStore
from pulsara_agent.plugins.skill_producer import PluginSkillDefinitionProducer
from pulsara_agent.plugins.view import EnabledPluginViewOwner
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialBoundAsyncClient,
    admit_process_credential_http_operation,
)
from pulsara_agent.llm.adapters.openai.client import admit_provider_request


PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def _make_package(
    root: Path,
    *,
    plugin_id: str = "physical-plugin",
    marker: str = "one",
    skill_name: str = "physical-skill",
    mcp_kind: str = "stdio",
    hooks: bool = False,
) -> Path:
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": PLUGIN_SCHEMA,
                "name": plugin_id,
                "version": marker,
                "description": f"physical fixture {marker}",
            }
        ),
        encoding="utf-8",
    )
    skill = root / "skills" / skill_name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_name}\n"
        f"description: Physical Skill {marker}.\n"
        "---\n\n"
        f"# Physical\n\nbody {marker}\n",
        encoding="utf-8",
    )
    if mcp_kind == "stdio":
        server = root / "server.py"
        server.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        server.chmod(0o755)
        server_value = {
            "type": "stdio",
            "command": "./server.py",
            "args": ["${PLUGIN_ROOT}", "${PLUGIN_DATA}"],
            "cwd": "${PLUGIN_DATA}",
            "env": {"PACKAGE_PATH": "${PLUGIN_ROOT}"},
        }
    elif mcp_kind == "http":
        server_value = {
            "type": "streamable-http",
            "url": "https://example.invalid/mcp",
            "headers": {"X-Public": "literal", "x-order": marker},
        }
    elif mcp_kind == "none":
        server_value = None
    else:
        raise ValueError("unknown fixture MCP kind")
    if server_value is not None:
        (root / "mcp.json").write_text(
            json.dumps(
                {"$schema": MCP_SCHEMA, "mcpServers": {"fixture": server_value}}
            ),
            encoding="utf-8",
        )
    if hooks:
        hook_root = root / "dev.pulsara" / "hooks"
        hook_root.mkdir(parents=True)
        (hook_root / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "matcher": "*",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "printf '{\"additionalContext\":\"ok\"}'",
                                    }
                                ],
                            }
                        ]
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
    store = ManagedPluginStore(pulsara_home=home, credential_boundary=boundary)
    return home, boundary, service, store


def _enable(service, installed, plugin_id: str, deadline: float) -> None:
    result = service.set_local_plugin_enabled(
        SetLocalPluginEnabledRequest(
            PluginScopeKind.USER,
            plugin_id,
            True,
            installed.package_install_id,
            deadline,
            external_process_acceptance=ExternalProcessAcceptance.ACCEPTED,
        )
    )
    assert result.disposition.value == "ENABLED"


def test_round9_3_diagnostic_vocabulary_and_severity_partition_are_exact() -> None:
    assert len(PluginDiagnosticCode) == 37
    partitions = {
        severity: {
            code
            for code in PluginDiagnosticCode
            if plugin_diagnostic_severity(code) is severity
        }
        for severity in PluginDiagnosticSeverity
    }
    assert set().union(*partitions.values()) == set(PluginDiagnosticCode)
    assert all(
        not partitions[left] & partitions[right]
        for left in PluginDiagnosticSeverity
        for right in PluginDiagnosticSeverity
        if left is not right
    )


def test_round9_3_manifest_preprocessing_and_component_isolation(
    tmp_path: Path,
) -> None:
    source = _make_package(tmp_path / "source", mcp_kind="none")
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    manifest["futureField"] = {"ignored": True}
    manifest["extensions"] = ["non-object-is-ignored"]
    (source / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "mcp.json").mkdir()
    (source / "unknown-resource").write_text("ordinary", encoding="utf-8")
    _home, _boundary, service, _store = _owners(tmp_path)

    result = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(source, monotonic() + 30)
    )
    assert result.disposition is PluginValidationDisposition.VALID
    assert result.summary.skills.disposition is PluginComponentObservationDisposition.COMPLETE
    assert result.summary.mcp.disposition is PluginComponentObservationDisposition.INVALID
    assert {item.code for item in result.diagnostics if hasattr(item, "code")} >= {
        PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED,
        PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED,
        PluginDiagnosticCode.COMPONENT_KIND_INVALID,
    }

    (source / "plugin.json").write_text(
        '{"$schema":"' + PLUGIN_SCHEMA + '","name":"one","name":"two"}',
        encoding="utf-8",
    )
    duplicate = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(source, monotonic() + 30)
    )
    assert duplicate.disposition is PluginValidationDisposition.INVALID
    assert duplicate.diagnostics[0].code is PluginDiagnosticCode.MANIFEST_INVALID_JSON


def test_round9_3_admission_rejects_final_and_tree_links_and_special_files(
    tmp_path: Path,
) -> None:
    _home, _boundary, service, _store = _owners(tmp_path)
    target = _make_package(tmp_path / "target", mcp_kind="none")
    final_link = tmp_path / "final-link"
    final_link.symlink_to(target, target_is_directory=True)
    final = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(final_link, monotonic() + 30)
    )
    assert final.disposition is PluginValidationDisposition.INVALID
    assert final.diagnostics[0].code is PluginDiagnosticCode.SOURCE_FINAL_SYMLINK

    tree = _make_package(tmp_path / "tree", mcp_kind="none")
    (tree / "linked").symlink_to(target / "plugin.json")
    linked = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(tree, monotonic() + 30)
    )
    assert linked.disposition is PluginValidationDisposition.INVALID
    assert linked.diagnostics[0].code is PluginDiagnosticCode.SOURCE_TREE_SYMLINK

    fifo = _make_package(tmp_path / "fifo", mcp_kind="none")
    os.mkfifo(fifo / "pipe")
    special = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(fifo, monotonic() + 30)
    )
    assert special.disposition is PluginValidationDisposition.INVALID
    assert special.diagnostics[0].code is PluginDiagnosticCode.SOURCE_SPECIAL_FILE


def test_round9_3_managed_runtime_narrows_resource_and_skill_symlinks(
    tmp_path: Path,
) -> None:
    source = _make_package(tmp_path / "source", hooks=False)
    _home, boundary, service, store = _owners(tmp_path)
    deadline = monotonic() + 30
    installed = service.install_local_plugin(
        InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    _enable(service, installed, "physical-plugin", deadline)
    package_root = installed.summary.manifest.name
    layout = store.layout(installed.identity)
    managed = layout.plugin_package_parent / installed.package_install_id
    assert package_root == "physical-plugin"
    outside = tmp_path / "outside-skill.md"
    outside.write_text("outside", encoding="utf-8")
    for directory in (managed, managed / "skills", managed / "skills" / "physical-skill"):
        directory.chmod(0o700)
    document = managed / "skills" / "physical-skill" / "SKILL.md"
    document.unlink()
    document.symlink_to(outside)
    (managed / "unrelated-link").symlink_to(outside)

    view = EnabledPluginViewOwner(store=store, credential_boundary=boundary).observe(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    try:
        assert view.disposition.value == "COMPLETE"
        instance = view.user_instances[0]
        assert instance.skills.disposition is PluginComponentObservationDisposition.COMPLETE
        assert instance.skills.candidates == ()
        assert instance.skills.invalid_diagnostics[0].code.value == "skill_file_escape"
        assert instance.mcp.disposition is PluginComponentObservationDisposition.COMPLETE
    finally:
        view.close()


def test_round9_3_publish_modes_replace_disable_data_retention_and_gc_grammar(
    tmp_path: Path,
) -> None:
    source = _make_package(tmp_path / "source")
    _home, _boundary, service, store = _owners(tmp_path)
    deadline = monotonic() + 30
    first = service.install_local_plugin(
        InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    layout = store.layout(first.identity)
    first_root = layout.plugin_package_parent / first.package_install_id
    assert stat.S_IMODE(first_root.stat().st_mode) == 0o500
    assert stat.S_IMODE((first_root / "plugin.json").stat().st_mode) == 0o400
    assert stat.S_IMODE((first_root / "server.py").stat().st_mode) == 0o500
    assert not layout.data_root.exists()
    _enable(service, first, "physical-plugin", deadline)
    assert stat.S_IMODE(layout.data_root.stat().st_mode) == 0o700

    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    manifest["version"] = "two"
    (source / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    second = service.install_local_plugin(
        InstallLocalPluginRequest(
            source, PluginScopeKind.USER, deadline, replace=True
        )
    )
    assert second.disposition is PluginInstallDisposition.REPLACED
    assert second.package_install_id != first.package_install_id
    assert second.enabled is False
    assert first_root.is_dir()
    assert layout.data_root.is_dir()
    removed = service.remove_local_plugin(
        RemoveLocalPluginRequest(PluginScopeKind.USER, "physical-plugin", deadline)
    )
    assert removed.disposition.value == "REMOVED"
    malformed = layout.plugin_package_parent / ".pulsara-stage-not-owned"
    malformed.mkdir()
    gc_outcome = service.gc_local_plugin_packages(
        GcLocalPluginPackagesRequest(deadline, workspace_root=tmp_path)
    )
    assert gc_outcome.disposition is PluginGcDisposition.COMPLETE
    assert not first_root.exists()
    assert not (layout.plugin_package_parent / second.package_install_id).exists()
    assert malformed.is_dir()
    assert layout.data_root.is_dir()


def test_round9_3_physical_anchor_blocks_gc_until_native_mcp_drain(
    tmp_path: Path,
) -> None:
    source = _make_package(tmp_path / "source", hooks=False)
    _home, boundary, service, store = _owners(tmp_path)
    deadline = monotonic() + 30
    installed = service.install_local_plugin(
        InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    _enable(service, installed, "physical-plugin", deadline)
    view = EnabledPluginViewOwner(store=store, credential_boundary=boundary).observe(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    mcp = normalize_plugin_mcp_configs(existing_configs=(), view=view)
    removed = service.remove_local_plugin(
        RemoveLocalPluginRequest(PluginScopeKind.USER, "physical-plugin", deadline)
    )
    assert removed.disposition.value == "REMOVED"
    busy = service.gc_local_plugin_packages(
        GcLocalPluginPackagesRequest(deadline, workspace_root=tmp_path)
    )
    assert busy.disposition is PluginGcDisposition.COMPLETE
    assert [item.package_install_id for item in busy.progress.ordered_in_use] == [
        installed.package_install_id
    ]
    mcp.close_plugin_anchors()
    view.close()
    drained = service.gc_local_plugin_packages(
        GcLocalPluginPackagesRequest(deadline, workspace_root=tmp_path)
    )
    assert drained.disposition is PluginGcDisposition.COMPLETE
    assert [item.package_install_id for item in drained.progress.ordered_removed] == [
        installed.package_install_id
    ]


def test_round9_3_replace_changes_mcp_lifetime_and_hook_trust_subject_state(
    tmp_path: Path,
) -> None:
    first_source = _make_package(
        tmp_path / "first", mcp_kind="http", hooks=True
    )
    _home, boundary, service, store = _owners(tmp_path)
    deadline = monotonic() + 30
    first = service.install_local_plugin(
        InstallLocalPluginRequest(first_source, PluginScopeKind.USER, deadline)
    )
    _enable(service, first, "physical-plugin", deadline)
    first_view = EnabledPluginViewOwner(store=store, credential_boundary=boundary).observe(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    first_mcp = normalize_plugin_mcp_configs(existing_configs=(), view=first_view)
    provider = LocalHookSourceProvider(
        workspace_root=tmp_path,
        workspace_kind="project",
        workspace_state_key="workspace-test",
        pulsara_home=store.home,
    )
    first_hooks = compose_hook_definition_view(
        local_view=FrozenHookDefinitionView(()),
        plugin_view=first_view,
        trust_store=provider.trust_store,
    )
    first_snapshot = first_hooks.source_snapshots[0]
    provider.trust_store.trust(
        first_snapshot.provenance.trust_subject,
        expected_digest=first_snapshot.trust.current_definition_digest or "",
    )

    second_source = _make_package(
        tmp_path / "second", marker="two", mcp_kind="http", hooks=True
    )
    second = service.install_local_plugin(
        InstallLocalPluginRequest(
            second_source, PluginScopeKind.USER, deadline, replace=True
        )
    )
    assert second.enabled is False
    _enable(service, second, "physical-plugin", deadline)
    second_view = EnabledPluginViewOwner(store=store, credential_boundary=boundary).observe(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    second_mcp = normalize_plugin_mcp_configs(existing_configs=(), view=second_view)
    assert (
        first_mcp.plugin_configs[0].semantic_config_fingerprint
        == second_mcp.plugin_configs[0].semantic_config_fingerprint
    )
    assert (
        first_mcp.plugin_configs[0].resolved_config_identity
        != second_mcp.plugin_configs[0].resolved_config_identity
    )
    headers = second_mcp.plugin_configs[0].resolved_headers(
        {"x-public": "protocol", "Content-Type": "application/json"}
    )
    assert headers["x-public"] == "protocol"
    assert "X-Public" not in headers
    second_hooks = compose_hook_definition_view(
        local_view=FrozenHookDefinitionView(()),
        plugin_view=second_view,
        trust_store=provider.trust_store,
    )
    second_snapshot = second_hooks.source_snapshots[0]
    assert second_snapshot.trust.disposition.value == "MODIFIED"
    assert second_snapshot.definitions[0].command == first_snapshot.definitions[0].command
    assert second_snapshot.provenance.identity.kind is HookSourceKind.PLUGIN

    for snapshot in (first_snapshot, second_snapshot):
        anchor = snapshot.provenance.physical_lifetime_anchor
        if anchor is not None:
            anchor.close()
    first_mcp.close_plugin_anchors()
    second_mcp.close_plugin_anchors()
    first_view.close()
    second_view.close()


def test_round9_3_same_tier_plugin_skill_conflict_falls_through_to_bundled(
    tmp_path: Path,
) -> None:
    bundled_name = "pulsara-plugin-installer"
    first_source = _make_package(
        tmp_path / "first",
        plugin_id="alpha-plugin",
        skill_name=bundled_name,
        mcp_kind="none",
    )
    second_source = _make_package(
        tmp_path / "second",
        plugin_id="beta-plugin",
        skill_name=bundled_name,
        mcp_kind="none",
    )
    home, boundary, service, store = _owners(tmp_path)
    deadline = monotonic() + 30
    for plugin_id, source in (
        ("alpha-plugin", first_source),
        ("beta-plugin", second_source),
    ):
        installed = service.install_local_plugin(
            InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
        )
        _enable(service, installed, plugin_id, deadline)
    view = EnabledPluginViewOwner(store=store, credential_boundary=boundary).observe(
        workspace_root=tmp_path,
        deadline_monotonic=deadline,
        cancellation=NeverCancelPluginOperation(),
    )
    binding = BundledSkillDistributionBindingOwner()
    try:
        loose_producer = LooseSkillDefinitionProducer(
            user_product_skills_root=tmp_path / "empty-user-product",
            user_agents_skills_root=tmp_path / "empty-user-agents",
            pulsara_home_resolution=home,
        )
        policy = loose_producer.prepare_root_policy(tmp_path)
        result = SkillCatalogResolver().resolve(
            loose_producer.observe(policy, deadline_monotonic=deadline),
            PluginSkillDefinitionProducer().observe(view),
            BundledSkillDefinitionProducer(binding).observe(
                deadline_monotonic=deadline
            ),
        )
        winner = next(item for item in result.winners if item.name == bundled_name)
        assert isinstance(winner.origin, BundledSkillOrigin)
        conflict = next(
            item
            for item in result.candidate_issues
            if isinstance(item, ConflictingSkillCandidateIssue)
            and item.name == bundled_name
        )
        assert [item.origin.plugin_id for item in conflict.candidates] == [
            "alpha-plugin",
            "beta-plugin",
        ]
    finally:
        binding.close()
        view.close()


def test_round9_3_cross_chunk_secret_and_closed_abort_outcomes(
    tmp_path: Path,
) -> None:
    secret = "round-9-3-boundary-secret"
    source = _make_package(tmp_path / "source", mcp_kind="none")
    (source / "large.bin").write_bytes(
        b"x" * (COPY_CHUNK_BYTES - 3) + secret.encode("utf-8") + b"tail"
    )
    _home, _boundary, service, _store = _owners(tmp_path, credential=secret)
    result = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(source, monotonic() + 30)
    )
    assert result.disposition is PluginValidationDisposition.INVALID
    assert secret not in repr(result)

    class Cancelled:
        def cancellation_requested(self) -> bool:
            return True

    cancelled = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(
            source, monotonic() + 30, cancellation=Cancelled()
        )
    )
    assert cancelled.disposition is PluginValidationDisposition.CANCELLED
    timed_out = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(source, monotonic() - 0.001)
    )
    assert timed_out.disposition is PluginValidationDisposition.TIMED_OUT
    with pytest.raises(SkillObservationCancelled):
        from pulsara_agent.capability.local_skills import check_skill_deadline

        check_skill_deadline(monotonic() + 30, Cancelled())


def test_round9_3_async_gate_cancel_joins_waiter_and_json_preflight_uses_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boundary = ProcessCredentialBoundary()
    entered = Event()
    release = Event()

    def hold() -> None:
        with boundary.sync_guard():
            entered.set()
            release.wait(timeout=5)

    holder = Thread(target=hold)
    holder.start()
    assert entered.wait(timeout=5)

    async def cancel_waiter() -> None:
        task = asyncio.create_task(boundary.rotate_async("rotated"))
        await asyncio.sleep(0.02)
        assert not task.done()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_waiter())
    holder.join(timeout=5)
    assert not holder.is_alive()
    with boundary.sync_guard():
        pass

    source = _make_package(tmp_path / "source", mcp_kind="none")
    home = tmp_path / "cli-home"
    monkeypatch.setenv("PULSARA_HOME", str(home))
    service = PluginManagementService(credential_boundary=boundary)
    installed = service.install_local_plugin(
        InstallLocalPluginRequest(
            source, PluginScopeKind.USER, monotonic() + 30
        )
    )
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    monkeypatch.setattr("builtins.input", lambda: "n")
    args = build_parser().parse_args(
        [
            "plugins",
            "enable",
            "--scope",
            "user",
            "--json",
            "physical-plugin",
        ]
    )
    rendered, status = _plugins_command(args, credential_boundary=boundary)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Exact package review" in captured.err
    assert json.loads(rendered)["disposition"] == "DECLINED"
    assert status == 1


def test_round9_3_inspection_cancel_is_closed_abort_not_partial(
    tmp_path: Path,
) -> None:
    source = _make_package(tmp_path / "source", mcp_kind="none")
    _home, _boundary, service, _store = _owners(tmp_path)
    deadline = monotonic() + 30
    installed = service.install_local_plugin(
        InstallLocalPluginRequest(source, PluginScopeKind.USER, deadline)
    )
    assert installed.disposition is PluginInstallDisposition.INSTALLED

    class Cancelled:
        def cancellation_requested(self) -> bool:
            return True

    result = service.inspect_local_plugins(
        InspectLocalPluginsRequest(
            deadline, workspace_root=tmp_path, cancellation=Cancelled()
        )
    )
    assert not hasattr(result, "instances")
    assert result.reason.value == "CANCELLED"
    complete = service.inspect_local_plugins(
        InspectLocalPluginsRequest(deadline, workspace_root=tmp_path)
    )
    assert complete.disposition is PluginInspectionDisposition.COMPLETE
    complete.close()


def test_round9_3_package_anchor_obeys_caller_deadline(tmp_path: Path) -> None:
    source = _make_package(tmp_path / "source", mcp_kind="none")
    _home, _boundary, service, store = _owners(tmp_path)
    installed = service.install_local_plugin(
        InstallLocalPluginRequest(
            source, PluginScopeKind.USER, monotonic() + 30
        )
    )
    assert installed.disposition is PluginInstallDisposition.INSTALLED
    layout = store.layout(installed.identity)
    descriptor = os.open(layout.package_lock_path(installed.package_install_id), os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with pytest.raises(PluginPackageTimedOut):
            store.acquire_package_anchor(
                layout,
                installed.package_install_id,
                deadline_monotonic=monotonic() + 0.05,
                cancellation=NeverCancelPluginOperation(),
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_round9_3_paired_source_read_failure_keeps_source_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_package(tmp_path / "source", mcp_kind="none")
    _home, _boundary, service, store = _owners(tmp_path)
    original_verify = package_store_module._verify_paired_bytes
    original_read = os.read
    state = {"inside": False, "fired": False}

    def verify(*args, **kwargs):
        state["inside"] = True
        try:
            return original_verify(*args, **kwargs)
        finally:
            state["inside"] = False

    def fail_first_source_read(descriptor: int, amount: int) -> bytes:
        if state["inside"] and not state["fired"]:
            state["fired"] = True
            raise OSError("synthetic paired source read failure")
        return original_read(descriptor, amount)

    monkeypatch.setattr(package_store_module, "_verify_paired_bytes", verify)
    monkeypatch.setattr(package_store_module.os, "read", fail_first_source_read)
    outcome = service.install_local_plugin(
        InstallLocalPluginRequest(
            source, PluginScopeKind.USER, monotonic() + 30
        )
    )
    assert state["fired"] is True
    assert outcome.disposition is PluginInstallDisposition.UNAVAILABLE
    assert [item.code for item in outcome.diagnostics] == [
        PluginDiagnosticCode.SOURCE_UNAVAILABLE
    ]
    identity = store.identity(
        scope=PluginScopeKind.USER,
        plugin_id="physical-plugin",
        workspace_root=None,
    )
    package_parent = store.layout(identity).plugin_package_parent
    assert not any(
        item.name.startswith(".pulsara-stage-")
        for item in package_parent.iterdir()
    )


def test_round9_3_reload_settlement_lock_obeys_absolute_deadline() -> None:
    class Probe:
        reload_capabilities = KernelHostSession.reload_capabilities

        def __init__(self) -> None:
            self._capability_reload_settlement_lock = asyncio.Lock()
            self.serialized_called = False

        def _require_open(self) -> None:
            return

        async def _reload_capabilities_serialized(self, deadline: float):
            del deadline
            self.serialized_called = True
            return {"status": "RELOADED"}

    async def run() -> None:
        probe = Probe()
        await probe._capability_reload_settlement_lock.acquire()
        try:
            with pytest.raises(TimeoutError):
                await probe.reload_capabilities(deadline_monotonic=monotonic() + 0.05)
            assert probe.serialized_called is False
        finally:
            probe._capability_reload_settlement_lock.release()

    asyncio.run(run())


def test_round9_3_provider_admission_blocks_rotation_until_sink() -> None:
    async def run() -> None:
        old = "synthetic-old-provider-boundary-key"
        future = "synthetic-future-provider-boundary-key"
        boundary = ProcessCredentialBoundary(old)
        started = asyncio.Event()
        proceed = asyncio.Event()
        sink_values: list[str | None] = []

        async def operation() -> str:
            started.set()
            await proceed.wait()
            sink_values.append(boundary.last_boundary_snapshot)
            return "ok"

        request = asyncio.create_task(
            admit_provider_request(
                credential_boundary=boundary,
                payload={"prompt": future},
                operation=operation,
            )
        )
        await started.wait()
        rotation = asyncio.create_task(boundary.rotate_async(future))
        await asyncio.sleep(0.05)
        assert not rotation.done()
        proceed.set()
        await request
        await rotation
        assert sink_values == [old]

    asyncio.run(run())


def test_round9_3_http_gate_releases_after_body_before_response_headers() -> None:
    async def run() -> None:
        body_received = asyncio.Event()
        allow_response = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            if length:
                await reader.readexactly(length)
            body_received.set()
            await allow_response.wait()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\nok"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        future = "synthetic-future-http-boundary-key"
        boundary = ProcessCredentialBoundary("synthetic-old-http-boundary-key")
        client = ProcessCredentialBoundAsyncClient(credential_boundary=boundary)
        payload = future.encode()
        try:
            request = asyncio.create_task(
                admit_process_credential_http_operation(
                    credential_boundary=boundary,
                    guarded_values=(payload,),
                    operation=lambda: client.post(
                        f"http://127.0.0.1:{port}/", content=payload
                    ),
                )
            )
            await body_received.wait()
            rotation = asyncio.create_task(boundary.rotate_async(future))
            await asyncio.sleep(0.05)
            released_before_headers = rotation.done()
            allow_response.set()
            response = await request
            await rotation
            assert released_before_headers is True
            assert response.status_code == 200
        finally:
            allow_response.set()
            await client.aclose()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_round9_3_http_boundary_only_exempts_frozen_credential_header() -> None:
    active = "synthetic-provider-credential-key"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {active}"
        return httpx.Response(200, content=b"ok")

    async def run() -> None:
        client = ProcessCredentialBoundAsyncClient(
            credential_boundary=ProcessCredentialBoundary(active),
            credential_header_names=frozenset({b"authorization"}),
            transport=httpx.MockTransport(handler),
        )
        try:
            accepted = await client.post(
                "https://example.invalid/provider",
                headers={"Authorization": f"Bearer {active}"},
                content=b"safe model payload",
            )
            assert accepted.status_code == 200
            with pytest.raises(ValueError, match="protected credential"):
                await client.post(
                    "https://example.invalid/provider",
                    headers={"Authorization": f"Bearer {active}"},
                    content=active.encode(),
                )
        finally:
            await client.aclose()

    asyncio.run(run())
