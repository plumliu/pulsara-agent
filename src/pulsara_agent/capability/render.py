"""Deterministic provider-visible rendering for Agent Skills data."""

from __future__ import annotations

from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    ResolvedSkillCatalogEntry,
    SkillCatalogUnavailableReason,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
)
from pulsara_agent.primitives.context import canonical_json_bytes


MAX_SKILL_CATALOG_UTF8_BYTES = 384 * 1024
MAX_ACTIVE_SKILLS = 16
MAX_ACTIVE_SKILL_BODY_UTF8_BYTES = 512 * 1024


class SkillProjectionOverbound(ValueError):
    def __init__(self, reason: SkillCatalogUnavailableReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def render_catalog_prompt(
    entries: tuple[ResolvedSkillCatalogEntry, ...],
) -> str | None:
    """Render the one exact full routing representation.

    Usage rules live in BASE_SYSTEM.  This body contains only complete
    name/description/location rows and is never summarized or prefix-truncated.
    """

    if not entries:
        return None
    ordered = tuple(sorted(entries, key=lambda item: item.name))
    if len({item.name for item in ordered}) != len(ordered):
        raise ValueError("Skill catalog names are duplicated")
    text = canonical_json_bytes(
        {
            "skills": tuple(
                {
                    "name": item.name,
                    "description": item.description,
                    "location": item.location,
                }
                for item in ordered
            )
        }
    ).decode("utf-8")
    if len(text.encode("utf-8")) > MAX_SKILL_CATALOG_UTF8_BYTES:
        raise SkillProjectionOverbound(SkillCatalogUnavailableReason.CATALOG_OVERBOUND)
    return text


def render_active_skill_prompt(
    injections: tuple[ActiveSkillInjection, ...],
) -> str | None:
    """Render exact parsed Markdown bodies in one closed JSON carrier."""

    if not injections:
        return None
    if len(injections) > MAX_ACTIVE_SKILLS:
        raise SkillProjectionOverbound(
            SkillCatalogUnavailableReason.ACTIVE_SELECTION_UNAVAILABLE
        )
    ordered = tuple(sorted(injections, key=lambda item: item.name))
    if len({item.name for item in ordered}) != len(ordered):
        raise ValueError("active Skill names are duplicated")
    body_bytes = sum(len(item.body.encode("utf-8")) for item in ordered)
    if body_bytes > MAX_ACTIVE_SKILL_BODY_UTF8_BYTES:
        raise SkillProjectionOverbound(
            SkillCatalogUnavailableReason.ACTIVE_SELECTION_UNAVAILABLE
        )
    text = canonical_json_bytes(
        {
            "skills": tuple(
                {
                    "name": item.name,
                    "location": item.location,
                    "reason": item.reason.value,
                    "body": item.body,
                }
                for item in ordered
            )
        }
    ).decode("utf-8")
    if len(text.encode("utf-8")) > MAX_ACTIVE_SKILL_BODY_UTF8_BYTES:
        raise SkillProjectionOverbound(
            SkillCatalogUnavailableReason.ACTIVE_SELECTION_UNAVAILABLE
        )
    return text


def catalog_projection_overbound_diagnostic() -> SkillDiagnostic:
    return SkillDiagnostic(
        severity=SkillDiagnosticSeverity.WARNING,
        code=SkillDiagnosticCode.CATALOG_PROJECTION_BOUND_EXCEEDED,
        message="Skill catalog projection exceeds its physical bound",
    )


def active_projection_overbound_diagnostic() -> SkillDiagnostic:
    return SkillDiagnostic(
        severity=SkillDiagnosticSeverity.WARNING,
        code=SkillDiagnosticCode.PROJECTION_OVERBOUND,
        message="Active Skill projection exceeds its physical bound",
    )


__all__ = [
    "MAX_ACTIVE_SKILL_BODY_UTF8_BYTES",
    "MAX_ACTIVE_SKILLS",
    "MAX_SKILL_CATALOG_UTF8_BYTES",
    "SkillProjectionOverbound",
    "active_projection_overbound_diagnostic",
    "catalog_projection_overbound_diagnostic",
    "render_active_skill_prompt",
    "render_catalog_prompt",
]
