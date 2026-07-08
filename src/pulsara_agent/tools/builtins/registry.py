"""Registry construction for Pulsara built-in tools."""

from __future__ import annotations

from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink

from pulsara_agent.memory.canonical.query import MemoryQuery
from pulsara_agent.memory.recall.service import MemoryRecallService
from pulsara_agent.runtime.permission import PermissionState
from pulsara_agent.tools.base import AsyncTool, Tool
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
    MemorySearchTool,
)
from pulsara_agent.tools.builtins.plan import AskPlanQuestionTool, EnterPlanTool, ExitPlanTool
from pulsara_agent.tools.builtins.subagent import (
    CreateAgentTasksTool,
    ListAgentsTool,
    ReportAgentPhaseTool,
    ReportAgentResultTool,
    SpawnAgentTool,
    StopAgentTool,
    StopAgentTaskTool,
    WaitAgentTool,
    WaitAgentTasksTool,
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
    permission_state: PermissionState | None = None,
    extra_tools: tuple[Tool | AsyncTool, ...] = (),
) -> ToolRegistry:
    if not isinstance(runtime_session, RuntimeSession):
        raise TypeError("build_core_tool_registry requires a RuntimeSession")
    root = runtime_session.workspace_root
    registry = ToolRegistry()
    # PERMISSION_POLICY_CONTRACT: gate is the sole authority. All tools are
    # registered unconditionally and stay visible across every mode; the
    # PolicyPermissionGate denies disallowed calls at evaluation time
    # (visible-but-blocked). This keeps the tools array constant across mode
    # switches so the prompt prefix cache stays stable.
    registry.register(ArtifactReadTool(runtime_session))
    registry.register(EnterPlanTool())
    registry.register(AskPlanQuestionTool())
    registry.register(ExitPlanTool())
    registry.register(ReadFileTool(root))
    registry.register(SearchFilesTool(root))
    registry.register(
        TerminalTool(
            root,
            runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
            owner_conversation_id=runtime_session.terminal_owner_conversation_id,
            permission_state=permission_state,
        )
    )
    registry.register(
        TerminalProcessTool(
            root,
            runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
            permission_state=permission_state,
        )
    )
    registry.register(EditFileTool(root))
    registry.register(WriteFileTool(root))
    registry.register(TodoTool())
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
    subagent_context = runtime_session.default_event_metadata.get("subagent")
    if runtime_session.subagent_runtime is not None and isinstance(subagent_context, dict):
        subagent_run_id = subagent_context.get("subagent_run_id")
        if isinstance(subagent_run_id, str) and subagent_run_id:
            registry.register(ReportAgentPhaseTool(runtime_session.subagent_runtime, subagent_run_id))
            registry.register(ReportAgentResultTool(runtime_session.subagent_runtime, subagent_run_id))
    elif runtime_session.subagent_runtime is not None:
        registry.register(SpawnAgentTool(runtime_session.subagent_runtime))
        registry.register(WaitAgentTool(runtime_session.subagent_runtime))
        registry.register(StopAgentTool(runtime_session.subagent_runtime))
        registry.register(ListAgentsTool(runtime_session.subagent_runtime))
        registry.register(CreateAgentTasksTool(runtime_session.subagent_runtime))
        registry.register(WaitAgentTasksTool(runtime_session.subagent_runtime))
        registry.register(StopAgentTaskTool(runtime_session.subagent_runtime))
    for tool in extra_tools:
        registry.register(tool)
    return registry
