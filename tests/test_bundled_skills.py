from __future__ import annotations

import json
import shutil
from pathlib import Path

from pulsara_agent.capability import LocalSkillProvider
from pulsara_agent.capability.bundled_skills import (
    BUNDLED_MANIFEST_FILE_NAME,
    BUNDLED_OPT_OUT_MARKER_NAME,
    compute_skill_dir_hash,
    bundled_skills_status,
    reset_bundled_skill,
    sync_bundled_skills,
)


def test_sync_bundled_skills_installs_manifest_provenance_and_runtime_discovery(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha", description="Alpha bundled skill.")

    result = sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)

    assert [item.action for item in result.items] == ["installed"]
    assert result.manifest_written is True
    target = pulsara_home / "skills" / "pulsara-alpha"
    assert (target / "SKILL.md").is_file()
    manifest = (pulsara_home / "skills" / BUNDLED_MANIFEST_FILE_NAME).read_text(encoding="utf-8")
    assert "pulsara-alpha:" in manifest
    provenance = json.loads((target / ".pulsara-skill-source.json").read_text(encoding="utf-8"))
    assert provenance["source"] == "bundled"
    assert provenance["bundled_from"] == "pulsara-agent"

    discovery = LocalSkillProvider(
        user_product_skills_root=pulsara_home / "skills",
        user_agents_skills_root=tmp_path / "empty-agents",
    ).discover(tmp_path / "workspace", available_tool_names=frozenset())

    assert len(discovery.skills) == 1
    assert discovery.skills[0].name == "pulsara-alpha"
    assert discovery.skills[0].source == "bundled"
    assert discovery.skills[0].location == "~/.pulsara/skills/pulsara-alpha/SKILL.md"


def test_runtime_discovery_classifies_bundled_skill_from_user_product_root(tmp_path) -> None:
    pulsara_home = tmp_path / "pulsara-home"
    skill_dir = _write_source_skill(pulsara_home / "skills", "pulsara-alpha", description="Alpha bundled skill.")
    (skill_dir / ".pulsara-skill-source.json").write_text(
        json.dumps(
            {
                "source": "bundled",
                "bundled_from": "pulsara-agent",
                "bundled_version": "0.1.0",
                "origin_hash": compute_skill_dir_hash(skill_dir),
            }
        ),
        encoding="utf-8",
    )

    discovery = LocalSkillProvider(
        user_product_skills_root=pulsara_home / "skills",
        user_agents_skills_root=tmp_path / "empty-agents",
    ).discover(tmp_path / "workspace", available_tool_names=frozenset())

    assert len(discovery.skills) == 1
    assert discovery.skills[0].name == "pulsara-alpha"
    assert discovery.skills[0].source == "bundled"


def test_sync_bundled_skills_second_sync_is_unchanged_noop(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha")
    sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)

    result = sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)

    assert [item.action for item in result.items] == ["unchanged"]
    assert result.manifest_written is False


def test_sync_bundled_skills_does_not_overwrite_user_modified_skill(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha", body="# Original\n")
    sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)
    target = pulsara_home / "skills" / "pulsara-alpha"
    (target / "user-note.txt").write_text("user modification\n", encoding="utf-8")
    _write_source_skill(source, "pulsara-alpha", body="# Updated bundled source\n")

    result = sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)

    assert [item.action for item in result.items] == ["skipped_modified"]
    assert (target / "user-note.txt").read_text(encoding="utf-8") == "user modification\n"
    assert "# Original" in (target / "SKILL.md").read_text(encoding="utf-8")


def test_sync_bundled_skills_does_not_restore_user_deleted_skill(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha")
    sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)
    shutil.rmtree(pulsara_home / "skills" / "pulsara-alpha")

    result = sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)
    status = bundled_skills_status(pulsara_home=pulsara_home, source_root=source)

    assert [item.action for item in result.items] == ["skipped_deleted"]
    assert not (pulsara_home / "skills" / "pulsara-alpha").exists()
    assert status.statuses[0].state == "deleted"


def test_sync_bundled_skills_removes_manifest_entry_when_source_is_removed(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha")
    sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)
    shutil.rmtree(source / "pulsara-alpha")

    result = sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)

    assert [item.action for item in result.items] == ["source_removed"]
    assert (pulsara_home / "skills" / BUNDLED_MANIFEST_FILE_NAME).read_text(encoding="utf-8") == ""
    assert (pulsara_home / "skills" / "pulsara-alpha").exists()


def test_sync_bundled_skills_respects_opt_out_marker(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha")
    pulsara_home.mkdir()
    (pulsara_home / BUNDLED_OPT_OUT_MARKER_NAME).write_text("", encoding="utf-8")

    result = sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)

    assert result.opt_out is True
    assert [item.action for item in result.items] == ["opted_out"]
    assert not (pulsara_home / "skills").exists()


def test_sync_bundled_skills_skips_existing_unmanaged_skill(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha", body="# Bundled\n")
    _write_source_skill(pulsara_home / "skills", "pulsara-alpha", body="# User existing\n")

    result = sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)

    assert [item.action for item in result.items] == ["skipped_existing_unmanaged"]
    assert "# User existing" in (pulsara_home / "skills" / "pulsara-alpha" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert not (pulsara_home / "skills" / BUNDLED_MANIFEST_FILE_NAME).exists()


def test_reset_bundled_skill_backs_up_and_restores_modified_target(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha", body="# Bundled source\n")
    sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)
    target = pulsara_home / "skills" / "pulsara-alpha"
    (target / "SKILL.md").write_text(
        """---
name: pulsara-alpha
description: Modified by user.
---
# Modified
""",
        encoding="utf-8",
    )

    result = reset_bundled_skill("pulsara-alpha", pulsara_home=pulsara_home, source_root=source)

    assert result.action == "reset"
    assert result.backup_path is not None
    assert (result.backup_path / "SKILL.md").is_file()
    assert "# Bundled source" in (target / "SKILL.md").read_text(encoding="utf-8")
    assert result.origin_hash == compute_skill_dir_hash(source / "pulsara-alpha")
    manifest = (pulsara_home / "skills" / BUNDLED_MANIFEST_FILE_NAME).read_text(encoding="utf-8")
    assert f"pulsara-alpha:{result.origin_hash}\n" in manifest


def test_bundled_skills_status_reports_available_to_sync_without_writing(tmp_path) -> None:
    source = tmp_path / "bundled-source"
    pulsara_home = tmp_path / "pulsara-home"
    _write_source_skill(source, "pulsara-alpha")

    result = bundled_skills_status(pulsara_home=pulsara_home, source_root=source)

    assert [status.state for status in result.statuses] == ["available_to_sync"]
    assert not (pulsara_home / "skills").exists()


def test_default_bundled_source_contains_first_official_skills(tmp_path) -> None:
    result = bundled_skills_status(pulsara_home=tmp_path / "pulsara-home")

    names = {status.name for status in result.statuses}
    assert {"pulsara-skill-installer", "pulsara-skill-creator"}.issubset(names)
    assert not (tmp_path / "pulsara-home" / "skills").exists()


def _write_source_skill(
    root: Path,
    name: str,
    *,
    description: str = "A bundled skill.",
    body: str = "# Bundled Skill\n",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
---
{body}
""",
        encoding="utf-8",
    )
    return skill_dir
