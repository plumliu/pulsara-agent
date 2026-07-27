"""Minimal legal capability invocation fixtures for component tests."""

from __future__ import annotations

from pathlib import Path

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.event import EventContext
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.message import Msg
from pulsara_agent.ports.tool_execution import (
    ToolInvocationOwnerKind,
    ToolRuntimeContext,
    tool_permission_invocation_from_snapshot,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.context import (
    CapabilityDescriptorRenderAttributionFact,
    context_fingerprint,
)
from pulsara_agent.runtime.permission_snapshot import snapshot_from_mode
from pulsara_agent.tools.registry import ToolRegistry


def preview_capability_plan(
    capability_runtime: CapabilityRuntime,
    *,
    workspace_root: Path,
    workspace_kind: str,
    memory_domain: MemoryDomainContext | None,
    tool_registry: ToolRegistry,
    archive: ArtifactStore,
    runtime_session_id: str,
    mcp_installation_id: str,
    user_input: str,
    prior_messages: tuple[Msg, ...] = (),
    active_skill_names: frozenset[str] = frozenset(),
):
    frozen = capability_runtime.freeze_execution_surface(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=workspace_root,
            workspace_kind=workspace_kind,  # type: ignore[arg-type]
            available_tool_names=frozenset(tool_registry.names()),
            mcp_installation_id=mcp_installation_id,
        ),
        tool_registry=tool_registry,
        archive=archive,
        runtime_session_id=runtime_session_id,
        owner_id=f"test_capability_preview:{runtime_session_id}",
    )
    return capability_runtime.preview_exposure_plan(
        CapabilityProjectionResolveContext(
            workspace_root=workspace_root,
            workspace_kind=workspace_kind,  # type: ignore[arg-type]
            memory_domain=memory_domain,
            user_input=user_input,
            prior_messages=prior_messages,
            active_skill_names=active_skill_names,
        ),
        frozen_surface=frozen,
    )


def tool_runtime_context(
    *,
    runtime_session_id: str,
    event_context: EventContext,
    owner_kind: ToolInvocationOwnerKind = ToolInvocationOwnerKind.HOST_MAIN_RUN,
    permission_mode: PermissionMode = PermissionMode.BYPASS_PERMISSIONS,
    context_id: str | None = None,
    model_call_index: int | None = None,
) -> ToolRuntimeContext:
    snapshot = snapshot_from_mode(
        runtime_session_id=runtime_session_id,
        run_id=event_context.run_id,
        permission_mode=permission_mode,
        permission_snapshot_source="session_default",
    )
    return ToolRuntimeContext(
        runtime_session_id=runtime_session_id,
        event_context=event_context,
        permission=tool_permission_invocation_from_snapshot(snapshot.to_context_fact()),
        owner_kind=owner_kind,
        context_id=context_id,
        model_call_index=model_call_index,
    )


def descriptor_attribution_for_test(
    descriptor,
    *,
    runtime_session_id: str,
) -> CapabilityDescriptorRenderAttributionFact:
    contract = descriptor.result_render_contract
    descriptor_fingerprint = descriptor.fingerprint()
    payload = {
        "owner_runtime_session_id": runtime_session_id,
        "exposure_id": f"capability-exposure:test:{runtime_session_id}",
        "exposure_fact_fingerprint": "sha256:" + "1" * 64,
        "descriptor_set_fingerprint": "sha256:" + "2" * 64,
        "descriptor_id": descriptor.id,
        "descriptor_fingerprint": descriptor_fingerprint,
        "result_render_contract_fingerprint": contract.contract_fingerprint,
        "descriptor_source_event_id": f"capability-exposure-event:test:{runtime_session_id}",
        "descriptor_source_sequence": 1,
        "descriptor_source_payload_fingerprint": "sha256:" + "3" * 64,
    }
    return CapabilityDescriptorRenderAttributionFact(
        **payload,
        attribution_fingerprint=context_fingerprint(
            "capability-descriptor-render-attribution:v1", payload
        ),
    )


__all__ = [
    "descriptor_attribution_for_test",
    "preview_capability_plan",
    "tool_runtime_context",
]
