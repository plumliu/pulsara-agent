"""In-memory approval resume models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

from pulsara_agent.message import ToolCallBlock, ToolCallState
from pulsara_agent.runtime.state import LoopState, LoopStatus


@dataclass(frozen=True, slots=True)
class ToolApprovalDecision:
    tool_call_id: str
    confirmed: bool
    rules: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    approval_id: str
    decisions: tuple[ToolApprovalDecision, ...]


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    host_session_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_calls: tuple[ToolCallBlock, ...]
    suggested_rules: tuple[dict, ...] = ()
    created_at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "host_session_id": self.host_session_id,
            "runtime_session_id": self.runtime_session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "reply_id": self.reply_id,
            "tool_calls": [call.model_dump(mode="json") for call in self.tool_calls],
            "suggested_rules": list(self.suggested_rules),
            "created_at": self.created_at,
        }


def pending_approval_from_state(state: LoopState, host_session_id: str) -> PendingApproval:
    if state.status is not LoopStatus.WAITING_USER:
        raise ValueError("cannot create pending approval from a non-waiting state")
    if not state.pending_tool_calls:
        raise ValueError("cannot create pending approval without pending tool calls")
    tool_calls = tuple(call.model_copy(deep=True) for call in state.pending_tool_calls)
    if any(call.state is not ToolCallState.ASKING for call in tool_calls):
        raise ValueError("pending approval requires ASKING tool calls")
    suggested_rules: list[dict] = []
    for call in tool_calls:
        suggested_rules.extend(call.suggested_rules)
    return PendingApproval(
        approval_id=f"approval:{uuid4().hex}",
        host_session_id=host_session_id,
        runtime_session_id=state.session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        tool_calls=tool_calls,
        suggested_rules=tuple(suggested_rules),
    )
