"""Model-visible projection of the immutable built-in capability catalog."""

from __future__ import annotations

from dataclasses import dataclass, replace

from pulsara_agent.capability.builtin_catalog import (
    builtin_tool_catalog,
    builtin_tool_descriptors,
)
from pulsara_agent.capability.provider import CapabilityDescriptorSnapshotOutput
from pulsara_agent.capability.types import CapabilityExecutionSurfaceSnapshotContext
from pulsara_agent.ports.tool_execution import (
    freeze_tool_json_object,
    thaw_tool_json_object,
)


@dataclass(frozen=True, slots=True)
class BuiltinToolCapabilityProvider:
    provider_id: str = "builtin-tools"

    def snapshot_descriptors(
        self,
        context: CapabilityExecutionSurfaceSnapshotContext,
    ) -> CapabilityDescriptorSnapshotOutput:
        available = context.available_tool_names
        return CapabilityDescriptorSnapshotOutput(
            descriptors=tuple(
                _owned_descriptor_copy(entry.descriptor)
                for entry in builtin_tool_catalog()
                if entry.name in available
            )
        )


def _owned_descriptor_copy(descriptor):
    schema = descriptor.input_schema
    return replace(
        descriptor,
        input_schema=(
            freeze_tool_json_object(thaw_tool_json_object(schema))
            if schema is not None
            else None
        ),
        metadata=freeze_tool_json_object(thaw_tool_json_object(descriptor.metadata)),
    )


__all__ = [
    "BuiltinToolCapabilityProvider",
    "builtin_tool_descriptors",
]
