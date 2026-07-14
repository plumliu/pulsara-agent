"""Pure model-call rollout accounting formulas."""

from __future__ import annotations

from pulsara_agent.event import (
    EventContext,
    ModelCallEndEvent,
    RolloutBudgetReservationSettledEvent,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    RolloutBudgetAccountFact,
    RolloutReservationFact,
    RolloutUsageChargeFact,
)


class ModelCallAccountingError(RuntimeError):
    """Frozen model-call accounting facts are inconsistent."""


def build_model_reservation_settlement_event(
    *,
    event_context: EventContext,
    account: RolloutBudgetAccountFact,
    reservation: RolloutReservationFact,
    model_end: ModelCallEndEvent,
) -> RolloutBudgetReservationSettledEvent:
    """Build the one canonical model-call settlement from frozen facts."""

    quote = reservation.model_call_reservation_quote
    if quote is None or quote.quote_fact_fingerprint is None:
        raise ModelCallAccountingError(
            "model settlement cannot rebind its rollout quote"
        )
    if reservation.account_id != account.account_id:
        raise ModelCallAccountingError("model settlement account mismatch")
    if model_end.usage_status == "reported":
        basis = "provider_reported_usage"
    elif model_end.provider_dispatch_status == "not_started":
        basis = "not_started_zero"
    elif model_end.outcome == "cancelled":
        basis = "cancelled_reserved"
    else:
        basis = "reserved_missing_usage"
    usage = model_end.usage
    if basis == "provider_reported_usage":
        if usage is None:
            raise ModelCallAccountingError(
                "reported model settlement lacks usage"
            )
        cached = usage.cached_input_tokens or 0
        charged = (
            (usage.input_tokens - cached)
            * account.policy.non_cached_input_weight_milli
            + cached * account.policy.cached_input_weight_milli
            + usage.output_tokens * account.policy.output_weight_milli
        )
        charged_output = usage.output_tokens
        reported_input = usage.input_tokens
        reported_cached = cached
        reported_output = usage.output_tokens
    elif basis == "not_started_zero":
        charged = 0
        charged_output = 0
        reported_input = reported_cached = reported_output = None
    else:
        charged = quote.reserved_milliunits
        charged_output = quote.output_token_upper_bound
        reported_input = reported_cached = reported_output = None
    charge_payload = {
        "accounting_basis": basis,
        "reported_input_tokens": reported_input,
        "reported_cached_input_tokens": reported_cached,
        "reported_output_tokens": reported_output,
        "pre_send_estimated_input_tokens": model_end.estimated_input_tokens,
        "physical_input_token_upper_bound": quote.physical_input_token_upper_bound,
        "output_token_upper_bound": quote.output_token_upper_bound,
        "charged_output_tokens": charged_output,
        "charged_milliunits": charged,
        "reservation_quote_fact_fingerprint": quote.quote_fact_fingerprint,
        "policy_fingerprint": account.policy.policy_fingerprint,
    }
    charge = RolloutUsageChargeFact(
        **charge_payload,
        charge_fingerprint=context_fingerprint(
            "rollout-usage-charge:v1", charge_payload
        ),
    )
    return RolloutBudgetReservationSettledEvent(
        **event_context.event_fields(),
        id=f"rollout_reservation_settled:{reservation.reservation_id}",
        reservation_id=reservation.reservation_id,
        charged_milliunits=charged,
        usage_status=basis,
        usage_charge=charge,
        source_model_call_end_event_id=model_end.id,
        source_tool_result_event_id=None,
        child_usage_handoff=None,
    )


__all__ = [
    "ModelCallAccountingError",
    "build_model_reservation_settlement_event",
]
