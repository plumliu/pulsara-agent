"""Per-call effective classification for capability permission decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pulsara_agent.capability.descriptor import CapabilityDescriptor
from pulsara_agent.capability.builtin_catalog import (
    builtin_action_permission_override,
    builtin_tool_catalog_entry,
)


@dataclass(frozen=True, slots=True)
class CapabilityCallClassification:
    descriptor_id: str
    tool_name: str
    effective_read_only: bool
    effective_concurrency_safe: bool
    effective_permission_category: str
    effective_is_destructive: bool
    effective_is_open_world: bool
    builtin_tool_family: str | None = None
    builtin_execution_binding_kind: str | None = None
    builtin_catalog_entry_fingerprint: str | None = None
    approval_reason: str | None = None
    deny_reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "descriptor_id": self.descriptor_id,
            "tool_name": self.tool_name,
            "effective_read_only": self.effective_read_only,
            "effective_concurrency_safe": self.effective_concurrency_safe,
            "effective_permission_category": self.effective_permission_category,
            "effective_is_destructive": self.effective_is_destructive,
            "effective_is_open_world": self.effective_is_open_world,
            "builtin_tool_family": self.builtin_tool_family,
            "builtin_execution_binding_kind": self.builtin_execution_binding_kind,
            "builtin_catalog_entry_fingerprint": (
                self.builtin_catalog_entry_fingerprint
            ),
            "approval_reason": self.approval_reason,
            "deny_reason": self.deny_reason,
            "metadata": self.metadata,
        }


class CapabilityCallClassifier(Protocol):
    def classify(
        self,
        call: Any,
        descriptor: CapabilityDescriptor,
    ) -> CapabilityCallClassification: ...


class DefaultCapabilityCallClassifier:
    def classify_builtin(self, call: Any) -> CapabilityCallClassification:
        entry = builtin_tool_catalog_entry(call.name)
        return self.classify(call, entry.descriptor)

    def classify(
        self,
        call: Any,
        descriptor: CapabilityDescriptor,
    ) -> CapabilityCallClassification:
        try:
            entry = builtin_tool_catalog_entry(call.name)
        except KeyError:
            entry = None
        if (
            entry is not None
            and entry.descriptor.fingerprint() != descriptor.fingerprint()
        ):
            raise ValueError("builtin call classifier descriptor/catalog mismatch")
        override = (
            builtin_action_permission_override(call.name, call.arguments)
            if entry is not None
            else None
        )
        if override is not None:
            return CapabilityCallClassification(
                descriptor_id=descriptor.id,
                tool_name=call.name,
                effective_read_only=override.allowed_in_read_only,
                effective_concurrency_safe=descriptor.is_concurrency_safe,
                effective_permission_category=override.permission_category,
                effective_is_destructive=False,
                effective_is_open_world=descriptor.is_open_world,
                builtin_tool_family=entry.tool_family,
                builtin_execution_binding_kind=entry.execution_binding_kind.value,
                builtin_catalog_entry_fingerprint=entry.entry_fingerprint,
                metadata={
                    override.discriminator_field: override.discriminator_value,
                    "builtin_catalog_entry_fingerprint": entry.entry_fingerprint,
                },
            )
        return CapabilityCallClassification(
            descriptor_id=descriptor.id,
            tool_name=call.name,
            effective_read_only=descriptor.is_read_only,
            effective_concurrency_safe=descriptor.is_concurrency_safe,
            effective_permission_category=descriptor.permission_category,
            effective_is_destructive=descriptor.is_destructive,
            effective_is_open_world=descriptor.is_open_world,
            builtin_tool_family=(entry.tool_family if entry is not None else None),
            builtin_execution_binding_kind=(
                entry.execution_binding_kind.value if entry is not None else None
            ),
            builtin_catalog_entry_fingerprint=(
                entry.entry_fingerprint if entry is not None else None
            ),
        )
