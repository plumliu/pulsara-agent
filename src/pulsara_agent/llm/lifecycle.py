"""Caller-owned model lifecycle start contracts.

The caller freezes lifecycle identity and durable companion candidates before
LLMRuntime installs the service-owned stream handle.  LLMRuntime then performs
the final validation and consumes this exact bundle; it never infers lifecycle
kind from incidental context fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ReplyStartEvent,
    RolloutBudgetReservationCreatedEvent,
)
from pulsara_agent.llm.control_contract import (
    build_model_call_control_downstream_contract,
)
from pulsara_agent.event_log.serialization import (
    FrozenEventWriteCandidate,
    decode_event_write_candidate,
    freeze_event_write_candidate,
)
from pulsara_agent.llm.estimator import estimate_model_context_for_call
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    ModelCallReservationQuoteFact,
    RolloutPhase,
    RolloutReservationFact,
    calculate_model_call_reservation,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.model_call import ModelCallPurpose, ModelContextMode
from pulsara_agent.primitives.run_boundary import (
    ModelStreamRecoveryPlanFact,
    RunExecutionActivationFact,
)

if TYPE_CHECKING:
    from pulsara_agent.llm.request import LLMContext
    from pulsara_agent.llm.resolution import ResolvedModelCall
    from pulsara_agent.runtime.session import RuntimeSession


ModelLifecycleKind = Literal[
    "main_assistant_reply",
    "direct_internal_call",
    "window_compaction_summary",
]
RolloutAccountingMode = Literal[
    "root_account",
    "child_subaccount",
    "not_rollout_accounted",
]


@dataclass(frozen=True, slots=True)
class ModelLifecycleStartCommitBundle:
    resolved_model_call_id: str
    lifecycle_kind: ModelLifecycleKind
    reply_id: str | None
    stable_reply_start_event_id: str | None
    stable_reply_end_event_id: str | None
    rollout_accounting_mode: RolloutAccountingMode
    expected_rollout_account_state_fingerprint: str | None
    reservation_quote: ModelCallReservationQuoteFact | None
    recovery_plan: ModelStreamRecoveryPlanFact
    companion_candidates: tuple[FrozenEventWriteCandidate, ...]
    bundle_fingerprint: str

    @property
    def reservation(self) -> RolloutReservationFact | None:
        reservations = tuple(
            event.reservation
            for event in self._companion_events()
            if isinstance(event, RolloutBudgetReservationCreatedEvent)
        )
        if len(reservations) > 1:
            raise ValueError("model lifecycle bundle has multiple reservations")
        return reservations[0] if reservations else None

    def _companion_events(self) -> tuple[AgentEvent, ...]:
        return tuple(
            decode_event_write_candidate(candidate)
            for candidate in self.companion_candidates
        )


@dataclass(frozen=True, slots=True)
class PreparedModelRolloutReservation:
    reservation: RolloutReservationFact | None
    accounting_mode: RolloutAccountingMode
    expected_account_state_fingerprint: str | None


def prepare_model_lifecycle_start_bundle(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
    event_context: EventContext,
    runtime_session: RuntimeSession,
    lifecycle_kind: ModelLifecycleKind,
    run_execution_activation: RunExecutionActivationFact | None = None,
    window_compaction_started_event_id: str | None = None,
    extra_companion_candidates: tuple[AgentEvent, ...] = (),
    prepared_rollout_reservation: PreparedModelRolloutReservation | None = None,
) -> ModelLifecycleStartCommitBundle:
    """Freeze estimate-only lifecycle facts without starting provider I/O."""

    estimate = estimate_model_context_for_call(call=call, context=context)
    prepared_reservation = (
        prepared_rollout_reservation
        or prepare_model_rollout_reservation(
            call=call,
            event_context=event_context,
            runtime_session=runtime_session,
        )
    )
    reservation = prepared_reservation.reservation
    accounting_mode = prepared_reservation.accounting_mode
    expected_account_fingerprint = (
        prepared_reservation.expected_account_state_fingerprint
    )
    recovery_plan = _build_recovery_plan(
        call=call,
        lifecycle_kind=lifecycle_kind,
        run_execution_activation=run_execution_activation,
        reservation=reservation,
        pre_send_estimated_input_tokens=estimate.total_input_tokens,
        window_compaction_started_event_id=window_compaction_started_event_id,
    )
    main = lifecycle_kind == "main_assistant_reply"
    companions: list[AgentEvent] = []
    if main:
        companions.append(
            ReplyStartEvent(
                id=recovery_plan.reply_start_event_id,
                **event_context.event_fields(),
                name="assistant",
            )
        )
    if reservation is not None:
        companions.append(
            RolloutBudgetReservationCreatedEvent(
                id=f"rollout_reservation_created:{call.fact.resolved_model_call_id}",
                **event_context.event_fields(),
                reservation=reservation,
            )
        )
    companions.extend(extra_companion_candidates)
    frozen_companions = tuple(
        freeze_event_write_candidate(event) for event in companions
    )
    payload = _bundle_payload(
        call_id=call.fact.resolved_model_call_id,
        lifecycle_kind=lifecycle_kind,
        reply_id=event_context.reply_id if main else None,
        recovery_plan=recovery_plan,
        rollout_accounting_mode=accounting_mode,
        expected_rollout_account_state_fingerprint=expected_account_fingerprint,
        reservation_quote=(
            reservation.model_call_reservation_quote
            if reservation is not None
            else None
        ),
        companion_candidates=frozen_companions,
    )
    bundle = ModelLifecycleStartCommitBundle(
        **payload,
        bundle_fingerprint=context_fingerprint(
            "model-lifecycle-start-bundle:v1",
            _bundle_fingerprint_payload(payload),
        ),
    )
    validate_model_lifecycle_start_bundle(
        bundle,
        call=call,
        context=context,
        event_context=event_context,
    )
    return bundle


def prepare_model_rollout_reservation(
    *,
    call: ResolvedModelCall,
    event_context: EventContext,
    runtime_session: RuntimeSession,
) -> PreparedModelRolloutReservation:
    reservation, accounting_mode, expected_fingerprint = _prepare_model_reservation(
        call=call,
        event_context=event_context,
        runtime_session=runtime_session,
    )
    return PreparedModelRolloutReservation(
        reservation=reservation,
        accounting_mode=accounting_mode,
        expected_account_state_fingerprint=expected_fingerprint,
    )


def validate_model_lifecycle_start_bundle(
    bundle: ModelLifecycleStartCommitBundle,
    *,
    call: ResolvedModelCall,
    context: LLMContext,
    event_context: EventContext,
) -> None:
    if bundle.resolved_model_call_id != call.fact.resolved_model_call_id:
        raise ValueError("model lifecycle bundle call identity mismatch")
    plan = bundle.recovery_plan
    if plan.lifecycle_kind != bundle.lifecycle_kind:
        raise ValueError("model lifecycle bundle recovery kind mismatch")
    estimate = estimate_model_context_for_call(call=call, context=context)
    if estimate.total_input_tokens != plan.pre_send_estimated_input_tokens:
        raise ValueError("model lifecycle bundle estimate drifted")
    main = bundle.lifecycle_kind == "main_assistant_reply"
    window = bundle.lifecycle_kind == "window_compaction_summary"
    if main:
        if (
            call.fact.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP
            or call.fact.context_mode is not ModelContextMode.COMPILED
            or bundle.rollout_accounting_mode == "not_rollout_accounted"
        ):
            raise ValueError(
                "main lifecycle requires an accounted compiled agent call"
            )
        if context.model_call_index is None:
            raise ValueError("main lifecycle requires a model call index")
        if (
            bundle.reply_id != event_context.reply_id
            or bundle.stable_reply_start_event_id != plan.reply_start_event_id
            or bundle.stable_reply_end_event_id != plan.stable_reply_end_event_id
        ):
            raise ValueError("main lifecycle reply identity mismatch")
    elif (
        bundle.reply_id is not None
        or bundle.stable_reply_start_event_id is not None
        or bundle.stable_reply_end_event_id is not None
    ):
        raise ValueError("direct/window lifecycle cannot carry reply identity")
    elif window:
        if (
            call.fact.purpose
            is not ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY
            or call.fact.context_mode is not ModelContextMode.DIRECT
            or bundle.rollout_accounting_mode == "not_rollout_accounted"
        ):
            raise ValueError(
                "window lifecycle requires an accounted compaction call"
            )
    elif (
        call.fact.purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        or call.fact.context_mode is not ModelContextMode.DIRECT
        or bundle.rollout_accounting_mode != "not_rollout_accounted"
    ):
        raise ValueError("direct lifecycle requires an unaccounted direct call")
    reservation = bundle.reservation
    if (reservation is None) != (bundle.reservation_quote is None):
        raise ValueError("model lifecycle reservation quote is all-or-none")
    if reservation is not None:
        if (
            reservation.owner_id != bundle.resolved_model_call_id
            or reservation.model_call_reservation_quote != bundle.reservation_quote
            or plan.reservation_id != reservation.reservation_id
        ):
            raise ValueError("model lifecycle reservation identity mismatch")
        if bundle.rollout_accounting_mode == "not_rollout_accounted":
            raise ValueError("accounted model bundle cannot use unaccounted mode")
    elif bundle.rollout_accounting_mode != "not_rollout_accounted":
        raise ValueError("unreserved model bundle must be not-rollout-accounted")
    companion_events = bundle._companion_events()
    reply_starts = tuple(
        event
        for event in companion_events
        if isinstance(event, ReplyStartEvent)
    )
    if main != (len(reply_starts) == 1):
        raise ValueError("model lifecycle reply companion mismatch")
    if any(
        (
            event.run_id,
            event.turn_id,
            event.reply_id,
        )
        != (
            event_context.run_id,
            event_context.turn_id,
            event_context.reply_id,
        )
        for event in companion_events
    ):
        raise ValueError("model lifecycle companion context mismatch")
    reservation_events = tuple(
        event
        for event in companion_events
        if isinstance(event, RolloutBudgetReservationCreatedEvent)
    )
    if main:
        if (
            len(companion_events) != 2
            or not isinstance(companion_events[0], ReplyStartEvent)
            or len(reservation_events) != 1
        ):
            raise ValueError(
                "main lifecycle requires reply-start plus one reservation"
            )
    elif window:
        started = tuple(
            event
            for event in companion_events
            if event.id == plan.window_compaction_started_event_id
            and event.type.value == "CONTEXT_WINDOW_COMPACTION_STARTED"
        )
        if (
            len(companion_events) != 2
            or len(reservation_events) != 1
            or len(started) != 1
        ):
            raise ValueError(
                "window lifecycle requires reservation plus matching started fact"
            )
    elif companion_events:
        raise ValueError("direct lifecycle cannot carry start companions")
    payload = _bundle_payload(
        call_id=bundle.resolved_model_call_id,
        lifecycle_kind=bundle.lifecycle_kind,
        reply_id=bundle.reply_id,
        recovery_plan=bundle.recovery_plan,
        rollout_accounting_mode=bundle.rollout_accounting_mode,
        expected_rollout_account_state_fingerprint=(
            bundle.expected_rollout_account_state_fingerprint
        ),
        reservation_quote=bundle.reservation_quote,
        companion_candidates=bundle.companion_candidates,
    )
    expected = context_fingerprint(
        "model-lifecycle-start-bundle:v1",
        _bundle_fingerprint_payload(payload),
    )
    if bundle.bundle_fingerprint != expected:
        raise ValueError("model lifecycle start bundle fingerprint mismatch")


def _prepare_model_reservation(
    *,
    call: ResolvedModelCall,
    event_context: EventContext,
    runtime_session: RuntimeSession,
) -> tuple[RolloutReservationFact | None, RolloutAccountingMode, str | None]:
    from pulsara_agent.runtime.long_horizon.accounting import (
        resolve_run_rollout_binding,
    )

    if call.fact.purpose not in {
        ModelCallPurpose.AGENT_MODEL_LOOP,
        ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY,
    }:
        return None, "not_rollout_accounted", None
    run_start = runtime_session.long_horizon_state_store.run_start(
        event_context.run_id
    )
    if run_start is None:
        return None, "not_rollout_accounted", None
    binding = resolve_run_rollout_binding(
        runtime_session,
        run_id=event_context.run_id,
    )
    account = binding.account
    state = binding.parent_state
    quote = calculate_model_call_reservation(
        target=call.target.fact,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        policy=account.policy,
    )
    if binding.child_state is not None:
        if state.phase in {
            RolloutPhase.FINALIZATION_ONLY,
            RolloutPhase.EXHAUSTED,
            RolloutPhase.EMERGENCY_HARD_STOP,
        }:
            raise RuntimeError(
                "child model call is unavailable after parent finalization"
            )
        if quote.reserved_milliunits > binding.child_state.remaining_milliunits:
            raise RuntimeError("child model call exceeds its rollout subaccount hard ceiling")
        bucket = "exploration"
        source_sequence = binding.child_state.through_sequence
        accounting_mode: RolloutAccountingMode = "child_subaccount"
        expected_state_fingerprint = binding.child_state.state_fingerprint
    else:
        from pulsara_agent.runtime.long_horizon.coordinator import (
            plan_root_model_admission,
        )

        admission = plan_root_model_admission(
            account=account,
            state=state,
            quote=quote,
            purpose=call.fact.purpose,
        )
        if admission.action != "admit" or admission.budget_bucket is None:
            raise RuntimeError(
                "model lifecycle start requires a completed rollout admission"
            )
        bucket = admission.budget_bucket.value
        source_sequence = state.through_sequence
        accounting_mode = "root_account"
        expected_state_fingerprint = state.state_fingerprint
    reservation_payload = {
        "reservation_id": (
            f"rollout_reservation:model:{call.fact.resolved_model_call_id}"
        ),
        "account_id": account.account_id,
        "owner_kind": "model_call",
        "owner_id": call.fact.resolved_model_call_id,
        "phase_at_reservation": state.phase,
        "budget_bucket": bucket,
        "reserved_milliunits": quote.reserved_milliunits,
        "model_call_reservation_quote": quote,
        "source_sequence": source_sequence,
    }
    return (
        RolloutReservationFact(
            **reservation_payload,
            semantic_fingerprint=context_fingerprint(
                "rollout-reservation:v1", reservation_payload
            ),
        ),
        accounting_mode,
        expected_state_fingerprint,
    )


def _build_recovery_plan(
    *,
    call: ResolvedModelCall,
    lifecycle_kind: ModelLifecycleKind,
    run_execution_activation: RunExecutionActivationFact | None,
    reservation: RolloutReservationFact | None,
    pre_send_estimated_input_tokens: int,
    window_compaction_started_event_id: str | None,
) -> ModelStreamRecoveryPlanFact:
    call_id = call.fact.resolved_model_call_id
    main = lifecycle_kind == "main_assistant_reply"
    if main:
        if run_execution_activation is None:
            raise ValueError("main model lifecycle requires run activation")
        downstream = build_model_call_control_downstream_contract()
    else:
        if run_execution_activation is not None:
            raise ValueError("direct/window lifecycle forbids run activation")
        downstream = None
    payload = {
        "schema_version": "model_stream_recovery_plan.v1",
        "lifecycle_kind": lifecycle_kind,
        "model_call_start_event_id": f"model_call_start:{call_id}",
        "stable_model_call_end_event_id": f"model_call_end:{call_id}",
        "reply_start_event_id": f"reply_start:{call_id}" if main else None,
        "stable_reply_end_event_id": f"reply_end:{call_id}" if main else None,
        "reservation_id": (
            reservation.reservation_id if reservation is not None else None
        ),
        "reservation_quote_fingerprint": (
            reservation.model_call_reservation_quote.quote_fact_fingerprint
            if reservation is not None
            and reservation.model_call_reservation_quote is not None
            else None
        ),
        "stable_settlement_event_id": (
            f"rollout_reservation_settled:{reservation.reservation_id}"
            if reservation is not None
            else None
        ),
        "window_compaction_started_event_id": window_compaction_started_event_id,
        "pre_send_estimated_input_tokens": pre_send_estimated_input_tokens,
        "run_execution_activation": run_execution_activation if main else None,
        "control_downstream_predicate_contract": downstream,
    }
    return ModelStreamRecoveryPlanFact(
        **payload,
        recovery_plan_fingerprint=sha256_fingerprint(
            "model-stream-recovery-plan:v1",
            {
                **payload,
                "run_execution_activation": (
                    run_execution_activation.model_dump(mode="json")
                    if main and run_execution_activation is not None
                    else None
                ),
                "control_downstream_predicate_contract": (
                    downstream.model_dump(mode="json")
                    if downstream is not None
                    else None
                ),
            },
        ),
    )


def _bundle_payload(
    *,
    call_id: str,
    lifecycle_kind: ModelLifecycleKind,
    reply_id: str | None,
    recovery_plan: ModelStreamRecoveryPlanFact,
    rollout_accounting_mode: RolloutAccountingMode,
    expected_rollout_account_state_fingerprint: str | None,
    reservation_quote: ModelCallReservationQuoteFact | None,
    companion_candidates: tuple[FrozenEventWriteCandidate, ...],
) -> dict[str, object]:
    return {
        "resolved_model_call_id": call_id,
        "lifecycle_kind": lifecycle_kind,
        "reply_id": reply_id,
        "stable_reply_start_event_id": recovery_plan.reply_start_event_id,
        "stable_reply_end_event_id": recovery_plan.stable_reply_end_event_id,
        "rollout_accounting_mode": rollout_accounting_mode,
        "expected_rollout_account_state_fingerprint": (
            expected_rollout_account_state_fingerprint
        ),
        "reservation_quote": reservation_quote,
        "recovery_plan": recovery_plan,
        "companion_candidates": companion_candidates,
    }


def _bundle_fingerprint_payload(payload: dict[str, object]) -> dict[str, object]:
    candidates = payload["companion_candidates"]
    assert isinstance(candidates, tuple)
    return {
        **payload,
        "companion_candidates": tuple(
            candidate.fingerprint_payload()
            for candidate in candidates
            if isinstance(candidate, FrozenEventWriteCandidate)
        ),
    }


__all__ = [
    "ModelLifecycleKind",
    "ModelLifecycleStartCommitBundle",
    "PreparedModelRolloutReservation",
    "prepare_model_lifecycle_start_bundle",
    "prepare_model_rollout_reservation",
    "validate_model_lifecycle_start_bundle",
]
