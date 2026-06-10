"""Runtime primitives for Pulsara."""

from pulsara_agent.runtime.agent import (
    AgentRunResult,
    AgentRuntime,
    build_tool_result_error_events,
)
from pulsara_agent.runtime.context import build_llm_context, msg_to_llm_messages
from pulsara_agent.runtime.hooks import (
    ControlHookResult,
    HookContext,
    HookDecision,
    HookDispatchError,
    MemoryHooks,
    NoopMemoryHooks,
    ObserverHookResult,
    RuntimeHookManager,
    ToolResultPersistenceHook,
)
from pulsara_agent.runtime.publisher import RuntimeEventPublisher, RuntimeEventSubscriber, RuntimePublishedEvent
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
    "ControlHookResult",
    "HookContext",
    "HookDecision",
    "HookDispatchError",
    "MemoryHooks",
    "NoopMemoryHooks",
    "ObserverHookResult",
    "RuntimeHookManager",
    "ToolResultPersistenceHook",
    "PermissionDecision",
    "PermissionDecisionKind",
    "PermissionGate",
    "RuntimeEventPublisher",
    "RuntimeEventSubscriber",
    "RuntimePublishedEvent",
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
    "build_tool_result_error_events",
    "msg_to_llm_messages",
]
