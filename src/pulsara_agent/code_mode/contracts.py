"""Process-local Code Mode boundary; business execution remains in the kernel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import asyncio
from typing import Awaitable, Callable, Literal, Mapping, Protocol


from .targets import cell_target, cell_target_schema


CellParticipationKind = Literal["query", "wait", "message", "stop"]
CellParticipationReference = tuple[str, str | None, CellParticipationKind]


class CellParticipationObserver(Protocol):
    def observe_agent_participation(
        self,
        cell_id: str,
        references: tuple[CellParticipationReference, ...],
    ) -> None: ...

    def mark_cell_participation_incomplete(self, cell_id: str) -> None: ...


def _object_schema(
    properties: dict[str, object], required: list[str]
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _yield_schema() -> dict[str, object]:
    return {"type": "integer", "minimum": 0, "maximum": 30_000, "default": 1_000}


def exec_cell_parameters_schema() -> dict[str, object]:
    return {
        "oneOf": [
            _object_schema(
                {
                    "target": {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "PATH"},
                            "path": {"type": "string", "minLength": 1},
                        },
                        "required": ["kind", "path"],
                        "additionalProperties": False,
                    }
                },
                ["target"],
            ),
            _object_schema({"code": {"type": "string"}}, ["code"]),
        ]
    }


def query_cells_parameters_schema() -> dict[str, object]:
    return _object_schema({"target": cell_target_schema()}, [])


def manage_cell_parameters_schema() -> dict[str, object]:
    return {
        "oneOf": [
            _object_schema(
                {
                    "action": {"type": "string", "const": "wait"},
                    "target": cell_target_schema(),
                    "yield_time_ms": _yield_schema(),
                },
                ["action", "target"],
            ),
            _object_schema(
                {
                    "action": {"type": "string", "const": "stop"},
                    "target": cell_target_schema(),
                },
                ["action", "target"],
            ),
        ]
    }


@dataclass(frozen=True, slots=True)
class CodeModeLimits:
    """Current working-set profile measured by the hard-cut resource experiment.

    Data accounts for source, transport, pending calls and unaccepted output;
    the separate 64 MiB QuickJS heap bounds each of four active VMs. These are
    simultaneous physical reservations, never total output or lifetime caps.
    """

    segment_ms: int = 5_000
    watchdog_ms: int = 10_000
    heap_bytes: int = 64 * 1024 * 1024
    stack_bytes: int = 256 * 1024
    # Unified delivery contract §9.1: one Cell and four-VM Host working data.
    cell_bytes: int = 32 * 1024 * 1024
    host_bytes: int = 128 * 1024 * 1024
    vm_slots: int = 4
    # Independent scratch for transport and bounded failure diagnostics.
    control_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.segment_ms,
                self.watchdog_ms,
                self.heap_bytes,
                self.stack_bytes,
                self.cell_bytes,
                self.host_bytes,
                self.vm_slots,
                self.control_bytes,
            )
        ):
            raise ValueError("Code Mode physical limits must be positive integers")
        if not (
            self.segment_ms < self.watchdog_ms
            and self.stack_bytes <= 512 * 1024
            and self.cell_bytes <= self.host_bytes
        ):
            raise ValueError("Inconsistent Code Mode physical profile")


@dataclass(frozen=True, slots=True)
class CodeModeInvocation:
    cell_id: str
    sequence: int
    tool_name: str
    arguments: dict[str, object]
    snapshot: object
    source_exec_attempt_id: str
    agent_message_cursors: dict[str, int] | None = None
    cancelled: asyncio.Event | None = None


@dataclass(frozen=True, slots=True)
class CodeModeToolResult:
    """The same accepted public business value supplied by the tool executor."""

    value: object = None
    state: str = "SUCCESS"
    result_id: str | None = None
    result_entry_id: str | None = None
    has_result: bool = True
    error_code: str | None = None
    error_message: str | None = None
    delivery_succeeded: Callable[[], Awaitable[None]] | None = None
    # Accepted-result owner can read a large/late result without another result store.
    read_undelivered: Callable[[], Awaitable[Mapping[str, object]]] | None = None


CodeModeInvoke = Callable[[CodeModeInvocation], Awaitable[CodeModeToolResult]]
CodeModePrepareBatch = Callable[[list[Mapping[str, object]]], Mapping[str, object]]
CellState = Literal["queued", "running", "completed", "failed", "terminated"]


class CodeModeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CodeModeOutputLease:
    """One immutable local record retained until its canonical queue admission settles."""

    cell_id: str
    source_turn_id: str
    source_exec_attempt_id: str
    record_sequence: int
    record: Mapping[str, object]
    snapshot: object
    occurred_at: datetime
    content: object | None = None


@dataclass(frozen=True, slots=True)
class CodeModeCellSnapshot:
    """One live observation, without reserving output or retaining a Cell."""

    cell_id: str
    state: CellState
    has_unread_output: bool
    pending_calls: int
    error: Mapping[str, object] | None
    source_exec_attempt_id: str

    def public_value(self) -> dict[str, object]:
        result: dict[str, object] = {
            "target": cell_target(self.cell_id),
            "state": self.state,
            "has_unread_output": self.has_unread_output,
            "pending_calls": self.pending_calls,
        }
        if self.error is not None:
            result["error"] = dict(self.error)
        return result
