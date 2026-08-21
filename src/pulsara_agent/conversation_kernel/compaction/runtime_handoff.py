"""Pure bounded projection of same-Host live state across a compaction rebase.

The physical owners freeze their own small public facts.  This module only
validates, orders and renders those facts; it owns no process, monitor, TODO or
subagent state and performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.conversation_kernel.todo_runtime import (
    FrozenTodoCompactionHandoff,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


MAXIMUM_HANDOFF_TERMINAL_PROCESSES = 8
MAXIMUM_HANDOFF_TERMINAL_MONITORS = 8
MAXIMUM_HANDOFF_TODOS = 64
MAXIMUM_HANDOFF_FLAT_SUBAGENTS = 8
MAXIMUM_HANDOFF_TEXT_UTF8_BYTES = 512
MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES = 32_768


class CompactionRuntimeHandoffBoundError(ValueError):
    """The current live state cannot be represented without lying."""


@dataclass(frozen=True, slots=True)
class FrozenTerminalProcessHandoffFact:
    process_id: str
    terminal_session_id: str
    status: str
    command_preview: str
    cwd: str

    def __post_init__(self) -> None:
        if (
            not self.process_id
            or not self.terminal_session_id
            or self.status != "running"
            or not self.cwd
        ):
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
class FrozenFlatSubagentHandoffFact:
    task_id: str
    status: str
    objective_preview: str

    def __post_init__(self) -> None:
        if not self.task_id or self.status != "ACTIVE":
            raise ValueError("flat subagent handoff fact is invalid")
        _require_text_bound(self.objective_preview)


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
            "pulsara.compaction-runtime-handoff-projection.v1",
            {"full": self.full_text, "compact": self.compact_text},
        )
        if self.source_fingerprint != expected:
            raise ValueError("runtime handoff fingerprint mismatch")


def freeze_compaction_runtime_handoff(
    *,
    terminal_processes: tuple[FrozenTerminalProcessHandoffFact, ...],
    terminal_monitors: tuple[FrozenTerminalMonitorHandoffFact, ...],
    todo: FrozenTodoCompactionHandoff | None,
    flat_subagents: tuple[FrozenFlatSubagentHandoffFact, ...],
    maximum_utf8_bytes: int = MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES,
) -> FrozenCompactionRuntimeHandoff | None:
    """Render one exact live-state snapshot, or ``None`` for a true clear."""

    if not 1 <= maximum_utf8_bytes <= MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES:
        raise ValueError("runtime handoff byte budget is invalid")
    if (
        len(terminal_processes) > MAXIMUM_HANDOFF_TERMINAL_PROCESSES
        or len(terminal_monitors) > MAXIMUM_HANDOFF_TERMINAL_MONITORS
        or len(flat_subagents) > MAXIMUM_HANDOFF_FLAT_SUBAGENTS
    ):
        raise CompactionRuntimeHandoffBoundError(
            "runtime owner exceeded its closed item capacity"
        )
    processes = tuple(sorted(terminal_processes, key=lambda item: item.process_id))
    monitors = tuple(sorted(terminal_monitors, key=lambda item: item.monitor_id))
    subagents = tuple(sorted(flat_subagents, key=lambda item: item.task_id))
    actionable = () if todo is None else todo.actionable_items
    if len(actionable) > MAXIMUM_HANDOFF_TODOS:
        raise CompactionRuntimeHandoffBoundError(
            "TODO handoff exceeded its closed item capacity"
        )
    if not processes and not monitors and todo is None and not subagents:
        return None

    full_payload = _payload(
        processes=processes,
        monitors=monitors,
        todo=todo,
        todo_items=actionable,
        subagents=subagents,
        omitted_todos=0,
    )
    full_bytes = canonical_json_bytes(full_payload)
    if len(full_bytes) <= maximum_utf8_bytes:
        compact_bytes = full_bytes
    else:
        compact_bytes = b""
        # Only TODO bodies may be removed.  Every process, monitor and flat
        # subagent public identity remains actionable in COMPACT.
        for count in range(len(actionable), -1, -1):
            candidate = canonical_json_bytes(
                _payload(
                    processes=processes,
                    monitors=monitors,
                    todo=todo,
                    todo_items=actionable[:count],
                    subagents=subagents,
                    omitted_todos=len(actionable) - count,
                )
            )
            if len(candidate) <= maximum_utf8_bytes:
                if actionable and count == 0:
                    raise CompactionRuntimeHandoffBoundError(
                        "runtime handoff cannot represent one whole actionable TODO"
                    )
                compact_bytes = candidate
                break
        if not compact_bytes:
            raise CompactionRuntimeHandoffBoundError(
                "runtime handoff fixed envelope exceeds its byte budget"
            )
        # FULL is a semantic variant and must itself remain within the sealed
        # 32 KiB contract; oversized owner state cannot be silently renamed as
        # FULL merely because a lossy COMPACT rendering happens to fit.
        if len(full_bytes) > MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES:
            raise CompactionRuntimeHandoffBoundError(
                "runtime handoff FULL representation exceeds its byte budget"
            )

    full_text = full_bytes.decode("utf-8")
    compact_text = compact_bytes.decode("utf-8")
    fingerprint = context_fingerprint(
        "pulsara.compaction-runtime-handoff-projection.v1",
        {"full": full_text, "compact": compact_text},
    )
    return FrozenCompactionRuntimeHandoff(
        full_text=full_text,
        compact_text=compact_text,
        source_fingerprint=fingerprint,
    )


def _payload(
    *,
    processes: tuple[FrozenTerminalProcessHandoffFact, ...],
    monitors: tuple[FrozenTerminalMonitorHandoffFact, ...],
    todo: FrozenTodoCompactionHandoff | None,
    todo_items: tuple[object, ...],
    subagents: tuple[FrozenFlatSubagentHandoffFact, ...],
    omitted_todos: int,
) -> dict[str, object]:
    pending = 0 if todo is None else sum(
        item.status.value == "pending" for item in todo.actionable_items
    )
    in_progress = 0 if todo is None else sum(
        item.status.value == "in_progress" for item in todo.actionable_items
    )
    return {
        "terminal_processes": tuple(
            {
                "process_id": item.process_id,
                "terminal_session_id": item.terminal_session_id,
                "status": item.status,
                "command_preview": item.command_preview,
                "cwd": item.cwd,
            }
            for item in processes
        ),
        "terminal_monitors": tuple(
            {
                "monitor_id": item.monitor_id,
                "process_id": item.process_id,
                "state": item.state,
                "pending_observation": item.pending_observation,
            }
            for item in monitors
        ),
        "todos": tuple(
            {
                "ordinal": item.ordinal,
                "status": item.status.value,
                "text": item.text,
            }
            for item in todo_items
        ),
        "todo_counts": {
            "pending": pending,
            "in_progress": in_progress,
            "completed_omitted": 0 if todo is None else todo.completed_omitted,
        },
        "flat_subagents": tuple(
            {
                "task_id": item.task_id,
                "status": item.status,
                "objective_preview": item.objective_preview,
            }
            for item in subagents
        ),
        "omitted": {
            "terminal_processes": 0,
            "terminal_monitors": 0,
            "todos": omitted_todos,
            "flat_subagents": 0,
        },
    }


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
    "FrozenFlatSubagentHandoffFact",
    "FrozenTerminalMonitorHandoffFact",
    "FrozenTerminalProcessHandoffFact",
    "MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES",
    "bounded_handoff_preview",
    "freeze_compaction_runtime_handoff",
]
