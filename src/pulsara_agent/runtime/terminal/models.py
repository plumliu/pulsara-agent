"""Terminal runtime request/result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class TerminalBackendType(StrEnum):
    LOCAL = "local"


class TerminalStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TerminalRequest:
    command: str
    workdir: str | None = None
    timeout_seconds: int = 30
    max_output_chars: int = 20_000
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TerminalResult:
    status: TerminalStatus
    output: str
    exit_code: int
    cwd: str
    timed_out: bool = False
    truncated: bool = False
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TerminalSessionState:
    session_id: str
    workspace_root: Path
    current_cwd: Path
    backend_type: TerminalBackendType
    backend_metadata: dict[str, Any] = field(default_factory=dict)

