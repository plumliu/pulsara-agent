"""Pure bounded projection of same-Host live state across a compaction rebase.

The physical owners freeze their own small public facts.  This module only
validates, orders and renders those facts; it owns no process, monitor, TODO or
subagent state and performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.conversation_kernel.todo_runtime import FrozenTodoSnapshot
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.todo import MAXIMUM_TODO_ITEMS


MAXIMUM_HANDOFF_TERMINAL_PROCESSES = 8
MAXIMUM_HANDOFF_TERMINAL_MONITORS = 8
MAXIMUM_HANDOFF_TEXT_UTF8_BYTES = 512
MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES = 32_768


class CompactionRuntimeHandoffBoundError(ValueError):
    """The current live state cannot be represented without lying."""


@dataclass(frozen=True, slots=True)
class FrozenTerminalProcessHandoffFact:
    process_id: str
    status: str
    command_preview: str
    cwd: str

    def __post_init__(self) -> None:
        if not self.process_id or self.status != "running" or not self.cwd:
            raise ValueError("terminal process handoff fact is invalid")
        _require_text_bound(self.command_preview)
        _require_text_bound(self.cwd)


@dataclass(frozen=True, slots=True)
class FrozenTerminalMonitorHandoffFact:
    monitor_id: str
    process_id: str
    state: str
    pending_observation: bool

    def __post_init__(self) -> None:
        if (
            not self.monitor_id
            or not self.process_id
            or self.state not in {"active", "dormant"}
        ):
            raise ValueError("terminal monitor handoff fact is invalid")


@dataclass(frozen=True, slots=True)
class FrozenCompactionRuntimeHandoff:
    full_text: str = field(repr=False)
    compact_text: str = field(repr=False)
    source_fingerprint: str

    def __post_init__(self) -> None:
        full = self.full_text.encode("utf-8")
        compact = self.compact_text.encode("utf-8")
        if (
            not full
            or not compact
            or len(full) > MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES
            or len(compact) > MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES
        ):
            raise ValueError("runtime handoff representation is out of bounds")
        expected = context_fingerprint(
            "pulsara.compaction-runtime-handoff-projection.v3-complete-todo",
            {"full": self.full_text, "compact": self.compact_text},
        )
        if self.source_fingerprint != expected:
            raise ValueError("runtime handoff fingerprint mismatch")


def freeze_compaction_runtime_handoff(
    *,
    terminal_processes: tuple[FrozenTerminalProcessHandoffFact, ...],
    terminal_monitors: tuple[FrozenTerminalMonitorHandoffFact, ...],
    todo: FrozenTodoSnapshot | None,
    agent_work_index: tuple[dict[str, object], ...] = (),
    code_cells: tuple[dict[str, object], ...] = (),
    maximum_utf8_bytes: int = MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES,
) -> FrozenCompactionRuntimeHandoff | None:
    """Project physical owners into one bounded, disposable context observation."""
    if not 1 <= maximum_utf8_bytes <= MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES:
        raise ValueError("runtime handoff byte budget is invalid")
    if (
        len(terminal_processes) > MAXIMUM_HANDOFF_TERMINAL_PROCESSES
        or len(terminal_monitors) > MAXIMUM_HANDOFF_TERMINAL_MONITORS
    ):
        raise CompactionRuntimeHandoffBoundError(
            "runtime owner exceeded its closed item capacity"
        )
    todo_items = () if todo is None else todo.ordered_items
    if len(todo_items) > MAXIMUM_TODO_ITEMS:
        raise CompactionRuntimeHandoffBoundError(
            "TODO handoff exceeded its closed item capacity"
        )
    if (
        not terminal_processes
        and not terminal_monitors
        and todo is None
        and not agent_work_index
        and not code_cells
    ):
        return None
    payload = {
        "terminal_processes": tuple(
            {
                "target": {"kind": "PROCESS", "process_id": item.process_id},
                "status": item.status,
                "command_preview": item.command_preview,
                "cwd": item.cwd,
            }
            for item in sorted(terminal_processes, key=lambda x: x.process_id)
        ),
        "terminal_monitors": tuple(
            {
                "target": {"kind": "MONITOR", "monitor_id": item.monitor_id},
                "process_target": {"kind": "PROCESS", "process_id": item.process_id},
                "state": item.state,
                "pending_observation": item.pending_observation,
            }
            for item in sorted(terminal_monitors, key=lambda x: x.monitor_id)
        ),
        "todos": tuple(
            {"status": item.status.value, "text": item.text} for item in todo_items
        ),
        "todo_counts": {
            "pending": 0 if todo is None else todo.pending_count,
            "in_progress": 0 if todo is None else todo.in_progress_count,
            "completed": 0 if todo is None else todo.completed_count,
            "total": len(todo_items),
        },
        "agents": [],
        "code_cells": [],
        "omitted": {"agents": len(agent_work_index), "code_cells": len(code_cells)},
    }
    if len(canonical_json_bytes(payload)) > maximum_utf8_bytes:
        raise CompactionRuntimeHandoffBoundError(
            "runtime handoff fixed envelope exceeds its byte budget"
        )
    # Index admission consumes only this observation's byte budget. It never
    # limits logical Work/cell lifetime or the owner's pageable inventory.
    for key, values in (("code_cells", code_cells), ("agents", agent_work_index)):
        for item in values:
            payload[key].append(item)
            payload["omitted"][key] -= 1
            if len(canonical_json_bytes(payload)) > maximum_utf8_bytes:
                payload[key].pop()
                payload["omitted"][key] += 1
                break
    text = canonical_json_bytes(payload).decode("utf-8")
    return FrozenCompactionRuntimeHandoff(
        text,
        text,
        context_fingerprint(
            "pulsara.compaction-runtime-handoff-projection.v3-complete-todo",
            {"full": text, "compact": text},
        ),
    )


def bounded_handoff_preview(value: str) -> str:
    """Return one UTF-8-safe public preview under the exact 512-byte bound."""

    encoded = value.encode("utf-8")
    if len(encoded) <= MAXIMUM_HANDOFF_TEXT_UTF8_BYTES:
        return value
    prefix = encoded[: MAXIMUM_HANDOFF_TEXT_UTF8_BYTES - 3]
    while True:
        try:
            return prefix.decode("utf-8") + "..."
        except UnicodeDecodeError:
            prefix = prefix[:-1]


def _require_text_bound(value: str) -> None:
    if len(value.encode("utf-8")) > MAXIMUM_HANDOFF_TEXT_UTF8_BYTES:
        raise ValueError("runtime handoff text exceeds its byte bound")


__all__ = [
    "CompactionRuntimeHandoffBoundError",
    "FrozenCompactionRuntimeHandoff",
    "FrozenTerminalMonitorHandoffFact",
    "FrozenTerminalProcessHandoffFact",
    "MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES",
    "bounded_handoff_preview",
    "freeze_compaction_runtime_handoff",
]
