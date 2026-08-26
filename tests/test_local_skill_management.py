from __future__ import annotations

from dataclasses import fields
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

import pytest

from pulsara_agent import cli
from pulsara_agent.capability.contracts import FrozenSkillProjectionInput
from pulsara_agent.capability.plugin_skill_contracts import (
    FrozenPluginSkillDefinitions,
    PluginSkillDefinitionsDisposition,
)
from pulsara_agent.capability.local_skill_management import (
    EventLocalSkillCancellationProbe,
    InspectEffectiveSkillCatalogRequest,
    InstallLooseLocalSkillRequest,
    LocalSkillInstallDisposition,
    LocalSkillInstallScope,
    LocalSkillManagementService,
    LocalSkillValidationDisposition,
    LocalSkillValidationUnavailableReason,
    ValidateLocalSkillSourceRequest,
)
from pulsara_agent.capability.bundled_skills import (
    BundledSkillDefinitionProducer,
    BundledSkillDefinitionsDisposition,
    BundledSkillDistributionBindingOwner,
)
from pulsara_agent.capability.local_skill_publisher import (
    AtomicLocalSkillPublisher,
    LocalSkillCleanupLocationStatus,
    LocalSkillPublishUnavailableReason,
    PlatformExclusiveDirectoryPublisher,
)
from pulsara_agent.capability.local_skills import (
    LooseSkillDefinitionProducer,
    LooseSkillDefinitionsDisposition,
    parse_skill_document,
    validate_skill_candidate_placement,
)
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    PulsaraHomeUnavailableReason,
    resolve_pulsara_home,
)
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
    EffectiveSkillCatalogDisposition,
    UnavailableEffectiveSkillCatalogInspection,
    inspection_diagnostics,
)
from pulsara_agent.capability.types import (
    InvalidSkillCandidateIssue,
    ShadowedSkillCandidateIssue,
    SkillDiagnosticCode,
    SkillProducerUnavailableReason,
)
from pulsara_agent.exclusive_publish import ExclusivePublishPrimitiveUnavailable


def _no_plugin_skills() -> FrozenPluginSkillDefinitions:
    return FrozenPluginSkillDefinitions(
        PluginSkillDefinitionsDisposition.COMPLETE
    )


def _document(
    name: str,
    *,
    description: str = "Portable local Skill.",
    body: str = "# Instructions\n",
    extra: str = "",
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"


def _source(
    root: Path,
    name: str = "example-skill",
    *,
    document: str | None = None,
) -> Path:
    source = root / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        document or _document(name),
        encoding="utf-8",
    )
    return source


def _workspace_request(source: Path, workspace: Path) -> InstallLooseLocalSkillRequest:
    return InstallLooseLocalSkillRequest(
        source_path=source,
        scope=LocalSkillInstallScope.WORKSPACE,
        workspace_root=workspace,
    )


def _loose_producer(base: Path) -> LooseSkillDefinitionProducer:
    return LooseSkillDefinitionProducer(
        user_product_skills_root=base / "test-user-product-skills",
        user_agents_skills_root=base / "test-user-agent-skills",
    )


def _wait_for_path(path: Path, process: subprocess.Popen[str] | None = None) -> None:
    deadline = time.monotonic() + 10.0
    while not path.exists():
        if process is not None and process.poll() is not None:
            _stdout, stderr = process.communicate()
            raise AssertionError(f"subprocess exited before gate: {stderr}")
        if time.monotonic() >= deadline:
            raise AssertionError(f"subprocess did not reach gate: {path}")
        time.sleep(0.01)


def test_local_skill_validation_reuses_root_neutral_production_parser(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        extra_name := "portable-skill",
        document=_document(
            extra_name,
            extra=(
                "license: Apache-2.0\n"
                "compatibility: Pulsara\n"
                "metadata: {owner: platform}\n"
                "hooks: inert\n"
            ),
        ),
    )
    service = LocalSkillManagementService()

    validation = service.validate_local_skill_source(
        ValidateLocalSkillSourceRequest(source)
    )
    runtime_provider = _loose_producer(tmp_path / "runtime-user")
    runtime_root = tmp_path / "runtime"
    runtime_source = runtime_root / ".pulsara" / "skills" / extra_name
    runtime_source.parent.mkdir(parents=True)
    os.rename(source, runtime_source)
    discovery = runtime_provider.observe(
        runtime_provider.prepare_root_policy(runtime_root)
    )

    assert validation.disposition is LocalSkillValidationDisposition.VALID
    assert validation.parsed is not None
    assert discovery.candidates[0].name == validation.parsed.name
    assert discovery.candidates[0].description == validation.parsed.description
    assert discovery.candidates[0].license == validation.parsed.license
    assert discovery.candidates[0].compatibility == validation.parsed.compatibility
    assert discovery.candidates[0].metadata == validation.parsed.metadata
    assert discovery.candidates[0].body == validation.parsed.body
    assert (
        discovery.candidates[0].raw_document_digest
        == validation.parsed.raw_document_digest
    )
    assert not hasattr(validation.parsed, "root_kind")
    assert not hasattr(validation.parsed, "location")


@pytest.mark.parametrize(
    "document",
    [
        _document("parser-case"),
        b"\xff\xfe",
        b"name: parser-case\n",
        b"---\nname: parser-case\nname: duplicate\ndescription: x\n---\n",
        b"---\nname: &anchor parser-case\ndescription: x\n---\n",
        b"---\nname: parser-case\ndescription: !custom x\n---\n",
        b"---\nname: parser-case\ndescription: x\n---\n---\nname: two\n",
        _document("wrong-directory"),
    ],
)
def test_validation_and_runtime_use_exactly_the_same_parser_contract(
    tmp_path: Path,
    document: str | bytes,
) -> None:
    source = tmp_path / "parser-case"
    source.mkdir()
    data = document.encode() if isinstance(document, str) else document
    (source / "SKILL.md").write_bytes(data)

    direct = parse_skill_document(data)
    placement = (
        validate_skill_candidate_placement(direct.parsed, "parser-case")
        if direct.parsed is not None
        else None
    )
    direct_valid = direct.parsed is not None and placement is not None and placement.valid
    direct_diagnostics = (
        *direct.diagnostics,
        *(placement.diagnostics if placement is not None else ()),
    )
    validation = LocalSkillManagementService().validate_local_skill_source(
        ValidateLocalSkillSourceRequest(source)
    )
    runtime_root = tmp_path / "runtime"
    runtime_source = runtime_root / ".pulsara" / "skills" / source.name
    runtime_source.parent.mkdir(parents=True)
    os.rename(source, runtime_source)
    provider = _loose_producer(tmp_path / "runtime-user")
    discovery = provider.observe(provider.prepare_root_policy(runtime_root))

    assert (validation.disposition is LocalSkillValidationDisposition.VALID) == (
        direct_valid
    )
    if not direct_valid:
        assert validation.disposition is LocalSkillValidationDisposition.INVALID
        assert [item.code for item in validation.diagnostics] == [
            item.code for item in direct_diagnostics
        ]
        assert len(discovery.invalid_issues) == 1
        issue = discovery.invalid_issues[0]
        assert isinstance(issue, InvalidSkillCandidateIssue)
        assert [item.code for item in issue.diagnostics] == [
            item.code for item in direct_diagnostics
        ]
    else:
        assert direct.parsed is not None
        assert [item.name for item in discovery.candidates] == [direct.parsed.name]


