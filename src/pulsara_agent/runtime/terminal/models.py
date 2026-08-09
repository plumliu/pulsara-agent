"""Terminal runtime request/result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.terminal_observation import (
    TerminalProcessObservationSemanticFact,
)

if TYPE_CHECKING:
    from pulsara_agent.event import AgentEvent, EventContext
    from pulsara_agent.ports.event_write import RuntimeThreadEventSettlementReceipt


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


@dataclass(frozen=True, slots=True)
class TerminalExecutionOwner:
    """Process-local lifecycle dependencies kept outside public request data."""

    origin_event_context: EventContext | None = None
    origin_tool_call_id: str | None = None
    origin_runtime_session_id: str | None = None
    origin_run_entry_kind: str | None = None
    output_callback: Callable[[str], None] | None = None
    record_event: Callable[[AgentEvent], RuntimeThreadEventSettlementReceipt] | None = (
        None
    )
    require_completion_notification_reservation: bool = False

    def __post_init__(self) -> None:
        has_event_owner = self.origin_event_context is not None
        if has_event_owner != (self.origin_tool_call_id is not None):
            raise ValueError(
                "terminal execution event context and tool-call identity must coexist"
            )
        if self.record_event is not None and not has_event_owner:
            raise ValueError("terminal event recorder requires exact event authority")


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
    full_output_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    observation_semantic: TerminalProcessObservationSemanticFact | None = None
    completion_event_reference: ContextEventReferenceFact | None = None


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
    owner_host_session_id: str | None = None
    owner_conversation_id: str | None = None

    def to_payload(self, *, include_owner: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if include_owner:
            payload["owner_host_session_id"] = self.owner_host_session_id
            payload["owner_conversation_id"] = self.owner_conversation_id
        return payload


@dataclass(frozen=True, slots=True)
class TerminalProcessLog:
    process: TerminalProcessInfo
    output: str
    truncated: bool
    full_output_text: str | None = None
    observation_semantic: TerminalProcessObservationSemanticFact | None = None
    completion_event_reference: ContextEventReferenceFact | None = None

    def to_payload(self, *, include_owner: bool = False) -> dict[str, Any]:
        return {
            "process": self.process.to_payload(include_owner=include_owner),
            "output": self.output,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class TerminalSessionState:
    session_id: str
    workspace_root: Path
    current_cwd: Path
    backend_type: TerminalBackendType
    backend_metadata: dict[str, Any] = field(default_factory=dict)
    owner_host_session_id: str | None = None
    owner_conversation_id: str | None = None
