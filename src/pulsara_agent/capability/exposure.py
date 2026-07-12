"""Per-turn capability exposure planning."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityAvailability,
    CapabilityDescriptor,
)
from pulsara_agent.capability.provider import CapabilityProjectionOutput
from pulsara_agent.capability.registry import CapabilityRegistrySnapshot
from pulsara_agent.capability.types import ActiveSkillInjection, CapabilityDiagnostic, ResolvedSkillCatalogEntry
from pulsara_agent.llm.input import ToolSpec


@dataclass(frozen=True, slots=True)
class CapabilityExposurePlan:
    registry_generation: int
    direct_tool_specs: tuple[ToolSpec, ...]
    direct_names: frozenset[str]
    deferred_names: frozenset[str]
    hidden_names: frozenset[str]
    callable_names: frozenset[str]
    descriptors_by_name: Mapping[str, CapabilityDescriptor]
    catalog_entries: tuple[ResolvedSkillCatalogEntry, ...]
    active_injections: tuple[ActiveSkillInjection, ...]
    catalog_prompt: str | None
    active_skill_prompt: str | None
    diagnostics: tuple[CapabilityDiagnostic, ...]

    def to_event_value(self) -> dict[str, object]:
        return {
            "registry_generation": self.registry_generation,
            "direct_descriptor_ids": [
                self.descriptors_by_name[name].id for name in sorted(self.direct_names) if name in self.descriptors_by_name
            ],
            "deferred_descriptor_ids": [
                self.descriptors_by_name[name].id
                for name in sorted(self.deferred_names)
                if name in self.descriptors_by_name
            ],
            "hidden_descriptor_ids": [
                self.descriptors_by_name[name].id for name in sorted(self.hidden_names) if name in self.descriptors_by_name
            ],
            "direct_names": sorted(self.direct_names),
            "deferred_names": sorted(self.deferred_names),
            "hidden_names": sorted(self.hidden_names),
            "callable_names": sorted(self.callable_names),
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def build_exposure_plan(
    snapshot: CapabilityRegistrySnapshot,
    *,
    provider_output: CapabilityProjectionOutput,
    bound_tool_names: frozenset[str] | None = None,
) -> CapabilityExposurePlan:
    descriptors = tuple(snapshot.descriptors)
    descriptors_by_name = {descriptor.name: descriptor for descriptor in descriptors}
    direct_names: set[str] = set()
    deferred_names: set[str] = set()
    hidden_names: set[str] = set()
    direct_specs: list[ToolSpec] = []

    diagnostics: list[CapabilityDiagnostic] = [*snapshot.diagnostics, *provider_output.diagnostics]

    for descriptor in sorted(descriptors, key=lambda item: item.name):
        missing_execution_binding = (
            bound_tool_names is not None
            and descriptor.is_model_callable
            and descriptor.name not in bound_tool_names
        )
        if missing_execution_binding:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="error",
                    code="capability_missing_execution_binding",
                    message=(
                        f"Capability descriptor {descriptor.id!r} is model-callable "
                        f"but has no ToolRegistry execution binding for {descriptor.name!r}."
                    ),
                )
            )
        if not descriptor.is_model_callable:
            hidden_names.add(descriptor.name)
            continue
        if descriptor.availability is CapabilityAvailability.UNAVAILABLE:
            hidden_names.add(descriptor.name)
            continue
        if descriptor.advertise_policy is CapabilityAdvertisePolicy.HIDDEN:
            hidden_names.add(descriptor.name)
            continue
        if descriptor.advertise_policy is CapabilityAdvertisePolicy.DEFERRED:
            deferred_names.add(descriptor.name)
            continue
        if missing_execution_binding:
            hidden_names.add(descriptor.name)
            continue
        direct_names.add(descriptor.name)
        direct_specs.append(
            ToolSpec(
                name=descriptor.name,
                description=descriptor.description,
                parameters=dict(descriptor.input_schema or {}),
            )
        )

    return CapabilityExposurePlan(
        registry_generation=snapshot.generation,
        direct_tool_specs=tuple(direct_specs),
        direct_names=frozenset(direct_names),
        deferred_names=frozenset(deferred_names),
        hidden_names=frozenset(hidden_names),
        callable_names=frozenset(direct_names),
        descriptors_by_name=MappingProxyType(descriptors_by_name),
        catalog_entries=provider_output.catalog_entries,
        active_injections=provider_output.active_injections,
        catalog_prompt=provider_output.catalog_prompt,
        active_skill_prompt=provider_output.active_skill_prompt,
        diagnostics=tuple(diagnostics),
    )
