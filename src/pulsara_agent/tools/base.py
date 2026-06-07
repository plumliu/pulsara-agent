"""Typed tool interface for Pulsara."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pulsara_agent.message.blocks import ToolResultState

@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    status: ToolResultState
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    is_read_only: bool
    is_concurrency_safe: bool

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        """Execute a tool call."""
