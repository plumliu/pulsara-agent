"""The sole production composition root for executable tool bindings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pulsara_agent.capability.builtin_catalog import (
    BuiltinToolBindingKind,
    builtin_tool_catalog,
)
from pulsara_agent.capability.descriptor import CapabilityDescriptor
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.memory.canonical.query import MemoryQuery
from pulsara_agent.memory.recall.service import MemoryRecallService
from pulsara_agent.ports.artifact import (
    ToolArtifactReadPort,
    ToolResultArtifactOptions,
    ToolResultArtifactProcessingPolicy,
    ToolResultArtifactProcessingPort,
    build_tool_result_artifact_processing_policy,
)
from pulsara_agent.ports.subagent import SubagentControlPort
from pulsara_agent.ports.terminal import (
    TerminalCommandPort,
    TerminalMonitorPort,
    TerminalProcessPort,
)
from pulsara_agent.ports.tool_execution import AsyncTool, Tool
from pulsara_agent.ports.tool_registry import (
    ToolBindingContract,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.runtime.tool_executor import ToolExecutor
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
    StopAgentTaskTool,
    StopAgentTool,
    WaitAgentTasksTool,
    WaitAgentTool,
)
from pulsara_agent.tools.builtins.terminal import TerminalTool
from pulsara_agent.tools.builtins.terminal_monitor import TerminalMonitorTool
from pulsara_agent.tools.builtins.terminal_process import TerminalProcessTool
from pulsara_agent.tools.builtins.todo import TodoTool
from pulsara_agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession, RuntimeThreadRecorder


@dataclass(frozen=True, slots=True)
class RuntimeToolBindingInstallation:
    tool: Tool | AsyncTool
    binding_contract: ToolBindingContract
    descriptor_id: str
    descriptor_fingerprint: str
    artifact_processing_policy: ToolResultArtifactProcessingPolicy
    installation_fingerprint: str

    def __post_init__(self) -> None:
        if self.tool.name != self.binding_contract.tool_name:
            raise ValueError("tool installation binding name mismatch")
        if (
            self.artifact_processing_policy.descriptor_id != self.descriptor_id
            or self.artifact_processing_policy.descriptor_fingerprint
            != self.descriptor_fingerprint
        ):
            raise ValueError("tool installation artifact policy identity mismatch")
        payload = {
            "tool_name": self.tool.name,
            "binding_contract_fingerprint": (
                self.binding_contract.contract_fact_fingerprint
            ),
            "descriptor_id": self.descriptor_id,
            "descriptor_fingerprint": self.descriptor_fingerprint,
            "artifact_policy_fingerprint": (
                self.artifact_processing_policy.policy_fingerprint
            ),
        }
        if self.installation_fingerprint != context_fingerprint(
            "runtime-tool-binding-installation:v1", payload
        ):
            raise ValueError("runtime tool installation fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class NoSubagentTools:
    exposure_kind: Literal["none"] = "none"


@dataclass(frozen=True, slots=True)
class MainSubagentTools:
    exposure_kind: Literal["main"] = "main"


@dataclass(frozen=True, slots=True)
class ChildSubagentTools:
    subagent_run_id: str
    exposure_kind: Literal["child"] = "child"

    def __post_init__(self) -> None:
        if not self.subagent_run_id:
            raise ValueError("child subagent exposure requires a run identity")


SubagentToolExposure = NoSubagentTools | MainSubagentTools | ChildSubagentTools


@dataclass(frozen=True, slots=True)
class RuntimeToolCompositionInput:
    workspace_root: Path
    runtime_session_id: str
    artifact_read_port: ToolArtifactReadPort
    artifact_processing_port: ToolResultArtifactProcessingPort
    artifact_options: ToolResultArtifactOptions
    terminal_command_port: TerminalCommandPort
    terminal_process_port: TerminalProcessPort
    terminal_monitor_port: TerminalMonitorPort
    subagent_control_port: SubagentControlPort | None
    subagent_exposure: SubagentToolExposure
    memory_proposal_sink: MemoryProposalSink | None
    memory_recall_service: MemoryRecallService | None
    memory_query: MemoryQuery | None
    graph_id: str | None
    memory_read_scopes: frozenset[str] | None
    dynamic_tool_installations: tuple[RuntimeToolBindingInstallation, ...]

    def __post_init__(self) -> None:
        if not self.runtime_session_id:
            raise ValueError("tool composition requires a runtime session identity")
        if isinstance(self.subagent_exposure, NoSubagentTools):
            if self.subagent_control_port is not None:
                raise ValueError("subagent port is present without an exposed surface")
        elif self.subagent_control_port is None:
            raise ValueError("subagent exposure requires the subagent control port")


def build_runtime_tool_executor(
    composition: RuntimeToolCompositionInput,
    *,
    recorder: RuntimeThreadRecorder | None = None,
) -> ToolExecutor:
    installations = _builtin_installations(composition)
    installations += composition.dynamic_tool_installations
    names = tuple(item.tool.name for item in installations)
    if len(set(names)) != len(names):
        raise ValueError("runtime tool composition produced duplicate tool names")
    registry = ToolRegistry()
    policies: dict[str, ToolResultArtifactProcessingPolicy] = {}
    for installation in installations:
        registry.register(
            installation.tool,
            binding_contract=installation.binding_contract,
        )
        policies[installation.tool.name] = installation.artifact_processing_policy
    return ToolExecutor(
        registry=registry,
        record_event=recorder,
        artifact_service=composition.artifact_processing_port,
        artifact_policies=policies,
        runtime_session_id=composition.runtime_session_id,
    )


def build_runtime_tool_composition_input(
    runtime_session: RuntimeSession,
    *,
    subagent_runtime,
    memory_proposal_sink: MemoryProposalSink | None,
    memory_recall_service: MemoryRecallService | None,
    memory_query: MemoryQuery | None,
    graph_id: str | None,
    memory_read_scopes: frozenset[str] | None,
    dynamic_tool_installations: tuple[RuntimeToolBindingInstallation, ...]
    | None = None,
) -> RuntimeToolCompositionInput:
    """Read the session service boundary once and freeze narrow tool ports."""

    from pulsara_agent.runtime.subagent.tool_port import RuntimeSubagentControlPort
    from pulsara_agent.runtime.terminal.tool_port import (
        RuntimeTerminalCommandPort,
        RuntimeTerminalMonitorPort,
        RuntimeTerminalProcessPort,
    )
    from pulsara_agent.runtime.tool_artifacts import RuntimeToolArtifactReadPort

    subagent_port = (
        RuntimeSubagentControlPort(subagent_runtime)
        if subagent_runtime is not None
        else None
    )
    subagent_context = runtime_session.default_event_metadata.get("subagent")
    if subagent_port is None:
        subagent_exposure: SubagentToolExposure = NoSubagentTools()
    elif isinstance(subagent_context, dict) and isinstance(
        subagent_context.get("subagent_run_id"), str
    ):
        subagent_exposure = ChildSubagentTools(
            subagent_run_id=subagent_context["subagent_run_id"]
        )
    else:
        subagent_exposure = MainSubagentTools()
    return RuntimeToolCompositionInput(
        workspace_root=runtime_session.workspace_root,
        runtime_session_id=runtime_session.runtime_session_id,
        artifact_read_port=RuntimeToolArtifactReadPort(
            archive=runtime_session.archive,
            index=runtime_session.tool_result_artifacts,
            runtime_session_id=runtime_session.runtime_session_id,
        ),
        artifact_processing_port=runtime_session.artifact_service,
        artifact_options=runtime_session.artifact_service.options,
        terminal_command_port=RuntimeTerminalCommandPort(
            workspace_root=runtime_session.workspace_root,
            terminal_sessions=runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
            owner_conversation_id=runtime_session.terminal_owner_conversation_id,
            terminal_notification_account=(
                runtime_session.terminal_notification_account_coordinator
            ),
            record_event=runtime_session.make_thread_recorder(),
        ),
        terminal_process_port=RuntimeTerminalProcessPort(
            workspace_root=runtime_session.workspace_root,
            terminal_sessions=runtime_session.terminal_sessions,
            owner_host_session_id=runtime_session.terminal_owner_host_session_id,
        ),
        terminal_monitor_port=RuntimeTerminalMonitorPort(
            workspace_root=runtime_session.workspace_root,
            terminal_monitor_coordinator=(runtime_session.terminal_monitor_coordinator),
        ),
        subagent_control_port=subagent_port,
        subagent_exposure=subagent_exposure,
        memory_proposal_sink=memory_proposal_sink,
        memory_recall_service=memory_recall_service,
        memory_query=memory_query,
        graph_id=graph_id,
        memory_read_scopes=memory_read_scopes,
        dynamic_tool_installations=(
            dynamic_tool_installations
            if dynamic_tool_installations is not None
            else tuple(runtime_session.dynamic_tool_installations)
        ),
    )


def build_runtime_tool_binding_installation(
    *,
    tool: Tool | AsyncTool,
    descriptor: CapabilityDescriptor,
    binding_contract: ToolBindingContract,
    artifact_options: ToolResultArtifactOptions,
) -> RuntimeToolBindingInstallation:
    if tool.name != descriptor.name or descriptor.name != binding_contract.tool_name:
        raise ValueError("tool/descriptor/binding names do not match")
    descriptor_fingerprint = descriptor.fingerprint()
    policy = build_tool_result_artifact_processing_policy(
        descriptor_id=descriptor.id,
        descriptor_fingerprint=descriptor_fingerprint,
        artifact_mode=descriptor.artifact_mode,
        source_reference_policy=(
            "reuse_input_artifact" if descriptor.name == "artifact_read" else "none"
        ),
        archive_threshold_bytes=artifact_options.effective_archive_threshold_bytes,
        complete_preview_body_chars=artifact_options.complete_preview_body_chars,
        large_preview_chars=artifact_options.effective_large_preview_chars,
        huge_output_chars=artifact_options.huge_output_chars,
        huge_preview_chars=artifact_options.huge_preview_chars,
        streaming_live_head_cap_chars=(artifact_options.streaming_live_head_cap_chars),
        max_inline_chars=descriptor.max_inline_chars,
    )
    payload = {
        "tool_name": tool.name,
        "binding_contract_fingerprint": binding_contract.contract_fact_fingerprint,
        "descriptor_id": descriptor.id,
        "descriptor_fingerprint": descriptor_fingerprint,
        "artifact_policy_fingerprint": policy.policy_fingerprint,
    }
    return RuntimeToolBindingInstallation(
        tool=tool,
        binding_contract=binding_contract,
        descriptor_id=descriptor.id,
        descriptor_fingerprint=descriptor_fingerprint,
        artifact_processing_policy=policy,
        installation_fingerprint=context_fingerprint(
            "runtime-tool-binding-installation:v1", payload
        ),
    )


def _builtin_installations(
    composition: RuntimeToolCompositionInput,
) -> tuple[RuntimeToolBindingInstallation, ...]:
    catalog = {entry.name: entry for entry in builtin_tool_catalog()}
    tools = _builtin_tools(composition)
    return tuple(
        build_runtime_tool_binding_installation(
            tool=tool,
            descriptor=catalog[tool.name].descriptor,
            binding_contract=catalog[tool.name].binding_contract,
            artifact_options=composition.artifact_options,
        )
        for tool in tools
    )


def _builtin_tools(
    composition: RuntimeToolCompositionInput,
) -> tuple[Tool | AsyncTool, ...]:
    root = composition.workspace_root
    tools: list[Tool | AsyncTool] = [
        ArtifactReadTool(composition.artifact_read_port),
        EnterPlanTool(),
        AskPlanQuestionTool(),
        ExitPlanTool(),
        ReadFileTool(root),
        SearchFilesTool(root),
        TerminalTool(root, composition.terminal_command_port),
        TerminalProcessTool(root, composition.terminal_process_port),
        TerminalMonitorTool(root, composition.terminal_monitor_port),
        EditFileTool(root),
        WriteFileTool(root),
        TodoTool(),
    ]
    if composition.memory_recall_service is not None:
        tools.append(
            MemorySearchTool(
                recall=composition.memory_recall_service,
                graph_id=composition.graph_id,
                read_scopes=composition.memory_read_scopes,
            )
        )
    if composition.memory_query is not None:
        tools.extend(
            (
                MemoryGetTool(
                    memory_query=composition.memory_query,
                    graph_id=composition.graph_id,
                    read_scopes=composition.memory_read_scopes,
                ),
                MemoryExplainTool(
                    memory_query=composition.memory_query,
                    graph_id=composition.graph_id,
                    read_scopes=composition.memory_read_scopes,
                ),
            )
        )
    if composition.memory_proposal_sink is not None:
        tools.extend(
            (
                RememberClaimTool(
                    sink=composition.memory_proposal_sink,
                    runtime_session_id=composition.runtime_session_id,
                ),
                RememberPreferenceTool(
                    sink=composition.memory_proposal_sink,
                    runtime_session_id=composition.runtime_session_id,
                ),
                RememberObservationTool(
                    sink=composition.memory_proposal_sink,
                    runtime_session_id=composition.runtime_session_id,
                ),
                RememberActionBoundaryTool(
                    sink=composition.memory_proposal_sink,
                    runtime_session_id=composition.runtime_session_id,
                ),
                RememberDecisionTool(
                    sink=composition.memory_proposal_sink,
                    runtime_session_id=composition.runtime_session_id,
                ),
            )
        )
    subagent = composition.subagent_control_port
    if isinstance(composition.subagent_exposure, MainSubagentTools):
        assert subagent is not None
        tools.extend(
            (
                SpawnAgentTool(subagent),
                WaitAgentTool(subagent),
                StopAgentTool(subagent),
                ListAgentsTool(subagent),
                CreateAgentTasksTool(subagent),
                WaitAgentTasksTool(subagent),
                StopAgentTaskTool(subagent),
            )
        )
    elif isinstance(composition.subagent_exposure, ChildSubagentTools):
        assert subagent is not None
        tools.extend(
            (
                ReportAgentPhaseTool(
                    subagent,
                    composition.subagent_exposure.subagent_run_id,
                ),
                ReportAgentResultTool(
                    subagent,
                    composition.subagent_exposure.subagent_run_id,
                ),
            )
        )
    _validate_catalog_binding_kinds(tools)
    return tuple(tools)


def _validate_catalog_binding_kinds(tools: list[Tool | AsyncTool]) -> None:
    catalog = {entry.name: entry for entry in builtin_tool_catalog()}
    for tool in tools:
        entry = catalog.get(tool.name)
        if entry is None:
            raise ValueError(f"builtin tool is absent from catalog: {tool.name}")
        if not isinstance(entry.execution_binding_kind, BuiltinToolBindingKind):
            raise ValueError(f"builtin tool binding kind is not closed: {tool.name}")


__all__ = [
    "ChildSubagentTools",
    "MainSubagentTools",
    "NoSubagentTools",
    "RuntimeToolBindingInstallation",
    "RuntimeToolCompositionInput",
    "SubagentToolExposure",
    "build_runtime_tool_binding_installation",
    "build_runtime_tool_composition_input",
    "build_runtime_tool_executor",
]
