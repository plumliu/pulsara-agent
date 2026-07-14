"""Pure rollout-account formulas and event reducer."""

from __future__ import annotations

from typing import Iterable

from pulsara_agent.event import (
    AgentEvent,
    RolloutBudgetAccountClosedEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RolloutPhaseTransitionedEvent,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    ROLLOUT_PHASE_ORDER,
    RolloutBudgetAccountFact,
    RolloutBudgetBucket,
    RolloutBudgetPolicyFact,
    RolloutBudgetStateFact,
    RolloutPhase,
    RolloutReservationFact,
)


class RolloutReducerContractError(RuntimeError):
    """A committed rollout event violates the account contract."""


def initial_rollout_budget_state(
    *, account: RolloutBudgetAccountFact, through_sequence: int
) -> RolloutBudgetStateFact:
    return _state(
        account_id=account.account_id,
        phase=RolloutPhase.EXPLORATION,
        charged=(0, 0, 0, 0),
        reserved=(0, 0, 0, 0),
        model_call_count=0,
        recovered_incomplete_model_stream_count=0,
        model_stream_reconciliation_blocker_count=0,
        tool_call_count=0,
        active_reservations=(),
        through_sequence=through_sequence,
    )


def rollout_state_with_phase(
    state: RolloutBudgetStateFact,
    *,
    phase: RolloutPhase,
    through_sequence: int | None = None,
) -> RolloutBudgetStateFact:
    """Return the deterministic post-transition state used by event candidates."""

    if ROLLOUT_PHASE_ORDER.index(phase) <= ROLLOUT_PHASE_ORDER.index(state.phase):
        raise RolloutReducerContractError("rollout phase cannot move backwards")
    return _copy_state(
        state,
        phase=phase,
        through_sequence=through_sequence,
    )


def fold_rollout_budget(
    events: Iterable[AgentEvent],
    *,
    account: RolloutBudgetAccountFact | None = None,
    initial: RolloutBudgetStateFact | None = None,
) -> tuple[RolloutBudgetAccountFact | None, RolloutBudgetStateFact | None]:
    current_account = account
    state = initial
    for event in events:
        current_account, state = apply_rollout_event(
            account=current_account,
            state=state,
            event=event,
        )
    return current_account, state


def apply_rollout_event(
    *,
    account: RolloutBudgetAccountFact | None,
    state: RolloutBudgetStateFact | None,
    event: AgentEvent,
) -> tuple[RolloutBudgetAccountFact | None, RolloutBudgetStateFact | None]:
    sequence = event.sequence
    if sequence is None or sequence < 1:
        raise RolloutReducerContractError("stored rollout input requires sequence")
    if isinstance(event, RolloutBudgetAccountOpenedEvent):
        if account is not None and event.account.account_id != account.account_id:
            if state is None:
                return account, state
            if sequence <= state.through_sequence:
                return account, state
            if sequence != state.through_sequence + 1:
                raise RolloutReducerContractError(
                    "rollout event stream is not contiguous"
                )
            return account, _copy_state(state, through_sequence=sequence)
        if account is not None or state is not None:
            raise RolloutReducerContractError("rollout account opened more than once")
        return event.account, initial_rollout_budget_state(
            account=event.account, through_sequence=sequence
        )
    if state is None:
        return account, None
    if sequence <= state.through_sequence:
        return account, state
    if sequence != state.through_sequence + 1:
        raise RolloutReducerContractError("rollout event stream is not contiguous")
    state_before_event = state
    state = _copy_state(state, through_sequence=sequence)
    if isinstance(event, RolloutBudgetReservationCreatedEvent):
        if account is None:
            raise RolloutReducerContractError("reservation has no rollout account")
        if event.reservation.account_id == state.account_id:
            state = _reserve(
                account=account,
                state=state,
                reservation=event.reservation,
            )
    elif isinstance(event, RolloutBudgetReservationSettledEvent):
        if any(
            item.reservation_id == event.reservation_id
            for item in state.active_reservations
        ):
            if account is None:
                raise RolloutReducerContractError(
                    "settlement has no rollout account"
                )
            state = _settle(account=account, state=state, event=event)
    elif isinstance(event, RolloutPhaseTransitionedEvent):
        if event.account_id == state.account_id:
            state = _transition(
                state_before_event=state_before_event,
                state_at_event_sequence=state,
                event=event,
            )
    elif isinstance(event, RolloutBudgetAccountClosedEvent):
        if event.account_id == state.account_id:
            if state_before_event.active_reservations:
                raise RolloutReducerContractError(
                    "rollout account closed with reservations"
                )
            if (
                event.final_state_fingerprint
                != state_before_event.state_fingerprint
                or event.charged_milliunits
                != state_before_event.charged_milliunits
                or event.model_call_count != state_before_event.model_call_count
                or event.tool_call_count != state_before_event.tool_call_count
            ):
                raise RolloutReducerContractError("rollout close summary mismatch")
    return account, state