def test_local_skill_validation_closed_invalid_and_unavailable_outcomes(
    tmp_path: Path,
) -> None:
    service = LocalSkillManagementService()
    missing = service.validate_local_skill_source(
        ValidateLocalSkillSourceRequest(tmp_path / "missing")
    )
    assert missing.disposition is LocalSkillValidationDisposition.UNAVAILABLE
    assert (
        missing.unavailable_reason
        is LocalSkillValidationUnavailableReason.SOURCE_MISSING
    )

    source = tmp_path / "no-document"
    source.mkdir()
    invalid = service.validate_local_skill_source(
        ValidateLocalSkillSourceRequest(source)
    )
    assert invalid.disposition is LocalSkillValidationDisposition.INVALID
    assert [item.code for item in invalid.diagnostics] == [
        SkillDiagnosticCode.MISSING_DOCUMENT
    ]

    mismatch = _source(
        tmp_path,
        "actual-name",
        document=_document("different-name"),
    )
    result = service.validate_local_skill_source(
        ValidateLocalSkillSourceRequest(mismatch)
    )
    assert result.disposition is LocalSkillValidationDisposition.INVALID
    assert SkillDiagnosticCode.DIRECTORY_NAME_MISMATCH in {
        item.code for item in result.diagnostics
    }


def test_shared_pulsara_home_resolution_is_absolute_and_cwd_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    invalid_a = resolve_pulsara_home("relative-home")
    monkeypatch.chdir(second)
    invalid_b = resolve_pulsara_home("relative-home")
    assert invalid_a == invalid_b
    assert invalid_a.disposition is PulsaraHomeDisposition.INVALID
    assert (
        invalid_a.unavailable_reason
        is PulsaraHomeUnavailableReason.RELATIVE_PULSARA_HOME
    )
    configured = resolve_pulsara_home(str(tmp_path / "configured"))
    assert configured.path == tmp_path / "configured"
    assert configured.path is not None and configured.path.is_absolute()


@pytest.mark.parametrize("configured", [None, "", "~/configured-home"])
def test_shared_home_unset_empty_and_tilde_are_cwd_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
) -> None:
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    first = tmp_path / "cwd-a"
    second = tmp_path / "cwd-b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    if configured is None:
        monkeypatch.delenv("PULSARA_HOME", raising=False)
    else:
        monkeypatch.setenv("PULSARA_HOME", configured)
    monkeypatch.chdir(first)
    first_resolution = resolve_pulsara_home()
    monkeypatch.chdir(second)
    second_resolution = resolve_pulsara_home()

    assert first_resolution == second_resolution
    assert first_resolution.disposition is PulsaraHomeDisposition.RESOLVED
    expected = (
        user_home / "configured-home"
        if configured == "~/configured-home"
        else user_home / ".pulsara"
    )
    assert first_resolution.path == expected


def test_relative_home_is_same_typed_failure_for_runtime_and_bundled_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PULSARA_HOME", "relative-home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = LooseSkillDefinitionProducer()
    discovery = provider.observe(provider.prepare_root_policy(workspace))

    assert discovery.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE
    assert discovery.unavailable_cause is not None
    assert discovery.unavailable_cause.reason is (
        SkillProducerUnavailableReason.LOOSE_CONFIGURATION_INVALID
    )
    with BundledSkillDistributionBindingOwner() as binding:
        bundled = BundledSkillDefinitionProducer(binding).observe()
    assert bundled.disposition is BundledSkillDefinitionsDisposition.COMPLETE
    assert not (workspace / "relative-home").exists()


