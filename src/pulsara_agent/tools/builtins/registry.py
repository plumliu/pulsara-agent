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
from pulsara_agent.tools.builtins.plan import (
    AskPlanQuestionTool,
    EnterPlanTool,
    ExitPlanTool,
)
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
from pulsara_agent.tools.builtins.terminal_monitor import TerminalMonitorTool
from pulsara_agent.tools.builtins.terminal_process import TerminalProcessTool
from pulsara_agent.tools.builtins.todo import TodoTool
from pulsara_agent.tools.registry import (
    ToolRegistry,
    build_tool_binding_contract,
)


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

    def register(
        tool: Tool | AsyncTool,
        *,
        origin: str = "builtin",
    ) -> None:
        binding_identity = getattr(tool, "binding_identity", None)
        binding_attributes = None
        if binding_identity is not None:
            binding_attributes = {
                "server_id": getattr(binding_identity, "server_id", None),
                "slot_id": getattr(binding_identity, "slot_id", None),
                "snapshot_id": getattr(binding_identity, "snapshot_id", None),
                "discovery_generation": getattr(
                    binding_identity, "discovery_generation", None
                ),
            }
        contract_id = str(
            getattr(tool, "binding_contract_id", f"pulsara.{origin}.{tool.name}")
        )
        contract_version = str(getattr(tool, "binding_contract_version", "v1"))
        registry.register(
            tool,
            binding_contract=build_tool_binding_contract(
                tool_name=tool.name,
                origin=origin,  # type: ignore[arg-type]
                contract_id=contract_id,
                contract_version=contract_version,
                binding_attributes=binding_attributes,
            ),
        )

    # PERMISSION_POLICY_CONTRACT: gate is the sole authority. All tools are
    # registered unconditionally and stay visible across every mode; the
    # PolicyPermissionGate denies disallowed calls at evaluation time
    # (visible-but-blocked). This keeps the tools array constant across mode
    # switches so the prompt prefix cache stays stable.
    register(ArtifactReadTool(runtime_session))
    register(EnterPlanTool(), origin="workflow")
    register(AskPlanQuestionTool(), origin="workflow")
    register(ExitPlanTool(), origin="workflow")
    register(ReadFileTool(root))
    register(SearchFilesTool(root))
    register(
        TerminalTool(
            root,
            runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
            owner_conversation_id=runtime_session.terminal_owner_conversation_id,
            permission_state=permission_state,
            terminal_notification_account=(
                runtime_session.terminal_notification_account_coordinator
            ),
        )
    )
    register(
        TerminalProcessTool(
            root,
            runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
            permission_state=permission_state,
        )
    )
    register(
        TerminalMonitorTool(
            root,
            runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
            permission_state=permission_state,
            terminal_monitor_coordinator=(runtime_session.terminal_monitor_coordinator),
        )
    )
    register(EditFileTool(root))
    register(WriteFileTool(root))
    register(TodoTool())
    if memory_recall_service is not None:
        register(
            MemorySearchTool(
                recall=memory_recall_service,
                graph_id=graph_id,
                read_scopes=memory_read_scopes,
            )
        )
    if memory_query is not None:
        register(
            MemoryGetTool(
                memory_query=memory_query,
                graph_id=graph_id,
                read_scopes=memory_read_scopes,
            )
        )
        register(
            MemoryExplainTool(
                memory_query=memory_query,
                graph_id=graph_id,
                read_scopes=memory_read_scopes,
            )
        )
    if memory_proposal_sink is not None:
        register(
            RememberClaimTool(
                sink=memory_proposal_sink,
                runtime_session_id=runtime_session.runtime_session_id,
            )
        )
        register(
            RememberPreferenceTool(
                sink=memory_proposal_sink,
                runtime_session_id=runtime_session.runtime_session_id,
            )
        )
        register(
            RememberObservationTool(
                sink=memory_proposal_sink,
                runtime_session_id=runtime_session.runtime_session_id,
            )
        )
        register(
            RememberActionBoundaryTool(
                sink=memory_proposal_sink,
                runtime_session_id=runtime_session.runtime_session_id,
            )
        )
        register(
            RememberDecisionTool(
                sink=memory_proposal_sink,
                runtime_session_id=runtime_session.runtime_session_id,
            )
        )
    subagent_context = runtime_session.default_event_metadata.get("subagent")
    if runtime_session.subagent_runtime is not None and isinstance(
        subagent_context, dict
    ):
        subagent_run_id = subagent_context.get("subagent_run_id")
        if isinstance(subagent_run_id, str) and subagent_run_id:
            register(
                ReportAgentPhaseTool(runtime_session.subagent_runtime, subagent_run_id),
                origin="subagent_system",
            )
            register(
                ReportAgentResultTool(
                    runtime_session.subagent_runtime, subagent_run_id
                ),
                origin="subagent_system",
            )
    elif runtime_session.subagent_runtime is not None:
        register(
            SpawnAgentTool(runtime_session.subagent_runtime), origin="subagent_system"
        )
        register(
            WaitAgentTool(runtime_session.subagent_runtime), origin="subagent_system"
        )
        register(
            StopAgentTool(runtime_session.subagent_runtime), origin="subagent_system"
        )
        register(
            ListAgentsTool(runtime_session.subagent_runtime), origin="subagent_system"
        )
        register(
            CreateAgentTasksTool(runtime_session.subagent_runtime),
            origin="subagent_system",
        )
        register(
            WaitAgentTasksTool(runtime_session.subagent_runtime),
            origin="subagent_system",
        )
        register(
            StopAgentTaskTool(runtime_session.subagent_runtime),
            origin="subagent_system",
        )
    for tool in extra_tools:
        origin = "mcp" if hasattr(tool, "binding_identity") else "custom"
        register(tool, origin=origin)
    _validate_terminal_public_input_bindings(registry)
    return registry


def _validate_terminal_public_input_bindings(registry: ToolRegistry) -> None:
    from pulsara_agent.capability.builtin_provider import builtin_tool_descriptors
    from pulsara_agent.primitives.context import context_fingerprint
    from pulsara_agent.terminal_public_api import builtin_tool_input_contract_binding

    descriptors = {item.name: item for item in builtin_tool_descriptors()}
    for name in ("terminal_process", "terminal_monitor"):
        binding = builtin_tool_input_contract_binding(name)
        tool_schema = registry.get(name).parameters
        descriptor_schema = descriptors[name].input_schema
        if (
            tool_schema != binding.input_schema
            or descriptor_schema != binding.input_schema
        ):
            raise ValueError(f"{name} public input schema binding drift")
        for schema in (tool_schema, descriptor_schema):
            if (
                context_fingerprint("builtin-tool-input-schema:v1", [name, schema])
                != binding.input_schema_fingerprint
            ):
                raise ValueError(f"{name} public input schema fingerprint drift")