def _reserve(
    *,
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    reservation: RolloutReservationFact,
) -> RolloutBudgetStateFact:
    if reservation.account_id != state.account_id:
        raise RolloutReducerContractError("reservation account mismatch")
    if reservation.phase_at_reservation is not state.phase:
        raise RolloutReducerContractError("reservation phase mismatch")
    if any(
        item.reservation_id == reservation.reservation_id
        for item in state.active_reservations
    ):
        raise RolloutReducerContractError("duplicate rollout reservation")
    bucket = _bucket_index(reservation.budget_bucket)
    charged = _bucket_charged(state)
    reserved = list(_bucket_reserved(state))
    reserved[bucket] += reservation.reserved_milliunits
    _validate_bucket_capacity(account, charged=charged, reserved=tuple(reserved))
    return _state(
        account_id=state.account_id,
        phase=state.phase,
        charged=charged,
        reserved=tuple(reserved),
        model_call_count=state.model_call_count,
        recovered_incomplete_model_stream_count=(
            state.recovered_incomplete_model_stream_count
        ),
        model_stream_reconciliation_blocker_count=(
            state.model_stream_reconciliation_blocker_count
        ),
        tool_call_count=state.tool_call_count,
        active_reservations=(*state.active_reservations, reservation),
        through_sequence=state.through_sequence,
    )


def _settle(
    *,
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    event: RolloutBudgetReservationSettledEvent,
) -> RolloutBudgetStateFact:
    matching = tuple(
        item
        for item in state.active_reservations
        if item.reservation_id == event.reservation_id
    )
    if len(matching) != 1:
        raise RolloutReducerContractError("settlement reservation is missing or ambiguous")
    reservation = matching[0]
    validate_rollout_settlement(
        policy=account.policy,
        reservation=reservation,
        event=event,
    )
    bucket = _bucket_index(reservation.budget_bucket)
    charged = list(_bucket_charged(state))
    reserved = list(_bucket_reserved(state))
    charged[bucket] += event.charged_milliunits
    reserved[bucket] -= reservation.reserved_milliunits
    child_aggregate = (
        event.child_usage_handoff.settlement_aggregate
        if reservation.owner_kind == "subagent_run"
        and event.child_usage_handoff is not None
        else None
    )
    return _state(
        account_id=state.account_id,
        phase=state.phase,
        charged=tuple(charged),
        reserved=tuple(reserved),
        model_call_count=state.model_call_count
        + (1 if reservation.owner_kind == "model_call" else 0)
        + (child_aggregate.model_call_count if child_aggregate is not None else 0),
        recovered_incomplete_model_stream_count=(
            state.recovered_incomplete_model_stream_count
        ),
        model_stream_reconciliation_blocker_count=(
            state.model_stream_reconciliation_blocker_count
        ),
        tool_call_count=state.tool_call_count
        + (1 if reservation.owner_kind == "tool_call" else 0)
        + (child_aggregate.tool_call_count if child_aggregate is not None else 0),
        active_reservations=tuple(
            item
            for item in state.active_reservations
            if item.reservation_id != event.reservation_id
        ),
        through_sequence=state.through_sequence,
    )


