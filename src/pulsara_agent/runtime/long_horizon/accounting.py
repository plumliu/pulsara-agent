"""Durable rollout bindings for root and child runs.

The parent event log owns the root account.  A child ledger records its own
model/tool reservations against the inherited root-account identity, but those
facts only consume the bounded child subaccount until one terminal handoff is
committed back to the parent ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from pulsara_agent.event import (
    AgentEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RunStartEvent,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    ChildRolloutSettlementAggregateFact,
    ChildRolloutSubaccountFact,
    RolloutBudgetAccountFact,
    RolloutBudgetPolicyFact,
    RolloutBudgetStateFact,
    RolloutReservationFact,
)
from pulsara_agent.runtime.long_horizon.rollout import (
    RolloutReducerContractError,
    apply_rollout_event,
    initial_rollout_budget_state,
    validate_rollout_settlement,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


class ChildRolloutAccountingError(RuntimeError):
    """A child ledger cannot be reconciled with its frozen subaccount."""


@dataclass(frozen=True, slots=True)
class ChildRolloutLedgerState:
    subaccount: ChildRolloutSubaccountFact
    active_reservations: tuple[RolloutReservationFact, ...]
    settlements: tuple[
        tuple[RolloutReservationFact, RolloutBudgetReservationSettledEvent], ...
    ]
    charged_milliunits: int
    reserved_milliunits: int
    through_sequence: int

    @property
    def remaining_milliunits(self) -> int:
        return (
            self.subaccount.reserved_milliunits
            - self.charged_milliunits
            - self.reserved_milliunits
        )

    @property
    def state_fingerprint(self) -> str:
        """Deterministic local CAS identity for child-owned reservations."""

        return context_fingerprint(
            "child-rollout-ledger-state:v1",
            {
                "subaccount_fingerprint": self.subaccount.subaccount_fingerprint,
                "active_reservation_fingerprints": tuple(
                    item.semantic_fingerprint for item in self.active_reservations
                ),
                "settlement_event_ids": tuple(
                    event.id for _reservation, event in self.settlements
                ),
                "charged_milliunits": self.charged_milliunits,
                "reserved_milliunits": self.reserved_milliunits,
                "through_sequence": self.through_sequence,
            },
        )


@dataclass(frozen=True, slots=True)
class RunRolloutBinding:
    run_start: RunStartEvent
    account: RolloutBudgetAccountFact
    parent_state: RolloutBudgetStateFact
    child_subaccount: ChildRolloutSubaccountFact | None
    child_state: ChildRolloutLedgerState | None


def resolve_run_rollout_binding(
    runtime_session: RuntimeSession,
    *,
    run_id: str,
) -> RunRolloutBinding:
    run_start = runtime_session.long_horizon_state_store.run_start(run_id)
    if run_start is None:
        raise ChildRolloutAccountingError(
            "rollout binding requires one active RunStartEvent projection"
        )
    account_id = run_start.long_horizon.rollout_account_id
    child_subaccount = run_start.child_rollout_subaccount
    if child_subaccount is None:
        account = runtime_session.long_horizon_state_store.rollout_account(account_id)
        state = runtime_session.long_horizon_state_store.rollout_state(account_id)
        if account is None or state is None:
            raise ChildRolloutAccountingError("root rollout account is unavailable")
        return RunRolloutBinding(
            run_start=run_start,
            account=account,
            parent_state=state,
            child_subaccount=None,
            child_state=None,
        )

    if child_subaccount.root_account_id != account_id:
        raise ChildRolloutAccountingError("child root-account attribution drifted")
    owner_store = runtime_session.rollout_account_owner_state_store
    if owner_store is None:
        raise ChildRolloutAccountingError(
            "child rollout binding requires its parent account state store"
        )
    account = owner_store.rollout_account(account_id)
    parent_state = owner_store.rollout_state(account_id)
    if account is None or parent_state is None:
        raise ChildRolloutAccountingError("parent rollout account is unavailable")
    reference = child_subaccount.parent_reservation
    matching_parent_reservations = tuple(
        reservation
        for reservation in parent_state.active_reservations
        if reservation.reservation_id == reference.reservation_id
    )
    if len(matching_parent_reservations) != 1:
        raise ChildRolloutAccountingError(
            "child parent reservation is no longer active or is ambiguous"
        )
    parent_reservation = matching_parent_reservations[0]
    entry = run_start.subagent_run_entry
    if (
        entry is None
        or
        parent_reservation.semantic_fingerprint
        != reference.reservation_fingerprint
        or parent_reservation.owner_kind != "subagent_run"
        or parent_reservation.owner_id != entry.subagent_run_id
    ):
        raise ChildRolloutAccountingError("child parent reservation identity mismatch")
    child_state = runtime_session.long_horizon_state_store.child_rollout_state(
        run_id
    )
    if child_state is None or child_state.subaccount != child_subaccount:
        raise ChildRolloutAccountingError("child rollout state is unavailable")
    return RunRolloutBinding(
        run_start=run_start,
        account=account,
        parent_state=parent_state,
        child_subaccount=child_subaccount,
        child_state=child_state,
    )


def fold_child_rollout_ledger(
    events: Iterable[AgentEvent],
    *,
    subaccount: ChildRolloutSubaccountFact,
    policy: RolloutBudgetPolicyFact,
) -> ChildRolloutLedgerState:
    ordered = tuple(sorted(events, key=_stored_sequence))
    active: dict[str, RolloutReservationFact] = {}
    settled_ids: set[str] = set()
    settlements: list[
        tuple[RolloutReservationFact, RolloutBudgetReservationSettledEvent]
    ] = []
    through_sequence = 0
    for event in ordered:
        through_sequence = _stored_sequence(event)
        if isinstance(event, RolloutBudgetReservationCreatedEvent):
            reservation = event.reservation
            if reservation.owner_kind == "subagent_run":
                raise ChildRolloutAccountingError(
                    "child ledger cannot contain a nested subagent reservation"
                )
            if reservation.account_id != subaccount.root_account_id:
                raise ChildRolloutAccountingError(
                    "child local reservation root-account mismatch"
                )
            if (
                reservation.reservation_id in active
                or reservation.reservation_id in settled_ids
            ):
                raise ChildRolloutAccountingError(
                    "child ledger contains a duplicate reservation identity"
                )
            active[reservation.reservation_id] = reservation
        elif isinstance(event, RolloutBudgetReservationSettledEvent):
            reservation = active.pop(event.reservation_id, None)
            if reservation is None:
                raise ChildRolloutAccountingError(
                    "child settlement has no matching active reservation"
                )
            try:
                validate_rollout_settlement(
                    policy=policy,
                    reservation=reservation,
                    event=event,
                )
            except RolloutReducerContractError as exc:
                raise ChildRolloutAccountingError(str(exc)) from exc
            settled_ids.add(event.reservation_id)
            settlements.append((reservation, event))

        charged = sum(event.charged_milliunits for _, event in settlements)
        reserved = sum(item.reserved_milliunits for item in active.values())
        if charged + reserved > subaccount.reserved_milliunits:
            raise ChildRolloutAccountingError(
                "child local reservations exceed the subaccount hard ceiling"
            )

    charged = sum(event.charged_milliunits for _, event in settlements)
    reserved = sum(item.reserved_milliunits for item in active.values())
    return ChildRolloutLedgerState(
        subaccount=subaccount,
        active_reservations=tuple(
            sorted(active.values(), key=lambda item: item.reservation_id)
        ),
        settlements=tuple(settlements),
        charged_milliunits=charged,
        reserved_milliunits=reserved,
        through_sequence=through_sequence,
    )


def initial_child_rollout_state(
    *, subaccount: ChildRolloutSubaccountFact, through_sequence: int
) -> ChildRolloutLedgerState:
    return ChildRolloutLedgerState(
        subaccount=subaccount,
        active_reservations=(),
        settlements=(),
        charged_milliunits=0,
        reserved_milliunits=0,
        through_sequence=through_sequence,
    )


def apply_child_rollout_event(
    state: ChildRolloutLedgerState,
    event: AgentEvent,
    *,
    policy: RolloutBudgetPolicyFact,
) -> ChildRolloutLedgerState:
    sequence = _stored_sequence(event)
    if sequence <= state.through_sequence:
        return state
    active = {item.reservation_id: item for item in state.active_reservations}
    settlements = list(state.settlements)
    settled_ids = {item.reservation_id for item, _event in settlements}
    if isinstance(event, RolloutBudgetReservationCreatedEvent):
        reservation = event.reservation
        if reservation.owner_kind == "subagent_run":
            raise ChildRolloutAccountingError(
                "child ledger cannot contain a nested subagent reservation"
            )
        if reservation.account_id != state.subaccount.root_account_id:
            raise ChildRolloutAccountingError(
                "child local reservation root-account mismatch"
            )
        if reservation.reservation_id in active or reservation.reservation_id in settled_ids:
            raise ChildRolloutAccountingError(
                "child ledger contains a duplicate reservation identity"
            )
        active[reservation.reservation_id] = reservation
    elif isinstance(event, RolloutBudgetReservationSettledEvent):
        reservation = active.pop(event.reservation_id, None)
        if reservation is None:
            raise ChildRolloutAccountingError(
                "child settlement has no matching active reservation"
            )
        try:
            validate_rollout_settlement(
                policy=policy,
                reservation=reservation,
                event=event,
            )
        except RolloutReducerContractError as exc:
            raise ChildRolloutAccountingError(str(exc)) from exc
        settlements.append((reservation, event))
    charged = sum(item.charged_milliunits for _, item in settlements)
    reserved = sum(item.reserved_milliunits for item in active.values())
    if charged + reserved > state.subaccount.reserved_milliunits:
        raise ChildRolloutAccountingError(
            "child local reservations exceed the subaccount hard ceiling"
        )
    return ChildRolloutLedgerState(
        subaccount=state.subaccount,
        active_reservations=tuple(
            sorted(active.values(), key=lambda item: item.reservation_id)
        ),
        settlements=tuple(settlements),
        charged_milliunits=charged,
        reserved_milliunits=reserved,
        through_sequence=sequence,
    )


def child_settlement_aggregate(
    state: ChildRolloutLedgerState,
) -> ChildRolloutSettlementAggregateFact:
    if state.active_reservations:
        raise ChildRolloutAccountingError(
            "child subaccount cannot close with active reservations: "
            + ", ".join(
                f"{item.owner_kind}:{item.owner_id}:{item.reservation_id}"
                for item in state.active_reservations
            )
        )
    provider_reported = reserved_missing = cancelled = not_started = 0
    tool_count = 0
    reported_input = reported_cached = reported_output = 0
    model_charged = tool_charged = 0
    for reservation, settlement in state.settlements:
        if reservation.owner_kind == "model_call":
            model_charged += settlement.charged_milliunits
            basis = settlement.usage_status
            if basis == "provider_reported_usage":
                provider_reported += 1
                charge = settlement.usage_charge
                if charge is None:
                    raise ChildRolloutAccountingError(
                        "reported child settlement lacks usage charge"
                    )
                reported_input += charge.reported_input_tokens or 0
                reported_cached += charge.reported_cached_input_tokens or 0
                reported_output += charge.reported_output_tokens or 0
            elif basis == "reserved_missing_usage":
                reserved_missing += 1
            elif basis == "cancelled_reserved":
                cancelled += 1
            elif basis == "not_started_zero":
                not_started += 1
            else:
                raise ChildRolloutAccountingError(
                    "child model reservation used a non-model settlement basis"
                )
        elif reservation.owner_kind == "tool_call":
            if settlement.usage_status != "tool_terminal":
                raise ChildRolloutAccountingError(
                    "child tool reservation used a non-tool settlement basis"
                )
            tool_count += 1
            tool_charged += settlement.charged_milliunits
        else:
            raise ChildRolloutAccountingError(
                "child local ledger contains a nested child reservation"
            )

    payload = {
        "subaccount_fingerprint": state.subaccount.subaccount_fingerprint,
        "provider_reported_model_call_count": provider_reported,
        "reserved_missing_model_call_count": reserved_missing,
        "cancelled_reserved_model_call_count": cancelled,
        "not_started_zero_model_call_count": not_started,
        "tool_terminal_settlement_count": tool_count,
        "model_call_count": provider_reported
        + reserved_missing
        + cancelled
        + not_started,
        "tool_call_count": tool_count,
        "reported_subset_input_tokens": reported_input,
        "reported_subset_cached_input_tokens": reported_cached,
        "reported_subset_output_tokens": reported_output,
        "model_charged_milliunits": model_charged,
        "tool_charged_milliunits": tool_charged,
        "charged_milliunits": model_charged + tool_charged,
        "through_sequence": max(1, state.through_sequence),
    }
    return ChildRolloutSettlementAggregateFact(
        **payload,
        aggregate_fingerprint=context_fingerprint(
            "child-rollout-settlement-aggregate:v1", payload
        ),
    )


def _fold_account(
    events: tuple[AgentEvent, ...],
    *,
    account_id: str,
) -> tuple[RolloutBudgetAccountFact, RolloutBudgetStateFact]:
    openings = tuple(
        event
        for event in events
        if isinstance(event, RolloutBudgetAccountOpenedEvent)
        and event.account.account_id == account_id
    )
    if len(openings) != 1:
        raise ChildRolloutAccountingError(
            "parent ledger lacks one matching rollout account"
        )
    opening = openings[0]
    sequence = _stored_sequence(opening)
    account = opening.account
    state = initial_rollout_budget_state(account=account, through_sequence=sequence)
    for event in sorted(events, key=_stored_sequence):
        event_sequence = _stored_sequence(event)
        if event_sequence <= sequence:
            continue
        _, next_state = apply_rollout_event(
            account=account,
            state=state,
            event=event,
        )
        if next_state is None:
            raise ChildRolloutAccountingError("parent rollout state disappeared")
        state = next_state
    return account, state


def _stored_sequence(event: AgentEvent) -> int:
    if event.sequence is None or event.sequence < 1:
        raise ChildRolloutAccountingError(
            "child rollout accounting requires committed events"
        )
    return event.sequence


__all__ = [
    "ChildRolloutAccountingError",
    "ChildRolloutLedgerState",
    "RunRolloutBinding",
    "child_settlement_aggregate",
    "fold_child_rollout_ledger",
    "initial_child_rollout_state",
    "apply_child_rollout_event",
    "resolve_run_rollout_binding",
]