def test_invalid_user_home_does_not_block_validation_or_workspace_install(
    tmp_path: Path,
) -> None:
    invalid_home = resolve_pulsara_home("relative-home")
    service = LocalSkillManagementService(pulsara_home_resolution=invalid_home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources")

    validation = service.validate_local_skill_source(
        ValidateLocalSkillSourceRequest(source)
    )
    installed = service.install_loose_local_skill(_workspace_request(source, workspace))
    user = service.install_loose_local_skill(
        InstallLooseLocalSkillRequest(
            source_path=source,
            scope=LocalSkillInstallScope.USER,
        )
    )
    inspection = service.inspect_effective_skill_catalog(
        InspectEffectiveSkillCatalogRequest(workspace, _no_plugin_skills())
    )

    assert validation.disposition is LocalSkillValidationDisposition.VALID
    assert installed.disposition is LocalSkillInstallDisposition.INSTALLED
    assert (
        user.disposition
        is LocalSkillInstallDisposition.TARGET_CONFIGURATION_UNAVAILABLE
    )
    assert (
        user.target_configuration_reason
        is PulsaraHomeUnavailableReason.RELATIVE_PULSARA_HOME
    )
    assert inspection.disposition is EffectiveSkillCatalogDisposition.UNAVAILABLE
    assert isinstance(inspection, UnavailableEffectiveSkillCatalogInspection)
    assert len(inspection.unavailable_causes) == 1
    assert inspection.unavailable_causes[0].reason is (
        SkillProducerUnavailableReason.LOOSE_CONFIGURATION_INVALID
    )
    assert [item.code for item in inspection_diagnostics(inspection)] == [
        SkillDiagnosticCode.USER_HOME_CONFIGURATION_INVALID
    ]


@pytest.mark.parametrize("failure_type", [RuntimeError, MemoryError])
def test_inspection_closes_one_failed_os_home_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    monkeypatch.delenv("PULSARA_HOME", raising=False)
    calls = 0

    def unavailable_home(_cls: type[Path]) -> Path:
        nonlocal calls
        calls += 1
        raise failure_type("synthetic OS home failure")

    monkeypatch.setattr(Path, "home", classmethod(unavailable_home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    inspection = LocalSkillManagementService().inspect_effective_skill_catalog(
        InspectEffectiveSkillCatalogRequest(workspace, _no_plugin_skills())
    )

    assert calls == 1
    assert inspection.disposition is EffectiveSkillCatalogDisposition.UNAVAILABLE
    assert isinstance(inspection, UnavailableEffectiveSkillCatalogInspection)
    assert [item.reason for item in inspection.unavailable_causes] == [
        SkillProducerUnavailableReason.LOOSE_CONFIGURATION_INVALID
    ]
    assert [item.code for item in inspection_diagnostics(inspection)] == [
        SkillDiagnosticCode.USER_HOME_CONFIGURATION_INVALID
    ]
    assert tuple(item.root_kind.value for item in inspection.root_policy.roots) == (
        "WORKSPACE_PULSARA",
        "WORKSPACE_AGENTS",
    )


def test_darwin_system_tmp_and_var_aliases_are_prepared_before_nofollow_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if sys.platform != "darwin":
        return
    service = LocalSkillManagementService()
    workspace = tmp_path / "workspace-aliases"
    workspace.mkdir()
    with tempfile.TemporaryDirectory(
        prefix="pulsara-skill-source-",
        dir="/tmp",
    ) as raw_tmp:
        tmp_source = _source(Path(raw_tmp), "tmp-alias-skill")
        tmp_validation = service.validate_local_skill_source(
            ValidateLocalSkillSourceRequest(tmp_source)
        )
        tmp_install = service.install_loose_local_skill(
            _workspace_request(tmp_source, workspace)
        )
        assert tmp_validation.disposition is LocalSkillValidationDisposition.VALID
        assert tmp_install.disposition is LocalSkillInstallDisposition.INSTALLED
        assert tmp_validation.source_path.as_posix().startswith("/private/tmp/")
        assert tmp_install.source_path.as_posix().startswith("/private/tmp/")

        cli_source = _source(Path(raw_tmp), "tmp-cli-alias-skill")
        monkeypatch.setattr(
            sys,
            "argv",
            ["pulsara", "skills", "validate", str(cli_source), "--json"],
        )
        cli.main()
        cli_validation = json.loads(capsys.readouterr().out)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pulsara",
                "skills",
                "install",
                "--scope",
                "workspace",
                "--workspace",
                str(workspace),
                str(cli_source),
                "--json",
            ],
        )
        cli.main()
        cli_install = json.loads(capsys.readouterr().out)
        assert cli_validation["disposition"] == "VALID"
        assert cli_install["disposition"] == "INSTALLED"

    physical_var_source = _source(tmp_path / "var-sources", "var-alias-skill")
    private_var = Path("/private/var")
    assert physical_var_source.is_relative_to(private_var)
    var_alias_source = Path("/var") / physical_var_source.relative_to(private_var)
    var_validation = service.validate_local_skill_source(
        ValidateLocalSkillSourceRequest(var_alias_source)
    )
    var_install = service.install_loose_local_skill(
        _workspace_request(var_alias_source, workspace)
    )

    assert var_validation.disposition is LocalSkillValidationDisposition.VALID
    assert var_install.disposition is LocalSkillInstallDisposition.INSTALLED
    assert var_validation.source_path == physical_var_source
    assert var_install.source_path == physical_var_source


def test_source_binding_preparation_never_follows_final_source_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-final-link"
    workspace.mkdir()
    real_source = _source(tmp_path / "real-sources", "final-link-skill")
    linked_source = tmp_path / "final-link-skill"
    linked_source.symlink_to(real_source, target_is_directory=True)
    service = LocalSkillManagementService()

    validation = service.validate_local_skill_source(
        ValidateLocalSkillSourceRequest(linked_source)
    )
    installed = service.install_loose_local_skill(
        _workspace_request(linked_source, workspace)
    )

    assert validation.disposition is LocalSkillValidationDisposition.UNAVAILABLE
    assert (
        validation.unavailable_reason
        is LocalSkillValidationUnavailableReason.SOURCE_NOT_DIRECTORY
    )
    assert installed.disposition is LocalSkillInstallDisposition.SOURCE_UNAVAILABLE
    assert not (workspace / ".pulsara" / "skills" / "final-link-skill").exists()


def test_workspace_install_uses_hidden_stage_exclusive_publish_and_portable_modes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o755)
    source = _source(tmp_path / "sources", "mode-skill")
    scripts = source / "scripts"
    scripts.mkdir(mode=0o755)
    executable = scripts / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    ordinary = source / "note.txt"
    ordinary.write_text("note\n", encoding="utf-8")
    ordinary.chmod(0o644)
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
    (source / ".DS_Store").write_bytes(b"ignored")

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        assert result.disposition is LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE
        assert (
            result.publish_unavailable_reason
            is LocalSkillPublishUnavailableReason.UNSUPPORTED_EXCLUSIVE_PRIMITIVE
        )
        return
    assert result.disposition is LocalSkillInstallDisposition.INSTALLED
    destination = workspace / ".pulsara" / "skills" / "mode-skill"
    assert result.destination_path == destination
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "scripts").stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "SKILL.md").stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "note.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "scripts" / "run.sh").stat().st_mode) == 0o711
    assert not (destination / "__pycache__").exists()
    assert not (destination / ".DS_Store").exists()
    assert not list((workspace / ".pulsara" / "skills").glob(".*install*"))
    assert stat.S_IMODE((workspace / ".pulsara").stat().st_mode) == 0o700
    assert stat.S_IMODE((workspace / ".pulsara" / "skills").stat().st_mode) == 0o700


def test_user_install_prepares_fresh_configured_absolute_home(
    tmp_path: Path,
) -> None:
    configured = resolve_pulsara_home(str(tmp_path / "new" / "pulsara-home"))
    source = _source(tmp_path / "sources", "user-skill")
    service = LocalSkillManagementService(pulsara_home_resolution=configured)

    result = service.install_loose_local_skill(
        InstallLooseLocalSkillRequest(
            source_path=source,
            scope=LocalSkillInstallScope.USER,
        )
    )

    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert result.disposition is LocalSkillInstallDisposition.INSTALLED
        assert result.destination_path == (
            tmp_path / "new" / "pulsara-home" / "skills" / "user-skill"
        )
    else:
        assert result.disposition is LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE


def test_existing_control_directories_keep_their_existing_modes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    control = workspace / ".pulsara"
    root = control / "skills"
    root.mkdir(parents=True)
    control.chmod(0o755)
    root.chmod(0o751)
    source = _source(tmp_path / "sources", "existing-root-mode")

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert result.disposition is LocalSkillInstallDisposition.INSTALLED
    assert stat.S_IMODE(control.stat().st_mode) == 0o755
    assert stat.S_IMODE(root.stat().st_mode) == 0o751


def test_install_never_overwrites_existing_destination(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "collision")
    destination = workspace / ".pulsara" / "skills" / "collision"
    destination.mkdir(parents=True)
    marker = destination / "owned.txt"
    marker.write_text("existing\n", encoding="utf-8")

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.DESTINATION_EXISTS
    assert marker.read_text(encoding="utf-8") == "existing\n"
    assert not (destination / "SKILL.md").exists()


