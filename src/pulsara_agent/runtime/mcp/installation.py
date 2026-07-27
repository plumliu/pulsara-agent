"""Runtime composition of MCP descriptors and executable bindings."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityAvailability,
    CapabilityDescriptor,
    CapabilityProviderKind,
    CapabilityProvenance,
)
from pulsara_agent.capability.result_contracts import generic_result_render_contract
from pulsara_agent.capability.tool_action import mcp_tool_action_policy
from pulsara_agent.capability.types import CapabilityDiagnostic
from pulsara_agent.ports.artifact import ToolResultArtifactOptions
from pulsara_agent.ports.mcp import McpToolExecutionPort
from pulsara_agent.ports.tool_registry import (
    McpToolBindingContract,
    ToolBindingOrigin,
    build_tool_binding_contract,
)
from pulsara_agent.primitives.mcp import McpBindingIdentityFact
from pulsara_agent.runtime.mcp.types import (
    McpInstalledCapabilitySnapshot,
    McpManagerSlot,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    mangle_mcp_tool_name,
)
from pulsara_agent.runtime.tool_composition import (
    build_runtime_tool_binding_installation,
)
from pulsara_agent.tools.adapters.mcp import McpCapabilityTool


def build_mcp_installation(
    *,
    execution_port: McpToolExecutionPort,
    artifact_options: ToolResultArtifactOptions,
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
    installations = []
    previous_by_name = {
        item.tool.name: item
        for item in (
            previous_installation.ordered_binding_installations
            if previous_installation is not None
            else ()
        )
    }
    used_model_names: dict[str, str] = {}
    for snapshot in snapshots:
        config = configs_by_server.get(snapshot.server_id)
        diagnostics.extend(_snapshot_diagnostics(snapshot))
        if snapshot.status is not McpServerStatus.READY or config is None:
            continue
        slot = slots_by_server.get(snapshot.server_id)
        if slot is None or slot.snapshot_id != snapshot.snapshot_id:
            diagnostics.append(
                CapabilityDiagnostic(
                    severity="error",
                    code="mcp_missing_execution_slot",
                    message=(
                        "MCP ready snapshot has no exact execution slot: "
                        f"{snapshot.server_id}"
                    ),
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
            descriptor = _descriptor_from_tool(
                snapshot,
                discovered_tool,
                config=config,
                model_name=model_name,
            )
            identity = slot.binding_identity
            identity_fact = McpBindingIdentityFact(
                server_id=identity.server_id,
                slot_id=identity.slot_id,
                snapshot_id=identity.snapshot_id,
                discovery_generation=identity.discovery_generation,
            )
            contract = build_tool_binding_contract(
                tool_name=model_name,
                origin=ToolBindingOrigin.MCP,
                contract_id=f"pulsara.mcp.{model_name}",
                contract_version="v1",
                binding_attributes=identity_fact.model_dump(mode="json"),
                mcp_binding_identity=identity_fact,
                original_tool_name=discovered_tool.name,
            )
            if not isinstance(contract, McpToolBindingContract):
                raise AssertionError("MCP binding factory returned a non-MCP branch")
            tool = McpCapabilityTool(
                binding=contract,
                execution_port=execution_port,
                timeout_ms=config.tool_timeout_ms,
            )
            previous = previous_by_name.get(model_name)
            if (
                previous is not None
                and previous.binding_contract == contract
                and previous.descriptor_fingerprint == descriptor.fingerprint()
                and previous.tool == tool
            ):
                descriptors.append(
                    next(
                        item
                        for item in previous_installation.descriptors
                        if item.name == model_name
                    )
                )
                installations.append(previous)
                continue
            descriptors.append(descriptor)
            installations.append(
                build_runtime_tool_binding_installation(
                    tool=tool,
                    descriptor=descriptor,
                    binding_contract=contract,
                    artifact_options=artifact_options,
                )
            )
    descriptor_names = {descriptor.name for descriptor in descriptors}
    installation_names = {item.tool.name for item in installations}
    if descriptor_names != installation_names:
        raise ValueError("MCP descriptor/execution installation names differ")
    return McpInstalledCapabilitySnapshot(
        installation_id=installation_id or f"mcp_installation:{uuid4().hex}",
        config_epoch=config_epoch,
        event_safe_config_set_fingerprint=event_safe_config_set_fingerprint,
        installed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        snapshots=snapshots,
        descriptors=tuple(descriptors),
        ordered_binding_installations=tuple(installations),
        diagnostics=tuple(diagnostics),
        ready_server_ids=frozenset(
            snapshot.server_id
            for snapshot in snapshots
            if snapshot.status is McpServerStatus.READY
        ),
        binding_identities=frozenset(
            slot.binding_identity for slot in slots_by_server.values()
        ),
    )


def empty_mcp_installation(
    *,
    config_epoch: int = 0,
    event_safe_config_set_fingerprint: str = "sha256:empty",
    installation_id: str = "mcp_installation:empty",
) -> McpInstalledCapabilitySnapshot:
    """Build an empty surface bound to an exact supervisor config identity."""

    return McpInstalledCapabilitySnapshot(
        installation_id=installation_id,
        config_epoch=config_epoch,
        event_safe_config_set_fingerprint=event_safe_config_set_fingerprint,
        installed_at_utc="1970-01-01T00:00:00Z",
        snapshots=(),
        descriptors=(),
        ordered_binding_installations=(),
        diagnostics=(),
        ready_server_ids=frozenset(),
        binding_identities=frozenset(),
    )


def _snapshot_diagnostics(
    snapshot: McpServerSnapshot,
) -> tuple[CapabilityDiagnostic, ...]:
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


__all__ = ["build_mcp_installation", "empty_mcp_installation"]
