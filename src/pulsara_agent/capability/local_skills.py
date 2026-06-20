"""Local SKILL.md discovery and V1 frontmatter parsing."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pulsara_agent.capability.types import CapabilityDiagnostic, LocalSkillManifest, SkillSource


WORKSPACE_PRODUCT_SKILL_ROOT_PARTS = (".pulsara", "skills")
WORKSPACE_AGENTS_SKILL_ROOT_PARTS = (".agents", "skills")
USER_PRODUCT_SKILL_ROOT_PARTS = (".pulsara", "skills")
USER_AGENTS_SKILL_ROOT_PARTS = (".agents", "skills")
PULSARA_HOME_ENV = "PULSARA_HOME"
SKILL_FILE_NAME = "SKILL.md"
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_SKILL_NAME_CHARS = 64
MAX_FRONTMATTER_TEXT_CHARS = 1024

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_KNOWN_FRONTMATTER = {
    "name",
    "description",
    "when_to_use",
    "provides_tools",
    "disable_model_invocation",
    "user_invocable",
}
_IGNORED_SCOPE_FRONTMATTER = {"allowed_scopes", "blocked_scopes"}


@dataclass(frozen=True, slots=True)
class LocalSkillDiscovery:
    skills: tuple[LocalSkillManifest, ...]
    diagnostics: tuple[CapabilityDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class _SkillRoot:
    path: Path
    source: SkillSource
    location_prefix: str
    containment_root: Path


class LocalSkillProvider:
    def __init__(
        self,
        *,
        max_skill_file_bytes: int = MAX_SKILL_FILE_BYTES,
        user_product_skills_root: Path | None = None,
        user_agents_skills_root: Path | None = None,
        include_user_skills: bool = True,
    ) -> None:
        self.max_skill_file_bytes = max_skill_file_bytes
        self.user_product_skills_root = user_product_skills_root
        self.user_agents_skills_root = user_agents_skills_root
        self.include_user_skills = include_user_skills

    def discover(
        self,
        workspace_root: Path,
        *,
        available_tool_names: frozenset[str],
    ) -> LocalSkillDiscovery:
        workspace_root = workspace_root.expanduser().resolve()
        skill_roots = self._skill_roots(workspace_root)
        diagnostics: list[CapabilityDiagnostic] = []
        skills: list[LocalSkillManifest] = []
        seen_names: set[str] = set()

        for root in skill_roots:
            if not root.path.exists():
                continue
            if not _is_within(root.path, root.containment_root):
                diagnostics.append(
                    CapabilityDiagnostic(
                        severity="warning",
                        code="skill_symlink_escape",
                        message=f"Skill root resolves outside allowed root: {root.path}",
                        path=root.path,
                    )
                )
                continue
            if not root.path.is_dir():
                diagnostics.append(
                    CapabilityDiagnostic(
                        severity="warning",
                        code="skill_root_not_directory",
                        message=f"Skill root is not a directory: {root.path}",
                        path=root.path,
                    )
                )
                continue

            for child in sorted(root.path.iterdir(), key=lambda path: path.name):
                if child.name.startswith("."):
                    continue
                if not child.is_dir():
                    continue
                if not _is_within(child, root.containment_root):
                    diagnostics.append(
                        CapabilityDiagnostic(
                            severity="warning",
                            code="skill_symlink_escape",
                            message=f"Skill directory resolves outside skill root: {child}",
                            path=child,
                        )
                    )
                    continue
                skill_file = child / SKILL_FILE_NAME
                if not skill_file.exists():
                    diagnostics.append(
                        CapabilityDiagnostic(
                            severity="warning",
                            code="skill_missing_file",
                            message=f"Skill directory has no {SKILL_FILE_NAME}: {child}",
                            path=child,
                        )
                    )
                    continue
                if not _is_within(skill_file, root.containment_root):
                    diagnostics.append(
                        CapabilityDiagnostic(
                            severity="warning",
                            code="skill_symlink_escape",
                            message=f"Skill file resolves outside skill root: {skill_file}",
                            path=skill_file,
                        )
                    )
                    continue
                skill, parse_diagnostics = self._parse_skill_file(
                    skill_file,
                    root=root,
                    available_tool_names=available_tool_names,
                )
                diagnostics.extend(parse_diagnostics)
                if skill is None:
                    continue
                if skill.name in seen_names:
                    diagnostics.append(
                        CapabilityDiagnostic(
                            severity="warning",
                            code="skill_duplicate_name",
                            message=f"Duplicate skill name ignored: {skill.name}",
                            path=skill.path,
                        )
                    )
                    continue
                seen_names.add(skill.name)
                skills.append(skill)
        return LocalSkillDiscovery(skills=tuple(skills), diagnostics=tuple(diagnostics))

    def _skill_roots(self, workspace_root: Path) -> tuple[_SkillRoot, ...]:
        roots = [
            _SkillRoot(
                path=workspace_root.joinpath(*WORKSPACE_PRODUCT_SKILL_ROOT_PARTS),
                source="workspace",
                location_prefix=".pulsara/skills",
                containment_root=workspace_root,
            ),
            _SkillRoot(
                path=workspace_root.joinpath(*WORKSPACE_AGENTS_SKILL_ROOT_PARTS),
                source="workspace",
                location_prefix=".agents/skills",
                containment_root=workspace_root,
            ),
        ]
        if self.include_user_skills:
            user_product_root = self.user_product_skills_root
            if user_product_root is None:
                user_product_root = _default_user_product_skills_root()
            user_agents_root = self.user_agents_skills_root
            if user_agents_root is None:
                user_agents_root = Path.home().joinpath(*USER_AGENTS_SKILL_ROOT_PARTS)
            roots.append(
                _SkillRoot(
                    path=user_product_root.expanduser().resolve(),
                    source="user",
                    location_prefix="~/.pulsara/skills",
                    containment_root=user_product_root.expanduser().resolve(),
                )
            )
            roots.append(
                _SkillRoot(
                    path=user_agents_root.expanduser().resolve(),
                    source="user",
                    location_prefix="~/.agents/skills",
                    containment_root=user_agents_root.expanduser().resolve(),
                )
            )
        return tuple(roots)

    def _parse_skill_file(
        self,
        path: Path,
        *,
        root: _SkillRoot,
        available_tool_names: frozenset[str],
    ) -> tuple[LocalSkillManifest | None, tuple[CapabilityDiagnostic, ...]]:
        diagnostics: list[CapabilityDiagnostic] = []
        try:
            content, too_large = _read_bounded_utf8(path, max_bytes=self.max_skill_file_bytes)
        except UnicodeDecodeError:
            return None, (
                CapabilityDiagnostic(
                    severity="error",
                    code="skill_invalid_utf8",
                    message=f"Skill file is not valid UTF-8: {path}",
                    path=path,
                ),
            )
        if too_large:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_body_too_large",
                    message=f"Skill file exceeds {self.max_skill_file_bytes} bytes and cannot be activated.",
                    path=path,
                )
            )

        frontmatter, has_frontmatter = _extract_frontmatter(content)
        if not has_frontmatter:
            return None, (
                *diagnostics,
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_missing_frontmatter",
                    message=f"Skill file has no YAML frontmatter: {path}",
                    path=path,
                ),
            )
        raw_fields, field_diagnostics = _parse_frontmatter(frontmatter, path=path)
        diagnostics.extend(field_diagnostics)
        diagnostics.extend(_frontmatter_key_diagnostics(raw_fields, path=path))

        name = _string_field(raw_fields, "name")
        description = _string_field(raw_fields, "description")
        if not name:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_missing_name",
                    message="Skill frontmatter is missing required field: name",
                    path=path,
                )
            )
        elif len(name) > MAX_SKILL_NAME_CHARS or not _NAME_RE.fullmatch(name):
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_invalid_name",
                    message="Skill name must be lowercase letters, digits, and hyphens, starting with a letter or digit.",
                    path=path,
                )
            )
            name = None

        if not description:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_missing_description",
                    message="Skill frontmatter is missing required field: description",
                    path=path,
                )
            )
        if not name or not description:
            return None, tuple(diagnostics)

        description = _clip_frontmatter_text(description)
        when_to_use = _string_field(raw_fields, "when_to_use")
        if when_to_use:
            when_to_use = _clip_frontmatter_text(when_to_use)
        provides_tools, tool_diagnostics = _provides_tools(
            raw_fields.get("provides_tools"),
            available_tool_names=available_tool_names,
            path=path,
        )
        diagnostics.extend(tool_diagnostics)
        return (
            LocalSkillManifest(
                name=name,
                description=description,
                path=path,
                base_dir=path.parent,
                location=_skill_location(path, root=root),
                content=content,
                source=root.source,
                when_to_use=when_to_use,
                provides_tools=provides_tools,
                disable_model_invocation=_bool_field(raw_fields, "disable_model_invocation", default=False),
                user_invocable=_bool_field(raw_fields, "user_invocable", default=True),
                body_too_large=too_large,
            ),
            tuple(diagnostics),
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        return False
    return True


def _skill_location(path: Path, *, root: _SkillRoot) -> str:
    relative = path.resolve().relative_to(root.path.resolve()).as_posix()
    return f"{root.location_prefix}/{relative}"


def _default_user_product_skills_root() -> Path:
    pulsara_home = os.getenv(PULSARA_HOME_ENV)
    if pulsara_home:
        return Path(pulsara_home).expanduser() / "skills"
    return Path.home().joinpath(*USER_PRODUCT_SKILL_ROOT_PARTS)


def _read_bounded_utf8(path: Path, *, max_bytes: int) -> tuple[str, bool]:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    too_large = len(data) > max_bytes
    if too_large:
        data = data[:max_bytes]
    return data.decode("utf-8"), too_large


def _extract_frontmatter(content: str) -> tuple[str, bool]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", False
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---" and not line[:1].isspace():
            return "\n".join(lines[1:index]), True
    return "", False


def _parse_frontmatter(frontmatter: str, *, path: Path) -> tuple[dict[str, Any], tuple[CapabilityDiagnostic, ...]]:
    try:
        parsed = yaml.safe_load(frontmatter) if frontmatter.strip() else {}
    except yaml.YAMLError as exc:
        return {}, (
            CapabilityDiagnostic(
                severity="warning",
                code="skill_invalid_frontmatter_yaml",
                message=f"Ignoring invalid YAML frontmatter: {exc}",
                path=path,
            ),
        )
    if parsed is None:
        return {}, ()
    if not isinstance(parsed, dict):
        return {}, (
            CapabilityDiagnostic(
                severity="warning",
                code="skill_invalid_frontmatter_type",
                message="Skill frontmatter must be a YAML mapping.",
                path=path,
            ),
        )
    return {str(key): value for key, value in parsed.items()}, ()


def _frontmatter_key_diagnostics(fields: dict[str, Any], *, path: Path) -> tuple[CapabilityDiagnostic, ...]:
    diagnostics: list[CapabilityDiagnostic] = []
    for key in sorted(fields):
        if key in _IGNORED_SCOPE_FRONTMATTER:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_scope_frontmatter_ignored_in_v1",
                    message=f"Ignoring V1 scope frontmatter field: {key}",
                    path=path,
                )
            )
        elif key not in _KNOWN_FRONTMATTER:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_unknown_frontmatter",
                    message=f"Ignoring unknown skill frontmatter field: {key}",
                    path=path,
                )
            )
    return tuple(diagnostics)


def _string_field(fields: dict[str, Any], key: str) -> str | None:
    value = fields.get(key)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _bool_field(fields: dict[str, Any], key: str, *, default: bool) -> bool:
    value = fields.get(key)
    if isinstance(value, bool):
        return value
    return default


def _clip_frontmatter_text(value: str) -> str:
    if len(value) <= MAX_FRONTMATTER_TEXT_CHARS:
        return value
    return value[:MAX_FRONTMATTER_TEXT_CHARS]


def _provides_tools(
    raw_value: Any,
    *,
    available_tool_names: frozenset[str],
    path: Path,
) -> tuple[tuple[str, ...], tuple[CapabilityDiagnostic, ...]]:
    if raw_value is None:
        return (), ()
    values: list[str]
    if isinstance(raw_value, str):
        values = [raw_value.strip()]
    elif isinstance(raw_value, list):
        values = [item.strip() for item in raw_value if isinstance(item, str) and item.strip()]
    else:
        return (), (
            CapabilityDiagnostic(
                severity="warning",
                code="skill_invalid_frontmatter_type",
                message="provides_tools must be a string or list of strings.",
                path=path,
            ),
        )
    diagnostics: list[CapabilityDiagnostic] = []
    filtered: list[str] = []
    for name in values:
        if name not in available_tool_names:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_unknown_tool_reference",
                    message=f"Skill references unknown tool: {name}",
                    path=path,
                )
            )
            continue
        if name not in filtered:
            filtered.append(name)
    return tuple(filtered), tuple(diagnostics)
