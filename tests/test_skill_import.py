import pytest

from pulsara_agent.capability.skill_import import (
    enumerate_skill_import_sources,
    normalize_skill_import_document,
)
from pulsara_agent.capability.local_skill_management import (
    InstallLooseLocalSkillRequest,
    LocalSkillInstallDisposition,
    LocalSkillInstallScope,
    LocalSkillManagementService,
)


def test_normalized_install_preserves_source_body_resources_and_executable(tmp_path):
    source = tmp_path / "Odd Source Directory"
    source.mkdir()
    body = b"\r\n# Instructions\r\nUse `scripts/run.sh` and references.\r\n"
    raw = b"---\r\nname: example\r\nlicense: MIT\r\n---\r\n" + body
    (source / "SKILL.md").write_bytes(raw)
    scripts = source / "scripts"
    scripts.mkdir()
    script = scripts / "run.sh"
    script.write_bytes(b"#!/bin/sh\nprintf do-not-run\n")
    script.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = LocalSkillManagementService().install_loose_local_skill(
        InstallLooseLocalSkillRequest(
            source,
            LocalSkillInstallScope.WORKSPACE,
            workspace,
            "portable",
            "用户填写的用途",
        )
    )
    assert result.disposition is LocalSkillInstallDisposition.INSTALLED
    destination = workspace / ".pulsara/skills/portable"
    assert (destination / "SKILL.md").read_bytes().endswith(body)
    assert b"license: MIT" in (destination / "SKILL.md").read_bytes()
    assert (destination / "scripts/run.sh").read_bytes() == script.read_bytes()
    assert (destination / "scripts/run.sh").stat().st_mode & 0o111 == 0o111
    assert (source / "SKILL.md").read_bytes() == raw


@pytest.mark.parametrize(
    "frontmatter",
    ["name: a\nname: b", "name: &a value\ndescription: *a", "[not, a, mapping]"],
)
def test_normalization_never_bypasses_native_yaml_validation(tmp_path, frontmatter):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nBody\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outcome = LocalSkillManagementService().install_loose_local_skill(
        InstallLooseLocalSkillRequest(
            source,
            LocalSkillInstallScope.WORKSPACE,
            workspace,
            "portable",
            "Description",
        )
    )
    assert outcome.disposition is LocalSkillInstallDisposition.SOURCE_INVALID
    assert not (workspace / ".pulsara/skills/portable").exists()


def test_collection_enumeration_only_selected_layouts_and_never_external_symlinks(
    tmp_path,
):
    expected = []
    for layout in (
        "skills",
        ".opencode/skill",
        ".opencode/skills",
        ".agents/skills",
        ".claude/skills",
    ):
        path = tmp_path / layout / "example"
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text("---\nname: example\n---\nBody")
        expected.append(path)
    external = tmp_path / "not-a-layout/hidden"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("ignored")
    (tmp_path / "skills/linked").symlink_to(external)
    assert enumerate_skill_import_sources(tmp_path) == tuple(sorted(expected, key=str))
    assert enumerate_skill_import_sources(expected[0]) == (expected[0],)


def test_no_normalization_is_byte_identical():
    raw = b"---\r\nname: example\r\ndescription: fine\r\n---\r\n# Body\r\n"
    assert normalize_skill_import_document(raw) is raw
