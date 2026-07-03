"""Descriptor registry for capability snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.capability.descriptor import CapabilityDescriptor
from pulsara_agent.capability.types import CapabilityDiagnostic


@dataclass(frozen=True, slots=True)
class CapabilityRegistrySnapshot:
    generation: int
    descriptors: tuple[CapabilityDescriptor, ...]
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()


@dataclass(slots=True)
class CapabilityRegistry:
    _descriptors_by_id: dict[str, CapabilityDescriptor] = field(default_factory=dict)
    _ids_by_name: dict[str, str] = field(default_factory=dict)
    _generation: int = 0
    _diagnostics: list[CapabilityDiagnostic] = field(default_factory=list)

    def register(self, descriptor: CapabilityDescriptor) -> None:
        existing = self._descriptors_by_id.get(descriptor.id)
        if existing is not None:
            if existing == descriptor:
                return
            raise ValueError(f"Capability descriptor id already registered with different data: {descriptor.id}")
        existing_id_for_name = self._ids_by_name.get(descriptor.name)
        if existing_id_for_name is not None and existing_id_for_name != descriptor.id:
            raise ValueError(
                f"Capability descriptor name {descriptor.name!r} already registered by {existing_id_for_name!r}"
            )
        self._descriptors_by_id[descriptor.id] = descriptor
        self._ids_by_name[descriptor.name] = descriptor.id
        self._generation += 1

    def get_by_name(self, name: str) -> CapabilityDescriptor:
        try:
            return self._descriptors_by_id[self._ids_by_name[name]]
        except KeyError as exc:
            raise KeyError(f"Unknown capability name: {name}") from exc

    def get_by_id(self, id: str) -> CapabilityDescriptor:
        try:
            return self._descriptors_by_id[id]
        except KeyError as exc:
            raise KeyError(f"Unknown capability id: {id}") from exc

    def snapshot(self) -> CapabilityRegistrySnapshot:
        descriptors = tuple(self._descriptors_by_id[id] for id in sorted(self._descriptors_by_id))
        return CapabilityRegistrySnapshot(
            generation=self._generation,
            descriptors=descriptors,
            diagnostics=tuple(self._diagnostics),
        )
