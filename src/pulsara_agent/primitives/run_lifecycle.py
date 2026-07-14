"""Low-level durable run lifecycle vocabulary."""

from __future__ import annotations

from enum import StrEnum


class RunStopReason(StrEnum):
    FINAL = "final"
    MODEL_ERROR = "model_error"
    TOOL_ERROR_BUDGET = "tool_error_budget"
    PLAN_INTERACTION_BUDGET = "plan_interaction_budget"
    MEMORY_HOOK_ERROR = "memory_hook_error"
    WAITING_USER = "waiting_user"
    ABORTED = "aborted"
    POST_COMMIT_INITIALIZATION_ERROR = "post_commit_initialization_error"
    RUNTIME_PUBLICATION_FAILURE = "runtime_publication_failure"
    INTERACTION_ROUTER_ERROR = "interaction_router_error"
    SUBAGENT_PENDING_UNSUPPORTED = "subagent_pending_unsupported"
    RUNTIME_EXECUTION_ERROR = "runtime_execution_error"
    ROLLOUT_EXHAUSTED = "rollout_exhausted"
    EMERGENCY_HARD_STOP = "emergency_hard_stop"


class RunTerminalizationKind(StrEnum):
    NORMAL = "normal"
    USER_STOP = "user_stop"
    HOST_TEARDOWN = "host_teardown"
    EXECUTION_FAILURE = "execution_failure"
    RECOVERED_INTERRUPTED = "recovered_interrupted"


FAILURE_STOP_REASONS = frozenset(
    {
        RunStopReason.MODEL_ERROR,
        RunStopReason.TOOL_ERROR_BUDGET,
        RunStopReason.PLAN_INTERACTION_BUDGET,
        RunStopReason.MEMORY_HOOK_ERROR,
        RunStopReason.POST_COMMIT_INITIALIZATION_ERROR,
        RunStopReason.RUNTIME_PUBLICATION_FAILURE,
        RunStopReason.INTERACTION_ROUTER_ERROR,
        RunStopReason.SUBAGENT_PENDING_UNSUPPORTED,
        RunStopReason.RUNTIME_EXECUTION_ERROR,
        RunStopReason.ROLLOUT_EXHAUSTED,
        RunStopReason.EMERGENCY_HARD_STOP,
    }
)


__all__ = [
    "FAILURE_STOP_REASONS",
    "RunStopReason",
    "RunTerminalizationKind",
]
