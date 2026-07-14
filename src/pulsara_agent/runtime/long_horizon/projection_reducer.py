"""Pure reducer for durable context-window projection generations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pulsara_agent.event import (
    AgentEvent,
    ContextProjectionRewritePageEvent,
    ContextWindowClosedEvent,
    ContextWindowOpenedEvent,
    RunEndEvent,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    ContextWindowFact,
    ContextWindowProjectionState,
    ObservationRollupFact,
    ToolObservationProjectionFact,
)


class ContextProjectionReducerError(RuntimeError):
    """Durable projection facts violate the frozen generation contract."""


@dataclass(frozen=True, slots=True)
class _PendingRewrite:
    rewrite_id: str
    pages: tuple[ContextProjectionRewritePageEvent, ...]


class ContextWindowProjectionReducer:
    """Memoized pure fold; the EventLog remains the only authority."""

    def __init__(self) -> None:
        self._windows: dict[str, ContextWindowFact] = {}
        self._states: dict[str, ContextWindowProjectionState] = {}
        self._active_window_by_run: dict[str, str] = {}
        self._pending: _PendingRewrite | None = None

    def clone(self) -> "ContextWindowProjectionReducer":
        clone = ContextWindowProjectionReducer()
        clone._windows = dict(self._windows)
        clone._states = dict(self._states)
        clone._active_window_by_run = dict(self._active_window_by_run)
        clone._pending = self._pending
        return clone

    def state(self, window_id: str) -> ContextWindowProjectionState | None:
        return self._states.get(window_id)

    def active_state(self, run_id: str) -> ContextWindowProjectionState | None:
        window_id = self._active_window_by_run.get(run_id)
        return self._states.get(window_id) if window_id is not None else None

    def apply_committed(self, events: Iterable[AgentEvent]) -> None:
        for event in events:
            if self._pending is not None and not (
                isinstance(event, ContextProjectionRewritePageEvent)
                and event.rewrite_id == self._pending.rewrite_id
            ):
                raise ContextProjectionReducerError(
                    "projection rewrite pages are not one contiguous committed batch"
                )
            if isinstance(event, ContextWindowOpenedEvent):
                self._open(event)
            elif isinstance(event, ContextProjectionRewritePageEvent):
                self._append_page(event)
            elif isinstance(event, ContextWindowClosedEvent):
                self._close(event)
            elif isinstance(event, RunEndEvent):
                self._retire_run(event.run_id)
        if self._pending is not None:
            raise ContextProjectionReducerError(
                "projection rewrite committed without all declared pages"
            )

    def _open(self, event: ContextWindowOpenedEvent) -> None:
        window = event.window
        if window.window_id in self._states:
            raise ContextProjectionReducerError("projection window opened twice")
        if event.run_id in self._active_window_by_run:
            raise ContextProjectionReducerError(
                "projection window opened while another window is active"
            )
        if window.initial_projection_unit_count != 0:
            raise ContextProjectionReducerError(
                "L0B window baseline cannot contain implicit projection units"
            )
        state = build_projection_state(
            window=window,
            projection_generation=0,
            through_sequence=window.source_through_sequence_at_open,
            unit_projections=(),
            rollups=(),
        )
        if state.state_semantic_fingerprint != (
            window.initial_projection_state_fingerprint
        ):
            raise ContextProjectionReducerError(
                "window projection baseline fingerprint mismatch"
            )
        self._states[window.window_id] = state
        self._windows[window.window_id] = window
        self._active_window_by_run[event.run_id] = window.window_id

    def _append_page(self, event: ContextProjectionRewritePageEvent) -> None:
        active_window = self._active_window_by_run.get(event.run_id)
        if active_window != event.window_id:
            raise ContextProjectionReducerError(
                "projection rewrite does not target the active window"
            )
        if self._pending is None:
            if event.page_index != 0:
                raise ContextProjectionReducerError(
                    "projection rewrite must start at page zero"
                )
            pending = _PendingRewrite(rewrite_id=event.rewrite_id, pages=(event,))
        else:
            pages = self._pending.pages
            if event.page_index != len(pages):
                raise ContextProjectionReducerError(
                    "projection rewrite page indices are not contiguous"
                )
            pending = _PendingRewrite(
                rewrite_id=event.rewrite_id,
                pages=(*pages, event),
            )
        self._pending = pending
        if len(pending.pages) == event.page_count:
            self._apply_complete_rewrite(pending.pages)
            self._pending = None

    def _apply_complete_rewrite(
        self,
        pages: tuple[ContextProjectionRewritePageEvent, ...],
    ) -> None:
        first = pages[0]
        if len(pages) != first.page_count:
            raise ContextProjectionReducerError("projection rewrite page count mismatch")
        shared = (
            first.run_id,
            first.window_id,
            first.from_projection_generation,
            first.to_projection_generation,
            first.source_through_sequence,
            first.page_count,
            first.plan_fingerprint,
            first.final_state_fingerprint,
            first.reason_code,
        )
        if any(
            (
                page.run_id,
                page.window_id,
                page.from_projection_generation,
                page.to_projection_generation,
                page.source_through_sequence,
                page.page_count,
                page.plan_fingerprint,
                page.final_state_fingerprint,
                page.reason_code,
            )
            != shared
            for page in pages
        ):
            raise ContextProjectionReducerError(
                "projection rewrite pages disagree on shared facts"
            )
        current = self._states.get(first.window_id)
        if current is None:
            raise ContextProjectionReducerError("projection rewrite window is unknown")
        if current.projection_generation != first.from_projection_generation:
            raise ContextProjectionReducerError(
                "projection rewrite lost its generation CAS"
            )
        if first.source_through_sequence < current.through_sequence:
            raise ContextProjectionReducerError(
                "projection rewrite source high-water moved backwards"
            )
        entries = tuple(entry for page in pages for entry in page.entries)
        entry_ids = tuple(entry.unit_id for entry in entries)
        if len(entry_ids) != len(set(entry_ids)):
            raise ContextProjectionReducerError(
                "projection rewrite contains duplicate unit entries"
            )
        current_by_id = {item.unit_id: item for item in current.unit_projections}
        by_id = {
            item.unit_id: advance_projection_generation(
                item,
                projection_generation=first.to_projection_generation,
            )
            for item in current.unit_projections
        }
        for entry in entries:
            previous = current_by_id.get(entry.unit_id)
            if previous is None:
                if entry.from_representation is not None:
                    raise ContextProjectionReducerError(
                        "new projection unit declares an old representation"
                    )
            elif entry.from_representation is not previous.representation:
                raise ContextProjectionReducerError(
                    "projection rewrite from-representation mismatch"
                )
            if (
                entry.to_projection.tool_result_sequence
                > first.source_through_sequence
            ):
                raise ContextProjectionReducerError(
                    "projection rewrite includes a result beyond its source high-water"
                )
            by_id[entry.unit_id] = entry.to_projection
        existing_order = tuple(item.unit_id for item in current.unit_projections)
        new_ids = tuple(
            sorted(
                (unit_id for unit_id in entry_ids if unit_id not in existing_order),
                key=lambda unit_id: (
                    by_id[unit_id].tool_result_sequence,
                    unit_id,
                ),
            )
        )
        ordered = tuple(by_id[unit_id] for unit_id in (*existing_order, *new_ids))
        rollups = tuple(rollup for page in pages for rollup in page.rollups)
        rollup_ids = tuple(item.rollup_id for item in rollups)
        if len(rollup_ids) != len(set(rollup_ids)):
            raise ContextProjectionReducerError(
                "projection rewrite contains duplicate rollups"
            )
        window = self._windows[first.window_id]
        state = build_projection_state(
            window=window,
            projection_generation=first.to_projection_generation,
            through_sequence=first.source_through_sequence,
            unit_projections=ordered,
            rollups=rollups or current.rollups,
        )
        if state.state_semantic_fingerprint != first.final_state_fingerprint:
            raise ContextProjectionReducerError(
                "projection rewrite final state fingerprint mismatch"
            )
        self._states[first.window_id] = state

    def _close(self, event: ContextWindowClosedEvent) -> None:
        if self._pending is not None:
            raise ContextProjectionReducerError(
                "window closed with an incomplete projection rewrite"
            )
        window_id = self._active_window_by_run.get(event.run_id)
        if window_id != event.window_id:
            raise ContextProjectionReducerError(
                "projection close does not target the active window"
            )
        state = self._states.get(event.window_id)
        if state is None:
            raise ContextProjectionReducerError("projection close window is unknown")
        if (
            event.final_projection_generation != state.projection_generation
            or event.final_projection_state_fingerprint
            != state.state_semantic_fingerprint
        ):
            raise ContextProjectionReducerError(
                "window close projection identity mismatch"
            )
        del self._active_window_by_run[event.run_id]

    def _retire_run(self, run_id: str) -> None:
        self._active_window_by_run.pop(run_id, None)
        window_ids = tuple(
            window_id
            for window_id, window in self._windows.items()
            if window.run_id == run_id
        )
        for window_id in window_ids:
            self._windows.pop(window_id, None)
            self._states.pop(window_id, None)


def build_projection_state(
    *,
    window: ContextWindowFact,
    projection_generation: int,
    through_sequence: int,
    unit_projections: tuple[ToolObservationProjectionFact, ...],
    rollups: tuple[ObservationRollupFact, ...],
) -> ContextWindowProjectionState:
    payload = {
        "window_id": window.window_id,
        "window_generation": window.generation,
        "projection_generation": projection_generation,
        "through_sequence": through_sequence,
        "unit_projections": unit_projections,
        "rollups": rollups,
        "total_projected_tokens": (
            sum(item.estimated_tokens for item in unit_projections)
            + sum(item.estimated_tokens for item in rollups)
        ),
        "protected_projected_tokens": sum(
            item.estimated_tokens
            for item in unit_projections
            if item.protected_reason_codes
        ),
    }
    semantic = context_fingerprint(
        "context-window-projection-state:v1",
        {
            "projection_generation": projection_generation,
            "unit_projections": unit_projections,
            "rollups": rollups,
        },
    )
    return ContextWindowProjectionState(
        **payload,
        state_semantic_fingerprint=semantic,
    )


def advance_projection_generation(
    projection: ToolObservationProjectionFact,
    *,
    projection_generation: int,
) -> ToolObservationProjectionFact:
    payload = projection.model_dump(
        mode="python",
        exclude={"projection_generation", "semantic_fingerprint"},
    )
    payload["projection_generation"] = projection_generation
    return ToolObservationProjectionFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "tool-observation-projection:v1", payload
        ),
    )
