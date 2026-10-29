"""Disposable state-only handoff; the live owners retain execution authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from pulsara_agent.conversation_kernel.todo_runtime import FrozenTodoSnapshot
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint

MAXIMUM_RUNTIME_HANDOFF_PMCA_TOKENS = 3_200
HANDOFF_CONTINUATION = (
    "This snapshot carries forward the work in progress before compaction. "
    "Continue that work using the existing targets."
)
HANDOFF_BUDGET_OMISSION = (
    "Additional active or pending objects are omitted; when available, use "
    "query_processes, query_monitors, query_cells, or query_agents without a "
    "target to discover currently retained objects."
)
_PROJECTION_CONTRACT = "pulsara.compaction-runtime-handoff-projection.v4-active-pmca"
PMCA_KEYS = ("terminal_processes", "terminal_monitors", "code_cells", "agents")


class CompactionRuntimeHandoffBoundError(ValueError):
    """The PMCA envelope cannot fit its specified local input boundary."""


@dataclass(frozen=True, slots=True)
class FrozenTerminalProcessHandoffFact:
    process_id: str
    status: str

    def __post_init__(self) -> None:
        if not self.process_id or self.status != "running":
            raise ValueError("terminal process handoff fact is invalid")


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
        if not self.full_text or self.full_text != self.compact_text:
            raise ValueError(
                "runtime handoff representations must be complete and equal"
            )
        expected = context_fingerprint(
            _PROJECTION_CONTRACT,
            {"full": self.full_text, "compact": self.compact_text},
        )
        if self.source_fingerprint != expected:
            raise ValueError("runtime handoff fingerprint mismatch")


def _body(payload: dict[str, object], *, omitted: bool) -> str:
    text = canonical_json_bytes(payload).decode("utf-8") + "\n" + HANDOFF_CONTINUATION
    return text + "\n" + HANDOFF_BUDGET_OMISSION if omitted else text


def _measure_pmca(body: str) -> int:
    # The compiler owns source encoding and the actual adapter/V3 wire quote.
    from pulsara_agent.model_input.compiler import estimate_runtime_handoff_pmca_tokens

    return estimate_runtime_handoff_pmca_tokens(body)


class CompactionRuntimeHandoffBuilder:
    """Whole-entry admission; callers can stop lazy inventory reads on overflow.

    `measure_pmca` receives the complete no-TODO body, including the actual
    explanation(s). It must quote its encoded runtime observation and framing.
    """

    def __init__(
        self,
        *,
        todo: FrozenTodoSnapshot | None,
        measure_pmca: Callable[[str], int] = _measure_pmca,
    ) -> None:
        self._todo = todo
        self._measure = measure_pmca
        self._payload: dict[str, list[dict[str, object]]] = {
            key: [] for key in PMCA_KEYS
        }
        self._admitted: list[str] = []
        self._has_candidates = False
        self._omitted = False
        if (
            self._measure(_body(self._payload, omitted=True))
            > MAXIMUM_RUNTIME_HANDOFF_PMCA_TOKENS
        ):
            raise CompactionRuntimeHandoffBoundError(
                "runtime handoff PMCA envelope exceeds its token budget"
            )

    def add(self, key: str, item: dict[str, object]) -> bool:
        if self._omitted:
            raise ValueError(
                "runtime handoff enumeration continued after budget omission"
            )
        self._has_candidates = True
        self._payload[key].append(item)
        if (
            self._measure(_body(self._payload, omitted=False))
            <= MAXIMUM_RUNTIME_HANDOFF_PMCA_TOKENS
        ):
            self._admitted.append(key)
            return True
        self._payload[key].pop()
        self._omitted = True
        while (
            self._measure(_body(self._payload, omitted=True))
            > MAXIMUM_RUNTIME_HANDOFF_PMCA_TOKENS
        ):
            self._payload[self._admitted.pop()].pop()
        return False

    def finish(self) -> FrozenCompactionRuntimeHandoff | None:
        todo_items = () if self._todo is None else self._todo.ordered_items
        if not self._has_candidates and not todo_items:
            return None
        todo = self._todo
        payload = dict(self._payload)
        payload["todos"] = tuple(
            {"status": item.status.value, "text": item.text} for item in todo_items
        )
        payload["todo_counts"] = {
            "pending": 0 if todo is None else todo.pending_count,
            "in_progress": 0 if todo is None else todo.in_progress_count,
            "completed": 0 if todo is None else todo.completed_count,
            "total": len(todo_items),
        }
        text = _body(payload, omitted=self._omitted)
        return FrozenCompactionRuntimeHandoff(
            text,
            text,
            context_fingerprint(_PROJECTION_CONTRACT, {"full": text, "compact": text}),
        )


def handoff_process_rows(
    facts: tuple[FrozenTerminalProcessHandoffFact, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "target": {"kind": "PROCESS", "process_id": item.process_id},
            "status": item.status,
        }
        for item in sorted(facts, key=lambda item: item.process_id)
    )


def handoff_monitor_rows(
    facts: tuple[FrozenTerminalMonitorHandoffFact, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "target": {"kind": "MONITOR", "monitor_id": item.monitor_id},
            "process_target": {"kind": "PROCESS", "process_id": item.process_id},
            "state": item.state,
            "pending_observation": item.pending_observation,
        }
        for item in sorted(facts, key=lambda item: item.monitor_id)
    )


def freeze_compaction_runtime_handoff(
    *,
    terminal_processes: tuple[FrozenTerminalProcessHandoffFact, ...],
    terminal_monitors: tuple[FrozenTerminalMonitorHandoffFact, ...],
    todo: FrozenTodoSnapshot | None,
    agent_work_index: Iterable[dict[str, object]] = (),
    code_cells: tuple[dict[str, object], ...] = (),
    measure_pmca: Callable[[str], int] = _measure_pmca,
) -> FrozenCompactionRuntimeHandoff | None:
    """Render frozen facts in deterministic P/M/C/A order without owner I/O.

    Agent rows are already in canonical ID order, allowing the owner to supply
    an iterator without forcing the renderer to enumerate the entire inventory.
    """
    builder = CompactionRuntimeHandoffBuilder(todo=todo, measure_pmca=measure_pmca)
    streams = {
        "terminal_processes": iter(handoff_process_rows(terminal_processes)),
        "terminal_monitors": iter(handoff_monitor_rows(terminal_monitors)),
        "code_cells": iter(
            sorted(code_cells, key=lambda row: row["target"]["cell_id"])
        ),
        "agents": iter(agent_work_index),
    }
    while streams:
        for key in tuple(streams):
            item = next(streams[key], None)
            if item is None:
                del streams[key]
            elif not builder.add(key, item):
                return builder.finish()
    return builder.finish()
