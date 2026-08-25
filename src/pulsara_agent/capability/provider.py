"""Effective Skill projection output contract."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    ActiveSkillProjectionUnavailableReason,
    ResolvedSkillCatalogEntry,
    SkillCatalogUnavailableCause,
    SkillDiagnostic,
)


@dataclass(frozen=True, slots=True)
class SkillProjectionOutput:
    catalog_entries: tuple[ResolvedSkillCatalogEntry, ...] = ()
    active_injections: tuple[ActiveSkillInjection, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    catalog_prompt: str | None = None
    active_skill_prompt: str | None = None
    catalog_unavailable_causes: tuple[SkillCatalogUnavailableCause, ...] = ()
    active_unavailable_reason: ActiveSkillProjectionUnavailableReason | None = None


__all__ = ["SkillProjectionOutput"]
