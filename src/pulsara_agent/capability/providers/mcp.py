"""MCP capability projection from one frozen installed snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping
from uuid import uuid4

from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityAvailability,
    CapabilityDescriptor,
    CapabilityProviderKind,
    CapabilityProvenance,
)
from pulsara_agent.capability.result_contracts import generic_result_render_contract
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
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import (
    McpInstalledCapabilitySnapshot,
    McpManagerSlot,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    mangle_mcp_tool_name,
)
from pulsara_agent.runtime.tool_action import mcp_tool_action_policy
from pulsara_agent.tools.adapters.mcp import McpCapabilityTool


@dataclass(frozen=True, slots=True)
class McpCapabilityProvider:
    installation: McpInstalledCapabilitySnapshot
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


def build_mcp_installation(
    *,
    supervisor: McpServerSupervisor,
    config_epoch: int,
    event_safe_config_set_fingerprint: str,
    snapshots: tuple[McpServerSnapshot, ...],
    configs_by_server: Mapping[str, McpServerConfig],
    slots_by_server: Mapping[str, McpManagerSlot],
    installation_id: str | None = None,
    previous_installation: McpInstalledCapabilitySnapshot | None = None,
) -> McpInstalledCapabilitySnapshot:
    diagnostics: list[CapabilityDiagnostic] = []
    descriptors: list[CapabilityDescriptor] = []
    tools: list[McpCapabilityTool] = []
    used_model_names: dict[str, str] = {}
    previous_snapshots = {
        snapshot.server_id: snapshot
        for snapshot in (
            previous_installation.snapshots if previous_installation else ()
        )
    }
    previous_descriptors = {
        descriptor.name: descriptor
        for descriptor in (
            previous_installation.descriptors if previous_installation else ()
        )
    }
    previous_tools = {
        tool.name: tool
        for tool in (previous_installation.tools if previous_installation else ())
        if isinstance(tool, McpCapabilityTool)
    }

    for snapshot in snapshots:
        config = configs_by_server.get(snapshot.server_id)
        diagnostics.extend(_snapshot_diagnostics(snapshot, config=config))
        if snapshot.status is not McpServerStatus.READY or config is None:
            continue
        slot = slots_by_server.get(snapshot.server_id)
        if slot is None or slot.snapshot_id != snapshot.snapshot_id:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="error",
                    code="mcp_missing_execution_slot",
                    message=f"MCP ready snapshot has no exact execution slot: {snapshot.server_id}",
                )
            )
            continue
        for discovered_tool in snapshot.tools:
            model_name = mangle_mcp_tool_name(snapshot.server_id, discovered_tool.name)
            descriptor_id = f"mcp:{snapshot.server_id}:{discovered_tool.name}"
            previous = used_model_names.get(model_name)
            if previous is not None:
                diagnostics.append(
                    CapabilityDiagnostic(
                        severity="error",
                        code="mcp_tool_name_collision",
                        message=(
                            f"MCP model tool name collision for {model_name!r}: "
                            f"{previous!r} and {descriptor_id!r}"
                        ),
                    )
                )
                continue
            used_model_names[model_name] = descriptor_id
            previous_snapshot = previous_snapshots.get(snapshot.server_id)
            previous_descriptor = previous_descriptors.get(model_name)
            previous_tool = previous_tools.get(model_name)
            reusable = (
                previous_snapshot is not None
                and previous_snapshot.snapshot_id == snapshot.snapshot_id
                and previous_snapshot.snapshot_semantic_fingerprint
                == snapshot.snapshot_semantic_fingerprint
                and previous_snapshot.event_safe_config_fingerprint
                == snapshot.event_safe_config_fingerprint
                and previous_descriptor is not None
                and previous_tool is not None
                and previous_tool.supervisor is supervisor
                and previous_tool.binding_identity == slot.binding_identity
                and previous_tool.original_tool_name == discovered_tool.name
                and previous_tool.timeout_ms == config.tool_timeout_ms
            )
            if reusable:
                descriptor = previous_descriptor
                bound_tool = previous_tool
            else:
                descriptor = _descriptor_from_tool(
                    snapshot,
                    discovered_tool,
                    config=config,
                    model_name=model_name,
                )
                bound_tool = McpCapabilityTool(
                    name=model_name,
                    description=descriptor.description,
                    parameters=dict(descriptor.input_schema or {}),
                    server_id=snapshot.server_id,
                    original_tool_name=discovered_tool.name,
                    supervisor=supervisor,
                    binding_identity=slot.binding_identity,
                    timeout_ms=config.tool_timeout_ms,
                    is_read_only=descriptor.is_read_only,
                    is_concurrency_safe=descriptor.is_concurrency_safe,
                )
            descriptors.append(descriptor)
            tools.append(bound_tool)

    descriptor_names = {descriptor.name for descriptor in descriptors}
    tool_names = {tool.name for tool in tools}
    if descriptor_names != tool_names:
        raise ValueError("MCP descriptor/execution binding names must match exactly")
    identities = frozenset(tool.binding_identity for tool in tools)
    return McpInstalledCapabilitySnapshot(
        installation_id=installation_id or f"mcp_installation:{uuid4().hex}",
        config_epoch=config_epoch,
        event_safe_config_set_fingerprint=event_safe_config_set_fingerprint,
        installed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        snapshots=snapshots,
        descriptors=tuple(descriptors),
        tools=tuple(tools),
        diagnostics=tuple(diagnostics),
        ready_server_ids=frozenset(
            snapshot.server_id
            for snapshot in snapshots
            if snapshot.status is McpServerStatus.READY
        ),
        binding_identities=identities,
    )


def empty_mcp_installation() -> McpInstalledCapabilitySnapshot:
    return McpInstalledCapabilitySnapshot(
        installation_id="mcp_installation:empty",
        config_epoch=0,
        event_safe_config_set_fingerprint="sha256:empty",
        installed_at_utc="1970-01-01T00:00:00Z",
        snapshots=(),
        descriptors=(),
        tools=(),
        diagnostics=(),
        ready_server_ids=frozenset(),
        binding_identities=frozenset(),
    )


def _render_mcp_lifecycle_prompt(
    installation: McpInstalledCapabilitySnapshot,
) -> str | None:
    """Render the run-frozen MCP lifecycle contract for the model.

    This projection is deliberately Pulsara-owned.  It never includes remote
    server messages, instructions, diagnostics, or catalog prose, so an MCP
    server cannot inject model instructions through its lifecycle metadata.
    """

    if not installation.snapshots:
        return None
    installed_tool_counts: dict[str, int] = {}
    for tool in installation.tools:
        server_id = str(getattr(tool, "server_id", ""))
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
            "- status=starting means background discovery is in progress; that server's "
            "tools are NOT available in this run.",
            "- Do not infer current MCP availability from prior messages, prior tool "
            "results, memory, or compaction summaries.",
            "- Do not describe status=starting as a configuration failure and do not ask "
            "the user to repair configuration solely because it is starting.",
            "- If asked about a starting server, say discovery is in progress and its "
            "tools may become available in a later run after a HostSession safe point; "
            "do not promise that the next run will succeed.",
            "- status=failed, degraded, needs_auth, disabled, closing, or closed exposes "
            "no callable tools from that server in this run.",
            "- status=ready only reports an installed server snapshot; the actual tool "
            "schema remains the sole authority for which tool names are callable.",
            "</mcp_lifecycle_contract>",
        ]
    )


def _snapshot_diagnostics(
    snapshot: McpServerSnapshot,
    *,
    config: McpServerConfig | None,
) -> Iterable[CapabilityDiagnostic]:
    if snapshot.status is McpServerStatus.READY:
        return ()
    severity = "error" if snapshot.required else "warning"
    if snapshot.status is McpServerStatus.DISABLED:
        severity = "info"
    code = {
        McpServerStatus.FAILED: "mcp_server_startup_failed",
        McpServerStatus.NEEDS_AUTH: "mcp_server_needs_auth",
        McpServerStatus.DEGRADED: "mcp_server_degraded",
        McpServerStatus.CLOSED: "mcp_server_closed",
        McpServerStatus.STARTING: "mcp_server_starting",
        McpServerStatus.DISABLED: "mcp_server_disabled",
    }.get(snapshot.status, "mcp_server_unavailable")
    return (
        CapabilityDiagnostic(
            severity=severity,
            code=code,
            message=snapshot.message
            or f"MCP server {snapshot.server_id!r} is {snapshot.status.value}.",
        ),
    )


def _descriptor_from_tool(
    snapshot: McpServerSnapshot,
    tool,
    *,
    config: McpServerConfig,
    model_name: str,
) -> CapabilityDescriptor:
    annotations = tool.annotations
    read_only = annotations.read_only_hint is True
    destructive = (
        True
        if annotations.destructive_hint is None
        else bool(annotations.destructive_hint)
    )
    open_world = (
        True
        if annotations.open_world_hint is None
        else bool(annotations.open_world_hint)
    )
    return CapabilityDescriptor(
        id=f"mcp:{snapshot.server_id}:{tool.name}",
        name=model_name,
        description=tool.description,
        input_schema=dict(tool.input_schema),
        namespace=f"mcp:{snapshot.server_id}",
        provider_kind=CapabilityProviderKind.MCP,
        provider_id=snapshot.server_id,
        is_model_callable=True,
        is_read_only=read_only,
        is_concurrency_safe=config.supports_parallel_tool_calls,
        is_destructive=destructive,
        is_open_world=open_world,
        requires_user_interaction=False,
        permission_category="mcp",
        result_render_contract=generic_result_render_contract(),
        long_horizon_policy=mcp_tool_action_policy(),
        approval_policy_hint=config.default_approval_mode,
        advertise_policy=CapabilityAdvertisePolicy.DIRECT,
        availability=CapabilityAvailability.AVAILABLE,
        timeout_ms=config.tool_timeout_ms,
        provenance=CapabilityProvenance(
            provider_kind=CapabilityProviderKind.MCP,
            provider_id=snapshot.server_id,
            source=config.transport_kind.value,
        ),
        metadata={
            "server_id": snapshot.server_id,
            "original_tool_name": tool.name,
            "transport": config.transport_kind.value,
            "annotations": annotations.to_dict(),
            "snapshot_id": snapshot.snapshot_id,
            "discovery_generation": snapshot.discovery_generation,
        },
    )


__all__ = [
    "McpCapabilityProvider",
    "build_mcp_installation",
    "empty_mcp_installation",
]
