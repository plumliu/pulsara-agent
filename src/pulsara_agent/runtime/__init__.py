"""Runtime primitives for Pulsara."""

from pulsara_agent.runtime.agent import (
    AgentRunResult,
    AgentRuntime,
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
from pulsara_agent.runtime.timeline import RunTimeline, RunTimelineItem, build_run_timeline
from pulsara_agent.runtime.tool_loop import build_tool_result_error_events
from pulsara_agent.runtime.terminal import (
    ExecPolicyDecision,
    ExecPolicyDecisionKind,
    TerminalBackendType,
    TerminalIOMode,
    TerminalRequest,
    TerminalResult,
    TerminalSession,
    TerminalSessionManager,
    TerminalSessionState,
    ProcessRegistry,
    TerminalExecPolicy,
    TerminalProcessState,
    TerminalPolicyPermissionGate,
    TerminalStatus,
)
from pulsara_agent.runtime.wiring import (
    AgentRuntimeWiring,
    RuntimeWiring,
    build_agent_runtime_wiring,
    build_durable_runtime_wiring,
    build_in_memory_runtime_wiring,
)

__all__ = [
    "AgentRunResult",
    "AgentRuntime",
    "AgentRuntimeWiring",
    "AllowAllPermissionGate",
    "ExecPolicyDecision",
    "ExecPolicyDecisionKind",
    "LoopBudget",
    "LoopState",
    "LoopStatus",
    "LoopTransition",
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
    "RuntimeWiring",
    "RunTimeline",
    "RunTimelineItem",
    "TerminalBackendType",
    "TerminalIOMode",
    "TerminalRequest",
    "TerminalResult",
    "TerminalSession",
    "TerminalSessionManager",
    "TerminalSessionState",
    "ProcessRegistry",
    "TerminalExecPolicy",
    "TerminalProcessState",
    "TerminalPolicyPermissionGate",
    "TerminalStatus",
    "build_llm_context",
    "build_agent_runtime_wiring",
    "build_durable_runtime_wiring",
    "build_in_memory_runtime_wiring",
    "build_run_timeline",
    "build_tool_result_error_events",
    "msg_to_llm_messages",
]
