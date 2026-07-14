"""Pure context-window chain reducer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Iterable, Mapping

from pulsara_agent.event import (
    AgentEvent,
    ContextWindowClosedEvent,
    ContextWindowOpenedEvent,
    RunEndEvent,
)
from pulsara_agent.primitives.long_horizon import (
    ContextWindowFact,
    LongHorizonDiagnosticFact,
    LongHorizonPreparationStage,
)


@dataclass(frozen=True, slots=True)
class ContextWindowChainState:
    run_id: str
    windows: Mapping[str, ContextWindowFact]
    ordered_window_ids: tuple[str, ...]
    active_window_id: str | None
    closed_window_ids: frozenset[str]
    through_sequence: int
    consistent: bool
    diagnostics: tuple[LongHorizonDiagnosticFact, ...]

    @classmethod
    def empty(
        cls,
        *,
        run_id: str,
        through_sequence: int = 0,
    ) -> "ContextWindowChainState":
        return cls(
            run_id=run_id,
            windows=MappingProxyType({}),
            ordered_window_ids=(),
            active_window_id=None,
            closed_window_ids=frozenset(),
            through_sequence=through_sequence,
            consistent=True,
            diagnostics=(),
        )


def fold_context_window_chain(
    events: Iterable[AgentEvent],
    *,
    initial: ContextWindowChainState,
) -> ContextWindowChainState:
    state = initial
    for event in events:
        state = apply_context_window_event(state, event)
    return state


def apply_context_window_event(
    state: ContextWindowChainState,
    event: AgentEvent,
) -> ContextWindowChainState:
    sequence = event.sequence
    if sequence is None or sequence < 1:
        return _inconsistent(
            state,
            code="context_window_event_sequence_invalid",
            message="Stored context-window reducer input requires a positive sequence.",
        )
    if sequence <= state.through_sequence:
        return state
    if sequence != state.through_sequence + 1:
        return _inconsistent(
            replace(state, through_sequence=sequence),
            code="context_window_event_sequence_gap",
            message="Context-window reducer input is not contiguous.",
            attributes=(
                ("actual_sequence", sequence),
                ("expected_sequence", state.through_sequence + 1),
            ),
        )
    state = replace(state, through_sequence=sequence)
    if event.run_id != state.run_id:
        return state
    if isinstance(event, ContextWindowOpenedEvent):
        return _open(state, event)
    if isinstance(event, ContextWindowClosedEvent):
        return _close(state, event)
    if isinstance(event, RunEndEvent) and state.active_window_id is not None:
        return _inconsistent(
            state,
            code="context_window_run_ended_while_open",
            message="RunEnd was committed while a context window remained open.",
        )
    return state


def _open(
    state: ContextWindowChainState,
    event: ContextWindowOpenedEvent,
) -> ContextWindowChainState:
    window = event.window
    if state.active_window_id is not None:
        return _inconsistent(
            state,
            code="context_window_multiple_open",
            message="A second context window opened before the active window closed.",
        )
    if window.window_id in state.windows:
        return _inconsistent(
            state,
            code="context_window_duplicate_id",
            message="A context window ID was reused.",
        )
    expected_generation = len(state.ordered_window_ids) + 1
    expected_previous = state.ordered_window_ids[-1] if state.ordered_window_ids else None
    if (
        window.generation != expected_generation
        or window.previous_window_id != expected_previous
    ):
        return _inconsistent(
            state,
            code="context_window_chain_conflict",
            message="Context window generation or previous identity is inconsistent.",
        )
    if expected_previous is not None and expected_previous not in state.closed_window_ids:
        return _inconsistent(
            state,
            code="context_window_previous_not_closed",
            message="A new window opened before its predecessor was durably closed.",
        )
    windows = dict(state.windows)
    windows[window.window_id] = window
    return replace(
        state,
        windows=MappingProxyType(windows),
        ordered_window_ids=(*state.ordered_window_ids, window.window_id),
        active_window_id=window.window_id,
    )


def _close(
    state: ContextWindowChainState,
    event: ContextWindowClosedEvent,
) -> ContextWindowChainState:
    if state.active_window_id != event.window_id:
        return _inconsistent(
            state,
            code="context_window_close_identity_mismatch",
            message="Context window close does not target the active window.",
        )
    window = state.windows.get(event.window_id)
    if window is None:
        return _inconsistent(
            state,
            code="context_window_missing",
            message="Context window close references an unknown window.",
        )
    if event.id != window.stable_close_event_id:
        return _inconsistent(
            state,
            code="context_window_close_event_id_mismatch",
            message="Context window close did not use its stable event ID.",
        )
    if event.window_generation != window.generation:
        return _inconsistent(
            state,
            code="context_window_close_generation_mismatch",
            message="Context window close generation differs from the active window.",
        )
    return replace(
        state,
        active_window_id=None,
        closed_window_ids=state.closed_window_ids | {event.window_id},
    )


def _inconsistent(
    state: ContextWindowChainState,
    *,
    code: str,
    message: str,
    attributes: tuple[tuple[str, str | int | float | bool | None], ...] = (),
) -> ContextWindowChainState:
    return replace(
        state,
        consistent=False,
        diagnostics=(
            *state.diagnostics,
            LongHorizonDiagnosticFact(
                code=code,
                message=message,
                stage=LongHorizonPreparationStage.STATE_REBUILD,
                attributes=attributes,
            ),
        ),
    )
