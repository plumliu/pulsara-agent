"""Deterministic provider-visible rendering for Agent Skills data."""

from __future__ import annotations

from pulsara_agent.capability.types import (
    ResolvedSkillCatalogEntry,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillResolutionUnavailableReason,
)
from pulsara_agent.primitives.context import canonical_json_bytes


MAX_SKILL_CATALOG_UTF8_BYTES = 384 * 1024


class SkillProjectionOverbound(ValueError):
    def __init__(self, reason: SkillResolutionUnavailableReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def render_catalog_prompt(
    entries: tuple[ResolvedSkillCatalogEntry, ...],
) -> str | None:
    """Render the one exact full routing representation.

    Usage rules live in BASE_SYSTEM.  This body contains only complete
    name/description/skill_path rows and is never summarized or prefix-truncated.
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
                    "target": {"kind": "SKILL", "skill_path": item.skill_path},
                }
                for item in ordered
            )
        }
    ).decode("utf-8")
    if len(text.encode("utf-8")) > MAX_SKILL_CATALOG_UTF8_BYTES:
        raise SkillProjectionOverbound(
            SkillResolutionUnavailableReason.CATALOG_PROJECTION_OVERBOUND
        )
    return text


def catalog_projection_overbound_diagnostic() -> SkillDiagnostic:
    return SkillDiagnostic(
        severity=SkillDiagnosticSeverity.WARNING,
        code=SkillDiagnosticCode.CATALOG_PROJECTION_BOUND_EXCEEDED,
        message="Skill catalog projection exceeds its physical bound",
    )




__all__ = [
    "MAX_SKILL_CATALOG_UTF8_BYTES",
    "SkillProjectionOverbound",
    "catalog_projection_overbound_diagnostic",
    "render_catalog_prompt",
]
