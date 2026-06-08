"""Runtime primitives for Pulsara."""

from pulsara_agent.runtime.agent import AgentRunResult, AgentRuntime, emit_tool_result_error
from pulsara_agent.runtime.context import build_llm_context, msg_to_llm_messages
from pulsara_agent.runtime.hooks import MemoryHooks, NoopMemoryHooks
from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionGate,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.state import LoopBudget, LoopState, LoopStatus, LoopTransition
from pulsara_agent.runtime.terminal import (
    LocalTerminalBackend,
    TerminalBackend,
    TerminalBackendType,
    TerminalRequest,
    TerminalResult,
    TerminalSession,
    TerminalSessionManager,
    TerminalSessionState,
    TerminalStatus,
)

__all__ = [
    "AgentRunResult",
    "AgentRuntime",
    "AllowAllPermissionGate",
    "LoopBudget",
    "LoopState",
    "LoopStatus",
    "LoopTransition",
    "LocalTerminalBackend",
    "MemoryHooks",
    "NoopMemoryHooks",
    "PermissionDecision",
    "PermissionDecisionKind",
    "PermissionGate",
    "RuntimeSession",
    "TerminalBackend",
    "TerminalBackendType",
    "TerminalRequest",
    "TerminalResult",
    "TerminalSession",
    "TerminalSessionManager",
    "TerminalSessionState",
    "TerminalStatus",
    "build_llm_context",
    "emit_tool_result_error",
    "msg_to_llm_messages",
]