def validate_rollout_settlement(
    *,
    policy: RolloutBudgetPolicyFact,
    reservation: RolloutReservationFact,
    event: RolloutBudgetReservationSettledEvent,
) -> None:
    """Validate one settlement against its frozen reservation and policy."""

    if event.charged_milliunits > reservation.reserved_milliunits:
        raise RolloutReducerContractError("settlement exceeds reservation")

    if reservation.owner_kind == "model_call":
        _validate_model_settlement(
            policy=policy,
            reservation=reservation,
            event=event,
        )
        return
    if reservation.owner_kind == "tool_call":
        if event.usage_status != "tool_terminal":
            raise RolloutReducerContractError(
                "tool reservation requires tool-terminal settlement"
            )
        if event.charged_milliunits != reservation.reserved_milliunits:
            raise RolloutReducerContractError(
                "tool settlement must charge its frozen reservation"
            )
        return

    if event.usage_status == "child_not_started_zero":
        if event.charged_milliunits != 0:
            raise RolloutReducerContractError(
                "unstarted child settlement must charge zero"
            )
        return
    if event.usage_status != "child_terminal_handoff":
        raise RolloutReducerContractError(
            "child reservation requires child-terminal settlement"
        )
    handoff = event.child_usage_handoff
    if handoff is None:
        raise RolloutReducerContractError(
            "child terminal settlement lacks usage handoff"
        )
    if (
        event.charged_milliunits
        != handoff.settlement_aggregate.charged_milliunits
    ):
        raise RolloutReducerContractError("child settlement aggregate mismatch")


def _validate_model_settlement(
    *,
    policy: RolloutBudgetPolicyFact,
    reservation: RolloutReservationFact,
    event: RolloutBudgetReservationSettledEvent,
) -> None:
    model_bases = {
        "provider_reported_usage",
        "not_started_zero",
        "reserved_missing_usage",
        "cancelled_reserved",
    }
    if event.usage_status not in model_bases:
        raise RolloutReducerContractError(
            "model reservation requires model-usage settlement"
        )
    quote = reservation.model_call_reservation_quote
    charge = event.usage_charge
    if quote is None or charge is None or quote.quote_fact_fingerprint is None:
        raise RolloutReducerContractError("model settlement lacks frozen quote")
    if quote.resolved_model_call_id != reservation.owner_id:
        raise RolloutReducerContractError("model settlement call identity mismatch")
    if (
        quote.policy_fingerprint != policy.policy_fingerprint
        or quote.non_cached_input_weight_milli
        != policy.non_cached_input_weight_milli
        or quote.output_weight_milli != policy.output_weight_milli
        or charge.policy_fingerprint != policy.policy_fingerprint
    ):
        raise RolloutReducerContractError("model settlement policy drifted")
    if (
        charge.reservation_quote_fact_fingerprint
        != quote.quote_fact_fingerprint
        or charge.physical_input_token_upper_bound
        != quote.physical_input_token_upper_bound
        or charge.output_token_upper_bound != quote.output_token_upper_bound
    ):
        raise RolloutReducerContractError("model settlement quote drifted")
    if charge.pre_send_estimated_input_tokens > quote.physical_input_token_upper_bound:
        raise RolloutReducerContractError(
            "model pre-send estimate exceeds physical bound"
        )

    if event.usage_status == "provider_reported_usage":
        assert charge.reported_input_tokens is not None
        assert charge.reported_cached_input_tokens is not None
        assert charge.reported_output_tokens is not None
        non_cached = (
            charge.reported_input_tokens - charge.reported_cached_input_tokens
        )
        expected = (
            non_cached * policy.non_cached_input_weight_milli
            + charge.reported_cached_input_tokens
            * policy.cached_input_weight_milli
            + charge.reported_output_tokens * policy.output_weight_milli
        )
    elif event.usage_status == "not_started_zero":
        expected = 0
    else:
        expected = quote.reserved_milliunits
    if charge.charged_milliunits != expected or event.charged_milliunits != expected:
        raise RolloutReducerContractError("model settlement charge arithmetic mismatch")


def _transition(
    *,
    state_before_event: RolloutBudgetStateFact,
    state_at_event_sequence: RolloutBudgetStateFact,
    event: RolloutPhaseTransitionedEvent,
) -> RolloutBudgetStateFact:
    if (
        event.account_id != state_before_event.account_id
        or event.from_phase is not state_before_event.phase
    ):
        raise RolloutReducerContractError("rollout phase transition source mismatch")
    if event.source_through_sequence != state_before_event.through_sequence:
        raise RolloutReducerContractError("rollout phase transition source sequence mismatch")
    if event.state_before_fingerprint != state_before_event.state_fingerprint:
        raise RolloutReducerContractError("rollout phase transition CAS mismatch")
    if ROLLOUT_PHASE_ORDER.index(event.to_phase) <= ROLLOUT_PHASE_ORDER.index(
        state_before_event.phase
    ):
        raise RolloutReducerContractError("rollout phase cannot move backwards")
    next_state = rollout_state_with_phase(
        state_at_event_sequence,
        phase=event.to_phase,
    )
    if next_state.state_fingerprint != event.state_after_fingerprint:
        raise RolloutReducerContractError("rollout phase target fingerprint mismatch")
    return next_state


