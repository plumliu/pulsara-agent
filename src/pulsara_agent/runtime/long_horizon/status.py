"""Pure rollout-status and exact-recurrence projection."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from pulsara_agent.event import (
    CapabilityGateDecisionEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RolloutPhaseTransitionedEvent,
    RunStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    context_fingerprint,
)
from pulsara_agent.primitives.long_horizon import (
    LongHorizonActionClass,
    LongHorizonRolloutStatusCandidateFact,
    RecentToolActionRecurrenceFact,
    RolloutBudgetAccountFact,
    RolloutBudgetStateFact,
    RolloutPhase,
    RolloutStatusHintPolicyFact,
    RolloutStatusShadowProjectionFact,
    ToolActionClassificationFact,
)
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventAuthorityView,
    ContextEventSlice,
)
from pulsara_agent.runtime.long_horizon.rollout import (
    apply_rollout_event,
    initial_rollout_budget_state,
)
from pulsara_agent.runtime.long_horizon.coordinator import (
    allowed_action_classes_for_phase,
)
from pulsara_agent.runtime.long_horizon.store import advance_rollout_state
from pulsara_agent.runtime.result_semantics import (
    typed_terminal_result_semantic_fingerprint,
)


_RECURRENCE_ACTION_CLASSES = frozenset(
    {
        LongHorizonActionClass.EVIDENCE_ACQUISITION,
        LongHorizonActionClass.EXTERNAL_ACTION,
    }
)


class RolloutStatusProjectionError(RuntimeError):
    """Canonical inputs cannot produce one exact rollout status projection."""


@dataclass(frozen=True, slots=True)
class _SettledToolOutcome:
    classification: ToolActionClassificationFact
    terminal_outcome_fingerprint: str
    terminal_ref: ContextEventReferenceFact


def derive_rollout_status_shadow(
    *,
    event_slice: ContextEventSlice,
    account_id: str,
    policy: RolloutStatusHintPolicyFact,
) -> RolloutStatusShadowProjectionFact:
    """Derive the non-model-visible L0B status shadow from one frozen ledger slice."""

    decoded = tuple(
        (stored, stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))
        for stored in event_slice.events
    )
    account, state = _fold_account(decoded=decoded, account_id=account_id)
    outcomes = _settled_tool_outcomes(
        decoded=decoded,
        account=account,
        recent_window=policy.recent_tool_call_window,
        runtime_session_id=event_slice.runtime_session_id,
    )
    recurrence = _recurrence(outcomes=outcomes, policy=policy)
    ratio_ppm = (
        state.exploration_charged_milliunits * 1_000_000
    ) // account.exploration_allowance_milliunits
    payload = {
        "account_id": account.account_id,
        "source_through_sequence": event_slice.through_sequence,
        "settled_model_call_count": state.model_call_count,
        "settled_tool_call_count": state.tool_call_count,
        "exploration_consumption_ratio_ppm": ratio_ppm,
        "recurrence": recurrence,
        "model_visible": False,
    }
    return RolloutStatusShadowProjectionFact(
        **payload,
        derivation_fingerprint=context_fingerprint(
            "rollout-status-shadow:v1", payload
        ),
    )


def derive_rollout_status_candidate(
    *,
    event_slice: ContextEventSlice,
    account_id: str,
    policy: RolloutStatusHintPolicyFact,
) -> LongHorizonRolloutStatusCandidateFact | None:
    """Derive the sole model-visible status fact from one frozen ledger slice."""

    decoded = tuple(
        (stored, stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))
        for stored in event_slice.events
    )
    account, state = _fold_account(decoded=decoded, account_id=account_id)
    outcomes = _settled_tool_outcomes(
        decoded=decoded,
        account=account,
        recent_window=policy.recent_tool_call_window,
        runtime_session_id=event_slice.runtime_session_id,
    )
    recurrence = _recurrence(outcomes=outcomes, policy=policy)
    if state.phase is RolloutPhase.EXPLORATION and not recurrence:
        return None
    return _build_rollout_status_candidate(
        decoded=decoded,
        account=account,
        state=state,
        policy=policy,
        runtime_session_id=event_slice.runtime_session_id,
        recurrence=recurrence,
    )


def derive_rollout_status_candidate_from_state(
    *,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    policy: RolloutStatusHintPolicyFact,
) -> LongHorizonRolloutStatusCandidateFact | None:
    """Render status from the session-owned reducer plus bounded evidence facts."""

    if account.account_id != state.account_id:
        raise RolloutStatusProjectionError("rollout account/state identity mismatch")
    decoded = tuple(
        (stored, stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))
        for stored in event_slice.events
    )
    outcomes = _settled_tool_outcomes(
        decoded=decoded,
        account=account,
        recent_window=policy.recent_tool_call_window,
        runtime_session_id=event_slice.runtime_session_id,
    )
    recurrence = _recurrence(outcomes=outcomes, policy=policy)
    return _build_rollout_status_candidate(
        decoded=decoded,
        account=account,
        state=state,
        policy=policy,
        runtime_session_id=event_slice.runtime_session_id,
        recurrence=recurrence,
    )


def fold_sparse_rollout_state(
    *,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    account_id: str,
    through_sequence: int | None = None,
) -> tuple[RolloutBudgetAccountFact, RolloutBudgetStateFact]:
    """Fold complete rollout facts while treating omitted event families as no-ops."""

    decoded = tuple(
        stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        for stored in event_slice.events
    )
    openings = tuple(
        event
        for event in decoded
        if isinstance(event, RolloutBudgetAccountOpenedEvent)
        and event.account.account_id == account_id
    )
    if len(openings) != 1 or openings[0].sequence is None:
        raise RolloutStatusProjectionError(
            "sparse rollout fold requires one committed account opening"
        )
    account = openings[0].account
    state = initial_rollout_budget_state(
        account=account,
        through_sequence=openings[0].sequence,
    )
    for event in decoded:
        sequence = getattr(event, "sequence", None)
        if not isinstance(sequence, int) or sequence <= state.through_sequence:
            continue
        if not isinstance(
            event,
            (
                RolloutBudgetReservationCreatedEvent,
                RolloutBudgetReservationSettledEvent,
                RolloutPhaseTransitionedEvent,
            ),
        ):
            continue
        state = advance_rollout_state(state, sequence - 1)
        _account, next_state = apply_rollout_event(
            account=account,
            state=state,
            event=event,
        )
        if next_state is None:
            raise RolloutStatusProjectionError("sparse rollout state disappeared")
        state = next_state
    target_through = (
        event_slice.through_sequence
        if through_sequence is None
        else through_sequence
    )
    if target_through < event_slice.through_sequence:
        raise RolloutStatusProjectionError(
            "sparse rollout high-water precedes selected authority"
        )
    return account, advance_rollout_state(state, target_through)


def _build_rollout_status_candidate(
    *,
    decoded: tuple[tuple[object, object], ...],
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    policy: RolloutStatusHintPolicyFact,
    runtime_session_id: str,
    recurrence: tuple[RecentToolActionRecurrenceFact, ...],
) -> LongHorizonRolloutStatusCandidateFact | None:
    if state.phase is RolloutPhase.EXPLORATION and not recurrence:
        return None
    ratio_ppm = (
        state.exploration_charged_milliunits * 1_000_000
    ) // account.exploration_allowance_milliunits
    payload = {
        "schema_version": "long_horizon_rollout_status_candidate.v1",
        "account_id": account.account_id,
        "rollout_phase": state.phase,
        "settled_model_call_count": state.model_call_count,
        "settled_tool_call_count": state.tool_call_count,
        "exploration_consumption_ratio_ppm": ratio_ppm,
        "remaining_exploration_milliunits": max(
            0,
            account.exploration_allowance_milliunits
            - state.exploration_charged_milliunits
            - state.exploration_reserved_milliunits,
        ),
        "finalization_reserve_milliunits": account.finalization_reserve_milliunits,
        "allowed_action_classes": allowed_action_classes_for_phase(state.phase),
        "recurrence": recurrence,
        "source_event_refs": _status_source_refs(
            decoded=decoded,
            account=account,
            recurrence=recurrence,
            runtime_session_id=runtime_session_id,
        ),
    }
    return LongHorizonRolloutStatusCandidateFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "long-horizon-rollout-status-candidate:v1", payload
        ),
    )


def derive_rollout_status_candidate_for_run(
    *,
    event_slice: ContextEventSlice,
    run_id: str,
) -> LongHorizonRolloutStatusCandidateFact | None:
    """Resolve the run-frozen policy and derive its root-account status candidate."""

    starts = tuple(
        event
        for stored in event_slice.events
        if stored.run_id == run_id
        if isinstance(
            (event := stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
            RunStartEvent,
        )
    )
    if len(starts) != 1:
        raise RolloutStatusProjectionError(
            "status candidate requires one matching RunStart event"
        )
    start = starts[0]
    contract = start.long_horizon
    if contract.rollout_account_owner_runtime_session_id != event_slice.runtime_session_id:
        # Child ledgers do not contain the parent account. A future child status
        # carrier must use its own frozen child-account facts, not a live parent read.
        return None
    return derive_rollout_status_candidate(
        event_slice=event_slice,
        account_id=contract.rollout_account_id,
        policy=contract.rollout_status_hint_policy,
    )


def render_rollout_status_candidate(
    candidate: LongHorizonRolloutStatusCandidateFact,
) -> str:
    """Render neutral state facts without prescribing whether work should continue."""

    allowed = ",".join(item.value for item in candidate.allowed_action_classes) or "none"
    lines = [
        "[rollout status]",
        f"phase={candidate.rollout_phase.value}",
        f"settled_model_calls={candidate.settled_model_call_count}",
        f"settled_tool_calls={candidate.settled_tool_call_count}",
        (
            "exploration_consumed="
            f"{candidate.exploration_consumption_ratio_ppm}ppm"
        ),
        (
            "remaining_exploration_milliunits="
            f"{candidate.remaining_exploration_milliunits}"
        ),
        (
            "finalization_reserve_milliunits="
            f"{candidate.finalization_reserve_milliunits}"
        ),
        f"currently_allowed_action_classes={allowed}",
    ]
    if candidate.recurrence:
        lines.append("recent_exact_action_recurrence:")
        lines.extend(
            (
                f"- action_class={item.action_class.value} "
                f"action_occurrences={item.action_occurrence_count} "
                "equivalent_terminal_outcomes="
                f"{item.equivalent_terminal_outcome_count}"
            )
            for item in candidate.recurrence
        )
    return "\n".join(lines)


def rollout_status_candidate_is_required(
    candidate: LongHorizonRolloutStatusCandidateFact,
) -> bool:
    """Non-exploration state is control-critical; recurrence alone is informational."""

    return candidate.rollout_phase is not RolloutPhase.EXPLORATION


def _fold_account(
    *,
    decoded: tuple[tuple[object, object], ...],
    account_id: str,
) -> tuple[RolloutBudgetAccountFact, RolloutBudgetStateFact]:
    openings = tuple(
        event
        for _stored, event in decoded
        if isinstance(event, RolloutBudgetAccountOpenedEvent)
        and event.account.account_id == account_id
    )
    if len(openings) != 1:
        raise RolloutStatusProjectionError(
            "status projection requires one matching rollout account opening"
        )
    opening = openings[0]
    if opening.sequence is None:
        raise RolloutStatusProjectionError("rollout opening is not committed")
    account = opening.account
    state = initial_rollout_budget_state(
        account=account,
        through_sequence=opening.sequence,
    )
    for _stored, event in decoded:
        sequence = getattr(event, "sequence", None)
        if not isinstance(sequence, int) or sequence <= opening.sequence:
            continue
        _account, next_state = apply_rollout_event(
            account=account,
            state=state,
            event=event,
        )
        if next_state is None:
            raise RolloutStatusProjectionError("rollout state disappeared")
        state = next_state
    if state.through_sequence != decoded[-1][0].sequence:  # type: ignore[attr-defined]
        raise RolloutStatusProjectionError("rollout state high-water drifted")
    return account, state


def _settled_tool_outcomes(
    *,
    decoded: tuple[tuple[object, object], ...],
    account: RolloutBudgetAccountFact,
    recent_window: int,
    runtime_session_id: str,
) -> tuple[_SettledToolOutcome, ...]:
    reservations = {
        event.reservation.reservation_id: event.reservation
        for _stored, event in decoded
        if isinstance(event, RolloutBudgetReservationCreatedEvent)
        and event.reservation.account_id == account.account_id
        and event.reservation.owner_kind == "tool_call"
    }
    by_id = {getattr(event, "id", ""): (stored, event) for stored, event in decoded}
    gate_events: dict[str, list[CapabilityGateDecisionEvent]] = defaultdict(list)
    semantic_events: dict[
        str, list[ToolResultTextDeltaEvent | ToolResultDataDeltaEvent]
    ] = defaultdict(list)
    for _stored, event in decoded:
        if (
            isinstance(event, CapabilityGateDecisionEvent)
            and event.action_classification is not None
        ):
            gate_events[event.tool_call_id].append(event)
        elif isinstance(event, (ToolResultTextDeltaEvent, ToolResultDataDeltaEvent)):
            semantic_events[event.tool_call_id].append(event)

    settlements = tuple(
        event
        for _stored, event in decoded
        if isinstance(event, RolloutBudgetReservationSettledEvent)
        and event.usage_status == "tool_terminal"
        and event.reservation_id in reservations
        and event.source_tool_result_event_id in by_id
    )[-recent_window:]
    outcomes: list[_SettledToolOutcome] = []
    for settlement in settlements:
        reservation = reservations[settlement.reservation_id]
        terminal_id = settlement.source_tool_result_event_id
        pair = by_id.get(terminal_id or "")
        if pair is None or not isinstance(pair[1], ToolResultEndEvent):
            raise RolloutStatusProjectionError(
                "tool settlement lacks its typed terminal result"
            )
        stored, terminal = pair
        if terminal.tool_call_id != reservation.owner_id:
            raise RolloutStatusProjectionError(
                "tool settlement terminal identity mismatch"
            )
        eligible_gates = tuple(
            event
            for event in gate_events.get(terminal.tool_call_id, ())
            if _sequence(event) < _sequence(terminal)
        )
        if not eligible_gates:
            raise RolloutStatusProjectionError(
                "settled tool result lacks action classification"
            )
        classifications = tuple(
            event.action_classification for event in eligible_gates
        )
        assert all(item is not None for item in classifications)
        fingerprints = {
            item.classification_fingerprint
            for item in classifications
            if item is not None
        }
        if len(fingerprints) != 1:
            raise RolloutStatusProjectionError(
                "tool call has conflicting action classifications"
            )
        classification = classifications[-1]
        assert classification is not None
        if classification.action_class not in _RECURRENCE_ACTION_CLASSES:
            continue
        outcome_fingerprint = typed_terminal_result_semantic_fingerprint(
            terminal=terminal,
            semantic_events=tuple(
                event
                for event in semantic_events.get(terminal.tool_call_id, ())
                if _sequence(event) < _sequence(terminal)
            ),
        )
        outcomes.append(
            _SettledToolOutcome(
                classification=classification,
                terminal_outcome_fingerprint=outcome_fingerprint,
                terminal_ref=stored.to_reference(runtime_session_id),
            )
        )
    return tuple(outcomes)


def _recurrence(
    *,
    outcomes: tuple[_SettledToolOutcome, ...],
    policy: RolloutStatusHintPolicyFact,
) -> tuple[RecentToolActionRecurrenceFact, ...]:
    action_counts: dict[str, int] = defaultdict(int)
    equivalent: dict[tuple[str, str], list[_SettledToolOutcome]] = defaultdict(list)
    for item in outcomes:
        action = item.classification.normalized_action_fingerprint
        action_counts[action] += 1
        equivalent[(action, item.terminal_outcome_fingerprint)].append(item)

    candidates: list[tuple[int, RecentToolActionRecurrenceFact]] = []
    for (action, terminal_outcome), matching in equivalent.items():
        if len(matching) < policy.minimum_equivalent_outcome_occurrences:
            continue
        classification = matching[-1].classification
        refs = tuple(item.terminal_ref for item in matching)
        payload = {
            "normalized_action_fingerprint": action,
            "terminal_outcome_fingerprint": terminal_outcome,
            "action_class": classification.action_class,
            "action_occurrence_count": action_counts[action],
            "equivalent_terminal_outcome_count": len(matching),
            "recent_tool_call_window": policy.recent_tool_call_window,
            "source_event_refs": refs,
        }
        fact = RecentToolActionRecurrenceFact(
            **payload,
            recurrence_fingerprint=context_fingerprint(
                "recent-tool-action-recurrence:v1", payload
            ),
        )
        candidates.append((refs[-1].sequence, fact))
    candidates.sort(
        key=lambda item: (
            -item[0],
            item[1].normalized_action_fingerprint,
            item[1].terminal_outcome_fingerprint,
        )
    )
    return tuple(item[1] for item in candidates[: policy.max_recurrence_entries])


def _status_source_refs(
    *,
    decoded: tuple[tuple[object, object], ...],
    account: RolloutBudgetAccountFact,
    recurrence: tuple[RecentToolActionRecurrenceFact, ...],
    runtime_session_id: str,
) -> tuple[ContextEventReferenceFact, ...]:
    opening = next(
        stored
        for stored, event in decoded
        if isinstance(event, RolloutBudgetAccountOpenedEvent)
        and event.account.account_id == account.account_id
    )
    reservation_ids = {
        event.reservation.reservation_id
        for _stored, event in decoded
        if isinstance(event, RolloutBudgetReservationCreatedEvent)
        and event.reservation.account_id == account.account_id
    }
    state_events = tuple(
        stored
        for stored, event in decoded
        if (
            isinstance(event, RolloutBudgetReservationCreatedEvent)
            and event.reservation.account_id == account.account_id
        )
        or (
            isinstance(event, RolloutBudgetReservationSettledEvent)
            and event.reservation_id in reservation_ids
        )
        or (
            isinstance(event, RolloutPhaseTransitionedEvent)
            and event.account_id == account.account_id
        )
    )
    transitions = tuple(
        stored
        for stored, event in decoded
        if isinstance(event, RolloutPhaseTransitionedEvent)
        and event.account_id == account.account_id
    )
    refs = {
        opening.event_id: opening.to_reference(runtime_session_id),
        **(
            {
                state_events[-1].event_id: state_events[-1].to_reference(
                    runtime_session_id
                )
            }
            if state_events
            else {}
        ),
        **(
            {
                transitions[-1].event_id: transitions[-1].to_reference(
                    runtime_session_id
                )
            }
            if transitions
            else {}
        ),
    }
    for item in recurrence:
        refs.update({ref.event_id: ref for ref in item.source_event_refs})
    return tuple(sorted(refs.values(), key=lambda item: (item.sequence, item.event_id)))


def _sequence(event: object) -> int:
    sequence = getattr(event, "sequence", None)
    if not isinstance(sequence, int) or sequence < 1:
        raise RolloutStatusProjectionError("status projection requires committed events")
    return sequence


__all__ = [
    "RolloutStatusProjectionError",
    "derive_rollout_status_candidate",
    "derive_rollout_status_candidate_from_state",
    "fold_sparse_rollout_state",
    "derive_rollout_status_candidate_for_run",
    "derive_rollout_status_shadow",
    "render_rollout_status_candidate",
    "rollout_status_candidate_is_required",
]
