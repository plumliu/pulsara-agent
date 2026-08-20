"""Local Skill projection output and source-owner protocol.

Tool execution discovery no longer passes through a generic provider protocol;
Builtin and MCP retain their physical owners.  This narrow module remains only
for the source-specific Skill renderer used by the Round 9 sibling view.
"""

from __future__ import annotations

from dataclasses import dataclass
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    SkillDiagnostic,
    RenderedSkillPrompt,
    ResolvedSkillCatalogEntry,
)


@dataclass(frozen=True, slots=True)
class SkillProjectionOutput:
    catalog_entries: tuple[ResolvedSkillCatalogEntry, ...] = ()
    active_injections: tuple[ActiveSkillInjection, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    catalog_prompt: str | None = None
    active_skill_prompt: str | None = None
    catalog_rendered: RenderedSkillPrompt | None = None
    active_skill_rendered: RenderedSkillPrompt | None = None


__all__ = ["SkillProjectionOutput"]