def test_install_rejects_reserved_control_symlink_and_source_target_overlap(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ordinary = _source(tmp_path / "reserved", "reserved-skill")
    (ordinary / ".pulsara-skill-source.json").write_text(
        "{}",
        encoding="utf-8",
    )
    ordinary_result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(ordinary, workspace)
    )
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert ordinary_result.disposition is LocalSkillInstallDisposition.INSTALLED
        assert ordinary_result.destination_path is not None
        assert (
            ordinary_result.destination_path / ".pulsara-skill-source.json"
        ).read_text(encoding="utf-8") == "{}"
    else:
        assert (
            ordinary_result.disposition
            is LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE
        )

    linked = _source(tmp_path / "linked", "linked-skill")
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (linked / "link").symlink_to(target)
    linked_result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(linked, workspace)
    )
    assert linked_result.disposition is LocalSkillInstallDisposition.UNSUPPORTED_ENTRY

    overlap_source = _source(tmp_path / "overlap", "overlap-skill")
    nested_workspace = overlap_source / "nested-workspace"
    nested_workspace.mkdir()
    overlap = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(overlap_source, nested_workspace)
    )
    assert overlap.disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE
    assert not (nested_workspace / ".pulsara").exists()


def test_fresh_target_root_never_follows_existing_control_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".pulsara").symlink_to(outside, target_is_directory=True)
    source = _source(tmp_path / "sources", "nofollow-skill")

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE
    assert not (outside / "skills").exists()


def test_source_replacement_after_copy_is_one_attempt_source_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "raced-skill")
    original = publisher_module._verify_stage_against_source
    changed = False

    def change_then_verify(source_fd, stage_fd, observation, probe):
        nonlocal changed
        if not changed:
            changed = True
            (source / "SKILL.md").write_text(
                _document("raced-skill", body="# Version two\n"),
                encoding="utf-8",
            )
        return original(source_fd, stage_fd, observation, probe)

    monkeypatch.setattr(
        publisher_module,
        "_verify_stage_against_source",
        change_then_verify,
    )
    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.SOURCE_RACED
    assert not (workspace / ".pulsara" / "skills" / "raced-skill").exists()
    assert not list((workspace / ".pulsara" / "skills").glob(".*install*"))


def test_stage_failure_arbitration_prefers_source_race_then_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stable = _source(tmp_path / "stable", "stable-skill")

    def stage_failure(*_args, **_kwargs):
        raise publisher_module._StageUnavailable("synthetic stage failure")

    monkeypatch.setattr(publisher_module, "_copy_source_to_stage", stage_failure)
    stable_result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(stable, workspace)
    )
    assert stable_result.disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE

    raced = _source(tmp_path / "raced", "raced-stage")

    def race_and_fail(*_args, **_kwargs):
        (raced / "added.txt").write_text("new", encoding="utf-8")
        raise publisher_module._StageUnavailable("synthetic stage failure")

    monkeypatch.setattr(publisher_module, "_copy_source_to_stage", race_and_fail)
    raced_result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(raced, workspace)
    )
    assert raced_result.disposition is LocalSkillInstallDisposition.SOURCE_RACED


def test_copy_source_read_eio_is_source_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "source-eio")
    resource = source / "resource.bin"
    resource.write_bytes(b"source bytes")
    resource_inode = resource.stat().st_ino
    original_read = publisher_module.os.read

    def source_eio(descriptor: int, amount: int) -> bytes:
        if os.fstat(descriptor).st_ino == resource_inode:
            raise OSError(errno.EIO, "synthetic source EIO")
        return original_read(descriptor, amount)

    monkeypatch.setattr(publisher_module.os, "read", source_eio)
    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.SOURCE_UNAVAILABLE
    assert not (workspace / ".pulsara" / "skills" / "source-eio").exists()
    assert not list((workspace / ".pulsara" / "skills").glob(".*install*"))


def test_resource_copy_compare_and_digest_are_constant_memory_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace-streaming"
    workspace.mkdir()
    source = _source(tmp_path / "streaming-sources", "streaming-skill")
    (source / "large-resource.bin").write_bytes(
        b"streaming-resource" * (2 * 1024 * 1024 // 18)
    )
    observed_read_sizes: list[int] = []
    original_read = publisher_module.os.read

    def bounded_read(descriptor: int, amount: int) -> bytes:
        observed_read_sizes.append(amount)
        return original_read(descriptor, amount)

    monkeypatch.setattr(publisher_module.os, "read", bounded_read)
    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert result.disposition is LocalSkillInstallDisposition.INSTALLED
    else:
        assert result.disposition is LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE
    assert len(observed_read_sizes) > 64
    assert max(observed_read_sizes) <= publisher_module._COPY_CHUNK_BYTES
    source_text = Path(publisher_module.__file__).read_text(encoding="utf-8")
    assert "sha256(stage_bytes)" not in source_text
    assert "source_bytes = _read_" not in source_text


def test_memory_error_after_stage_creation_is_typed_and_cleans_hidden_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace-memory-error"
    workspace.mkdir()
    source = _source(tmp_path / "memory-sources", "memory-skill")
    (source / "resource.bin").write_bytes(b"resource")
    original_digest = publisher_module._digest_stage_file
    injected = False

    def fail_one_digest(root_fd, relative, *, expected_metadata, probe):
        nonlocal injected
        if relative.name == "resource.bin" and not injected:
            injected = True
            raise MemoryError("synthetic resource allocation failure")
        return original_digest(
            root_fd,
            relative,
            expected_metadata=expected_metadata,
            probe=probe,
        )

    monkeypatch.setattr(publisher_module, "_digest_stage_file", fail_one_digest)
    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    root = workspace / ".pulsara" / "skills"
    assert injected is True
    assert result.disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE
    assert not (root / source.name).exists()
    assert not list(root.glob(".pulsara-skill-install-*"))

    second_workspace = tmp_path / "workspace-stage-open-memory-error"
    second_workspace.mkdir()
    second_source = _source(tmp_path / "stage-open-sources", "stage-open-skill")
    original_open = publisher_module.os.open
    stage_open_failed = False

    def fail_stage_directory_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal stage_open_failed
        if (
            not stage_open_failed
            and isinstance(path, str)
            and path.startswith(".pulsara-skill-install-")
            and flags & getattr(os, "O_DIRECTORY", 0)
        ):
            stage_open_failed = True
            raise MemoryError("synthetic stage binding allocation failure")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(publisher_module.os, "open", fail_stage_directory_open)
    second = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(second_source, second_workspace)
    )

    second_root = second_workspace / ".pulsara" / "skills"
    assert stage_open_failed is True
    assert second.disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE
    assert not (second_root / second_source.name).exists()
    assert not list(second_root.glob(".pulsara-skill-install-*"))


