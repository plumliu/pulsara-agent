"""Typed tool interface for Pulsara."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pulsara_agent.event import EventContext
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
    artifact_candidates: tuple["ToolResultArtifactCandidate", ...] = ()


@dataclass(frozen=True, slots=True)
class ToolExecutionSuspended:
    tool_call_id: str
    tool_name: str
    interaction_kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolRuntimeContext:
    runtime_session_id: str
    event_context: EventContext


@dataclass(frozen=True, slots=True)
class ToolResultArtifactCandidate:
    role: str
    media_type: str
    text: str | None = None
    data: bytes | None = None
    redacted: bool = True
    stored_complete: bool = True
    loss_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.text is None) == (self.data is None):
            raise ValueError("ToolResultArtifactCandidate requires exactly one of text or data")


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    # True iff the tool causes no side effect on the user's workspace, external
    # systems, terminal processes, or durable memory. Mutating agent-local
    # ephemeral state (e.g. an in-memory todo list) still counts as read-only.
    # The read-only permission mode allows exactly the tools with this True
    # (see PERMISSION_POLICY_CONTRACT §3 and READ_ONLY_ALLOWED_TOOL_NAMES).
    is_read_only: bool
    # True iff the tool can run concurrently with others in a batch (no shared
    # mutable state / ordering dependency). Independent of is_read_only.
    is_concurrency_safe: bool

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        """Execute a tool call."""


class AsyncTool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    is_read_only: bool
    is_concurrency_safe: bool

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        """Execute a tool call on the agent runtime event loop."""
