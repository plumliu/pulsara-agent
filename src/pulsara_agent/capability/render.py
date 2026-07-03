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


DEFAULT_CATALOG_BUDGET_CHARS = 18_000
COMPACT_CATALOG_DESCRIPTION_CHARS = 160
DETAIL_CATALOG_DESCRIPTION_CHARS = 300
MAX_CATALOG_DESCRIPTION_CHARS = 500
DEFAULT_SENTINEL_ATTEMPTS = 100


def render_catalog_prompt(
    entries: tuple[ResolvedSkillCatalogEntry, ...],
    *,
    budget_chars: int = DEFAULT_CATALOG_BUDGET_CHARS,
    max_description_chars: int = MAX_CATALOG_DESCRIPTION_CHARS,
    compact_description_chars: int = COMPACT_CATALOG_DESCRIPTION_CHARS,
    detail_description_chars: int = DETAIL_CATALOG_DESCRIPTION_CHARS,
) -> RenderedCapabilityPrompt:
    if not entries:
        return RenderedCapabilityPrompt(text=None)
    diagnostics: list[CapabilityDiagnostic] = []
    header = (
        "Available Skills:\n"
        "A skill is a local bundle of instructions stored in SKILL.md. This catalog is a routing index, "
        "not the full skill body. If the user names a skill or the task clearly matches a listed skill, "
        "use the existing read tool to read the listed SKILL.md completely before following the skill.\n\n"
    )
    footer = (
        "How to use skills:\n"
        "- If the user explicitly names a skill with $skill-name or skill:skill-name, treat it as active for this turn.\n"
        "- If a task clearly matches a listed skill, read its SKILL.md completely before acting; do not infer the full workflow from this catalog alone.\n"
        "- Resolve relative paths in a skill relative to the directory containing SKILL.md.\n"
        "- Use existing tools only. A skill cannot grant tools that are not already available in this session."
    )
    full_detail_limit = min(max_description_chars, detail_description_chars)
    compact_index, compact_truncated = _render_available_skill_index(
        entries,
        include_description=True,
        description_chars=compact_description_chars,
    )
    detail_entries, detail_truncated = _render_skill_detail_entries(
        entries,
        description_chars=full_detail_limit,
    )
    detail_header = "<skill_details>\n"
    detail_footer = "</skill_details>\n\n"
    rendered_details: list[str] = []
    current_len = len(header) + len(compact_index) + len(footer)
    if current_len <= budget_chars:
        for rendered in detail_entries:
            detail_len = len(detail_header) + len(detail_footer) + sum(len(item) for item in rendered_details)
            if current_len + detail_len + len(rendered) > budget_chars:
                break
            rendered_details.append(rendered)
        omitted_details = len(entries) - len(rendered_details)
        if omitted_details:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="info",
                    code="skill_catalog_details_omitted",
                    message=f"Skill detail entries omitted due to catalog budget: {omitted_details}",
                )
            )
        detail_block = ""
        if rendered_details:
            detail_block = detail_header + "".join(rendered_details) + detail_footer
        _append_description_truncation_diagnostics(
            diagnostics,
            compact_truncated=compact_truncated,
            detail_truncated=detail_truncated,
        )
        diagnostics.append(
            CapabilityDiagnostic(
                severity="info",
                code="skill_catalog_mode",
                message=(
                    f"mode=hybrid budget_chars={budget_chars} indexed={len(entries)} "
                    f"detailed={len(rendered_details)} omitted_details={omitted_details}"
                ),
            )
        )
        return RenderedCapabilityPrompt(
            text=header + compact_index + detail_block + footer,
            diagnostics=tuple(diagnostics),
        )

    compact_no_description, _ = _render_available_skill_index(
        entries,
        include_description=False,
        description_chars=0,
    )
    if len(header) + len(compact_no_description) + len(footer) <= budget_chars:
        diagnostics.append(
            CapabilityDiagnostic(
                severity="warning",
                code="skill_catalog_details_omitted",
                message=f"Skill catalog fell back to name/location-only index due to budget: {len(entries)}",
            )
        )
        diagnostics.append(
            CapabilityDiagnostic(
                severity="info",
                code="skill_catalog_mode",
                message=f"mode=compact budget_chars={budget_chars} indexed={len(entries)} detailed=0",
            )
        )
        return RenderedCapabilityPrompt(text=header + compact_no_description + footer, diagnostics=tuple(diagnostics))

    rendered_entries: list[str] = []
    current_len = len(header) + len("<available_skill_index>\n</available_skill_index>\n\n") + len(footer)
    for entry in entries:
        rendered = _render_index_entry(entry, description=None)
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
    diagnostics.append(
        CapabilityDiagnostic(
            severity="info",
            code="skill_catalog_mode",
            message=f"mode=truncated budget_chars={budget_chars} indexed={len(rendered_entries)} total={len(entries)} detailed=0",
        )
    )
    index = "<available_skill_index>\n" + "".join(rendered_entries) + "</available_skill_index>\n\n"
    return RenderedCapabilityPrompt(text=header + index + footer, diagnostics=tuple(diagnostics))


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