def test_resource_read_memory_error_is_typed_and_runs_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace-read-memory-error"
    workspace.mkdir()
    source = _source(tmp_path / "read-memory-sources", "read-memory-skill")
    resource = source / "resource.bin"
    resource.write_bytes(b"resource")
    resource_inode = resource.stat().st_ino
    original_read = publisher_module.os.read
    injected = False

    def fail_source_resource_read(descriptor: int, amount: int) -> bytes:
        nonlocal injected
        if os.fstat(descriptor).st_ino == resource_inode and not injected:
            injected = True
            raise MemoryError("synthetic source resource allocation failure")
        return original_read(descriptor, amount)

    monkeypatch.setattr(publisher_module.os, "read", fail_source_resource_read)
    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    root = workspace / ".pulsara" / "skills"
    assert injected is True
    assert result.disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE
    assert not (root / source.name).exists()
    assert not list(root.glob(".pulsara-skill-install-*"))


def test_allocation_failure_cleanup_failure_preserves_typed_prior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace-cleanup-memory-error"
    workspace.mkdir()
    source = _source(tmp_path / "cleanup-memory-sources", "cleanup-memory-skill")
    (source / "resource.bin").write_bytes(b"resource")

    monkeypatch.setattr(
        publisher_module,
        "_digest_stage_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MemoryError("synthetic digest allocation failure")
        ),
    )
    monkeypatch.setattr(
        publisher_module,
        "_cleanup_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MemoryError("synthetic cleanup allocation failure")
        ),
    )

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE
    assert result.prior_disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE
    assert result.attempted_staging_path is not None
    assert (
        result.cleanup_location_status
        is LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION
    )


@pytest.mark.parametrize("failure_kind", ["write", "readback", "mode"])
def test_stage_physical_failures_remain_stage_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", f"stage-{failure_kind}")
    if failure_kind == "write":
        monkeypatch.setattr(
            publisher_module.os,
            "write",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError(errno.ENOSPC, "synthetic stage ENOSPC")
            ),
        )
    elif failure_kind == "readback":
        monkeypatch.setattr(
            publisher_module,
            "_digest_stage_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                publisher_module._StageUnavailable("synthetic readback failure")
            ),
        )
    else:
        original_fchmod = publisher_module.os.fchmod

        def fail_file_mode(descriptor: int, mode: int) -> None:
            if mode != 0o700:
                raise OSError(errno.EIO, "synthetic stage mode failure")
            original_fchmod(descriptor, mode)

        monkeypatch.setattr(publisher_module.os, "fchmod", fail_file_mode)

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE
    assert not (workspace / ".pulsara" / "skills" / source.name).exists()
    assert not list((workspace / ".pulsara" / "skills").glob(".*install*"))


def test_stage_failure_arbitration_prefers_source_unavailable_over_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "source-unavailable-priority")
    monkeypatch.setattr(
        publisher_module,
        "_copy_source_to_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            publisher_module._StageUnavailable("synthetic stage failure")
        ),
    )
    monkeypatch.setattr(
        publisher_module,
        "_revalidate_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            publisher_module._SourceUnavailable("synthetic source failure")
        ),
    )

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.SOURCE_UNAVAILABLE


class _CountingCancellation:
    def __init__(self, cancel_at: int) -> None:
        self.calls = 0
        self.cancel_at = cancel_at

    def cancellation_requested(self) -> bool:
        self.calls += 1
        return self.calls >= self.cancel_at


def test_cancel_during_copy_joins_owner_and_cleans_hidden_stage(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "cancel-skill")
    (source / "large.bin").write_bytes(b"x" * (256 * 1024))
    probe = _CountingCancellation(cancel_at=4)

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace),
        cancellation=probe,
    )

    assert result.disposition is LocalSkillInstallDisposition.CANCELLED
    root = workspace / ".pulsara" / "skills"
    assert not (root / "cancel-skill").exists()
    assert not list(root.glob(".*install*"))


def test_cleanup_failure_preserves_only_closed_attempt_location_and_prior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "cleanup-skill")
    monkeypatch.setattr(
        publisher_module,
        "_copy_source_to_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            publisher_module._Cancelled("synthetic cancellation")
        ),
    )
    monkeypatch.setattr(
        publisher_module,
        "_cleanup_stage",
        lambda _target, _stage: (
            LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION
        ),
    )

    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace),
    )

    assert result.disposition is LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE
    assert result.prior_disposition is LocalSkillInstallDisposition.CANCELLED
    assert result.attempted_staging_path is not None
    assert (
        result.cleanup_location_status
        is LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION
    )
    assert result.destination_path is None
    assert result.publish_unavailable_reason is None


class _ScanBeforePublish:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.observed_names: tuple[str, ...] | None = None
        self.delegate = PlatformExclusiveDirectoryPublisher()

    def publish(self, root_fd: int, staging_name: str, final_name: str) -> None:
        provider = _loose_producer(self.workspace / "test-user")
        discovery = provider.observe(provider.prepare_root_policy(self.workspace))
        self.observed_names = tuple(item.name for item in discovery.candidates)
        self.delegate.publish(root_fd, staging_name, final_name)


def test_hidden_staging_is_scanner_inert_until_exclusive_publish(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "hidden-stage")
    adapter = _ScanBeforePublish(workspace)
    service = LocalSkillManagementService(
        publisher=AtomicLocalSkillPublisher(exclusive_publisher=adapter)
    )

    result = service.install_loose_local_skill(_workspace_request(source, workspace))

    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert result.disposition is LocalSkillInstallDisposition.INSTALLED
        assert adapter.observed_names == ()
        provider = _loose_producer(tmp_path / "visible-user")
        visible = provider.observe(provider.prepare_root_policy(workspace))
        assert [item.name for item in visible.candidates] == ["hidden-stage"]
    else:
        assert result.disposition is LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE


def test_cancel_after_final_cut_never_enters_publish_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    class MustNotPublish:
        called = False

        def publish(self, _root_fd: int, _staging_name: str, _final_name: str) -> None:
            self.called = True

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "cancel-before-publish")
    probe = EventLocalSkillCancellationProbe()
    adapter = MustNotPublish()
    original_cut = publisher_module._final_binding_cut

    def cut_then_cancel(*args, **kwargs) -> None:
        original_cut(*args, **kwargs)
        probe.cancel()

    monkeypatch.setattr(publisher_module, "_final_binding_cut", cut_then_cancel)
    service = LocalSkillManagementService(
        publisher=AtomicLocalSkillPublisher(exclusive_publisher=adapter)
    )
    result = service.install_loose_local_skill(
        _workspace_request(source, workspace),
        cancellation=probe,
    )

    assert result.disposition is LocalSkillInstallDisposition.CANCELLED
    assert adapter.called is False
    assert not (workspace / ".pulsara" / "skills" / source.name).exists()


