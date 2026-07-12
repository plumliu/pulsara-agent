"""Test helpers for the split capability execution/projection contracts."""

from __future__ import annotations

from pathlib import Path

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.message import Msg
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


__all__ = ["preview_capability_plan"]
