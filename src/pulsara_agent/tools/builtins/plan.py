"""Visible workflow tool specs for Plan mode.

These tools are advertised through the ordinary registry so the provider tool
catalog stays constant. They are executed by AgentRuntime before the permission
gate, not by ToolExecutor. The execute methods are defensive fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import object_schema


@dataclass(slots=True)
class EnterPlanTool:
    name: str = "enter_plan"
    description: str = "Enter Plan workflow, narrowing the session to read-only planning."
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "reason": {
                "type": "string",
                "description": "Brief reason for entering Plan workflow.",
            },
        },
        required=[],
    ))
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        return _workflow_tool_fallback(call)


@dataclass(slots=True)
class AskPlanQuestionTool:
    name: str = "ask_plan_question"
    description: str = "Ask the user a blocking question while in Plan workflow."
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "question": {
                "type": "string",
                "description": "The question to ask the user.",
            },
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "Short user-facing option label.",
                        },
                        "description": {
                            "type": "string",
                            "description": "One concise sentence explaining this option.",
                        },
                        "recommended": {
                            "type": "boolean",
                            "description": "Whether this option is the recommended safe default.",
                        },
                    },
                    "required": ["label"],
                    "additionalProperties": False,
                },
                "description": "Optional structured suggested answers. Provide 2-3 mutually exclusive options when asking for a design decision.",
            },
            "allow_free_text": {
                "type": "boolean",
                "description": "Whether the user may answer with free text.",
            },
            "reason": {
                "type": "string",
                "description": "Why this question is needed to complete the plan.",
            },
        },
        required=["question"],
    ))
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        return _workflow_tool_fallback(call)


@dataclass(slots=True)
class ExitPlanTool:
    name: str = "exit_plan"
    description: str = "Submit a plan draft and ask the user whether to exit Plan workflow."
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "plan": {
                "type": "string",
                "description": "The complete plan draft for user approval.",
            },
            "summary": {
                "type": "string",
                "description": "Short summary of the plan.",
            },
        },
        required=["plan"],
    ))
    is_read_only: bool = False
    is_concurrency_safe: bool = False

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