def test_cancel_during_exclusive_syscall_settles_the_physical_publish_result(
    tmp_path: Path,
) -> None:
    class CancelInsidePublish:
        def __init__(self, probe: EventLocalSkillCancellationProbe) -> None:
            self.probe = probe
            self.delegate = PlatformExclusiveDirectoryPublisher()

        def publish(self, root_fd: int, staging_name: str, final_name: str) -> None:
            self.probe.cancel()
            self.delegate.publish(root_fd, staging_name, final_name)

    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "cancel-in-syscall")
    probe = EventLocalSkillCancellationProbe()
    service = LocalSkillManagementService(
        publisher=AtomicLocalSkillPublisher(
            exclusive_publisher=CancelInsidePublish(probe)
        )
    )

    result = service.install_loose_local_skill(
        _workspace_request(source, workspace),
        cancellation=probe,
    )

    assert result.disposition is LocalSkillInstallDisposition.INSTALLED
    assert (workspace / ".pulsara" / "skills" / source.name / "SKILL.md").is_file()


def test_pre_syscall_root_rebinding_is_typed_and_cleanup_location_is_honest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "root-race")
    original_cut = publisher_module._final_binding_cut

    def rebind_then_cut(target, stage, final_name, evidence, probe) -> None:
        moved = target.path.with_name("skills-moved")
        os.rename(target.path, moved)
        target.path.mkdir()
        original_cut(target, stage, final_name, evidence, probe)

    monkeypatch.setattr(publisher_module, "_final_binding_cut", rebind_then_cut)
    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE
    assert result.prior_disposition is LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE
    assert (
        result.cleanup_location_status
        is LocalSkillCleanupLocationStatus.UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE
    )
    assert result.attempted_staging_path is not None
    assert result.destination_path is None


def test_pre_syscall_stage_basename_replacement_is_typed_and_not_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skill_publisher as publisher_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "stage-race")
    original_cut = publisher_module._final_binding_cut

    def replace_then_cut(target, stage, final_name, evidence, probe) -> None:
        os.rename(
            stage.name,
            ".displaced-stage",
            src_dir_fd=target.descriptor,
            dst_dir_fd=target.descriptor,
        )
        os.mkdir(stage.name, dir_fd=target.descriptor)
        original_cut(target, stage, final_name, evidence, probe)

    monkeypatch.setattr(publisher_module, "_final_binding_cut", replace_then_cut)
    result = LocalSkillManagementService().install_loose_local_skill(
        _workspace_request(source, workspace)
    )

    assert result.disposition is LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE
    assert result.prior_disposition is LocalSkillInstallDisposition.STAGING_UNAVAILABLE
    assert (
        result.cleanup_location_status
        is LocalSkillCleanupLocationStatus.UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE
    )
    root = workspace / ".pulsara" / "skills"
    assert result.attempted_staging_path is not None
    assert (root / ".displaced-stage" / "SKILL.md").is_file()
    assert (root / result.attempted_staging_path.name).is_dir()


def test_post_cut_same_uid_root_move_is_out_of_contract_not_path_liveness(
    tmp_path: Path,
) -> None:
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return

    class MoveRootAfterCut:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.moved = root.with_name("skills-moved-after-cut")
            self.delegate = PlatformExclusiveDirectoryPublisher()

        def publish(self, root_fd: int, staging_name: str, final_name: str) -> None:
            os.rename(self.root, self.moved)
            self.delegate.publish(root_fd, staging_name, final_name)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "moved-root")
    root = workspace / ".pulsara" / "skills"
    adapter = MoveRootAfterCut(root)
    result = LocalSkillManagementService(
        publisher=AtomicLocalSkillPublisher(exclusive_publisher=adapter)
    ).install_loose_local_skill(_workspace_request(source, workspace))

    assert result.disposition is LocalSkillInstallDisposition.INSTALLED
    assert result.destination_path == root / source.name
    assert not result.destination_path.exists()
    assert (adapter.moved / source.name / "SKILL.md").is_file()
    assert not hasattr(result, "reply_time_path_is_live")
    assert not hasattr(result, "exact_path_guarantee")


def test_post_cut_same_uid_stage_replacement_has_no_verified_stage_claim(
    tmp_path: Path,
) -> None:
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return

    class ReplaceStageAfterCut:
        def __init__(self) -> None:
            self.delegate = PlatformExclusiveDirectoryPublisher()

        def publish(self, root_fd: int, staging_name: str, final_name: str) -> None:
            os.rename(
                staging_name,
                ".displaced-after-cut",
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            os.mkdir(staging_name, dir_fd=root_fd)
            stage_fd = os.open(
                staging_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                dir_fd=root_fd,
            )
            try:
                marker = os.open(
                    "interference-marker",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=stage_fd,
                )
                os.close(marker)
            finally:
                os.close(stage_fd)
            self.delegate.publish(root_fd, staging_name, final_name)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "replaced-stage")
    result = LocalSkillManagementService(
        publisher=AtomicLocalSkillPublisher(exclusive_publisher=ReplaceStageAfterCut())
    ).install_loose_local_skill(_workspace_request(source, workspace))

    assert result.disposition is LocalSkillInstallDisposition.INSTALLED
    assert result.destination_path is not None
    assert (result.destination_path / "interference-marker").is_file()
    assert not hasattr(result, "verified_stage_identity")


