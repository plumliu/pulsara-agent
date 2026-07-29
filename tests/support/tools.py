"""Component-test convenience around the production tool composition root."""

from __future__ import annotations

from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.memory.canonical.query import MemoryQuery
from pulsara_agent.memory.recall.service import MemoryRecallService
from pulsara_agent.event import EventContext
from pulsara_agent.capability.builtin_provider import builtin_tool_descriptors
from pulsara_agent.capability.descriptor import CapabilityDescriptor
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.runtime.tool_executor import ToolExecutor
from pulsara_agent.runtime.permission import PermissionState
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.tool_composition import (
    build_runtime_tool_composition_input,
    build_runtime_tool_executor,
)
from pulsara_agent.tools.registry import ToolRegistry
from tests.support.capability import (
    descriptor_attribution_for_test,
    tool_runtime_context,
)


def build_component_tool_registry(
    runtime_session: RuntimeSession,
    *,
    subagent_runtime=None,
    memory_proposal_sink: MemoryProposalSink | None = None,
    memory_recall_service: MemoryRecallService | None = None,
    memory_query: MemoryQuery | None = None,
    graph_id: str | None = None,
    memory_read_scopes: frozenset[str] | None = None,
    permission_state: PermissionState | None = None,
) -> ToolRegistry:
    if not isinstance(runtime_session, RuntimeSession):
        raise TypeError("build_component_tool_registry requires a RuntimeSession")
    # Permission is intentionally not a composition input. The runtime gate is
    # the sole authority, so every preset sees the same stable tool catalog.
    del permission_state
    composition = build_runtime_tool_composition_input(
        runtime_session,
        subagent_runtime=subagent_runtime,
        memory_proposal_sink=memory_proposal_sink,
        memory_recall_service=memory_recall_service,
        memory_query=memory_query,
        graph_id=graph_id,
        memory_read_scopes=memory_read_scopes,
    )
    return build_runtime_tool_executor(composition).registry


def build_component_tool_executor(
    runtime_session: RuntimeSession,
    *,
    subagent_runtime=None,
    memory_proposal_sink: MemoryProposalSink | None = None,
    memory_recall_service: MemoryRecallService | None = None,
    memory_query: MemoryQuery | None = None,
    graph_id: str | None = None,
    memory_read_scopes: frozenset[str] | None = None,
    recorder=None,
):
    if not isinstance(runtime_session, RuntimeSession):
        raise TypeError("build_component_tool_executor requires a RuntimeSession")
    composition = build_runtime_tool_composition_input(
        runtime_session,
        subagent_runtime=subagent_runtime,
        memory_proposal_sink=memory_proposal_sink,
        memory_recall_service=memory_recall_service,
        memory_query=memory_query,
        graph_id=graph_id,
        memory_read_scopes=memory_read_scopes,
    )
    return build_runtime_tool_executor(composition, recorder=recorder)


def execute_component_tool(
    executor: ToolExecutor,
    call: ToolCall,
    *,
    event_context: EventContext,
    permission_mode: PermissionMode = PermissionMode.BYPASS_PERMISSIONS,
    descriptor: CapabilityDescriptor | None = None,
):
    resolved_descriptor = descriptor or next(
        (item for item in builtin_tool_descriptors() if item.name == call.name),
        None,
    )
    runtime_session_id = executor.runtime_session_id or "runtime:test-component-tool"
    return executor.execute(
        call,
        event_context=event_context,
        descriptor=resolved_descriptor,
        descriptor_attribution=(
            descriptor_attribution_for_test(
                resolved_descriptor,
                runtime_session_id=runtime_session_id,
            )
            if resolved_descriptor is not None
            else None
        ),
        runtime_context=tool_runtime_context(
            runtime_session_id=runtime_session_id,
            event_context=event_context,
            permission_mode=permission_mode,
        ),
    )


__all__ = [
    "build_component_tool_executor",
    "build_component_tool_registry",
    "execute_component_tool",
]
