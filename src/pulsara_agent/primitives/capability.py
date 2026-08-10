"""Process-local capability execution-surface identity."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives.model_call import sha256_fingerprint


class CapabilityDescriptorBindingIdentityFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_name: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    descriptor_id: str = Field(min_length=1)
    descriptor_fingerprint: str = Field(min_length=1)
    descriptor_artifact_id: str = Field(min_length=1)
    binding_fingerprint: str | None
    binding_contract_id: str | None
    binding_contract_version: str | None

    @model_validator(mode="after")
    def _binding_fields(self) -> "CapabilityDescriptorBindingIdentityFact":
        values = (
            self.binding_fingerprint,
            self.binding_contract_id,
            self.binding_contract_version,
        )
        if any(value is None for value in values) and any(
            value is not None for value in values
        ):
            raise ValueError("capability binding fields must be all-or-none")
        return self


def _descriptor_set_fingerprint(
    entries: tuple[CapabilityDescriptorBindingIdentityFact, ...],
) -> str:
    return sha256_fingerprint(
        "capability-descriptor-set:v1",
        tuple(
            (
                entry.capability_name,
                entry.provider_id,
                entry.descriptor_id,
                entry.descriptor_fingerprint,
            )
            for entry in entries
        ),
    )


def _binding_set_fingerprint(
    entries: tuple[CapabilityDescriptorBindingIdentityFact, ...],
) -> str:
    return sha256_fingerprint(
        "capability-binding-set:v1",
        tuple(
            (
                entry.capability_name,
                entry.binding_fingerprint,
                entry.binding_contract_id,
                entry.binding_contract_version,
            )
            for entry in entries
        ),
    )


class CapabilityExecutionSurfaceIdentityFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_contract_version: str = Field(min_length=1)
    entries: tuple[CapabilityDescriptorBindingIdentityFact, ...]
    descriptor_set_fingerprint: str
    execution_binding_set_fingerprint: str
    execution_surface_fingerprint: str

    @model_validator(mode="after")
    def _surface(self) -> "CapabilityExecutionSurfaceIdentityFact":
        names = tuple(entry.capability_name for entry in self.entries)
        descriptors = tuple(entry.descriptor_id for entry in self.entries)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("capability entries must be name-sorted and unique")
        if len(descriptors) != len(set(descriptors)):
            raise ValueError("capability descriptor IDs must be unique")
        descriptor_fingerprint = _descriptor_set_fingerprint(self.entries)
        binding_fingerprint = _binding_set_fingerprint(self.entries)
        surface_fingerprint = sha256_fingerprint(
            "capability-execution-surface:v1",
            (
                self.surface_contract_version,
                descriptor_fingerprint,
                binding_fingerprint,
            ),
        )
        if self.descriptor_set_fingerprint != descriptor_fingerprint:
            raise ValueError("capability descriptor set fingerprint mismatch")
        if self.execution_binding_set_fingerprint != binding_fingerprint:
            raise ValueError("capability binding set fingerprint mismatch")
        if self.execution_surface_fingerprint != surface_fingerprint:
            raise ValueError("capability surface fingerprint mismatch")
        return self


def build_capability_execution_surface_identity(
    *,
    surface_contract_version: str,
    entries: tuple[CapabilityDescriptorBindingIdentityFact, ...],
) -> CapabilityExecutionSurfaceIdentityFact:
    descriptor_fingerprint = _descriptor_set_fingerprint(entries)
    binding_fingerprint = _binding_set_fingerprint(entries)
    return CapabilityExecutionSurfaceIdentityFact(
        surface_contract_version=surface_contract_version,
        entries=entries,
        descriptor_set_fingerprint=descriptor_fingerprint,
        execution_binding_set_fingerprint=binding_fingerprint,
        execution_surface_fingerprint=sha256_fingerprint(
            "capability-execution-surface:v1",
            (surface_contract_version, descriptor_fingerprint, binding_fingerprint),
        ),
    )


__all__ = [
    "CapabilityDescriptorBindingIdentityFact",
    "CapabilityExecutionSurfaceIdentityFact",
    "build_capability_execution_surface_identity",
]