def _validate_bucket_capacity(
    account: RolloutBudgetAccountFact,
    *,
    charged: tuple[int, int, int, int],
    reserved: tuple[int, int, int, int],
) -> None:
    capacities = (
        account.exploration_allowance_milliunits,
        account.finalization_agent_reserve_milliunits,
        account.finalization_compaction_reserve_milliunits,
        account.finalization_tool_reserve_milliunits,
    )
    if any(
        used + held > capacity
        for used, held, capacity in zip(charged, reserved, capacities, strict=True)
    ):
        raise RolloutReducerContractError("rollout reservation exceeds bucket capacity")


def _bucket_index(bucket: RolloutBudgetBucket) -> int:
    return tuple(RolloutBudgetBucket).index(bucket)


def _bucket_charged(state: RolloutBudgetStateFact) -> tuple[int, int, int, int]:
    return (
        state.exploration_charged_milliunits,
        state.finalization_agent_charged_milliunits,
        state.finalization_compaction_charged_milliunits,
        state.finalization_tool_charged_milliunits,
    )


def _bucket_reserved(state: RolloutBudgetStateFact) -> tuple[int, int, int, int]:
    return (
        state.exploration_reserved_milliunits,
        state.finalization_agent_reserved_milliunits,
        state.finalization_compaction_reserved_milliunits,
        state.finalization_tool_reserved_milliunits,
    )


def _copy_state(
    state: RolloutBudgetStateFact,
    *,
    phase: RolloutPhase | None = None,
    through_sequence: int | None = None,
) -> RolloutBudgetStateFact:
    return _state(
        account_id=state.account_id,
        phase=phase or state.phase,
        charged=_bucket_charged(state),
        reserved=_bucket_reserved(state),
        model_call_count=state.model_call_count,
        recovered_incomplete_model_stream_count=(
            state.recovered_incomplete_model_stream_count
        ),
        model_stream_reconciliation_blocker_count=(
            state.model_stream_reconciliation_blocker_count
        ),
        tool_call_count=state.tool_call_count,
        active_reservations=state.active_reservations,
        through_sequence=(
            state.through_sequence if through_sequence is None else through_sequence
        ),
    )


def _state(
    *,
    account_id: str,
    phase: RolloutPhase,
    charged: tuple[int, int, int, int],
    reserved: tuple[int, int, int, int],
    model_call_count: int,
    recovered_incomplete_model_stream_count: int,
    model_stream_reconciliation_blocker_count: int,
    tool_call_count: int,
    active_reservations: tuple[RolloutReservationFact, ...],
    through_sequence: int,
) -> RolloutBudgetStateFact:
    payload = {
        "account_id": account_id,
        "phase": phase,
        "charged_milliunits": sum(charged),
        "reserved_milliunits": sum(reserved),
        "exploration_charged_milliunits": charged[0],
        "exploration_reserved_milliunits": reserved[0],
        "finalization_agent_charged_milliunits": charged[1],
        "finalization_agent_reserved_milliunits": reserved[1],
        "finalization_compaction_charged_milliunits": charged[2],
        "finalization_compaction_reserved_milliunits": reserved[2],
        "finalization_tool_charged_milliunits": charged[3],
        "finalization_tool_reserved_milliunits": reserved[3],
        "model_call_count": model_call_count,
        "recovered_incomplete_model_stream_count": (
            recovered_incomplete_model_stream_count
        ),
        "model_stream_reconciliation_blocker_count": (
            model_stream_reconciliation_blocker_count
        ),
        "tool_call_count": tool_call_count,
        "active_reservations": active_reservations,
        "through_sequence": through_sequence,
    }
    return RolloutBudgetStateFact(
        **payload,
        state_fingerprint=context_fingerprint("rollout-budget-state:v1", payload),
    )
