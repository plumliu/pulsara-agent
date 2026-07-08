"""Runtime primitives for Pulsara.

This package facade is intentionally lazy. Several runtime submodules depend on
``pulsara_agent.tools`` and memory modules, while tool built-ins also import
runtime submodules. Eager re-exports here therefore create import cycles during
normal submodule imports such as ``pulsara_agent.runtime.session``.
"""

from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "AgentRunResult": ("pulsara_agent.runtime.agent", "AgentRunResult"),
    "AgentRuntime": ("pulsara_agent.runtime.agent", "AgentRuntime"),
    "AgentRuntimeWiring": ("pulsara_agent.runtime.wiring", "AgentRuntimeWiring"),
    "AllowAllPermissionGate": ("pulsara_agent.runtime.permission", "AllowAllPermissionGate"),
    "ApprovalResolution": ("pulsara_agent.runtime.approval", "ApprovalResolution"),
    "ApprovalPolicy": ("pulsara_agent.runtime.permission", "ApprovalPolicy"),
    "AbortKind": ("pulsara_agent.runtime.recovery", "AbortKind"),
    "EffectivePermissionPolicy": ("pulsara_agent.runtime.permission", "EffectivePermissionPolicy"),
    "ExecPolicyDecision": ("pulsara_agent.runtime.terminal", "ExecPolicyDecision"),
    "ExecPolicyDecisionKind": ("pulsara_agent.runtime.terminal", "ExecPolicyDecisionKind"),
    "GuidanceKind": ("pulsara_agent.runtime.recovery", "GuidanceKind"),
    "InRunRecoveryCause": ("pulsara_agent.runtime.recovery", "InRunRecoveryCause"),
    "InRunRecoveryState": ("pulsara_agent.runtime.recovery", "InRunRecoveryState"),
    "LoopBudget": ("pulsara_agent.runtime.state", "LoopBudget"),
    "LoopState": ("pulsara_agent.runtime.state", "LoopState"),
    "LoopStatus": ("pulsara_agent.runtime.state", "LoopStatus"),
    "LoopTransition": ("pulsara_agent.runtime.state", "LoopTransition"),
    "ControlHookResult": ("pulsara_agent.runtime.hooks", "ControlHookResult"),
    "HookContext": ("pulsara_agent.runtime.hooks", "HookContext"),
    "HookDecision": ("pulsara_agent.runtime.hooks", "HookDecision"),
    "HookDispatchError": ("pulsara_agent.runtime.hooks", "HookDispatchError"),
    "MemoryHooks": ("pulsara_agent.runtime.hooks", "MemoryHooks"),
    "NoopMemoryHooks": ("pulsara_agent.runtime.hooks", "NoopMemoryHooks"),
    "ObserverHookResult": ("pulsara_agent.runtime.hooks", "ObserverHookResult"),
    "RuntimeHookManager": ("pulsara_agent.runtime.hooks", "RuntimeHookManager"),
    "ToolResultPersistenceHook": ("pulsara_agent.runtime.hooks", "ToolResultPersistenceHook"),
    "PermissionDecision": ("pulsara_agent.runtime.permission", "PermissionDecision"),
    "PermissionDecisionKind": ("pulsara_agent.runtime.permission", "PermissionDecisionKind"),
    "PermissionGate": ("pulsara_agent.runtime.permission", "PermissionGate"),
    "PermissionProfile": ("pulsara_agent.runtime.permission", "PermissionProfile"),
    "PendingApproval": ("pulsara_agent.runtime.approval", "PendingApproval"),
    "PendingInteraction": ("pulsara_agent.runtime.plan", "PendingInteraction"),
    "PendingMcpElicitation": ("pulsara_agent.runtime.plan", "PendingMcpElicitation"),
    "PendingPlanInteraction": ("pulsara_agent.runtime.plan", "PendingPlanInteraction"),
    "McpElicitationResolution": ("pulsara_agent.runtime.plan", "McpElicitationResolution"),
    "PlanExitResolution": ("pulsara_agent.runtime.plan", "PlanExitResolution"),
    "PlanInteractionResolution": ("pulsara_agent.runtime.plan", "PlanInteractionResolution"),
    "PlanQuestionOption": ("pulsara_agent.runtime.plan", "PlanQuestionOption"),
    "PlanQuestionResolution": ("pulsara_agent.runtime.plan", "PlanQuestionResolution"),
    "PlanWorkflowState": ("pulsara_agent.runtime.plan", "PlanWorkflowState"),
    "normalize_plan_question_options": ("pulsara_agent.runtime.plan", "normalize_plan_question_options"),
    "RecoveryProjection": ("pulsara_agent.runtime.recovery", "RecoveryProjection"),
    "StopRequest": ("pulsara_agent.runtime.recovery", "StopRequest"),
    "reduce_plan_workflow_state": ("pulsara_agent.runtime.plan", "reduce_plan_workflow_state"),
    "PolicyPermissionGate": ("pulsara_agent.runtime.permission", "PolicyPermissionGate"),
    "RuntimeEventPublisher": ("pulsara_agent.runtime.publisher", "RuntimeEventPublisher"),
    "RuntimeEventSubscriber": ("pulsara_agent.runtime.publisher", "RuntimeEventSubscriber"),
    "RuntimePublishedEvent": ("pulsara_agent.runtime.publisher", "RuntimePublishedEvent"),
    "RuntimeSession": ("pulsara_agent.runtime.session", "RuntimeSession"),
    "RuntimeWiring": ("pulsara_agent.runtime.wiring", "RuntimeWiring"),
    "SubagentBudget": ("pulsara_agent.runtime.subagent", "SubagentBudget"),
    "SubagentContextPolicy": ("pulsara_agent.runtime.subagent", "SubagentContextPolicy"),
    "SubagentRuntime": ("pulsara_agent.runtime.subagent", "SubagentRuntime"),
    "RunTimeline": ("pulsara_agent.runtime.timeline", "RunTimeline"),
    "RunTimelineItem": ("pulsara_agent.runtime.timeline", "RunTimelineItem"),
    "TerminalBackendType": ("pulsara_agent.runtime.terminal", "TerminalBackendType"),
    "TerminalAccess": ("pulsara_agent.runtime.permission", "TerminalAccess"),
    "TerminalIOMode": ("pulsara_agent.runtime.terminal", "TerminalIOMode"),
    "TerminalRequest": ("pulsara_agent.runtime.terminal", "TerminalRequest"),
    "TerminalResult": ("pulsara_agent.runtime.terminal", "TerminalResult"),
    "TerminalSession": ("pulsara_agent.runtime.terminal", "TerminalSession"),
    "TerminalSessionManager": ("pulsara_agent.runtime.terminal", "TerminalSessionManager"),
    "TerminalSessionState": ("pulsara_agent.runtime.terminal", "TerminalSessionState"),
    "ProcessRegistry": ("pulsara_agent.runtime.terminal", "ProcessRegistry"),
    "TerminalExecPolicy": ("pulsara_agent.runtime.terminal", "TerminalExecPolicy"),
    "TerminalProcessState": ("pulsara_agent.runtime.terminal", "TerminalProcessState"),
    "TerminalStatus": ("pulsara_agent.runtime.terminal", "TerminalStatus"),
    "ToolApprovalDecision": ("pulsara_agent.runtime.approval", "ToolApprovalDecision"),
    "ToolSeverity": ("pulsara_agent.runtime.recovery", "ToolSeverity"),
    "UnfinishedState": ("pulsara_agent.runtime.recovery", "UnfinishedState"),
    "UnfinishedToolCall": ("pulsara_agent.runtime.recovery", "UnfinishedToolCall"),
    "build_llm_context": ("pulsara_agent.runtime.context", "build_llm_context"),
    "build_agent_runtime_wiring": ("pulsara_agent.runtime.wiring", "build_agent_runtime_wiring"),
    "build_durable_runtime_wiring": ("pulsara_agent.runtime.wiring", "build_durable_runtime_wiring"),
    "build_in_memory_runtime_wiring": ("pulsara_agent.runtime.wiring", "build_in_memory_runtime_wiring"),
    "build_run_timeline": ("pulsara_agent.runtime.timeline", "build_run_timeline"),
    "build_tool_result_error_events": ("pulsara_agent.runtime.tool_loop", "build_tool_result_error_events"),
    "classify_unfinished_tool_calls": ("pulsara_agent.runtime.recovery", "classify_unfinished_tool_calls"),
    "default_permission_policy": ("pulsara_agent.runtime.permission", "default_permission_policy"),
    "msg_to_llm_messages": ("pulsara_agent.runtime.context", "msg_to_llm_messages"),
    "project_recovery_from_events": ("pulsara_agent.runtime.recovery", "project_recovery_from_events"),
    "project_recovery_from_state": ("pulsara_agent.runtime.recovery", "project_recovery_from_state"),
    "render_recovery_text": ("pulsara_agent.runtime.recovery", "render_recovery_text"),
    "render_unfinished_summary": ("pulsara_agent.runtime.recovery", "render_unfinished_summary"),
    "resolve_permission_policy": ("pulsara_agent.runtime.permission", "resolve_permission_policy"),
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
