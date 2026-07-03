"""MCP capability provider and same-source binding bundle builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityAvailability,
    CapabilityDescriptor,
    CapabilityProviderKind,
    CapabilityProvenance,
)
from pulsara_agent.capability.provider import CapabilityProviderOutput
from pulsara_agent.capability.types import CapabilityDiagnostic, CapabilityResolveContext
from pulsara_agent.runtime.mcp.manager import McpClientManager
from pulsara_agent.runtime.mcp.types import (
    McpDiscoveredTool,
    McpServerSnapshot,
    McpServerStatus,
    mangle_mcp_tool_name,
)
from pulsara_agent.tools.adapters.mcp import McpCapabilityTool
from pulsara_agent.tools.base import AsyncTool


@dataclass(frozen=True, slots=True)
class McpCapabilityBindingBundle:
    descriptors: tuple[CapabilityDescriptor, ...]
    tools: tuple[AsyncTool, ...]
    diagnostics: tuple[CapabilityDiagnostic, ...]
    manager: McpClientManager
    generation: int = 0


@dataclass(frozen=True, slots=True)
class McpCapabilityProvider:
    bundle: McpCapabilityBindingBundle
    provider_id: str = "mcp"

    def resolve(
        self,
        context: CapabilityResolveContext,
        *,
        bound_tool_names: frozenset[str],
    ) -> CapabilityProviderOutput:
        del context, bound_tool_names
        return CapabilityProviderOutput(
            descriptors=self.bundle.descriptors,
            diagnostics=self.bundle.diagnostics,
        )


def build_mcp_bundle(manager: McpClientManager) -> McpCapabilityBindingBundle:
    diagnostics: list[CapabilityDiagnostic] = []
    descriptors: list[CapabilityDescriptor] = []
    tools: list[AsyncTool] = []
    used_model_names: dict[str, str] = {}
    generation = 0

    for snapshot in manager.snapshots:
        generation = max(generation, snapshot.generation)
        diagnostics.extend(_snapshot_diagnostics(snapshot))
        if snapshot.status is not McpServerStatus.READY:
            continue
        for discovered_tool in snapshot.tools:
            model_name = mangle_mcp_tool_name(snapshot.config.server_id, discovered_tool.name)
            previous = used_model_names.get(model_name)
            descriptor_id = f"mcp:{snapshot.config.server_id}:{discovered_tool.name}"
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
            descriptor = _descriptor_from_tool(snapshot, discovered_tool, model_name=model_name)
            descriptors.append(descriptor)
            tools.append(
                McpCapabilityTool(
                    name=model_name,
                    description=descriptor.description,
                    parameters=dict(descriptor.input_schema or {}),
                    server_id=snapshot.config.server_id,
                    original_tool_name=discovered_tool.name,
                    client_manager=manager,
                    timeout_ms=snapshot.config.tool_timeout_ms,
                    is_read_only=descriptor.is_read_only,
                    is_concurrency_safe=descriptor.is_concurrency_safe,
                )
            )

    descriptor_names = {descriptor.name for descriptor in descriptors}
    tool_names = {tool.name for tool in tools}
    for missing in sorted(descriptor_names.difference(tool_names)):
        diagnostics.append(
            CapabilityDiagnostic(
                severity="error",
                code="mcp_missing_execution_binding",
                message=f"MCP descriptor has no execution binding: {missing}",
            )
        )
    for missing in sorted(tool_names.difference(descriptor_names)):
        diagnostics.append(
            CapabilityDiagnostic(
                severity="error",
                code="mcp_missing_descriptor",
                message=f"MCP execution binding has no descriptor: {missing}",
            )
        )

    return McpCapabilityBindingBundle(
        descriptors=tuple(descriptors),
        tools=tuple(tools),
        diagnostics=tuple(diagnostics),
        manager=manager,
        generation=generation,
    )


def _snapshot_diagnostics(snapshot: McpServerSnapshot) -> Iterable[CapabilityDiagnostic]:
    server_id = snapshot.config.server_id
    if not snapshot.config.enabled:
        yield CapabilityDiagnostic(
            severity="info",
            code="mcp_server_disabled",
            message=f"MCP server {server_id!r} is disabled.",
        )
        return
    if snapshot.status is McpServerStatus.READY:
        return
    severity = "error" if snapshot.config.required else "warning"
    code = {
        McpServerStatus.FAILED: "mcp_server_startup_failed",
        McpServerStatus.NEEDS_AUTH: "mcp_server_needs_auth",
        McpServerStatus.DEGRADED: "mcp_server_degraded",
        McpServerStatus.CLOSED: "mcp_server_closed",
        McpServerStatus.STARTING: "mcp_server_starting",
        McpServerStatus.DISABLED: "mcp_server_disabled",
    }.get(snapshot.status, "mcp_server_unavailable")
    yield CapabilityDiagnostic(
        severity=severity,
        code=code,
        message=snapshot.message or f"MCP server {server_id!r} is {snapshot.status.value}.",
    )


def _descriptor_from_tool(
    snapshot: McpServerSnapshot,
    tool: McpDiscoveredTool,
    *,
    model_name: str,
) -> CapabilityDescriptor:
    annotations = tool.annotations
    read_only = annotations.read_only_hint is True
    destructive = True if annotations.destructive_hint is None else bool(annotations.destructive_hint)
    open_world = True if annotations.open_world_hint is None else bool(annotations.open_world_hint)
    return CapabilityDescriptor(
        id=f"mcp:{snapshot.config.server_id}:{tool.name}",
        name=model_name,
        description=tool.description,
        input_schema=dict(tool.input_schema),
        namespace=f"mcp:{snapshot.config.server_id}",
        provider_kind=CapabilityProviderKind.MCP,
        provider_id=snapshot.config.server_id,
        is_model_callable=True,
        is_read_only=read_only,
        is_concurrency_safe=snapshot.config.supports_parallel_tool_calls,
        is_destructive=destructive,
        is_open_world=open_world,
        requires_user_interaction=False,
        permission_category="mcp",
        approval_policy_hint=snapshot.config.default_approval_mode,
        advertise_policy=CapabilityAdvertisePolicy.DIRECT,
        availability=CapabilityAvailability.AVAILABLE,
        timeout_ms=snapshot.config.tool_timeout_ms,
        provenance=CapabilityProvenance(
            provider_kind=CapabilityProviderKind.MCP,
            provider_id=snapshot.config.server_id,
            source=snapshot.config.transport_kind.value,
        ),
        metadata={
            "server_id": snapshot.config.server_id,
            "original_tool_name": tool.name,
            "transport": snapshot.config.transport_kind.value,
            "annotations": annotations.to_dict(),
            "generation": snapshot.generation,
        },
    )