@pytest.mark.parametrize("phase", ["before", "after"])
def test_sigkill_publication_weak_guarantee_is_filesystem_truth(
    tmp_path: Path,
    phase: str,
) -> None:
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return
    workspace = tmp_path / f"workspace-{phase}"
    workspace.mkdir()
    source = _source(tmp_path / f"sources-{phase}", f"sigkill-{phase}")
    gate = tmp_path / f"{phase}.gate"
    script = (
        "from pathlib import Path\n"
        "import sys, time\n"
        "from pulsara_agent.capability.local_skill_management import "
        "InstallLooseLocalSkillRequest, LocalSkillInstallScope, "
        "LocalSkillManagementService\n"
        "from pulsara_agent.capability.local_skill_publisher import "
        "AtomicLocalSkillPublisher, PlatformExclusiveDirectoryPublisher\n"
        "source, workspace, gate, phase = map(Path, sys.argv[1:])\n"
        "class Gate:\n"
        "    def publish(self, root_fd, staging_name, final_name):\n"
        "        if phase.name == 'after':\n"
        "            PlatformExclusiveDirectoryPublisher().publish("
        "root_fd, staging_name, final_name)\n"
        "        gate.write_text(staging_name, encoding='utf-8')\n"
        "        while True:\n"
        "            time.sleep(1)\n"
        "service = LocalSkillManagementService("
        "publisher=AtomicLocalSkillPublisher(exclusive_publisher=Gate()))\n"
        "service.install_loose_local_skill(InstallLooseLocalSkillRequest("
        "source, LocalSkillInstallScope.WORKSPACE, workspace))\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(source), str(workspace), str(gate), phase],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_path(gate, process)
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    root = workspace / ".pulsara" / "skills"
    final = root / source.name
    if phase == "before":
        assert not final.exists()
        assert list(root.glob(".pulsara-skill-install-*"))
        provider = _loose_producer(tmp_path / "sigkill-user")
        discovery = provider.observe(provider.prepare_root_policy(workspace))
        assert discovery.candidates == ()
    else:
        assert (final / "SKILL.md").is_file()
        assert not list(root.glob(".pulsara-skill-install-*"))


def test_inspection_preserves_all_invalid_and_shadowed_candidate_issues(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / ".pulsara" / "skills" / "shared"
    second = workspace / ".agents" / "skills" / "shared"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "SKILL.md").write_text(_document("shared"), encoding="utf-8")
    (second / "SKILL.md").write_text(_document("shared"), encoding="utf-8")
    invalid_root = workspace / ".agents" / "skills"
    for index in range(129):
        name = f"invalid-{index:03d}"
        candidate = invalid_root / name
        candidate.mkdir()
        (candidate / "SKILL.md").write_text(
            f"---\nname: {name}\n---\nbody\n",
            encoding="utf-8",
        )
    provider = _loose_producer(tmp_path / "inspection-user")
    service = LocalSkillManagementService(loose_producer=provider)

    inspection = service.inspect_effective_skill_catalog(
        InspectEffectiveSkillCatalogRequest(workspace, _no_plugin_skills())
    )

    assert inspection.disposition is EffectiveSkillCatalogDisposition.COMPLETE
    assert isinstance(inspection, CompleteEffectiveSkillCatalogInspection)
    invalid = [
        item
        for item in inspection.candidate_issues
        if isinstance(item, InvalidSkillCandidateIssue)
    ]
    shadowed = [
        item
        for item in inspection.candidate_issues
        if isinstance(item, ShadowedSkillCandidateIssue)
    ]
    assert len(invalid) == 129
    assert len(shadowed) == 1
    shared = next(item for item in inspection.winners if item.name == "shared")
    assert shadowed[0].winner_path == shared.path == first / "SKILL.md"
    assert shadowed[0].winner_origin == shared.origin
    assert shadowed[0].name == shared.name == "shared"
    assert len(inspection_diagnostics(inspection)) == 129
    assert not hasattr(inspection, "enumerated_candidate_count")
    assert not hasattr(inspection, "observed_utf8_bytes")
    assert not hasattr(inspection, "diagnostics")


def test_direct_copy_modify_delete_remain_next_scan_filesystem_truth(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / ".pulsara" / "skills"
    source = _source(root, "direct-skill")
    provider = _loose_producer(tmp_path / "direct-user")
    policy = provider.prepare_root_policy(workspace)

    first = provider.observe(policy)
    (source / "SKILL.md").write_text(
        _document("direct-skill", description="Modified."),
        encoding="utf-8",
    )
    second = provider.observe(policy)
    for child in source.iterdir():
        child.unlink()
    source.rmdir()
    third = provider.observe(policy)

    assert first.candidates[0].description == "Portable local Skill."
    assert second.candidates[0].description == "Modified."
    assert third.candidates == ()
    assert third.disposition is LooseSkillDefinitionsDisposition.COMPLETE


def test_root_replacement_cannot_produce_a_mixed_complete_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skills as local_skills_module

    workspace = tmp_path / "workspace-root-replacement"
    root = workspace / ".pulsara" / "skills"
    _source(
        root,
        "alpha",
        document=_document("alpha", description="Old observation."),
    )
    provider = _loose_producer(tmp_path / "replacement-user")
    policy = provider.prepare_root_policy(workspace)
    original = local_skills_module.read_observed_skill_document
    replaced = False

    def replace_root_then_read(
        child, *, maximum: int, deadline_monotonic, cancellation=None
    ) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            displaced = root.with_name("skills-before-replacement")
            os.rename(root, displaced)
            _source(
                root,
                "alpha",
                document=_document("alpha", description="New observation."),
            )
            _source(root, "extra")
        return original(
            child,
            maximum=maximum,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )

    monkeypatch.setattr(
        local_skills_module,
        "read_observed_skill_document",
        replace_root_then_read,
    )
    raced = provider.observe(policy)

    assert raced.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE
    assert raced.unavailable_cause is not None
    assert raced.unavailable_cause.reason is (
        SkillProducerUnavailableReason.LOOSE_DISCOVERY_RACED
    )
    assert raced.candidates == ()
    assert raced.invalid_issues == ()
    assert [item.code for item in raced.unavailable_cause.diagnostics] == [
        SkillDiagnosticCode.ENUMERATION_RACED
    ]

    monkeypatch.setattr(
        local_skills_module,
        "read_observed_skill_document",
        original,
    )
    successor = provider.observe(policy)
    assert [item.name for item in successor.candidates] == ["alpha", "extra"]
    assert successor.candidates[0].description == "New observation."


def test_direct_filesystem_read_race_makes_the_whole_inspection_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.capability.local_skills as local_skills_module

    workspace = tmp_path / "workspace"
    root = workspace / ".pulsara" / "skills"
    _source(root, "first-valid")
    raced = _source(root, "second-raced") / "SKILL.md"
    original = local_skills_module.read_observed_skill_document

    def fail_one(
        child, *, maximum: int, deadline_monotonic, cancellation=None
    ) -> bytes:
        if child.evidence.name == raced.parent.name:
            raise OSError(errno.EIO, "synthetic direct-copy race")
        return original(
            child,
            maximum=maximum,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )

    monkeypatch.setattr(
        local_skills_module,
        "read_observed_skill_document",
        fail_one,
    )
    provider = _loose_producer(tmp_path / "race-user")
    discovery = provider.observe(provider.prepare_root_policy(workspace))

    assert discovery.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE
    assert discovery.candidates == ()
    assert discovery.invalid_issues == ()
    assert discovery.unavailable_cause is not None
    assert discovery.unavailable_cause.reason is (
        SkillProducerUnavailableReason.LOOSE_DISCOVERY_RACED
    )


def test_cli_four_loose_commands_project_typed_service_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "cli-skill")
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))

    monkeypatch.setattr(
        sys,
        "argv",
        ["pulsara", "skills", "validate", str(source), "--json"],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["disposition"] == "VALID"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pulsara",
            "skills",
            "install",
            "--scope",
            "workspace",
            "--workspace",
            str(workspace),
            str(source),
            "--json",
        ],
    )
    cli.main()
    install = json.loads(capsys.readouterr().out)
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert install["disposition"] == "INSTALLED"
    else:
        assert install["disposition"] == "PUBLISH_UNAVAILABLE"
        return

    for command in ("list", "doctor"):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pulsara",
                "skills",
                command,
                "--workspace",
                str(workspace),
                "--json",
            ],
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["operation"] == "inspect_effective_skill_catalog"
        assert {item["name"] for item in payload["skills"]} == {
            "cli-skill",
            "pulsara-plugin-installer",
            "pulsara-skill-creator",
            "pulsara-skill-installer",
        }