def _render_available_skill_index(
    entries: tuple[ResolvedSkillCatalogEntry, ...],
    *,
    include_description: bool,
    description_chars: int,
) -> tuple[str, int]:
    rendered = ["<available_skill_index>\n"]
    truncated = 0
    for entry in entries:
        description = _catalog_description(entry)
        if include_description:
            description, was_truncated = _truncate_description(
                description,
                limit=description_chars,
            )
            if was_truncated:
                truncated += 1
        else:
            description = None
        rendered.append(_render_index_entry(entry, description=description))
    rendered.append("</available_skill_index>\n\n")
    return "".join(rendered), truncated


def _render_skill_detail_entries(
    entries: tuple[ResolvedSkillCatalogEntry, ...],
    *,
    description_chars: int,
) -> tuple[list[str], int]:
    rendered: list[str] = []
    truncated = 0
    for entry in entries:
        description, was_truncated = _truncate_description(
            _catalog_description(entry),
            limit=description_chars,
        )
        if was_truncated:
            truncated += 1
        rendered.append(_render_detail_entry(entry, description=description))
    return rendered, truncated


def _catalog_description(entry: ResolvedSkillCatalogEntry) -> str:
    if entry.when_to_use:
        return f"{entry.description}\nWhen to use: {entry.when_to_use}"
    return entry.description


def _truncate_description(
    description: str,
    *,
    limit: int,
) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(description)
    if len(description) <= limit:
        return description, False
    return description[: max(0, limit - 17)] + "... [truncated]", True


def _append_description_truncation_diagnostics(
    diagnostics: list[CapabilityDiagnostic],
    *,
    compact_truncated: int,
    detail_truncated: int,
) -> None:
    if compact_truncated:
        diagnostics.append(
            CapabilityDiagnostic(
                severity="info",
                code="skill_catalog_compact_descriptions_truncated",
                message=f"Skill compact index descriptions truncated: {compact_truncated}",
            )
        )
    if detail_truncated:
        diagnostics.append(
            CapabilityDiagnostic(
                severity="info",
                code="skill_catalog_detail_descriptions_truncated",
                message=f"Skill detail descriptions truncated: {detail_truncated}",
            )
        )


def _render_index_entry(entry: ResolvedSkillCatalogEntry, *, description: str | None) -> str:
    lines = [
        "  <skill>",
        f"    <name>{_xml_text(entry.name)}</name>",
        f"    <location>{_xml_text(entry.location)}</location>",
    ]
    if description:
        lines.append(f"    <description>{_xml_text(description)}</description>")
    lines.append("  </skill>")
    return "\n".join(lines) + "\n"


def _render_detail_entry(entry: ResolvedSkillCatalogEntry, *, description: str) -> str:
    lines = [
        "  <skill_detail>",
        f"    <name>{_xml_text(entry.name)}</name>",
        f"    <description>{_xml_text(description)}</description>",
    ]
    if entry.provides_tools:
        lines.append(f"    <provides_tools>{_xml_text(', '.join(entry.provides_tools))}</provides_tools>")
    lines.append("  </skill_detail>")
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
