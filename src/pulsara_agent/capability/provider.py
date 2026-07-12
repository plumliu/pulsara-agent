"""Split capability provider protocols.

Execution descriptors are frozen before ``RunStart``. Model-visible catalog and
active-skill projections are resolved only after that execution surface exists.
There is intentionally no mixed ``resolve()`` protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pulsara_agent.capability.descriptor import CapabilityDescriptor
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    CapabilityDiagnostic,
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
    RenderedCapabilityPrompt,
    ResolvedSkillCatalogEntry,
)
from pulsara_agent.primitives.capability import CapabilityExecutionSurfaceIdentityFact


@dataclass(frozen=True, slots=True)
class CapabilityDescriptorSnapshotOutput:
    descriptors: tuple[CapabilityDescriptor, ...] = ()
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityProjectionOutput:
    catalog_entries: tuple[ResolvedSkillCatalogEntry, ...] = ()
    active_injections: tuple[ActiveSkillInjection, ...] = ()
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()
    catalog_prompt: str | None = None
    active_skill_prompt: str | None = None
    catalog_rendered: RenderedCapabilityPrompt | None = None
    active_skill_rendered: RenderedCapabilityPrompt | None = None


class CapabilityExecutionSurfaceProvider(Protocol):
    provider_id: str

    def snapshot_descriptors(
        self,
        context: CapabilityExecutionSurfaceSnapshotContext,
    ) -> CapabilityDescriptorSnapshotOutput: ...


class CapabilityProjectionProvider(Protocol):
    provider_id: str

    def resolve_projection(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        execution_surface: CapabilityExecutionSurfaceIdentityFact,
    ) -> CapabilityProjectionOutput: ...


type CapabilityProviderComponent = (
    CapabilityExecutionSurfaceProvider | CapabilityProjectionProvider
)
