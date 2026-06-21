#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class SkillValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    skill_md: Path


def resolve_workspace(raw_workspace: str | None) -> Path:
    workspace = Path(raw_workspace or ".").expanduser().resolve()
    if not workspace.is_dir():
        raise SkillValidationError(f"workspace is not a directory: {workspace}")
    return workspace


def default_dest_root(workspace: Path) -> Path:
    return workspace / ".pulsara" / "skills"


def resolve_source(workspace: Path, raw_src: str) -> Path:
    src = Path(raw_src).expanduser()
    if not src.is_absolute():
        src = workspace / src
    src = src.resolve()
    if not src.is_dir():
        raise SkillValidationError(f"source skill directory not found: {src}")
    return src


def extract_frontmatter(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError("SKILL.md must start with YAML frontmatter")

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---" and not line[:1].isspace():
            return "\n".join(lines[1:index])
    raise SkillValidationError("SKILL.md frontmatter is missing a closing --- fence")


def load_frontmatter(skill_md: Path) -> dict[str, Any]:
    frontmatter = extract_frontmatter(skill_md)
    try:
        import yaml
    except ModuleNotFoundError:
        return _load_minimal_frontmatter(frontmatter)

    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillValidationError("frontmatter must be a YAML mapping")
    return data


def _load_minimal_frontmatter(frontmatter: str) -> dict[str, Any]:
    data: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            data[key] = value
    return data


def validate_skill_dir(skill_dir: Path) -> SkillManifest:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise SkillValidationError(f"missing SKILL.md: {skill_md}")

    frontmatter = load_frontmatter(skill_md)
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillValidationError("frontmatter field 'name' must be a non-empty string")
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError("frontmatter field 'description' must be a non-empty string")

    name = name.strip()
    description = description.strip()
    if not SKILL_NAME_RE.fullmatch(name):
        raise SkillValidationError(
            "skill name must use lowercase letters, digits, and hyphens, "
            "must not start/end with a hyphen, and must be at most 64 characters"
        )
    if skill_dir.name != name:
        raise SkillValidationError(
            f"skill folder name must match frontmatter name: folder={skill_dir.name!r}, name={name!r}"
        )
    return SkillManifest(name=name, description=description, skill_md=skill_md)


def reject_symlinks(skill_dir: Path) -> None:
    for path in skill_dir.rglob("*"):
        if path.is_symlink():
            raise SkillValidationError(f"skill directory contains a symlink, refusing to copy: {path}")


def manifest_to_json(manifest: SkillManifest, path: Path) -> str:
    return json.dumps(
        {
            "name": manifest.name,
            "description": manifest.description,
            "path": str(path),
            "skill_md": str(manifest.skill_md),
        },
        ensure_ascii=False,
        indent=2,
    )
