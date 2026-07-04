"""Tool registry, built-in tools, and execution boundary.

This package facade is lazy to avoid import cycles between runtime session,
permission checks, and built-in tool modules.
"""

from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "AsyncTool": ("pulsara_agent.tools.base", "AsyncTool"),
    "Tool": ("pulsara_agent.tools.base", "Tool"),
    "ToolCall": ("pulsara_agent.tools.base", "ToolCall"),
    "ToolExecutionResult": ("pulsara_agent.tools.base", "ToolExecutionResult"),
    "ToolExecutionSuspended": ("pulsara_agent.tools.base", "ToolExecutionSuspended"),
    "ToolResultArtifactCandidate": ("pulsara_agent.tools.base", "ToolResultArtifactCandidate"),
    "ToolRuntimeContext": ("pulsara_agent.tools.base", "ToolRuntimeContext"),
    "ToolExecutor": ("pulsara_agent.tools.executor", "ToolExecutor"),
    "ToolRegistry": ("pulsara_agent.tools.registry", "ToolRegistry"),
    "ArtifactReadTool": ("pulsara_agent.tools.builtins", "ArtifactReadTool"),
    "AskPlanQuestionTool": ("pulsara_agent.tools.builtins", "AskPlanQuestionTool"),
    "EditFileTool": ("pulsara_agent.tools.builtins", "EditFileTool"),
    "EnterPlanTool": ("pulsara_agent.tools.builtins", "EnterPlanTool"),
    "ExitPlanTool": ("pulsara_agent.tools.builtins", "ExitPlanTool"),
    "ReadFileTool": ("pulsara_agent.tools.builtins", "ReadFileTool"),
    "RememberActionBoundaryTool": ("pulsara_agent.tools.builtins", "RememberActionBoundaryTool"),
    "RememberClaimTool": ("pulsara_agent.tools.builtins", "RememberClaimTool"),
    "RememberDecisionTool": ("pulsara_agent.tools.builtins", "RememberDecisionTool"),
    "RememberObservationTool": ("pulsara_agent.tools.builtins", "RememberObservationTool"),
    "RememberPreferenceTool": ("pulsara_agent.tools.builtins", "RememberPreferenceTool"),
    "SearchFilesTool": ("pulsara_agent.tools.builtins", "SearchFilesTool"),
    "TerminalProcessTool": ("pulsara_agent.tools.builtins", "TerminalProcessTool"),
    "TerminalTool": ("pulsara_agent.tools.builtins", "TerminalTool"),
    "TodoTool": ("pulsara_agent.tools.builtins", "TodoTool"),
    "WriteFileTool": ("pulsara_agent.tools.builtins", "WriteFileTool"),
    "build_core_tool_registry": ("pulsara_agent.tools.builtins", "build_core_tool_registry"),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