def test_cli_user_scope_rejects_workspace_as_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path / "sources", "usage-skill")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pulsara",
            "skills",
            "install",
            "--scope",
            "user",
            "--workspace",
            str(tmp_path),
            str(source),
        ],
    )
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 2


def test_local_skill_management_hard_cut_has_no_aggregate_join_fingerprint() -> None:
    assert "discovery_semantic_fingerprint" not in {
        item.name for item in fields(FrozenSkillProjectionInput)
    }
    import pulsara_agent.capability.local_skills as local_skills
    import pulsara_agent.conversation_kernel.capability as capability

    assert not hasattr(local_skills, "local_skill_root_policy_identity_digest")
    assert not hasattr(capability, "skill_discovery_semantic_fingerprint")


def test_management_boundary_is_cli_independent_and_private_installer_is_deleted() -> (
    None
):
    import pulsara_agent.capability.local_skill_management as management_module

    repository = Path(__file__).resolve().parents[1]
    management_source = Path(management_module.__file__).read_text(encoding="utf-8")
    cli_source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "pulsara_agent.cli" not in management_source
    assert "LocalSkillManagementService" in cli_source
    installer = repository / "src/pulsara_agent/bundled_skills/pulsara-skill-installer"
    for name in (
        "scripts/skill_utils.py",
        "scripts/install-local-skill.py",
        "scripts/list-installed-skills.py",
    ):
        assert not (installer / name).exists()
    bundled_text = (
        "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                repository / "src/pulsara_agent/bundled_skills/pulsara-skill-creator"
            ).rglob("*.md")
        )
        + "\n"
        + "\n".join(
            path.read_text(encoding="utf-8") for path in installer.rglob("*.md")
        )
    )
    assert "install-local-skill.py" not in bundled_text
    assert "list-installed-skills.py" not in bundled_text
    assert "skill_utils.py" not in bundled_text


def test_validate_and_install_share_one_narrow_source_binding_seam() -> None:
    import pulsara_agent.capability.local_skill_management as management_module
    import pulsara_agent.capability.local_skill_publisher as publisher_module
    import pulsara_agent.local_source_binding as binding_module

    binding_source = Path(binding_module.__file__).read_text(encoding="utf-8")
    management_source = Path(management_module.__file__).read_text(encoding="utf-8")
    publisher_source = Path(publisher_module.__file__).read_text(encoding="utf-8")
    cli_source = Path(cli.__file__).read_text(encoding="utf-8")

    assert binding_source.count("def prepare_local_source_path(") == 1
    assert binding_source.count("def open_absolute_directory_nofollow(") == 1
    assert "prepare_local_source_path" in management_source
    assert "prepare_local_source_path" in publisher_source
    assert "def _open_absolute_directory_nofollow(" not in management_source
    assert "def _open_absolute_directory_nofollow(" not in publisher_source
    assert "prepare_local_source_path" not in cli_source


def test_exclusive_adapter_reports_real_collision_without_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    stage = root / ".stage"
    final = root / "final"
    stage.mkdir()
    final.mkdir()
    marker = final / "marker"
    marker.write_text("existing", encoding="utf-8")
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            with pytest.raises(OSError) as raised:
                PlatformExclusiveDirectoryPublisher().publish(
                    root_fd,
                    stage.name,
                    final.name,
                )
            assert raised.value.errno in {errno.EEXIST, errno.ENOTEMPTY}
            assert marker.read_text(encoding="utf-8") == "existing"
            assert stage.is_dir()
        else:
            with pytest.raises(RuntimeError):
                PlatformExclusiveDirectoryPublisher().publish(
                    root_fd,
                    stage.name,
                    final.name,
                )
    finally:
        os.close(root_fd)


def test_exclusive_adapter_does_not_replace_an_existing_empty_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    stage = root / ".stage"
    final = root / "final"
    stage.mkdir()
    (stage / "payload").write_text("staged", encoding="utf-8")
    final.mkdir()
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            with pytest.raises(OSError) as raised:
                PlatformExclusiveDirectoryPublisher().publish(
                    root_fd,
                    stage.name,
                    final.name,
                )
            assert raised.value.errno in {errno.EEXIST, errno.ENOTEMPTY}
            assert final.is_dir() and not list(final.iterdir())
            assert (stage / "payload").read_text(encoding="utf-8") == "staged"
    finally:
        os.close(root_fd)


def test_unavailable_exclusive_primitive_never_falls_back_to_plain_rename(
    tmp_path: Path,
) -> None:
    class Unsupported:
        def publish(self, _root_fd: int, _staging_name: str, _final_name: str) -> None:
            raise ExclusivePublishPrimitiveUnavailable

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "unsupported-platform")
    result = LocalSkillManagementService(
        publisher=AtomicLocalSkillPublisher(exclusive_publisher=Unsupported())
    ).install_loose_local_skill(_workspace_request(source, workspace))

    assert result.disposition is LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE
    assert (
        result.publish_unavailable_reason
        is LocalSkillPublishUnavailableReason.UNSUPPORTED_EXCLUSIVE_PRIMITIVE
    )
    assert not (workspace / ".pulsara" / "skills" / source.name).exists()
    assert not list((workspace / ".pulsara" / "skills").glob(".*install*"))


def test_exclusive_publish_io_failure_is_closed_and_cleans_stage(
    tmp_path: Path,
) -> None:
    class PublishIoFailure:
        def publish(self, _root_fd: int, _staging_name: str, _final_name: str) -> None:
            raise OSError(errno.EIO, "synthetic publish I/O failure")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _source(tmp_path / "sources", "publish-io")
    result = LocalSkillManagementService(
        publisher=AtomicLocalSkillPublisher(exclusive_publisher=PublishIoFailure())
    ).install_loose_local_skill(_workspace_request(source, workspace))

    assert result.disposition is LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE
    assert (
        result.publish_unavailable_reason
        is LocalSkillPublishUnavailableReason.PUBLISH_IO_FAILURE
    )
    root = workspace / ".pulsara" / "skills"
    assert not (root / source.name).exists()
    assert not list(root.glob(".*install*"))


def test_cancellation_probe_is_process_local_and_has_no_deadline_or_retry_state() -> (
    None
):
    probe = EventLocalSkillCancellationProbe()
    assert probe.cancellation_requested() is False
    probe.cancel()
    assert probe.cancellation_requested() is True
    assert not hasattr(probe, "deadline")
    assert not hasattr(probe, "retry_count")
