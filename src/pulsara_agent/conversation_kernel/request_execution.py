"""Execution of an accepted new-turn request, derived from canonical facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pulsara_agent.conversation_kernel.contracts import TurnStatus


class RequestExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RequestExecution:
    status: RequestExecutionStatus
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.status is RequestExecutionStatus.STOPPED) != bool(self.terminal_reason):
            raise ValueError("only stopped requests have a terminal reason")

    def to_dict(self) -> dict[str, str | None]:
        return {"status": self.status.value, "terminal_reason": self.terminal_reason}


@dataclass(frozen=True, slots=True)
class NewTurnExecutionFacts:
    queue_status: str
    queue_terminal_reason: str | None
    consumed_entry_id: str | None
    turn_status: TurnStatus | None
    turn_terminal_reason: str | None


def project_request_execution(facts: NewTurnExecutionFacts) -> RequestExecution:
    """Project a validated NEW_TURN queue and its exact consumed turn."""
    status = facts.queue_status
    if status == "CONSUMED":
        if not facts.consumed_entry_id or facts.turn_status is None:
            raise ValueError("consumed request has no execution")
        if facts.turn_status is TurnStatus.RUNNING:
            return RequestExecution(RequestExecutionStatus.RUNNING)
        if facts.turn_status is TurnStatus.COMPLETED:
            return RequestExecution(RequestExecutionStatus.COMPLETED)
        if facts.turn_status is TurnStatus.INTERRUPTED:
            return RequestExecution(RequestExecutionStatus.STOPPED, facts.turn_terminal_reason)
    elif facts.consumed_entry_id is not None or facts.turn_status is not None:
        raise ValueError("unconsumed request has an execution")
    elif status == "PENDING":
        return RequestExecution(RequestExecutionStatus.QUEUED)
    elif status in {"CANCELLED", "REJECTED"}:
        return RequestExecution(RequestExecutionStatus.STOPPED, facts.queue_terminal_reason)
    raise ValueError(f"invalid request execution facts: {status}")
