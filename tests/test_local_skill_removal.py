import pytest

from pulsara_agent.capability.local_skill_management import LocalSkillManagementService
from pulsara_agent.capability.local_skill_publisher import LocalSkillInstallScope
from pulsara_agent.capability.local_skill_removal import LocalSkillRemovalDisposition
from pulsara_agent.capability.user_skill_config import (
    load_user_skill_config,
    set_user_skill_enabled,
)


def installed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("PULSARA_HOME", str(home))
    folder = home / "skills" / "test-skill"
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text(
        "---\nname: test-skill\ndescription: Testing.\n---\nUse the resource.\n"
    )
    return LocalSkillManagementService(), path


def test_removal_unbinds_complete_copy_and_clears_only_its_override(
    tmp_path, monkeypatch
):
    service, path = installed(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "resource.txt").write_text("source remains")
    (path.parent / "resource.txt").write_text("installed resource")
    (path.parent / "link").symlink_to(source, target_is_directory=True)
    other = path.parent.parent / "other" / "SKILL.md"
    set_user_skill_enabled(skill_path=path, enabled=False)
    set_user_skill_enabled(skill_path=other, enabled=False)
    expected = service.inspect_loose_skill_removal(
        skill_path=path, scope=LocalSkillInstallScope.USER
    )
    outcome = service.remove_loose_local_skill(
        skill_path=path, scope=LocalSkillInstallScope.USER, expected=expected
    )
    assert outcome.disposition is LocalSkillRemovalDisposition.REMOVED
    assert not path.parent.exists()
    assert (source / "resource.txt").read_text() == "source remains"
    assert not list(path.parent.parent.glob(".pulsara-skill-delete-*"))
    settings = load_user_skill_config()
    assert settings.enabled_for(path)
    assert not settings.enabled_for(other)
    assert (
        service.remove_loose_local_skill(
            skill_path=path, scope=LocalSkillInstallScope.USER, expected=expected
        ).disposition
        is LocalSkillRemovalDisposition.NOT_FOUND
    )


def test_replaced_installed_directory_is_stale_not_deleted(tmp_path, monkeypatch):
    service, path = installed(tmp_path, monkeypatch)
    expected = service.inspect_loose_skill_removal(
        skill_path=path, scope=LocalSkillInstallScope.USER
    )
    path.parent.rename(path.parent.parent / "saved-original")
    path.parent.mkdir()
    path.write_text("replacement")
    outcome = service.remove_loose_local_skill(
        skill_path=path, scope=LocalSkillInstallScope.USER, expected=expected
    )
    assert outcome.disposition is LocalSkillRemovalDisposition.STALE
    assert path.read_text() == "replacement"
    assert (path.parent.parent / "saved-original" / "SKILL.md").exists()


@pytest.mark.parametrize(
    "suffix",
    [
        "plugins/example/SKILL.md",
        "skills/.hidden/SKILL.md",
        "skills/one/nested/SKILL.md",
    ],
)
def test_removal_rejects_non_owned_or_non_immediate_targets(
    tmp_path, monkeypatch, suffix
):
    service, original = installed(tmp_path, monkeypatch)
    other = tmp_path / "home" / suffix
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("not the selected loose skill")
    with pytest.raises(ValueError):
        service.inspect_loose_skill_removal(
            skill_path=other, scope=LocalSkillInstallScope.USER
        )
    assert original.exists() and other.exists()


def test_symlink_replacement_is_stale_and_target_is_untouched(tmp_path, monkeypatch):
    service, path = installed(tmp_path, monkeypatch)
    expected = service.inspect_loose_skill_removal(
        skill_path=path, scope=LocalSkillInstallScope.USER
    )
    outside = tmp_path / "outside"
    path.parent.rename(outside)
    path.parent.symlink_to(outside, target_is_directory=True)
    outcome = service.remove_loose_local_skill(
        skill_path=path, scope=LocalSkillInstallScope.USER, expected=expected
    )
    assert outcome.disposition is LocalSkillRemovalDisposition.STALE
    assert (outside / "SKILL.md").exists()
    assert path.parent.is_symlink()
