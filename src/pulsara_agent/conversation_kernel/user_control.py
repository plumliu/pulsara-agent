"""Process-local exact user-control command contracts.

These values describe one Host-owned attempt, not a durable operation log.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


CONTROL_ADMISSION_WINDOW_MS = 3_600_000
CONTROL_RESULT_RETENTION_MS = 3_600_000


class UserControlOperation(StrEnum):
    STOP_CODE_MODE_CELL = "STOP_CODE_MODE_CELL"
    STOP_ACTIVE_TURN = "STOP_ACTIVE_TURN"
    STOP_CONTEXT_COMPACTION = "STOP_CONTEXT_COMPACTION"
    TERMINATE_BACKGROUND_PROCESS = "TERMINATE_BACKGROUND_PROCESS"


class UserControlTargetKind(StrEnum):
    CODE_MODE_CELL = "CODE_MODE_CELL"
    ROOT_TURN = "ROOT_TURN"
    MANUAL_COMPACTION = "MANUAL_COMPACTION"
    BACKGROUND_PROCESS = "BACKGROUND_PROCESS"


class UserControlExecution(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


class ControlQueryStatus(StrEnum):
    FOUND = "FOUND"
    RESULT_UNAVAILABLE = "RESULT_UNAVAILABLE"
    OWNER_UNAVAILABLE = "OWNER_UNAVAILABLE"


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
    admission_deadline_ms: int


@dataclass(frozen=True, slots=True)
class RootControlResult:
    status: str | None
    reason: str | None




@dataclass(frozen=True, slots=True)
class CodeModeControlResult:
    cell_id: str
    state: str
    pending_calls: int


@dataclass(frozen=True, slots=True)
class ProcessControlResult:
    disposition: str
    status: str
    exit_code: int | None
    physical_state: str
    group_alive: bool | None


@dataclass(frozen=True, slots=True)
class UserControlOutcome:
    operation: UserControlOperation
    session_id: str
    host_session_id: str
    target: UserControlTarget
    accepted: bool
    execution: UserControlExecution
    root: RootControlResult | None = None
    cell: CodeModeControlResult | None = None
    process: ProcessControlResult | None = None


__all__ = [
    "CONTROL_ADMISSION_WINDOW_MS",
    "CONTROL_RESULT_RETENTION_MS",
    "ControlQueryStatus",
    "CodeModeControlResult",
    "ProcessControlResult",
    "RootControlResult",
    "UserControlExecution",
    "UserControlOperation",
    "UserControlOutcome",
    "UserControlRequest",
    "UserControlTarget",
    "UserControlTargetKind",
]
