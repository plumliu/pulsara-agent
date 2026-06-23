"""Registry construction for Pulsara built-in tools."""

from __future__ import annotations

from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink

from pulsara_agent.memory.canonical.query import MemoryQuery
from pulsara_agent.memory.recall.service import MemoryRecallService
from pulsara_agent.runtime.permission import EffectivePermissionPolicy, is_tool_allowed_by_policy
from pulsara_agent.tools.builtins.artifact import ArtifactReadTool
from pulsara_agent.tools.builtins.filesystem import (
    EditFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from pulsara_agent.tools.builtins.memory import (
    RememberActionBoundaryTool,
    RememberClaimTool,
    RememberDecisionTool,
    RememberObservationTool,
    RememberPreferenceTool,
)
from pulsara_agent.tools.builtins.memory_query import (
    MemoryExplainTool,
    MemoryGetTool,
    MemoryRelatedTool,
    MemorySearchTool,
)
from pulsara_agent.tools.builtins.terminal import TerminalTool
from pulsara_agent.tools.builtins.terminal_process import TerminalProcessTool
from pulsara_agent.tools.builtins.todo import TodoTool
from pulsara_agent.tools.registry import ToolRegistry


def build_core_tool_registry(
    runtime_session: RuntimeSession,
    *,
    memory_proposal_sink: MemoryProposalSink | None = None,
    memory_recall_service: MemoryRecallService | None = None,
    memory_query: MemoryQuery | None = None,
    graph_id: str | None = None,
    memory_read_scopes: frozenset[str] | None = None,
    permission_policy: EffectivePermissionPolicy | None = None,
) -> ToolRegistry:
    if not isinstance(runtime_session, RuntimeSession):
        raise TypeError("build_core_tool_registry requires a RuntimeSession")
    root = runtime_session.workspace_root
    registry = ToolRegistry()
    _register_if_allowed(registry, ArtifactReadTool(runtime_session), permission_policy)
    _register_if_allowed(registry, ReadFileTool(root), permission_policy)
    _register_if_allowed(registry, SearchFilesTool(root), permission_policy)
    _register_if_allowed(
        registry,
        TerminalTool(
            root,
            runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
            permission_policy=permission_policy,
        ),
        permission_policy,
    )
    _register_if_allowed(
        registry,
        TerminalProcessTool(
            root,
            runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
            permission_policy=permission_policy,
        ),
        permission_policy,
    )
    _register_if_allowed(registry, EditFileTool(root), permission_policy)
    _register_if_allowed(registry, WriteFileTool(root), permission_policy)
    _register_if_allowed(registry, TodoTool(), permission_policy)
    if memory_recall_service is not None:
        registry.register(
            MemorySearchTool(
                recall=memory_recall_service,
                graph_id=graph_id,
                read_scopes=memory_read_scopes,
            )
        )
    if memory_query is not None:
        registry.register(
            MemoryGetTool(
                memory_query=memory_query,
                graph_id=graph_id,
                read_scopes=memory_read_scopes,
            )
        )
        registry.register(
            MemoryRelatedTool(
                memory_query=memory_query,
                graph_id=graph_id,
                read_scopes=memory_read_scopes,
            )
        )
        registry.register(
            MemoryExplainTool(
                memory_query=memory_query,
                graph_id=graph_id,
                read_scopes=memory_read_scopes,
            )
        )
    if memory_proposal_sink is not None:
        registry.register(RememberClaimTool(sink=memory_proposal_sink))
        registry.register(RememberPreferenceTool(sink=memory_proposal_sink))
        registry.register(RememberObservationTool(sink=memory_proposal_sink))
        registry.register(RememberActionBoundaryTool(sink=memory_proposal_sink))
        registry.register(RememberDecisionTool(sink=memory_proposal_sink))
    return registry


def _register_if_allowed(registry: ToolRegistry, tool, permission_policy: EffectivePermissionPolicy | None) -> None:
    if permission_policy is not None and not is_tool_allowed_by_policy(tool.name, permission_policy):
        return
    registry.register(tool)
