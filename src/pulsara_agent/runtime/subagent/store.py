"""Process-local owner for the canonical subagent graph reducer output."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from threading import RLock

from pulsara_agent.event import AgentEvent
from pulsara_agent.runtime.subagent.facts import SubagentGraphState
from pulsara_agent.runtime.subagent.reducer import apply_subagent_event, fold_subagent_graph


class SubagentReducerApplyError(RuntimeError):
    """A committed event stream could not be applied without reconciliation."""


class SubagentGraphStateStore:
    """Thin state owner; all transitions remain in the pure reducer."""

    reducer_id = "subagent_graph:v1"

    def __init__(self, events: Iterable[AgentEvent] = ()) -> None:
        self._lock = RLock()
        self._state = fold_subagent_graph(events)
        self._reconciliation_required = not self._state.consistent

    @property
    def state(self) -> SubagentGraphState:
        with self._lock:
            return self._state

    @property
    def through_sequence(self) -> int:
        return self.state.through_sequence

    @property
    def reconciliation_required(self) -> bool:
        with self._lock:
            return self._reconciliation_required

    def apply_committed(self, events: Sequence[AgentEvent]) -> SubagentGraphState:
        with self._lock:
            state = self._state
            for event in sorted(events, key=_stored_sequence):
                state = apply_subagent_event(state, event)
            self._state = state
            if not state.consistent:
                self._reconciliation_required = True
                raise SubagentReducerApplyError(
                    "Committed subagent graph facts require reconciliation"
                )
            return state

    def rebuild(self, events: Iterable[AgentEvent]) -> SubagentGraphState:
        state = fold_subagent_graph(events)
        with self._lock:
            self._state = state
            self._reconciliation_required = not state.consistent
        if not state.consistent:
            raise SubagentReducerApplyError(
                "Rebuilt subagent graph facts remain inconsistent"
            )
        return state


def _stored_sequence(event: AgentEvent) -> int:
    if event.sequence is None:
        raise ValueError("SubagentGraphStateStore requires committed events")
    return event.sequence
