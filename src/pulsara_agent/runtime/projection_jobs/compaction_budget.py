"""Deterministic input and session budget state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.event import ModelCallEndEvent
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.compaction import (
    BackgroundDerivedWorkBudgetAccountFact,
    BackgroundDerivedWorkBudgetAccountReferenceFact,
    BackgroundDerivedWorkBudgetAdmissionFailureFact,
    BackgroundDerivedWorkBudgetPolicyFact,
    BackgroundDerivedWorkBudgetReservationFact,
    BackgroundDerivedWorkBudgetSettlementFact,
    CompactionMemoryInputBudgetFailureFact,
    DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
    EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT,
    ResolvedExtractionInputBudgetAttributionFact,
    build_background_budget_genesis,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.long_horizon import (
    ModelCallReservationQuoteFact,
    default_rollout_budget_policy,
)
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact


@dataclass(frozen=True, slots=True)
class BackgroundBudgetReserveOutcome:
    account: BackgroundDerivedWorkBudgetAccountFact
    reservation: BackgroundDerivedWorkBudgetReservationFact | None
    failure: BackgroundDerivedWorkBudgetAdmissionFailureFact | None


@dataclass(frozen=True, slots=True)
class BackgroundBudgetSettlementOutcome:
    account: BackgroundDerivedWorkBudgetAccountFact
    settlement: BackgroundDerivedWorkBudgetSettlementFact | None
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class ExtractionInputBudgetOutcome:
    budget: ResolvedExtractionInputBudgetAttributionFact
    failure: CompactionMemoryInputBudgetFailureFact | None


def _account_reference(
    account: BackgroundDerivedWorkBudgetAccountFact,
) -> BackgroundDerivedWorkBudgetAccountReferenceFact:
    return build_frozen_fact(
        BackgroundDerivedWorkBudgetAccountReferenceFact,
        schema_version="background_derived_work_budget_account_ref.v1",
        runtime_session_id=account.runtime_session_id,
        account_revision=account.account_revision,
        account_fingerprint=account.account_fingerprint,
    )


def validate_background_budget_account(
    *,
    account: BackgroundDerivedWorkBudgetAccountFact,
    policy: BackgroundDerivedWorkBudgetPolicyFact,
) -> None:
    if account.policy_fingerprint != policy.policy_fingerprint:
        raise ValueError("background budget policy/account mismatch")
    if account.dispatched_call_count > policy.maximum_dispatched_calls_per_session:
        raise ValueError("background budget dispatched-call cap exceeded")
    if (
        account.settled_charged_input_tokens + account.open_reserved_input_tokens
        > policy.maximum_physical_input_tokens_per_session
    ):
        raise ValueError("background budget input-token cap exceeded")
    if (
        account.settled_charged_output_tokens + account.open_reserved_output_tokens
        > policy.maximum_output_tokens_per_session
    ):
        raise ValueError("background budget output-token cap exceeded")
    if (
        account.settled_charged_milliunits + account.open_reserved_milliunits
        > policy.maximum_milliunits_per_session
    ):
        raise ValueError("background budget milliunit cap exceeded")


def resolve_extraction_input_budget(
    *,
    target: ResolvedModelTargetFact,
    static_prompt_tokens: int,
    carrier_and_framing_reserve_tokens: int = 256,
    safety_margin_tokens: int = 256,
    maximum_physical_input_utf8_bytes: int = 512 * 1024,
) -> ExtractionInputBudgetOutcome:
    if min(
        static_prompt_tokens,
        carrier_and_framing_reserve_tokens,
        safety_margin_tokens,
    ) < 0:
        raise ValueError("extraction input budget components must be non-negative")
    output_reserve = target.context_budget.effective_output_tokens
    usable = max(
        0,
        target.context_budget.input_budget_tokens
        - static_prompt_tokens
        - carrier_and_framing_reserve_tokens
        - output_reserve
        - safety_margin_tokens,
    )
    attribution = build_frozen_fact(
        ResolvedExtractionInputBudgetAttributionFact,
        schema_version="resolved_extraction_input_budget_attribution.v1",
        resolved_model_target_fingerprint=target.target_fingerprint,
        target_input_limit_tokens=target.context_budget.input_budget_tokens,
        static_prompt_tokens=static_prompt_tokens,
        carrier_and_framing_reserve_tokens=carrier_and_framing_reserve_tokens,
        output_reserve_tokens=output_reserve,
        safety_margin_tokens=safety_margin_tokens,
        usable_evidence_tokens=usable,
        maximum_physical_input_utf8_bytes=maximum_physical_input_utf8_bytes,
        token_estimator_contract_fingerprint=target.token_estimator.estimator_fingerprint,
        budget_selection_contract_fingerprint=(
            EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT
        ),
    )
    failure = None
    if usable == 0:
        failure = build_frozen_fact(
            CompactionMemoryInputBudgetFailureFact,
            schema_version="compaction_memory_input_budget_failure.v1",
            failure_kind="prompt_and_reserves_exceed_target",
            resolved_budget_attribution_fingerprint=(
                attribution.attribution_fingerprint
            ),
        )
    return ExtractionInputBudgetOutcome(budget=attribution, failure=failure)


def reserve_background_budget(
    *,
    account: BackgroundDerivedWorkBudgetAccountFact,
    policy: BackgroundDerivedWorkBudgetPolicyFact,
    reservation_id: str,
    extraction_job_id: str,
    operation_id: str,
    dispatch_attempt_ordinal: int,
    quote: ModelCallReservationQuoteFact,
) -> BackgroundBudgetReserveOutcome:
    validate_background_budget_account(account=account, policy=policy)
    failure_kind: str | None = None
    if account.account_status == "reconciliation_required":
        failure_kind = "account_reconciliation_required"
    elif account.dispatched_call_count + 1 > policy.maximum_dispatched_calls_per_session:
        failure_kind = "call_cap_exhausted"
    elif account.settled_charged_input_tokens + account.open_reserved_input_tokens + quote.physical_input_token_upper_bound > policy.maximum_physical_input_tokens_per_session:
        failure_kind = "input_token_cap_exhausted"
    elif account.settled_charged_output_tokens + account.open_reserved_output_tokens + quote.output_token_upper_bound > policy.maximum_output_tokens_per_session:
        failure_kind = "output_token_cap_exhausted"
    elif account.settled_charged_milliunits + account.open_reserved_milliunits + quote.reserved_milliunits > policy.maximum_milliunits_per_session:
        failure_kind = "milliunit_cap_exhausted"
    if failure_kind is not None:
        failure = build_frozen_fact(
            BackgroundDerivedWorkBudgetAdmissionFailureFact,
            schema_version="background_derived_work_budget_admission_failure.v1",
            failure_kind=failure_kind,
            source_account_reference=_account_reference(account),
            rejected_quote_fact_fingerprint=(
                quote.quote_fact_fingerprint or quote.quote_semantic_fingerprint
            ),
        )
        return BackgroundBudgetReserveOutcome(account=account, reservation=None, failure=failure)

    reservation = build_frozen_fact(
        BackgroundDerivedWorkBudgetReservationFact,
        schema_version="background_derived_work_budget_reservation.v1",
        reservation_id=reservation_id,
        runtime_session_id=account.runtime_session_id,
        extraction_job_id=extraction_job_id,
        operation_id=operation_id,
        dispatch_attempt_ordinal=dispatch_attempt_ordinal,
        model_call_reservation_quote=quote,
        source_account_revision=account.account_revision,
    )
    updated = build_frozen_fact(
        BackgroundDerivedWorkBudgetAccountFact,
        schema_version="background_derived_work_budget_account.v1",
        runtime_session_id=account.runtime_session_id,
        policy_fingerprint=account.policy_fingerprint,
        account_revision=account.account_revision + 1,
        account_status=account.account_status,
        dispatched_call_count=account.dispatched_call_count + 1,
        settled_call_count=account.settled_call_count,
        open_reservation_count=account.open_reservation_count + 1,
        open_reserved_input_tokens=account.open_reserved_input_tokens + quote.physical_input_token_upper_bound,
        open_reserved_output_tokens=account.open_reserved_output_tokens + quote.output_token_upper_bound,
        open_reserved_milliunits=account.open_reserved_milliunits + quote.reserved_milliunits,
        settled_charged_input_tokens=account.settled_charged_input_tokens,
        settled_charged_output_tokens=account.settled_charged_output_tokens,
        settled_charged_milliunits=account.settled_charged_milliunits,
    )
    validate_background_budget_account(account=updated, policy=policy)
    return BackgroundBudgetReserveOutcome(account=updated, reservation=reservation, failure=None)


def settle_background_budget(
    *,
    account: BackgroundDerivedWorkBudgetAccountFact,
    policy: BackgroundDerivedWorkBudgetPolicyFact,
    reservation: BackgroundDerivedWorkBudgetReservationFact,
    model_end: ModelCallEndEvent,
) -> BackgroundBudgetSettlementOutcome:
    validate_background_budget_account(account=account, policy=policy)
    quote = reservation.model_call_reservation_quote
    if (
        reservation.runtime_session_id != account.runtime_session_id
        or reservation.source_account_revision >= account.account_revision
        or quote.resolved_model_call_id != model_end.resolved_model_call_id
        or quote.policy_fingerprint
        != default_rollout_budget_policy().policy_fingerprint
    ):
        raise ValueError("background budget settlement authority mismatch")
    if (
        account.open_reservation_count < 1
        or account.open_reserved_input_tokens
        < quote.physical_input_token_upper_bound
        or account.open_reserved_output_tokens < quote.output_token_upper_bound
        or account.open_reserved_milliunits < quote.reserved_milliunits
    ):
        raise ValueError("background budget settlement lacks an open reservation")

    basis: Literal[
        "provider_reported_usage",
        "not_started_zero",
        "reserved_missing_usage",
        "cancelled_reserved",
    ]
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
            raise ValueError("reported background usage is absent")
        cached = usage.cached_input_tokens or 0
        pricing = default_rollout_budget_policy()
        charged_input = usage.input_tokens
        charged_output = usage.output_tokens
        charged_milliunits = (
            (usage.input_tokens - cached) * pricing.non_cached_input_weight_milli
            + cached * pricing.cached_input_weight_milli
            + usage.output_tokens * pricing.output_weight_milli
        )
        if (
            usage.input_tokens > quote.physical_input_token_upper_bound
            or usage.output_tokens > quote.output_token_upper_bound
            or charged_milliunits > quote.reserved_milliunits
        ):
            reconciled = build_frozen_fact(
                BackgroundDerivedWorkBudgetAccountFact,
                schema_version="background_derived_work_budget_account.v1",
                **{
                    **account.model_dump(
                        mode="python",
                        exclude={"schema_version", "account_fingerprint"},
                    ),
                    "account_revision": account.account_revision + 1,
                    "account_status": "reconciliation_required",
                },
            )
            return BackgroundBudgetSettlementOutcome(
                account=reconciled,
                settlement=None,
                reconciliation_required=True,
            )
    elif basis == "not_started_zero":
        charged_input = charged_output = charged_milliunits = 0
    else:
        charged_input = quote.physical_input_token_upper_bound
        charged_output = quote.output_token_upper_bound
        charged_milliunits = quote.reserved_milliunits

    usage_charge_fingerprint = context_fingerprint(
        "background-derived-work-budget-usage-charge:v1",
        {
            "reservation_fingerprint": reservation.reservation_fingerprint,
            "model_call_end_event_id": model_end.id,
            "accounting_basis": basis,
            "charged_input_tokens": charged_input,
            "charged_output_tokens": charged_output,
            "charged_milliunits": charged_milliunits,
        },
    )
    settlement = build_frozen_fact(
        BackgroundDerivedWorkBudgetSettlementFact,
        schema_version="background_derived_work_budget_settlement.v1",
        reservation_fingerprint=reservation.reservation_fingerprint,
        model_call_end_event_id=model_end.id,
        accounting_basis=basis,
        charged_input_tokens=charged_input,
        charged_output_tokens=charged_output,
        charged_milliunits=charged_milliunits,
        usage_charge_fingerprint=usage_charge_fingerprint,
        source_account_revision=account.account_revision,
        resulting_account_revision=account.account_revision + 1,
    )
    updated = build_frozen_fact(
        BackgroundDerivedWorkBudgetAccountFact,
        schema_version="background_derived_work_budget_account.v1",
        runtime_session_id=account.runtime_session_id,
        policy_fingerprint=account.policy_fingerprint,
        account_revision=account.account_revision + 1,
        account_status=account.account_status,
        dispatched_call_count=account.dispatched_call_count,
        settled_call_count=account.settled_call_count + 1,
        open_reservation_count=account.open_reservation_count - 1,
        open_reserved_input_tokens=(
            account.open_reserved_input_tokens
            - quote.physical_input_token_upper_bound
        ),
        open_reserved_output_tokens=(
            account.open_reserved_output_tokens - quote.output_token_upper_bound
        ),
        open_reserved_milliunits=(
            account.open_reserved_milliunits - quote.reserved_milliunits
        ),
        settled_charged_input_tokens=(
            account.settled_charged_input_tokens + charged_input
        ),
        settled_charged_output_tokens=(
            account.settled_charged_output_tokens + charged_output
        ),
        settled_charged_milliunits=(
            account.settled_charged_milliunits + charged_milliunits
        ),
    )
    validate_background_budget_account(account=updated, policy=policy)
    return BackgroundBudgetSettlementOutcome(
        account=updated,
        settlement=settlement,
        reconciliation_required=False,
    )


__all__ = [
    "BackgroundBudgetReserveOutcome",
    "BackgroundBudgetSettlementOutcome",
    "DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY",
    "EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT",
    "ExtractionInputBudgetOutcome",
    "build_background_budget_genesis",
    "reserve_background_budget",
    "resolve_extraction_input_budget",
    "settle_background_budget",
    "validate_background_budget_account",
]
