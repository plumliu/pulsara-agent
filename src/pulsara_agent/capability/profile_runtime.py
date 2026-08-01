"""Build a capability runtime constrained by one child execution profile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pulsara_agent.capability.provider import (
    CapabilityDescriptorSnapshotOutput,
    CapabilityProjectionOutput,
)
from pulsara_agent.capability.render import (
    render_active_skill_prompt,
    render_catalog_prompt,
)
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.primitives.capability import (
    CapabilityExecutionSurfaceIdentityFact,
)


@dataclass(frozen=True, slots=True)
class _ProfileFilteredExecutionSurfaceProvider:
    provider: Any
    allowed_tool_names: frozenset[str]
    allowed_descriptor_ids: frozenset[str]

    @property
    def provider_id(self) -> str:
        return str(getattr(self.provider, "provider_id", "profile-filtered"))

    def snapshot_descriptors(
        self,
        context: CapabilityExecutionSurfaceSnapshotContext,
    ) -> CapabilityDescriptorSnapshotOutput:
        output = self.provider.snapshot_descriptors(context)
        return CapabilityDescriptorSnapshotOutput(
            descriptors=tuple(
                descriptor
                for descriptor in output.descriptors
                if descriptor.name in self.allowed_tool_names
                or descriptor.id in self.allowed_descriptor_ids
            ),
            diagnostics=output.diagnostics,
        )


@dataclass(frozen=True, slots=True)
class _ProfileFilteredProjectionProvider:
    provider: Any
    allowed_skill_names: frozenset[str]

    @property
    def provider_id(self) -> str:
        return str(getattr(self.provider, "provider_id", "profile-filtered"))

    def resolve_projection(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        execution_surface: CapabilityExecutionSurfaceIdentityFact,
    ) -> CapabilityProjectionOutput:
        output = self.provider.resolve_projection(
            context,
            execution_surface=execution_surface,
        )
        catalog_entries = tuple(
            entry
            for entry in output.catalog_entries
            if entry.name in self.allowed_skill_names
        )
        active_injections = tuple(
            injection
            for injection in output.active_injections
            if injection.name in self.allowed_skill_names
        )
        catalog_rendered = render_catalog_prompt(catalog_entries)
        active_rendered = render_active_skill_prompt(active_injections)
        return CapabilityProjectionOutput(
            catalog_entries=catalog_entries,
            active_injections=active_injections,
            diagnostics=(
                *output.diagnostics,
                *catalog_rendered.diagnostics,
                *active_rendered.diagnostics,
            ),
            catalog_prompt=catalog_rendered.text,
            active_skill_prompt=active_rendered.text,
            catalog_rendered=catalog_rendered,
            active_skill_rendered=active_rendered,
        )


def profile_filtered_capability_runtime(
    parent: CapabilityRuntime,
    profile: Any,
) -> CapabilityRuntime:
    """Return only providers and entries admitted by ``profile``."""

    allowed_tool_names = frozenset(getattr(profile, "allowed_tool_names", ()) or ())
    allowed_descriptor_ids = frozenset(
        getattr(profile, "allowed_descriptor_ids", ()) or ()
    )
    allowed_skill_names = frozenset(getattr(profile, "allowed_skill_names", ()) or ())
    if (
        not allowed_tool_names
        and not allowed_descriptor_ids
        and not allowed_skill_names
    ):
        return CapabilityRuntime(providers=())

    filtered: list[Any] = []
    for provider in parent.providers:
        if hasattr(provider, "snapshot_descriptors"):
            filtered.append(
                _ProfileFilteredExecutionSurfaceProvider(
                    provider=provider,
                    allowed_tool_names=allowed_tool_names,
                    allowed_descriptor_ids=allowed_descriptor_ids,
                )
            )
        if hasattr(provider, "resolve_projection"):
            filtered.append(
                _ProfileFilteredProjectionProvider(
                    provider=provider,
                    allowed_skill_names=allowed_skill_names,
                )
            )
    return CapabilityRuntime(providers=tuple(filtered))


__all__ = ["profile_filtered_capability_runtime"]
