"""Process-local exact user-control command contracts.

These values describe one Host-owned attempt.  They are deliberately not a
durable operation log; only the separately typed background-process feedback
may enter canonical history.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


CONTROL_ADMISSION_WINDOW_MS = 3_600_000
CONTROL_RESULT_RETENTION_MS = 3_600_000


class UserControlOperation(StrEnum):
    STOP_ACTIVE_TURN = "STOP_ACTIVE_TURN"
    CANCEL_SUBAGENT_TASK = "CANCEL_SUBAGENT_TASK"
    TERMINATE_BACKGROUND_PROCESS = "TERMINATE_BACKGROUND_PROCESS"


class UserControlTargetKind(StrEnum):
    ROOT_TURN = "ROOT_TURN"
    SUBAGENT_TASK = "SUBAGENT_TASK"
    BACKGROUND_PROCESS = "BACKGROUND_PROCESS"


class UserControlExecution(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


class ControlQueryStatus(StrEnum):
    FOUND = "FOUND"
    RESULT_UNAVAILABLE = "RESULT_UNAVAILABLE"
    OWNER_UNAVAILABLE = "OWNER_UNAVAILABLE"


class FeedbackCanonicalStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class FeedbackInclusionStatus(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    INCLUDED = "INCLUDED"
    TARGET_ENDED_BEFORE_INCLUSION = "TARGET_ENDED_BEFORE_INCLUSION"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class FeedbackOwnerAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class UserControlTarget:
    kind: UserControlTargetKind
    target_id: str


@dataclass(frozen=True, slots=True)
class UserControlRequest:
    operation: UserControlOperation
    command_id: str
    session_id: str
    host_session_id: str
    target: UserControlTarget


@dataclass(frozen=True, slots=True)
class RootControlResult:
    status: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class SubagentControlResult:
    disposition: str
    status: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ProcessControlResult:
    disposition: str
    status: str
    exit_code: int | None
    physical_state: str
    group_alive: bool | None


@dataclass(frozen=True, slots=True)
class MonitorControlResult:
    monitor_id: str
    outcome: str
    in_flight_observation_ids: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class FeedbackTransportResult:
    invocation_attempted: bool = False
    invocation_succeeded: bool | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class UserControlFeedbackState:
    canonical_status: FeedbackCanonicalStatus
    inclusion_status: FeedbackInclusionStatus
    owner_availability: FeedbackOwnerAvailability
    reason: str | None = None
    target_root_turn_id: str | None = None
    entry_id: str | None = None
    context_binding_revision_id: str | None = None
    model_call_index: int | None = None
    transport: FeedbackTransportResult = FeedbackTransportResult()


@dataclass(frozen=True, slots=True)
class UserControlOutcome:
    operation: UserControlOperation
    session_id: str
    host_session_id: str
    target: UserControlTarget
    accepted: bool
    execution: UserControlExecution
    root: RootControlResult | None = None
    subagent: SubagentControlResult | None = None
    process: ProcessControlResult | None = None
    monitor: MonitorControlResult | None = None
    feedback: UserControlFeedbackState | None = None


def parse_control_command_deadline_ms(command_id: str) -> int | None:
    parts = command_id.split(":")
    if len(parts) != 4 or parts[:2] != ["command", "control"]:
        return None
    try:
        deadline = int(parts[2])
    except ValueError:
        return None
    if deadline < 1 or not parts[3]:
        return None
    return deadline


__all__ = [
    "CONTROL_ADMISSION_WINDOW_MS",
    "CONTROL_RESULT_RETENTION_MS",
    "ControlQueryStatus",
    "FeedbackCanonicalStatus",
    "FeedbackInclusionStatus",
    "FeedbackOwnerAvailability",
    "FeedbackTransportResult",
    "MonitorControlResult",
    "ProcessControlResult",
    "RootControlResult",
    "SubagentControlResult",
    "UserControlExecution",
    "UserControlFeedbackState",
    "UserControlOperation",
    "UserControlOutcome",
    "UserControlRequest",
    "UserControlTarget",
    "UserControlTargetKind",
    "parse_control_command_deadline_ms",
]
