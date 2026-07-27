"""Visible workflow tool specs for Plan mode.

These tools are advertised through the ordinary registry so the provider tool
catalog stays constant. They are executed by AgentRuntime before the permission
gate, not by ToolExecutor. The execute methods are defensive fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult


@dataclass(slots=True)
class EnterPlanTool:
    name: str = "enter_plan"

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        return _workflow_tool_fallback(call)


@dataclass(slots=True)
class AskPlanQuestionTool:
    name: str = "ask_plan_question"

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        return _workflow_tool_fallback(call)


@dataclass(slots=True)
class ExitPlanTool:
    name: str = "exit_plan"

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        return _workflow_tool_fallback(call)


def _workflow_tool_fallback(call: ToolCall) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.ERROR,
        output=(
            "[TOOL_ERROR] Plan workflow tools must be handled by the runtime control plane "
            "before ordinary tool execution."
        ),
    )
