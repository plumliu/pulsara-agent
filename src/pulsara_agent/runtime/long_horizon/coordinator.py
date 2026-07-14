"""Deterministic L5 rollout phase and admission planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.event import EventContext, RolloutPhaseTransitionedEvent
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    LongHorizonActionClass,
    ModelCallReservationQuoteFact,
    RolloutBudgetAccountFact,
    RolloutBudgetBucket,
    RolloutBudgetStateFact,
    RolloutPhase,
    RolloutTransitionReason,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.runtime.long_horizon.rollout import rollout_state_with_phase


class RolloutAdmissionBlocked(RuntimeError):
    """A durable reservation must settle before admission can be decided."""


@dataclass(frozen=True, slots=True)
class RolloutModelAdmissionPlan:
    action: Literal["admit", "transition", "blocked", "terminal"]
    budget_bucket: RolloutBudgetBucket | None
    transition_to: RolloutPhase | None
    reason_code: RolloutTransitionReason | None
    source_state_fingerprint: str


def plan_root_model_admission(
    *,
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    quote: ModelCallReservationQuoteFact,
    purpose: ModelCallPurpose,
) -> RolloutModelAdmissionPlan:
    """Plan one root model call without mutating account state."""

    if quote.policy_fingerprint != account.policy.policy_fingerprint:
        raise ValueError("rollout admission quote policy drifted")
    if state.account_id != account.account_id:
        raise ValueError("rollout admission state account drifted")
    if state.model_stream_reconciliation_blocker_count:
        return _plan(state, action="blocked")
    if state.model_call_count >= account.policy.emergency_model_call_limit:
        return _transition_plan(
            state,
            phase=RolloutPhase.EMERGENCY_HARD_STOP,
            reason=RolloutTransitionReason.EMERGENCY_CIRCUIT_BREAKER,
        )

    threshold_phase = phase_for_settled_exploration(account=account, state=state)
    if _phase_rank(threshold_phase) > _phase_rank(state.phase):
        return _transition_plan(
            state,
            phase=threshold_phase,
            reason=RolloutTransitionReason.WEIGHTED_TOKEN_THRESHOLD,
        )

    if state.phase in {
        RolloutPhase.EXPLORATION,
        RolloutPhase.WARNING,
        RolloutPhase.RESTRICTED,
    }:
        remaining = _remaining(account, state, RolloutBudgetBucket.EXPLORATION)
        if quote.reserved_milliunits <= remaining:
            return _plan(
                state,
                action="admit",
                bucket=RolloutBudgetBucket.EXPLORATION,
            )
        if _has_reclaimable_reservation(state, RolloutBudgetBucket.EXPLORATION):
            return _plan(
                state,
                action="blocked",
                bucket=RolloutBudgetBucket.EXPLORATION,
            )
        reason = (
            RolloutTransitionReason.EXPLORATION_COMPACTION_ADMISSION_UNREACHABLE
            if purpose is ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY
            else RolloutTransitionReason.EXPLORATION_ADMISSION_UNREACHABLE
        )
        return _transition_plan(
            state,
            phase=RolloutPhase.FINALIZATION_ONLY,
            reason=reason,
        )

    if state.phase is RolloutPhase.FINALIZATION_ONLY:
        bucket = (
            RolloutBudgetBucket.FINALIZATION_COMPACTION
            if purpose is ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY
            else RolloutBudgetBucket.FINALIZATION_AGENT
        )
        if quote.reserved_milliunits <= _remaining(account, state, bucket):
            return _plan(state, action="admit", bucket=bucket)
        if _has_reclaimable_reservation(state, bucket):
            return _plan(state, action="blocked", bucket=bucket)
        reason = (
            RolloutTransitionReason.WINDOW_COMPACTION_UNAVAILABLE
            if bucket is RolloutBudgetBucket.FINALIZATION_COMPACTION
            else RolloutTransitionReason.FINALIZATION_AGENT_UNAVAILABLE
        )
        return _transition_plan(
            state,
            phase=RolloutPhase.EXHAUSTED,
            reason=reason,
        )

    return _plan(state, action="terminal")


def plan_root_tool_admission(
    *,
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    attempted_tool_call_count: int,
) -> RolloutModelAdmissionPlan:
    """Plan the phase and bucket for one atomic tool-call batch."""

    if state.account_id != account.account_id:
        raise ValueError("rollout tool admission state account drifted")
    if attempted_tool_call_count <= 0:
        raise ValueError("rollout tool admission requires a non-empty batch")
    if state.model_stream_reconciliation_blocker_count:
        return _plan(state, action="blocked")
    if (
        state.tool_call_count + attempted_tool_call_count
        > account.policy.emergency_tool_call_limit
    ):
        return _transition_plan(
            state,
            phase=RolloutPhase.EMERGENCY_HARD_STOP,
            reason=RolloutTransitionReason.EMERGENCY_CIRCUIT_BREAKER,
        )

    threshold_phase = phase_for_settled_exploration(account=account, state=state)
    if _phase_rank(threshold_phase) > _phase_rank(state.phase):
        return _transition_plan(
            state,
            phase=threshold_phase,
            reason=RolloutTransitionReason.WEIGHTED_TOKEN_THRESHOLD,
        )
    if state.phase in {
        RolloutPhase.EXPLORATION,
        RolloutPhase.WARNING,
        RolloutPhase.RESTRICTED,
    }:
        return _plan(
            state,
            action="admit",
            bucket=RolloutBudgetBucket.EXPLORATION,
        )
    if state.phase is RolloutPhase.FINALIZATION_ONLY:
        return _plan(
            state,
            action="admit",
            bucket=RolloutBudgetBucket.FINALIZATION_TOOL,
        )
    return _plan(state, action="terminal")


def phase_for_settled_exploration(
    *,
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
) -> RolloutPhase:
    ratio = (
        state.exploration_charged_milliunits * 1_000_000
    ) // account.exploration_allowance_milliunits
    policy = account.policy
    if ratio >= policy.finalization_consumption_ratio_ppm:
        return RolloutPhase.FINALIZATION_ONLY
    if ratio >= policy.restricted_consumption_ratio_ppm:
        return RolloutPhase.RESTRICTED
    if ratio >= policy.warning_consumption_ratio_ppm:
        return RolloutPhase.WARNING
    return RolloutPhase.EXPLORATION


def build_rollout_phase_transition_event(
    *,
    event_context: EventContext,
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    plan: RolloutModelAdmissionPlan,
) -> RolloutPhaseTransitionedEvent:
    if plan.action != "transition" or plan.transition_to is None:
        raise ValueError("rollout transition event requires a transition plan")
    if plan.reason_code is None:
        raise ValueError("rollout transition plan lacks a reason")
    if plan.source_state_fingerprint != state.state_fingerprint:
        raise ValueError("rollout transition source state drifted")
    next_state = rollout_state_with_phase(
        state,
        phase=plan.transition_to,
        through_sequence=state.through_sequence + 1,
    )
    identity = {
        "account_id": account.account_id,
        "from_phase": state.phase,
        "to_phase": plan.transition_to,
        "source_through_sequence": state.through_sequence,
        "state_before_fingerprint": state.state_fingerprint,
        "reason_code": plan.reason_code,
    }
    event_id = "rollout_phase_transitioned:" + context_fingerprint(
        "rollout-phase-transition-event-id:v1", identity
    ).removeprefix("sha256:")
    return RolloutPhaseTransitionedEvent(
        id=event_id,
        **event_context.event_fields(),
        account_id=account.account_id,
        from_phase=state.phase,
        to_phase=plan.transition_to,
        source_through_sequence=state.through_sequence,
        state_before_fingerprint=state.state_fingerprint,
        state_after_fingerprint=next_state.state_fingerprint,
        reason_code=plan.reason_code,
    )


def allowed_action_classes_for_phase(
    phase: RolloutPhase,
) -> tuple[LongHorizonActionClass, ...]:
    if phase in {RolloutPhase.EXHAUSTED, RolloutPhase.EMERGENCY_HARD_STOP}:
        return ()
    if phase is RolloutPhase.FINALIZATION_ONLY:
        values = {
            LongHorizonActionClass.EVIDENCE_HYDRATION,
            LongHorizonActionClass.SYNTHESIS_MUTATION,
            LongHorizonActionClass.BOUNDED_VERIFICATION,
            LongHorizonActionClass.USER_INTERACTION,
            LongHorizonActionClass.PROCESS_CONTROL,
        }
    elif phase is RolloutPhase.RESTRICTED:
        values = set(LongHorizonActionClass) - {
            LongHorizonActionClass.EXTERNAL_ACTION
        }
    else:
        values = set(LongHorizonActionClass)
    return tuple(sorted(values, key=str))


def rollout_bucket_remaining(
    *,
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    bucket: RolloutBudgetBucket,
) -> int:
    if state.account_id != account.account_id:
        raise ValueError("rollout bucket state account drifted")
    return _remaining(account, state, bucket)


def _remaining(
    account: RolloutBudgetAccountFact,
    state: RolloutBudgetStateFact,
    bucket: RolloutBudgetBucket,
) -> int:
    values = {
        RolloutBudgetBucket.EXPLORATION: (
            account.exploration_allowance_milliunits,
            state.exploration_charged_milliunits,
            state.exploration_reserved_milliunits,
        ),
        RolloutBudgetBucket.FINALIZATION_AGENT: (
            account.finalization_agent_reserve_milliunits,
            state.finalization_agent_charged_milliunits,
            state.finalization_agent_reserved_milliunits,
        ),
        RolloutBudgetBucket.FINALIZATION_COMPACTION: (
            account.finalization_compaction_reserve_milliunits,
            state.finalization_compaction_charged_milliunits,
            state.finalization_compaction_reserved_milliunits,
        ),
        RolloutBudgetBucket.FINALIZATION_TOOL: (
            account.finalization_tool_reserve_milliunits,
            state.finalization_tool_charged_milliunits,
            state.finalization_tool_reserved_milliunits,
        ),
    }
    capacity, charged, reserved = values[bucket]
    return max(0, capacity - charged - reserved)


def _has_reclaimable_reservation(
    state: RolloutBudgetStateFact,
    bucket: RolloutBudgetBucket,
) -> bool:
    return any(item.budget_bucket is bucket for item in state.active_reservations)


def _phase_rank(phase: RolloutPhase) -> int:
    return tuple(RolloutPhase).index(phase)


def _plan(
    state: RolloutBudgetStateFact,
    *,
    action: Literal["admit", "transition", "blocked", "terminal"],
    bucket: RolloutBudgetBucket | None = None,
) -> RolloutModelAdmissionPlan:
    return RolloutModelAdmissionPlan(
        action=action,
        budget_bucket=bucket,
        transition_to=None,
        reason_code=None,
        source_state_fingerprint=state.state_fingerprint,
    )


def _transition_plan(
    state: RolloutBudgetStateFact,
    *,
    phase: RolloutPhase,
    reason: RolloutTransitionReason,
) -> RolloutModelAdmissionPlan:
    if _phase_rank(phase) <= _phase_rank(state.phase):
        return _plan(state, action="terminal")
    return RolloutModelAdmissionPlan(
        action="transition",
        budget_bucket=None,
        transition_to=phase,
        reason_code=reason,
        source_state_fingerprint=state.state_fingerprint,
    )


__all__ = [
    "RolloutAdmissionBlocked",
    "RolloutModelAdmissionPlan",
    "allowed_action_classes_for_phase",
    "build_rollout_phase_transition_event",
    "phase_for_settled_exploration",
    "plan_root_model_admission",
    "plan_root_tool_admission",
    "rollout_bucket_remaining",
]
