"""Pure capability projection from one frozen MCP installation view."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pulsara_agent.capability.descriptor import CapabilityDescriptor
from pulsara_agent.capability.provider import (
    CapabilityDescriptorSnapshotOutput,
    CapabilityProjectionOutput,
)
from pulsara_agent.capability.types import (
    CapabilityDiagnostic,
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.primitives.capability import CapabilityExecutionSurfaceIdentityFact


class McpCapabilityInstallationView(Protocol):
    installation_id: str
    snapshots: tuple[object, ...]
    descriptors: tuple[CapabilityDescriptor, ...]
    diagnostics: tuple[CapabilityDiagnostic, ...]
    ordered_binding_installations: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class McpCapabilityProvider:
    installation: McpCapabilityInstallationView
    provider_id: str = "mcp"

    def snapshot_descriptors(
        self,
        context: CapabilityExecutionSurfaceSnapshotContext,
    ) -> CapabilityDescriptorSnapshotOutput:
        if context.mcp_installation_id != self.installation.installation_id:
            raise ValueError("MCP descriptor snapshot installation mismatch")
        available = context.available_tool_names
        return CapabilityDescriptorSnapshotOutput(
            descriptors=tuple(
                descriptor
                for descriptor in self.installation.descriptors
                if descriptor.name in available
            ),
            diagnostics=tuple(self.installation.diagnostics),
        )

    def resolve_projection(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        execution_surface: CapabilityExecutionSurfaceIdentityFact,
    ) -> CapabilityProjectionOutput:
        del context
        if execution_surface.mcp_installation_id != self.installation.installation_id:
            raise ValueError("MCP projection installation mismatch")
        return CapabilityProjectionOutput(
            diagnostics=tuple(self.installation.diagnostics),
            catalog_prompt=_render_mcp_lifecycle_prompt(self.installation),
        )


def _render_mcp_lifecycle_prompt(
    installation: McpCapabilityInstallationView,
) -> str | None:
    if not installation.snapshots:
        return None
    installed_tool_counts: dict[str, int] = {}
    for item in installation.ordered_binding_installations:
        contract = getattr(item, "binding_contract", None)
        identity = getattr(contract, "binding_identity", None)
        server_id = str(getattr(identity, "server_id", ""))
        installed_tool_counts[server_id] = installed_tool_counts.get(server_id, 0) + 1
    server_lines = [
        (
            f"- server={snapshot.server_id}; status={snapshot.status.value}; "
            f"installed_tool_count={installed_tool_counts.get(snapshot.server_id, 0)}"
        )
        for snapshot in sorted(
            installation.snapshots,
            key=lambda item: item.server_id,
        )
    ]
    return "\n".join(
        [
            "<mcp_lifecycle_contract>",
            "MCP capability state is frozen for this run.",
            "Current run server states:",
            *server_lines,
            "Mandatory behavior:",
            "- Only MCP tools present in this run's actual tool schema are callable.",
            "- status=starting means background discovery is in progress; that server's tools are NOT available in this run.",
            "- Do not infer current MCP availability from prior messages, prior tool results, memory, or compaction summaries.",
            "- Do not describe status=starting as a configuration failure and do not ask the user to repair configuration solely because it is starting.",
            "- If asked about a starting server, say discovery is in progress and its tools may become available in a later run after a HostSession safe point; do not promise that the next run will succeed.",
            "- status=failed, degraded, needs_auth, disabled, closing, or closed exposes no callable tools from that server in this run.",
            "- status=ready only reports an installed server snapshot; the actual tool schema remains the sole authority for which tool names are callable.",
            "</mcp_lifecycle_contract>",
        ]
    )


__all__ = ["McpCapabilityInstallationView", "McpCapabilityProvider"]
