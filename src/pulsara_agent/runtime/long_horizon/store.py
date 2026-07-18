"""Session-owned live projections over canonical long-horizon facts."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import replace
from threading import RLock

from pulsara_agent.event import (
    AgentEvent,
    ContextProjectionRewritePageEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionFailedEvent,
    ContextWindowCompactionStartedEvent,
    ContextWindowClosedEvent,
    ContextWindowOpenedEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetAccountClosedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RolloutPhaseTransitionedEvent,
    RunEndEvent,
    RunStartEvent,
)
from pulsara_agent.primitives.long_horizon import (
    ContextWindowProjectionState,
    RolloutBudgetAccountFact,
    RolloutBudgetStateFact,
)
from pulsara_agent.runtime.long_horizon.projection_reducer import (
    ContextWindowProjectionReducer,
)
from pulsara_agent.runtime.long_horizon.rollout import apply_rollout_event
from pulsara_agent.runtime.long_horizon.window import (
    ContextWindowChainState,
    apply_context_window_event,
)


class LongHorizonReducerApplyError(RuntimeError):
    """Committed window or rollout facts violated the frozen contract."""


class LongHorizonStateStore:
    """Memoized reducer output; the EventLog remains the only authority."""

    reducer_id = "long_horizon:v1"
    _max_closed_states = 128

    def __init__(
        self,
        events: Iterable[AgentEvent] = (),
        *,
        initial_through_sequence: int = 0,
    ) -> None:
        if initial_through_sequence < 0:
            raise ValueError("initial long-horizon high-water cannot be negative")
        self._lock = RLock()
        self._window_states: dict[str, ContextWindowChainState] = {}
        self._closed_window_states: OrderedDict[str, ContextWindowChainState] = (
            OrderedDict()
        )
        self._rollout_accounts: dict[str, RolloutBudgetAccountFact] = {}
        self._rollout_states: dict[str, RolloutBudgetStateFact] = {}
        self._closed_rollout_accounts: OrderedDict[
            str, tuple[RolloutBudgetAccountFact, RolloutBudgetStateFact]
        ] = OrderedDict()
        self._reservation_accounts: dict[str, str] = {}
        self._run_starts: dict[str, RunStartEvent] = {}
        self._closed_run_starts: OrderedDict[str, RunStartEvent] = OrderedDict()
        self._child_rollout_states: dict[str, object] = {}
        self._closed_child_rollout_states: OrderedDict[str, object] = OrderedDict()
        self._window_compaction_failure_counts: dict[str, int] = {}
        self._window_compaction_attempt_max: dict[str, int] = {}
        self._pending_window_compactions: dict[
            str, ContextWindowCompactionStartedEvent
        ] = {}
        self._window_compaction_terminal_ids: set[str] = set()
        self._completed_window_compactions: dict[
            str, ContextWindowCompactionCompletedEvent
        ] = {}
        self._projection_reducer = ContextWindowProjectionReducer()
        self._through_sequence = initial_through_sequence
        self.apply_committed(tuple(events))

    @classmethod
    def from_sparse_bootstrap(
        cls,
        events: Sequence[AgentEvent],
        *,
        through_sequence: int,
    ) -> "LongHorizonStateStore":
        """Rebuild from reducer-relevant facts plus one atomic ledger high-water."""

        ordered = tuple(sorted(events, key=_stored_sequence))
        if ordered and _stored_sequence(ordered[-1]) > through_sequence:
            raise ValueError("long-horizon bootstrap exceeds ledger high-water")
        store = cls()
        projection = ContextWindowProjectionReducer()
        projection.apply_committed(ordered)
        with store._lock:
            for event in ordered:
                sequence = _stored_sequence(event)
                store._through_sequence = sequence - 1
                store._apply_one(event)
                store._through_sequence = sequence
            store._through_sequence = through_sequence
            store._projection_reducer = projection
        return store

    @property
    def through_sequence(self) -> int:
        with self._lock:
            return self._through_sequence

    def window_state(self, run_id: str) -> ContextWindowChainState | None:
        with self._lock:
            state = self._window_states.get(run_id)
            if state is None:
                return self._closed_window_states.get(run_id)
            return replace(state, through_sequence=self._through_sequence)

    def run_start(self, run_id: str) -> RunStartEvent | None:
        with self._lock:
            return self._run_starts.get(run_id) or self._closed_run_starts.get(run_id)

    def run_start_by_event_id(self, event_id: str) -> RunStartEvent | None:
        with self._lock:
            return next(
                (
                    item
                    for item in (
                        *self._run_starts.values(),
                        *self._closed_run_starts.values(),
                    )
                    if item.id == event_id
                ),
                None,
            )

    def child_rollout_state(self, run_id: str):
        with self._lock:
            state = self._child_rollout_states.get(run_id)
            if state is None:
                return self._closed_child_rollout_states.get(run_id)
            return replace(state, through_sequence=self._through_sequence)

    def child_rollout_state_at(self, run_id: str, *, through_sequence: int):
        """Freeze child accounting at an already observed ledger high-water."""

        with self._lock:
            if through_sequence > self._through_sequence:
                raise LongHorizonReducerApplyError(
                    "requested child rollout snapshot exceeds reducer high-water"
                )
            state = self._child_rollout_states.get(run_id)
            if state is None:
                state = self._closed_child_rollout_states.get(run_id)
            if state is None:
                return None
            if state.through_sequence > through_sequence:
                raise LongHorizonReducerApplyError(
                    "requested child rollout snapshot precedes a semantic update"
                )
            return replace(state, through_sequence=through_sequence)

    def rollout_account(self, account_id: str) -> RolloutBudgetAccountFact | None:
        with self._lock:
            account = self._rollout_accounts.get(account_id)
            if account is not None:
                return account
            closed = self._closed_rollout_accounts.get(account_id)
            return closed[0] if closed is not None else None

    def rollout_state(self, account_id: str) -> RolloutBudgetStateFact | None:
        with self._lock:
            state = self._rollout_states.get(account_id)
            if state is None:
                closed = self._closed_rollout_accounts.get(account_id)
                return closed[1] if closed is not None else None
            return advance_rollout_state(state, self._through_sequence)

    def rollout_state_snapshot(
        self, account_id: str
    ) -> tuple[int, RolloutBudgetStateFact | None]:
        """Return one immutable account state and its exact reducer high-water."""

        with self._lock:
            state = self._rollout_states.get(account_id)
            if state is None:
                closed = self._closed_rollout_accounts.get(account_id)
                state = closed[1] if closed is not None else None
            return (
                self._through_sequence,
                (
                    advance_rollout_state(state, self._through_sequence)
                    if state is not None
                    else None
                ),
            )

    def rollout_state_at(
        self,
        account_id: str,
        *,
        through_sequence: int,
    ) -> RolloutBudgetStateFact | None:
        """Freeze one account at an already observed ledger high-water.

        The stored state advances only when a rollout event changes it; ordinary
        event families are deterministic no-ops.  A state newer than the caller's
        high-water means the caller raced a semantic write and must re-freeze its
        authority instead of rewinding that fact.
        """

        with self._lock:
            if through_sequence > self._through_sequence:
                raise LongHorizonReducerApplyError(
                    "requested rollout snapshot exceeds reducer high-water"
                )
            state = self._rollout_states.get(account_id)
            if state is None:
                closed = self._closed_rollout_accounts.get(account_id)
                state = closed[1] if closed is not None else None
            if state is None:
                return None
            if state.through_sequence > through_sequence:
                raise LongHorizonReducerApplyError(
                    "requested rollout snapshot precedes a semantic update"
                )
            return advance_rollout_state(state, through_sequence)

    def rollout_states(self) -> tuple[RolloutBudgetStateFact, ...]:
        with self._lock:
            return tuple(
                advance_rollout_state(state, self._through_sequence)
                for state in self._rollout_states.values()
            )

    def projection_state(
        self, window_id: str
    ) -> ContextWindowProjectionState | None:
        with self._lock:
            return self._projection_reducer.state(window_id)

    def window_compaction_failure_count(self, window_id: str) -> int:
        with self._lock:
            return self._window_compaction_failure_counts.get(window_id, 0)

    def next_window_compaction_attempt_index(self, window_id: str) -> int:
        with self._lock:
            return self._window_compaction_attempt_max.get(window_id, 0) + 1

    def pending_window_compactions(
        self,
    ) -> tuple[ContextWindowCompactionStartedEvent, ...]:
        with self._lock:
            return tuple(self._pending_window_compactions.values())

    def completed_window_compactions(
        self,
    ) -> tuple[ContextWindowCompactionCompletedEvent, ...]:
        with self._lock:
            return tuple(self._completed_window_compactions.values())

    def active_projection_state(
        self, run_id: str
    ) -> ContextWindowProjectionState | None:
        with self._lock:
            return self._projection_reducer.active_state(run_id)

    def apply_committed(self, events: Sequence[AgentEvent]) -> None:
        with self._lock:
            ordered = tuple(
                event
                for event in sorted(events, key=_stored_sequence)
                if _stored_sequence(event) > self._through_sequence
            )
            if ordered and not any(_is_long_horizon_event(event) for event in ordered):
                self._advance_noop_prefix(ordered)
                return
            projection = self._projection_reducer.clone()
            projection.apply_committed(ordered)
            for event in ordered:
                sequence = _stored_sequence(event)
                if sequence != self._through_sequence + 1:
                    raise LongHorizonReducerApplyError(
                        "long-horizon reducer input is not contiguous"
                    )
                try:
                    self._apply_one(event)
                except Exception as exc:
                    raise LongHorizonReducerApplyError(
                        f"failed to apply {event.type} at sequence {sequence}: {exc}"
                    ) from exc
                self._through_sequence = sequence
            self._projection_reducer = projection

    def rebuild(self, events: Iterable[AgentEvent]) -> None:
        with self._lock:
            self._window_states = {}
            self._closed_window_states = OrderedDict()
            self._rollout_accounts = {}
            self._rollout_states = {}
            self._closed_rollout_accounts = OrderedDict()
            self._reservation_accounts = {}
            self._run_starts = {}
            self._closed_run_starts = OrderedDict()
            self._child_rollout_states = {}
            self._closed_child_rollout_states = OrderedDict()
            self._window_compaction_failure_counts = {}
            self._window_compaction_attempt_max = {}
            self._pending_window_compactions = {}
            self._window_compaction_terminal_ids = set()
            self._completed_window_compactions = {}
            self._projection_reducer = ContextWindowProjectionReducer()
            self._through_sequence = 0
        self.apply_committed(tuple(events))

    def validate_next_batch(self, events: Sequence[AgentEvent]) -> None:
        """Purely validate one prospective contiguous batch.

        Facts are immutable, so shallow copies of the state maps are sufficient;
        applying candidates to the clone cannot mutate the live reducer.  The
        caller must hold the RuntimeSession write lock so the predicted sequence
        range remains the exact next ledger range through append.
        """

        if not events:
            return
        if not any(_is_long_horizon_event(event) for event in events):
            return
        with self._lock:
            clone = object.__new__(LongHorizonStateStore)
            clone._lock = RLock()
            clone._window_states = dict(self._window_states)
            clone._closed_window_states = self._closed_window_states.copy()
            clone._rollout_accounts = dict(self._rollout_accounts)
            clone._rollout_states = dict(self._rollout_states)
            clone._closed_rollout_accounts = self._closed_rollout_accounts.copy()
            clone._reservation_accounts = dict(self._reservation_accounts)
            clone._run_starts = dict(self._run_starts)
            clone._closed_run_starts = self._closed_run_starts.copy()
            clone._child_rollout_states = dict(self._child_rollout_states)
            clone._closed_child_rollout_states = (
                self._closed_child_rollout_states.copy()
            )
            clone._window_compaction_failure_counts = dict(
                self._window_compaction_failure_counts
            )
            clone._window_compaction_attempt_max = dict(
                self._window_compaction_attempt_max
            )
            clone._pending_window_compactions = dict(
                self._pending_window_compactions
            )
            clone._window_compaction_terminal_ids = set(
                self._window_compaction_terminal_ids
            )
            clone._completed_window_compactions = dict(
                self._completed_window_compactions
            )
            clone._projection_reducer = self._projection_reducer.clone()
            clone._through_sequence = self._through_sequence
            predicted = tuple(
                event.model_copy(
                    update={"sequence": self._through_sequence + index},
                    deep=True,
                )
                for index, event in enumerate(events, start=1)
            )
        clone.apply_committed(predicted)

    def _apply_one(self, event: AgentEvent) -> None:
        sequence = _stored_sequence(event)
        if isinstance(event, ContextWindowCompactionStartedEvent):
            compaction_id = event.plan.compaction_id
            if (
                compaction_id in self._pending_window_compactions
                or compaction_id in self._window_compaction_terminal_ids
            ):
                raise LongHorizonReducerApplyError(
                    "window compaction lifecycle identity was reused"
                )
            self._pending_window_compactions[compaction_id] = event
            window_id = event.plan.source_window_id
            self._window_compaction_attempt_max[window_id] = max(
                self._window_compaction_attempt_max.get(window_id, 0),
                event.plan.compaction_attempt_index,
            )
        elif isinstance(event, ContextWindowCompactionCompletedEvent):
            if event.compaction_id in self._window_compaction_terminal_ids:
                raise LongHorizonReducerApplyError(
                    "window compaction has multiple terminal facts"
                )
            if self._pending_window_compactions.pop(event.compaction_id, None) is None:
                raise LongHorizonReducerApplyError(
                    "window compaction completion lacks its Started fact"
                )
            self._window_compaction_terminal_ids.add(event.compaction_id)
            self._completed_window_compactions[event.compaction_id] = event
        elif isinstance(event, ContextWindowCompactionFailedEvent):
            if event.compaction_id in self._window_compaction_terminal_ids:
                raise LongHorizonReducerApplyError(
                    "window compaction has multiple terminal facts"
                )
            if event.started_event_id is not None:
                if self._pending_window_compactions.pop(event.compaction_id, None) is None:
                    raise LongHorizonReducerApplyError(
                        "window compaction failure lacks its Started fact"
                    )
            self._window_compaction_terminal_ids.add(event.compaction_id)
            self._window_compaction_failure_counts[event.source_window_id] = (
                self._window_compaction_failure_counts.get(event.source_window_id, 0)
                + 1
            )
            self._window_compaction_attempt_max[event.source_window_id] = max(
                self._window_compaction_attempt_max.get(event.source_window_id, 0),
                event.compaction_attempt_index,
            )
        if isinstance(event, RunStartEvent):
            if event.run_id in self._window_states:
                raise LongHorizonReducerApplyError(
                    "long-horizon run was opened more than once"
                )
            self._window_states[event.run_id] = ContextWindowChainState.empty(
                run_id=event.run_id,
                through_sequence=sequence - 1,
            )
            self._run_starts[event.run_id] = event
            if event.child_rollout_subaccount is not None:
                from pulsara_agent.runtime.long_horizon.accounting import (
                    initial_child_rollout_state,
                )

                self._child_rollout_states[event.run_id] = (
                    initial_child_rollout_state(
                        subaccount=event.child_rollout_subaccount,
                        through_sequence=sequence,
                    )
                )
        elif isinstance(event, RunEndEvent):
            self._completed_window_compactions = {
                key: item
                for key, item in self._completed_window_compactions.items()
                if item.run_id != event.run_id
            }
        if isinstance(event, (ContextWindowOpenedEvent, ContextWindowClosedEvent, RunEndEvent)):
            state = self._window_states.get(event.run_id)
            if state is None:
                raise LongHorizonReducerApplyError(
                    f"context-window event targets unknown run {event.run_id}"
                )
            state = replace(state, through_sequence=sequence - 1)
            next_state = apply_context_window_event(state, event)
            if not next_state.consistent:
                raise LongHorizonReducerApplyError(
                    f"context-window chain became inconsistent for {event.run_id}"
                )
            self._window_states[event.run_id] = next_state
            if isinstance(event, RunEndEvent):
                self._window_states.pop(event.run_id)
                self._remember_closed_window(event.run_id, next_state)
                start = self._run_starts.pop(event.run_id, None)
                if start is not None:
                    self._remember_closed(
                        self._closed_run_starts, event.run_id, start
                    )
                child_state = self._child_rollout_states.pop(event.run_id, None)
                if child_state is not None:
                    self._remember_closed(
                        self._closed_child_rollout_states,
                        event.run_id,
                        child_state,
                    )

        child_state = self._child_rollout_states.get(event.run_id)
        if child_state is not None and isinstance(
            event,
            (RolloutBudgetReservationCreatedEvent, RolloutBudgetReservationSettledEvent),
        ):
            from pulsara_agent.runtime.long_horizon.accounting import (
                apply_child_rollout_event,
            )

            start = self._run_starts[event.run_id]
            self._child_rollout_states[event.run_id] = apply_child_rollout_event(
                child_state,
                event,
                policy=start.long_horizon.rollout_policy,
            )
            return

        if isinstance(event, RolloutBudgetAccountOpenedEvent):
            account_id = event.account.account_id
            if account_id in self._rollout_accounts:
                raise LongHorizonReducerApplyError(
                    "rollout account was opened more than once"
                )
            account, state = apply_rollout_event(
                account=None,
                state=None,
                event=event,
            )
            assert account is not None and state is not None
            self._rollout_accounts[account_id] = account
            self._rollout_states[account_id] = state
            return

        account_id = self._rollout_event_account_id(event)
        if account_id is None:
            return
        account = self._rollout_accounts.get(account_id)
        state = self._rollout_states.get(account_id)
        if account is None or state is None:
            # Child ledgers may carry reservations owned by the parent ledger.
            # The parent store applies its canonical copy; this store must not
            # invent a local account merely to track cross-ledger attribution.
            return
        state = advance_rollout_state(state, sequence - 1)
        _, next_state = apply_rollout_event(
            account=account,
            state=state,
            event=event,
        )
        assert next_state is not None
        self._rollout_states[account_id] = next_state
        if isinstance(event, RolloutBudgetReservationCreatedEvent):
            self._reservation_accounts[event.reservation.reservation_id] = account_id
        elif isinstance(event, RolloutBudgetReservationSettledEvent):
            self._reservation_accounts.pop(event.reservation_id, None)
        elif isinstance(event, RolloutBudgetAccountClosedEvent):
            self._rollout_accounts.pop(account_id)
            self._rollout_states.pop(account_id)
            self._closed_rollout_accounts[account_id] = (account, next_state)
            self._closed_rollout_accounts.move_to_end(account_id)
            while len(self._closed_rollout_accounts) > self._max_closed_states:
                self._closed_rollout_accounts.popitem(last=False)

    def _rollout_event_account_id(self, event: AgentEvent) -> str | None:
        if isinstance(event, RolloutBudgetReservationCreatedEvent):
            return event.reservation.account_id
        if isinstance(event, RolloutBudgetReservationSettledEvent):
            return self._reservation_accounts.get(event.reservation_id)
        if isinstance(event, (RolloutPhaseTransitionedEvent, RolloutBudgetAccountClosedEvent)):
            return event.account_id
        return None

    def _remember_closed_window(
        self, run_id: str, state: ContextWindowChainState
    ) -> None:
        self._closed_window_states[run_id] = state
        self._closed_window_states.move_to_end(run_id)
        while len(self._closed_window_states) > self._max_closed_states:
            self._closed_window_states.popitem(last=False)

    def _remember_closed(self, target: OrderedDict, key: str, value: object) -> None:
        target[key] = value
        target.move_to_end(key)
        while len(target) > self._max_closed_states:
            target.popitem(last=False)

    def _advance_noop_prefix(self, events: Sequence[AgentEvent]) -> None:
        for event in events:
            sequence = _stored_sequence(event)
            if sequence != self._through_sequence + 1:
                raise LongHorizonReducerApplyError(
                    "long-horizon reducer input is not contiguous"
                )
            self._through_sequence = sequence


def _stored_sequence(event: AgentEvent) -> int:
    if event.sequence is None or event.sequence < 1:
        raise LongHorizonReducerApplyError(
            "long-horizon state store requires committed events"
        )
    return event.sequence


def _is_long_horizon_event(event: AgentEvent) -> bool:
    return isinstance(
        event,
        (
            RunStartEvent,
            RunEndEvent,
            ContextWindowOpenedEvent,
            ContextWindowClosedEvent,
            ContextProjectionRewritePageEvent,
            ContextWindowCompactionStartedEvent,
            ContextWindowCompactionCompletedEvent,
            ContextWindowCompactionFailedEvent,
            RolloutBudgetAccountOpenedEvent,
            RolloutBudgetAccountClosedEvent,
            RolloutBudgetReservationCreatedEvent,
            RolloutBudgetReservationSettledEvent,
            RolloutPhaseTransitionedEvent,
        ),
    )


def advance_rollout_state(
    state: RolloutBudgetStateFact, through_sequence: int
) -> RolloutBudgetStateFact:
    if through_sequence <= state.through_sequence:
        return state
    payload = state.model_dump(mode="python", exclude={"state_fingerprint"})
    payload["through_sequence"] = through_sequence
    from pulsara_agent.primitives.context import context_fingerprint

    return RolloutBudgetStateFact(
        **payload,
        state_fingerprint=context_fingerprint("rollout-budget-state:v1", payload),
    )
