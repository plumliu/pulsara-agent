"""Finite Skill frontmatter normalization; source bodies/resources stay unchanged."""

from __future__ import annotations

import yaml
import os
from pathlib import Path
import stat

from pulsara_agent.local_source_binding import open_absolute_directory_nofollow

from .local_skills import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_FRONTMATTER_BYTES,
    _extract_frontmatter,
    _load_unique_yaml_mapping,
    _validate_yaml_shape,
)


def enumerate_skill_import_sources(source: Path) -> tuple[Path, ...]:
    """Read only the explicitly selected source and conventional collection roots.

    These are import locations, not additional runtime discovery roots. Never
    descend through a symlink or execute any source file.
    """
    candidates: set[Path] = set()
    for relative in (
        ".",
        "skills",
        ".opencode/skill",
        ".opencode/skills",
        ".agents/skills",
        ".claude/skills",
    ):
        directory = source / relative
        try:
            descriptor = open_absolute_directory_nofollow(directory)
        except (FileNotFoundError, NotADirectoryError):
            continue
        try:
            names = sorted(os.listdir(descriptor))
            if "SKILL.md" in names:
                candidates.add(directory)
                continue
            for name in names:
                if name.startswith("."):
                    continue
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    if "SKILL.md" in os.listdir(child):
                        candidates.add(directory / name)
                finally:
                    os.close(child)
        finally:
            os.close(descriptor)
    return tuple(sorted(candidates, key=str))


def normalize_skill_import_document(
    source: bytes,
    *,
    name: str | None = None,
    description: str | None = None,
) -> bytes:
    if name is None and description is None:
        return source
    if len(source) > MAX_SKILL_FILE_BYTES:
        raise ValueError("Skill source exceeds the existing document byte bound")
    frontmatter, body = _extract_frontmatter(source.decode("utf-8"))
    if frontmatter is None or body is None:
        raise ValueError("Skill import requires a frontmatter document")
    if len(frontmatter.encode("utf-8")) > MAX_SKILL_FRONTMATTER_BYTES:
        raise ValueError("Skill source frontmatter exceeds its byte bound")
    _validate_yaml_shape(frontmatter)
    fields = _load_unique_yaml_mapping(frontmatter)
    if name is not None:
        if not isinstance(name, str):
            raise ValueError("Skill name must be text")
        fields["name"] = name
    if description is not None:
        if not isinstance(description, str):
            raise ValueError("Skill description must be text")
        fields["description"] = description
    # Decode/encode UTF-8 preserves the exact body, including CRLF and blank lines.
    return (
        "---\n" + yaml.safe_dump(fields, sort_keys=False, allow_unicode=True) + "---\n"
    ).encode("utf-8") + body.encode("utf-8")
