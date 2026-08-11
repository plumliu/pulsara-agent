"""Closed DTOs for the process-local terminal owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pulsara_agent.ports.tool_execution import ToolOutputArtifactCandidate
from pulsara_agent.terminal_process.output import (
    TerminalOutputReadDisposition,
    TerminalOutputSourceCoverage,
)


class TerminalBackendType(StrEnum):
    LOCAL = "local"


class TerminalIOMode(StrEnum):
    PIPE = "pipe"
    PTY = "pty"


class TerminalStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    KILLED = "killed"


class TerminalPhysicalState(StrEnum):
    RUNNING = "RUNNING"
    TERMINALIZING = "TERMINALIZING"
    PHYSICALLY_JOINED = "PHYSICALLY_JOINED"
    PRUNABLE = "PRUNABLE"


@dataclass(frozen=True, slots=True)
class TerminalProcessOrigin:
    """Process-local attribution for live Terminal lifecycle events."""

    turn_id: str
    conversation_scope_kind: str
    scope_subagent_task_id: str | None = None

    def __post_init__(self) -> None:
        if not self.turn_id:
            raise ValueError("terminal process origin turn is required")
        if self.conversation_scope_kind not in {"ROOT", "SUBAGENT_TASK"}:
            raise ValueError("terminal process origin scope is invalid")
        if (self.conversation_scope_kind == "SUBAGENT_TASK") != (
            self.scope_subagent_task_id is not None
        ):
            raise ValueError("terminal process subagent attribution is inconsistent")


@dataclass(frozen=True, slots=True)
class TerminalRequest:
    command: str
    workdir: str | None = None
    yield_time_ms: int = 10_000
    max_output_chars: int = 20_000
    tty: bool = False
    max_lifetime_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("terminal command must not be empty")
        if self.yield_time_ms < 0:
            raise ValueError("terminal yield time must be non-negative")
        if self.max_output_chars <= 0:
            raise ValueError("terminal output bound must be positive")
        if self.max_lifetime_seconds is not None and self.max_lifetime_seconds <= 0:
            raise ValueError("terminal lifetime must be positive")


@dataclass(frozen=True, slots=True)
class TerminalResult:
    status: TerminalStatus
    output: str
    exit_code: int
    cwd: str
    timed_out: bool = False
    truncated: bool = False
    error: str | None = None
    process_id: str | None = None
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None
    output_disposition: TerminalOutputReadDisposition = (
        TerminalOutputReadDisposition.CURRENT_SNAPSHOT
    )
    output_cursor: str | None = None
    retained_from_cursor: str | None = None
    gap_before_output: bool = False
    truncated_by_response_bound: bool = False
    source_coverage: TerminalOutputSourceCoverage = (
        TerminalOutputSourceCoverage.COMPLETE
    )
    shell_diagnostic: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class TerminalProcessInfo:
    process_id: str
    terminal_session_id: str
    command: str
    cwd: str
    backend_type: str
    io_mode: str
    status: str
    exit_code: int | None
    timed_out: bool
    stdin_closed: bool
    started_at_monotonic: float
    ended_at_monotonic: float | None
    duration_seconds: float
    owner_host_session_id: str
    origin: TerminalProcessOrigin
    stream_id: str = ""
    output_revision: int = 0
    output_cursor: str = ""
    retained_from_cursor: str = ""
    physical_state: str = TerminalPhysicalState.RUNNING.value

    def to_payload(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "terminal_session_id": self.terminal_session_id,
            "command": self.command,
            "cwd": self.cwd,
            "backend_type": self.backend_type,
            "io_mode": self.io_mode,
            "status": self.status,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdin_closed": self.stdin_closed,
            "duration_seconds": self.duration_seconds,
            "stream_id": self.stream_id,
            "output_revision": self.output_revision,
            "output_cursor": self.output_cursor,
            "retained_from_cursor": self.retained_from_cursor,
            "physical_state": self.physical_state,
        }


@dataclass(frozen=True, slots=True)
class TerminalProcessLog:
    process: TerminalProcessInfo
    output: str
    truncated: bool
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None
    output_disposition: TerminalOutputReadDisposition = (
        TerminalOutputReadDisposition.CURRENT_SNAPSHOT
    )
    output_cursor: str = ""
    retained_from_cursor: str = ""
    gap_before_output: bool = False
    truncated_by_response_bound: bool = False
    source_coverage: TerminalOutputSourceCoverage = (
        TerminalOutputSourceCoverage.COMPLETE
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "process": self.process.to_payload(),
            "output": self.output,
            "truncated": self.truncated,
            "output_disposition": self.output_disposition.value,
            "output_cursor": self.output_cursor,
            "retained_from_cursor": self.retained_from_cursor,
            "gap_before_output": self.gap_before_output,
            "truncated_by_response_bound": self.truncated_by_response_bound,
            "source_coverage": self.source_coverage.value,
        }


@dataclass(slots=True)
class TerminalSessionState:
    session_id: str
    workspace_root: Path
    current_cwd: Path
    backend_type: TerminalBackendType = TerminalBackendType.LOCAL
    owner_host_session_id: str = ""
