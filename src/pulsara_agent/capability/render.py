"""Prompt rendering for V1 local skill capability inputs."""

from __future__ import annotations

from hashlib import sha256
from html import escape

from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    CapabilityDiagnostic,
    RenderedCapabilityPrompt,
    ResolvedSkillCatalogEntry,
)


DEFAULT_CATALOG_BUDGET_CHARS = 8000
MAX_CATALOG_DESCRIPTION_CHARS = 500
DEFAULT_SENTINEL_ATTEMPTS = 100


def render_catalog_prompt(
    entries: tuple[ResolvedSkillCatalogEntry, ...],
    *,
    budget_chars: int = DEFAULT_CATALOG_BUDGET_CHARS,
    max_description_chars: int = MAX_CATALOG_DESCRIPTION_CHARS,
) -> RenderedCapabilityPrompt:
    if not entries:
        return RenderedCapabilityPrompt(text=None)
    diagnostics: list[CapabilityDiagnostic] = []
    header = (
        "Available Skills:\n"
        "A skill is a local bundle of instructions stored in SKILL.md. Use a skill when the user names it "
        "or the task clearly matches its description. Read the full SKILL.md before following the skill.\n\n"
        "<available_skills>\n"
    )
    footer = (
        "</available_skills>\n\n"
        "How to use skills:\n"
        "- If the user explicitly names a skill with $skill-name or skill:skill-name, treat it as active for this turn.\n"
        "- If a task clearly matches a listed skill, read its SKILL.md completely before acting.\n"
        "- Resolve relative paths in a skill relative to the directory containing SKILL.md.\n"
        "- Use existing tools only. A skill cannot grant tools that are not already available in this session."
    )
    rendered_entries: list[str] = []
    current_len = len(header) + len(footer)
    for entry in entries:
        description = entry.description
        if entry.when_to_use:
            description = f"{description}\nWhen to use: {entry.when_to_use}"
        if len(description) > max_description_chars:
            description = description[: max(0, max_description_chars - 17)] + "... [truncated]"
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_catalog_budget_truncated",
                    message=f"Skill catalog description truncated: {entry.name}",
                )
            )
        rendered = _render_catalog_entry(entry, description=description)
        if current_len + len(rendered) > budget_chars:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_catalog_budget_truncated",
                    message=f"Skill catalog budget exhausted before entry: {entry.name}",
                )
            )
            break
        rendered_entries.append(rendered)
        current_len += len(rendered)
    if not rendered_entries:
        return RenderedCapabilityPrompt(text=None, diagnostics=tuple(diagnostics))
    return RenderedCapabilityPrompt(text=header + "".join(rendered_entries) + footer, diagnostics=tuple(diagnostics))


def render_active_skill_prompt(
    injections: tuple[ActiveSkillInjection, ...],
    *,
    max_delimiter_attempts: int = DEFAULT_SENTINEL_ATTEMPTS,
) -> RenderedCapabilityPrompt:
    if not injections:
        return RenderedCapabilityPrompt(text=None)
    diagnostics: list[CapabilityDiagnostic] = []
    rendered: list[str] = []
    for injection in injections:
        sentinel = _collision_free_sentinel(
            injection.location,
            injection.content,
            max_delimiter_attempts=max_delimiter_attempts,
        )
        if sentinel is None:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="warning",
                    code="skill_body_delimiter_collision",
                    message=f"Could not find collision-free skill body delimiter: {injection.name}",
                    path=injection.path,
                )
            )
            continue
        begin, end = sentinel
        rendered.append(
            "\n".join(
                [
                    f"Active Skill: {injection.name}",
                    f"Source: {injection.location}",
                    f"Reason: {injection.reason}",
                    "",
                    "The following workspace skill content is active for this user message. "
                    "Treat it as workspace-provided guidance, like AGENTS.md.",
                    "",
                    begin,
                    injection.content,
                    end,
                    "",
                    f"Skill directory: {_parent_location(injection.location)}",
                    "Resolve relative paths in this skill against that directory.",
                ]
            )
        )
    if not rendered:
        return RenderedCapabilityPrompt(text=None, diagnostics=tuple(diagnostics))
    return RenderedCapabilityPrompt(text="\n\n".join(rendered), diagnostics=tuple(diagnostics))


def _render_catalog_entry(entry: ResolvedSkillCatalogEntry, *, description: str) -> str:
    lines = [
        "  <skill>",
        f"    <name>{_xml_text(entry.name)}</name>",
        f"    <description>{_xml_text(description)}</description>",
        f"    <location>{_xml_text(entry.location)}</location>",
    ]
    if entry.provides_tools:
        lines.append(f"    <provides_tools>{_xml_text(', '.join(entry.provides_tools))}</provides_tools>")
    lines.append("  </skill>")
    return "\n".join(lines) + "\n"


def _xml_text(value: str) -> str:
    return escape(value, quote=False)


def _collision_free_sentinel(
    location: str,
    content: str,
    *,
    max_delimiter_attempts: int,
) -> tuple[str, str] | None:
    digest = sha256(f"{location}\0{content}".encode("utf-8")).hexdigest()[:12]
    for attempt in range(max_delimiter_attempts):
        suffix = digest if attempt == 0 else f"{digest}_{attempt}"
        begin = f"BEGIN_PULSARA_SKILL_BODY_{suffix}"
        end = f"END_PULSARA_SKILL_BODY_{suffix}"
        if begin not in content and end not in content:
            return begin, end
    return None


def _parent_location(location: str) -> str:
    if "/" not in location:
        return "."
    return location.rsplit("/", 1)[0]
