"""Loop state for the main agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, TypeAlias
from uuid import uuid4

from pulsara_agent.message import Msg, ToolCallBlock, ToolResultBlock, Usage

if TYPE_CHECKING:
    from pulsara_agent.runtime.recovery import (
        AbortKind,
        InRunRecoveryState,
        StopRequest,
    )

StopReason: TypeAlias = Literal[
    "final",
    "max_turns",
    "model_error",
    "tool_error_budget",
    "plan_interaction_budget",
    "memory_hook_error",
    "waiting_user",
    "aborted",
]


class LoopStatus(StrEnum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    FINISHED = "finished"
    FAILED = "failed"
    ABORTED = "aborted"


class LoopTransition(StrEnum):
    START = "start"
    CONTINUE_AFTER_MODEL = "continue_after_model"
    CONTINUE_AFTER_TOOL = "continue_after_tool"
    CONTINUE_AFTER_RECOVERY = "continue_after_recovery"
    FINISH = "finish"
    FAIL = "fail"
    EXCEED_MAX_ITERS = "exceed_max_iters"
    WAIT_FOR_USER = "wait_for_user"


@dataclass(frozen=True, slots=True)
class LoopBudget:
    max_turns: int = 20
    max_tool_calls: int = 64
    max_consecutive_model_failures: int = 2
    max_consecutive_tool_failures: int = 8
    max_plan_interactions_per_run: int = 16
    max_plan_exit_revisions_per_run: int = 8
    projection_token_budget: int = 2_000
    recall_hard_timeout_ms: int = 1_500
    tool_result_context_chars: int = 8_000


@dataclass(slots=True)
class LoopState:
    """Short-lived state for one active agent loop.

    This is the Working Context Cache. It is not a durable fact source.
    """

    session_id: str
    run_id: str = field(default_factory=lambda: f"run:{uuid4().hex}")
    turn_id: str = field(default_factory=lambda: f"turn:{uuid4().hex}")
    reply_id: str = field(default_factory=lambda: f"reply:{uuid4().hex}")
    turn_index: int = 0
    current_scope: str | None = None
    status: LoopStatus = LoopStatus.RUNNING
    last_transition: LoopTransition = LoopTransition.START
    messages: list[Msg] = field(default_factory=list)
    pending_tool_calls: list[ToolCallBlock] = field(default_factory=list)
    pending_interaction_kind: str | None = None
    pending_interaction_payload: dict[str, Any] = field(default_factory=dict)
    tool_results: list[ToolResultBlock] = field(default_factory=list)
    memory_projection: dict[str, Any] | None = None
    token_usage: Usage = field(default_factory=Usage)
    tool_call_count: int = 0
    consecutive_model_failures: int = 0
    consecutive_tool_failures: int = 0
    in_run_recovery: InRunRecoveryState | None = None
    stop_request: StopRequest | None = None
    abort_kind: AbortKind | None = None
    compacted: bool = False
    stop_reason: StopReason | None = None
    error_message: str | None = None
    finalized: bool = False
    scratchpad: dict[str, Any] = field(default_factory=dict)
    budget: LoopBudget = field(default_factory=LoopBudget)

    def begin_next_turn(self) -> None:
        self.turn_index += 1
        self.turn_id = f"turn:{uuid4().hex}"
        self.reply_id = f"reply:{uuid4().hex}"
        self.pending_tool_calls = []
        self.pending_interaction_kind = None
        self.pending_interaction_payload = {}
        self.tool_results = []

    def transition(self, transition: LoopTransition) -> None:
        self.last_transition = transition
