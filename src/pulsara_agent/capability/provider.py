"""Capability provider protocol and common output shape."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pulsara_agent.capability.descriptor import CapabilityDescriptor
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    CapabilityDiagnostic,
    CapabilityResolveContext,
    ResolvedSkillCatalogEntry,
)


@dataclass(frozen=True, slots=True)
class CapabilityProviderOutput:
    descriptors: tuple[CapabilityDescriptor, ...] = ()
    catalog_entries: tuple[ResolvedSkillCatalogEntry, ...] = ()
    active_injections: tuple[ActiveSkillInjection, ...] = ()
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()
    catalog_prompt: str | None = None
    active_skill_prompt: str | None = None


class CapabilityProvider(Protocol):
    provider_id: str

    def resolve(self, context: CapabilityResolveContext, *, bound_tool_names: frozenset[str]) -> CapabilityProviderOutput:
        """Resolve descriptors and prompt capability projections for one turn."""
