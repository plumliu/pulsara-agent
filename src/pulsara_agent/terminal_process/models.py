"""Closed DTOs for the process-local terminal owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pulsara_agent.ports.tool_execution import ToolOutputArtifactCandidate


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
        }


@dataclass(frozen=True, slots=True)
class TerminalProcessLog:
    process: TerminalProcessInfo
    output: str
    truncated: bool
    output_artifact_candidate: ToolOutputArtifactCandidate | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "process": self.process.to_payload(),
            "output": self.output,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class TerminalSessionState:
    session_id: str
    workspace_root: Path
    current_cwd: Path
    backend_type: TerminalBackendType = TerminalBackendType.LOCAL
    owner_host_session_id: str = ""
