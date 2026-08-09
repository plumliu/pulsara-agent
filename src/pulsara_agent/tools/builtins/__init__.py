"""Lazy compatibility facade for built-in tools.

Narrow Stage 2 imports (for example ``tools.builtins.filesystem``) must not
initialize every legacy tool family and its old EventLog dependencies.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ArtifactReadTool": ("pulsara_agent.tools.builtins.artifact", "ArtifactReadTool"),
    "EditFileTool": ("pulsara_agent.tools.builtins.filesystem", "EditFileTool"),
    "ReadFileTool": ("pulsara_agent.tools.builtins.filesystem", "ReadFileTool"),
    "SearchFilesTool": ("pulsara_agent.tools.builtins.filesystem", "SearchFilesTool"),
    "WriteFileTool": ("pulsara_agent.tools.builtins.filesystem", "WriteFileTool"),
    "RememberActionBoundaryTool": (
        "pulsara_agent.tools.builtins.memory",
        "RememberActionBoundaryTool",
    ),
    "RememberClaimTool": (
        "pulsara_agent.tools.builtins.memory",
        "RememberClaimTool",
    ),
    "RememberDecisionTool": (
        "pulsara_agent.tools.builtins.memory",
        "RememberDecisionTool",
    ),
    "RememberObservationTool": (
        "pulsara_agent.tools.builtins.memory",
        "RememberObservationTool",
    ),
    "RememberPreferenceTool": (
        "pulsara_agent.tools.builtins.memory",
        "RememberPreferenceTool",
    ),
    "AskPlanQuestionTool": (
        "pulsara_agent.tools.builtins.plan",
        "AskPlanQuestionTool",
    ),
    "EnterPlanTool": ("pulsara_agent.tools.builtins.plan", "EnterPlanTool"),
    "ExitPlanTool": ("pulsara_agent.tools.builtins.plan", "ExitPlanTool"),
    "CreateAgentTasksTool": (
        "pulsara_agent.tools.builtins.subagent",
        "CreateAgentTasksTool",
    ),
    "ListAgentsTool": ("pulsara_agent.tools.builtins.subagent", "ListAgentsTool"),
    "ReportAgentPhaseTool": (
        "pulsara_agent.tools.builtins.subagent",
        "ReportAgentPhaseTool",
    ),
    "ReportAgentResultTool": (
        "pulsara_agent.tools.builtins.subagent",
        "ReportAgentResultTool",
    ),
    "SpawnAgentTool": ("pulsara_agent.tools.builtins.subagent", "SpawnAgentTool"),
    "StopAgentTool": ("pulsara_agent.tools.builtins.subagent", "StopAgentTool"),
    "StopAgentTaskTool": (
        "pulsara_agent.tools.builtins.subagent",
        "StopAgentTaskTool",
    ),
    "WaitAgentTool": ("pulsara_agent.tools.builtins.subagent", "WaitAgentTool"),
    "WaitAgentTasksTool": (
        "pulsara_agent.tools.builtins.subagent",
        "WaitAgentTasksTool",
    ),
    "TerminalTool": ("pulsara_agent.tools.builtins.terminal", "TerminalTool"),
    "TerminalMonitorTool": (
        "pulsara_agent.tools.builtins.terminal_monitor",
        "TerminalMonitorTool",
    ),
    "TerminalProcessTool": (
        "pulsara_agent.tools.builtins.terminal_process",
        "TerminalProcessTool",
    ),
    "TodoTool": ("pulsara_agent.tools.builtins.todo", "TodoTool"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AskPlanQuestionTool",
    "CreateAgentTasksTool",
    "EditFileTool",
    "EnterPlanTool",
    "ArtifactReadTool",
    "ExitPlanTool",
    "ListAgentsTool",
    "ReportAgentPhaseTool",
    "ReportAgentResultTool",
    "ReadFileTool",
    "RememberActionBoundaryTool",
    "RememberClaimTool",
    "RememberDecisionTool",
    "RememberObservationTool",
    "RememberPreferenceTool",
    "SearchFilesTool",
    "SpawnAgentTool",
    "StopAgentTool",
    "StopAgentTaskTool",
    "TerminalProcessTool",
    "TerminalMonitorTool",
    "TerminalTool",
    "TodoTool",
    "WaitAgentTool",
    "WaitAgentTasksTool",
    "WriteFileTool",
]
