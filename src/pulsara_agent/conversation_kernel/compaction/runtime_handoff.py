"""Pure bounded projection of same-Host live state across a compaction rebase.

The physical owners freeze their own small public facts.  This module only
validates, orders and renders those facts; it owns no process, monitor, TODO or
subagent state and performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pulsara_agent.conversation_kernel.todo_runtime import (
    FrozenTodoCompactionHandoff,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


MAXIMUM_HANDOFF_TERMINAL_PROCESSES = 8
MAXIMUM_HANDOFF_TERMINAL_MONITORS = 8
MAXIMUM_HANDOFF_TODOS = 64
MAXIMUM_HANDOFF_SUBAGENT_TASKS = 16
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
class FrozenRootSubagentTaskBoardHandoffFact:
    task_id: str
    task_key: str | None
    label: str | None
    status: str
    objective_preview: str | None
    dependency_total: int
    dependency_remaining: int
    pending_message_count: int
    accepted_at: datetime
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.task_id
            or self.status
            not in {"ACTIVE", "PENDING_START", "WAITING_DEPENDENCY"}
            or self.dependency_total < 0
            or self.dependency_total > 16
            or not 0 <= self.dependency_remaining <= self.dependency_total
            or not 0 <= self.pending_message_count <= 16
            or self.accepted_at.tzinfo is None
        ):
            raise ValueError("subagent task-board handoff fact is invalid")
        for value in (self.task_key, self.label):
            if value is not None and len(value.encode("utf-8")) > 64:
                raise ValueError("subagent task-board display identity is too large")
        if self.objective_preview is not None:
            _require_text_bound(self.objective_preview)
        expected = context_fingerprint(
            "pulsara.root-subagent-task-board-handoff-fact.v1",
            {
                "task_id": self.task_id,
                "task_key": self.task_key,
                "label": self.label,
                "objective_preview": self.objective_preview,
                "status": self.status,
                "dependency_total": self.dependency_total,
                "dependency_remaining": self.dependency_remaining,
                "pending_message_count": self.pending_message_count,
                "accepted_at": self.accepted_at.isoformat(),
            },
        )
        if self.fact_fingerprint != expected:
            raise ValueError("subagent task-board handoff fingerprint mismatch")


def freeze_subagent_task_board_fact(
    *,
    task_id: str,
    task_key: str | None,
    label: str | None,
    objective_preview: str | None,
    status: str,
    dependency_total: int,
    dependency_remaining: int,
    pending_message_count: int,
    accepted_at: datetime,
) -> FrozenRootSubagentTaskBoardHandoffFact:
    payload = {
        "task_id": task_id,
        "task_key": task_key,
        "label": label,
        "objective_preview": objective_preview,
        "status": status,
        "dependency_total": dependency_total,
        "dependency_remaining": dependency_remaining,
        "pending_message_count": pending_message_count,
        "accepted_at": accepted_at.isoformat(),
    }
    return FrozenRootSubagentTaskBoardHandoffFact(
        task_id=task_id,
        task_key=task_key,
        label=label,
        objective_preview=objective_preview,
        status=status,
        dependency_total=dependency_total,
        dependency_remaining=dependency_remaining,
        pending_message_count=pending_message_count,
        accepted_at=accepted_at,
        fact_fingerprint=context_fingerprint(
            "pulsara.root-subagent-task-board-handoff-fact.v1", payload
        ),
    )


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
            "pulsara.compaction-runtime-handoff-projection.v2-task-board",
            {"full": self.full_text, "compact": self.compact_text},
        )
        if self.source_fingerprint != expected:
            raise ValueError("runtime handoff fingerprint mismatch")


def freeze_compaction_runtime_handoff(
    *,
    terminal_processes: tuple[FrozenTerminalProcessHandoffFact, ...],
    terminal_monitors: tuple[FrozenTerminalMonitorHandoffFact, ...],
    todo: FrozenTodoCompactionHandoff | None,
    subagent_tasks: tuple[FrozenRootSubagentTaskBoardHandoffFact, ...],
    subagent_task_totals: tuple[tuple[str, int], ...] | None = None,
    maximum_utf8_bytes: int = MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES,
) -> FrozenCompactionRuntimeHandoff | None:
    """Render one exact live-state snapshot, or ``None`` for a true clear."""

    if not 1 <= maximum_utf8_bytes <= MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES:
        raise ValueError("runtime handoff byte budget is invalid")
    if (
        len(terminal_processes) > MAXIMUM_HANDOFF_TERMINAL_PROCESSES
        or len(terminal_monitors) > MAXIMUM_HANDOFF_TERMINAL_MONITORS
    ):
        raise CompactionRuntimeHandoffBoundError(
            "runtime owner exceeded its closed item capacity"
        )
    processes = tuple(sorted(terminal_processes, key=lambda item: item.process_id))
    monitors = tuple(sorted(terminal_monitors, key=lambda item: item.monitor_id))
    status_order = {"ACTIVE": 0, "PENDING_START": 1, "WAITING_DEPENDENCY": 2}
    ordered_tasks = tuple(
        sorted(
            subagent_tasks,
            key=lambda item: (
                status_order[item.status],
                item.accepted_at,
                item.task_id,
            ),
        )
    )
    totals = dict(
        subagent_task_totals
        or tuple(
            (status, sum(item.status == status for item in ordered_tasks))
            for status in ("ACTIVE", "PENDING_START", "WAITING_DEPENDENCY")
        )
    )
    if (
        set(totals) != {"ACTIVE", "PENDING_START", "WAITING_DEPENDENCY"}
        or any(value < 0 for value in totals.values())
        or any(
            totals[status] < sum(item.status == status for item in ordered_tasks)
            for status in totals
        )
    ):
        raise ValueError("subagent task-board totals are invalid")
    active_tasks = tuple(item for item in ordered_tasks if item.status == "ACTIVE")
    if len(active_tasks) > 4:
        raise CompactionRuntimeHandoffBoundError(
            "runtime owner exceeded active subagent capacity"
        )
    visible_tasks = ordered_tasks[:MAXIMUM_HANDOFF_SUBAGENT_TASKS]
    actionable = () if todo is None else todo.actionable_items
    if len(actionable) > MAXIMUM_HANDOFF_TODOS:
        raise CompactionRuntimeHandoffBoundError(
            "TODO handoff exceeded its closed item capacity"
        )
    if not processes and not monitors and todo is None and not ordered_tasks:
        return None

    full_payload = _payload(
        processes=processes,
        monitors=monitors,
        todo=todo,
        todo_items=actionable,
        subagent_tasks=visible_tasks,
        subagent_task_totals=totals,
        omitted_todos=0,
    )
    full_bytes = canonical_json_bytes(full_payload)
    if len(full_bytes) <= maximum_utf8_bytes:
        compact_bytes = full_bytes
    else:
        compact_bytes = b""
        # ACTIVE tasks are mandatory.  PENDING/WAITING rows and TODO bodies may
        # only be removed whole from their deterministic tails.
        for task_count in range(len(visible_tasks), len(active_tasks) - 1, -1):
            for todo_count in range(len(actionable), -1, -1):
                candidate = canonical_json_bytes(
                    _payload(
                        processes=processes,
                        monitors=monitors,
                        todo=todo,
                        todo_items=actionable[:todo_count],
                        subagent_tasks=visible_tasks[:task_count],
                        subagent_task_totals=totals,
                        omitted_todos=len(actionable) - todo_count,
                    )
                )
                if len(candidate) <= maximum_utf8_bytes:
                    compact_bytes = candidate
                    break
            if compact_bytes:
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
        "pulsara.compaction-runtime-handoff-projection.v2-task-board",
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
    subagent_tasks: tuple[FrozenRootSubagentTaskBoardHandoffFact, ...],
    subagent_task_totals: dict[str, int],
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
        "subagent_tasks": tuple(
            {
                "task_id": item.task_id,
                "task_key": item.task_key,
                "label": item.label,
                "status": item.status,
                "objective_preview": item.objective_preview,
                "dependency_total": item.dependency_total,
                "dependency_remaining": item.dependency_remaining,
                "pending_message_count": item.pending_message_count,
            }
            for item in subagent_tasks
        ),
        "subagent_task_counts": {
            status: {
                "total": subagent_task_totals[status],
                "omitted": subagent_task_totals[status]
                - sum(item.status == status for item in subagent_tasks),
            }
            for status in ("ACTIVE", "PENDING_START", "WAITING_DEPENDENCY")
        },
        "omitted": {
            "terminal_processes": 0,
            "terminal_monitors": 0,
            "todos": omitted_todos,
            "subagent_tasks": sum(subagent_task_totals.values())
            - len(subagent_tasks),
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
    "FrozenRootSubagentTaskBoardHandoffFact",
    "FrozenTerminalMonitorHandoffFact",
    "FrozenTerminalProcessHandoffFact",
    "MAXIMUM_RUNTIME_HANDOFF_UTF8_BYTES",
    "bounded_handoff_preview",
    "freeze_subagent_task_board_fact",
    "freeze_compaction_runtime_handoff",
]
