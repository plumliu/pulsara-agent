"""Local Skill inspection and actionable workspace targets for the desktop UI."""

from __future__ import annotations

from pathlib import Path

from pulsara_agent.capability.local_skill_management import (
    LocalSkillInstallScope,
    LocalSkillManagementService,
)
from pulsara_agent.identity import derive_id, require_id


def _absolute_local_source(value: str, label: str) -> Path:
    source = Path(value.strip()).expanduser()
    if not value.strip() or not source.is_absolute():
        raise ValueError(f"{label} source path must be absolute")
    return source



def _skill_removal_identity(
    path: Path, scope: LocalSkillInstallScope, workspace_root: Path | None = None
):
    try:
        identity = LocalSkillManagementService().inspect_loose_skill_removal(
            skill_path=path,
            scope=scope,
            workspace_root=workspace_root,
        )
    except (ValueError, OSError):
        return None
    return {
        name: str(getattr(identity, name))
        for name in (
            "root_device",
            "root_inode",
            "directory_device",
            "directory_inode",
        )
    }



def _inspect_user_skills() -> dict[str, object]:
    return _inspect_loose_skill_management(LocalSkillInstallScope.USER, None)



def _inspect_workspace_skills(workspace_root: Path) -> dict[str, object]:
    return _inspect_loose_skill_management(LocalSkillInstallScope.WORKSPACE, workspace_root)



def _inspect_loose_skill_management(scope, workspace_root) -> dict[str, object]:
    observed, _, _ = LocalSkillManagementService().inspect_loose_skills(
        scope=scope, workspace_root=workspace_root)
    observed["status"] = "ready" if observed["status"] == "COMPLETE" and observed["config_available"] else "attention"
    target_facts = []
    for item in observed["items"]:
        path = Path(item["path"])
        item["removal_identity"] = _skill_removal_identity(path, scope, workspace_root)
        if workspace_root is not None:
            item["id"] = derive_id("skill", "ui-target", item["root"], item["name"])
            target_facts.append((item["id"], (item["root"], item["name"])))
    _check_skill_target_collisions(target_facts)
    return observed



def _check_skill_target_collisions(facts):
    ordered = sorted(facts)
    if any(a[0] == b[0] and a[1] != b[1] for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Skill target identity collision")



def _workspace_skill_path(observed: dict[str, object], skill_id: str) -> Path:
    require_id(skill_id, "skill")
    raw_items = observed.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("project Skill inventory is invalid")
    matches = [
        item
        for item in raw_items
        if isinstance(item, dict) and item.get("id") == skill_id
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("path"), str):
        raise ValueError("Skill is not in this project's capability directories")
    return Path(str(matches[0]["path"])).resolve(strict=False)
