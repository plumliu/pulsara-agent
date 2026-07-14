"""Run-entry long-horizon contract and initial durable facts."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    ChildRolloutReservationPolicyFact,
    ChildRolloutSubaccountFact,
    ContextWindowFact,
    ContextWindowOpenReason,
    ContextWindowTranscriptBasisFact,
    ResolvedChildRolloutBudgetFact,
    RolloutBudgetAccountFact,
    RolloutBudgetStateFact,
    RolloutReservationFact,
    RolloutReservationReferenceFact,
    RunLongHorizonContractFact,
    SubagentGraphReducerContractFact,
    calculate_model_call_reservation,
    default_child_rollout_policy,
    default_long_horizon_context_policy,
    default_rollout_budget_policy,
    default_rollout_status_hint_policy,
    evaluate_rollout_budget_feasibility,
)
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact


class LongHorizonRunConfigurationError(RuntimeError):
    """Resolved run targets cannot support the frozen rollout contract."""


@dataclass(frozen=True, slots=True)
class PreparedLongHorizonRunFacts:
    contract: RunLongHorizonContractFact
    initial_window: ContextWindowFact
    root_account: RolloutBudgetAccountFact | None
    opening_batch_id: str


@dataclass(frozen=True, slots=True)
class PreparedChildRolloutReservation:
    resolved_budget: ResolvedChildRolloutBudgetFact
    reservation: RolloutReservationFact


def prepare_root_long_horizon_run(
    *,
    runtime_session_id: str,
    run_id: str,
    run_start_event_id: str,
    primary_target: ResolvedModelTargetFact,
    summarizer_target: ResolvedModelTargetFact,
    graph_reducer_contract: SubagentGraphReducerContractFact,
    source_through_sequence_at_open: int,
    initial_projection_unit_count: int,
    initial_projection_state_fingerprint: str,
) -> PreparedLongHorizonRunFacts:
    account_id = f"rollout_account:{uuid4().hex}"
    return _prepare(
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        run_start_event_id=run_start_event_id,
        primary_target=primary_target,
        summarizer_target=summarizer_target,
        graph_reducer_contract=graph_reducer_contract,
        source_through_sequence_at_open=source_through_sequence_at_open,
        initial_projection_unit_count=initial_projection_unit_count,
        initial_projection_state_fingerprint=initial_projection_state_fingerprint,
        account_id=account_id,
        account_owner_runtime_session_id=runtime_session_id,
        account_owner_run_id=run_id,
        inherited_rollout_reservation=None,
        root_account_required=True,
    )


def prepare_child_long_horizon_run(
    *,
    child_runtime_session_id: str,
    child_run_id: str,
    run_start_event_id: str,
    primary_target: ResolvedModelTargetFact,
    summarizer_target: ResolvedModelTargetFact,
    graph_reducer_contract: SubagentGraphReducerContractFact,
    account_id: str,
    account_owner_runtime_session_id: str,
    account_owner_run_id: str,
    inherited_rollout_reservation: RolloutReservationReferenceFact,
    source_through_sequence_at_open: int = 0,
) -> PreparedLongHorizonRunFacts:
    return _prepare(
        runtime_session_id=child_runtime_session_id,
        run_id=child_run_id,
        run_start_event_id=run_start_event_id,
        primary_target=primary_target,
        summarizer_target=summarizer_target,
        graph_reducer_contract=graph_reducer_contract,
        source_through_sequence_at_open=source_through_sequence_at_open,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
        account_id=account_id,
        account_owner_runtime_session_id=account_owner_runtime_session_id,
        account_owner_run_id=account_owner_run_id,
        inherited_rollout_reservation=inherited_rollout_reservation,
        root_account_required=False,
    )


def empty_projection_state_fingerprint() -> str:
    return context_fingerprint(
        "context-window-projection-state:v1",
        {
            "projection_generation": 0,
            "unit_projections": (),
            "rollups": (),
        },
    )


def prepare_child_rollout_reservation(
    *,
    child_profile: str,
    child_run_id: str,
    child_primary_target: ResolvedModelTargetFact,
    child_summarizer_target: ResolvedModelTargetFact,
    child_window_policy_fingerprint: str,
    parent_account: RolloutBudgetAccountFact,
    parent_state: RolloutBudgetStateFact,
    source_sequence: int,
    child_policy: ChildRolloutReservationPolicyFact,
) -> PreparedChildRolloutReservation:
    policy = parent_account.policy
    primary_quote = calculate_model_call_reservation(
        target=child_primary_target,
        resolved_model_call_id=None,
        policy=policy,
    )
    compaction_quote = calculate_model_call_reservation(
        target=child_summarizer_target,
        resolved_model_call_id=None,
        policy=policy,
    )
    tool_reserve = (
        child_policy.max_tool_cost_units_per_child
        * policy.tool_cost_unit_weight_milli
    )
    profile_limit = (
        primary_quote.reserved_milliunits
        * child_policy.max_agent_model_calls_per_child
        + compaction_quote.reserved_milliunits
        * child_policy.max_window_compactions_per_child
        + tool_reserve
    )
    exploration_remaining = (
        parent_account.exploration_allowance_milliunits
        - parent_state.exploration_charged_milliunits
        - parent_state.exploration_reserved_milliunits
    )
    parent_share = (
        exploration_remaining * child_policy.max_parent_exploration_share_ppm
    ) // 1_000_000
    maximum = min(profile_limit, parent_share)
    if maximum < primary_quote.reserved_milliunits:
        raise LongHorizonRunConfigurationError(
            "child rollout reservation cannot fund one primary model call"
        )
    resolved_payload = {
        "child_profile": child_profile,
        "child_primary_target_fingerprint": child_primary_target.target_fingerprint,
        "child_summarizer_target_fingerprint": (
            child_summarizer_target.target_fingerprint
        ),
        "child_window_policy_fingerprint": child_window_policy_fingerprint,
        "child_policy_fingerprint": child_policy.policy_fingerprint,
        "child_primary_reservation_quote_semantic_fingerprint": (
            primary_quote.quote_semantic_fingerprint
        ),
        "child_compaction_reservation_quote_semantic_fingerprint": (
            compaction_quote.quote_semantic_fingerprint
        ),
        "one_agent_call_reserve_milliunits": primary_quote.reserved_milliunits,
        "one_compaction_call_reserve_milliunits": (
            compaction_quote.reserved_milliunits
        ),
        "tool_reserve_milliunits": tool_reserve,
        "profile_limit_milliunits": profile_limit,
        "parent_share_limit_milliunits": parent_share,
        "max_rollout_milliunits_per_child": maximum,
        "parent_account_state_fingerprint": parent_state.state_fingerprint,
    }
    resolved = ResolvedChildRolloutBudgetFact(
        **resolved_payload,
        resolution_fingerprint=context_fingerprint(
            "resolved-child-rollout-budget:v1", resolved_payload
        ),
    )
    reservation_payload = {
        "reservation_id": f"rollout_reservation:subagent:{child_run_id}",
        "account_id": parent_account.account_id,
        "owner_kind": "subagent_run",
        "owner_id": child_run_id,
        "phase_at_reservation": parent_state.phase,
        "budget_bucket": "exploration",
        "reserved_milliunits": maximum,
        "model_call_reservation_quote": None,
        "source_sequence": source_sequence,
    }
    reservation = RolloutReservationFact(
        **reservation_payload,
        semantic_fingerprint=context_fingerprint(
            "rollout-reservation:v1", reservation_payload
        ),
    )
    return PreparedChildRolloutReservation(
        resolved_budget=resolved,
        reservation=reservation,
    )


def build_child_rollout_subaccount(
    *,
    child_runtime_session_id: str,
    child_run_id: str,
    resolved_budget: ResolvedChildRolloutBudgetFact,
    reservation_reference: RolloutReservationReferenceFact,
    root_account_id: str,
) -> ChildRolloutSubaccountFact:
    payload = {
        "root_account_id": root_account_id,
        "parent_reservation": reservation_reference,
        "child_runtime_session_id": child_runtime_session_id,
        "child_run_id": child_run_id,
        "resolved_budget": resolved_budget,
        "reserved_milliunits": resolved_budget.max_rollout_milliunits_per_child,
    }
    return ChildRolloutSubaccountFact(
        **payload,
        subaccount_fingerprint=context_fingerprint(
            "child-rollout-subaccount:v1", payload
        ),
    )


def _prepare(
    *,
    runtime_session_id: str,
    run_id: str,
    run_start_event_id: str,
    primary_target: ResolvedModelTargetFact,
    summarizer_target: ResolvedModelTargetFact,
    graph_reducer_contract: SubagentGraphReducerContractFact,
    source_through_sequence_at_open: int,
    initial_projection_unit_count: int,
    initial_projection_state_fingerprint: str,
    account_id: str,
    account_owner_runtime_session_id: str,
    account_owner_run_id: str,
    inherited_rollout_reservation: RolloutReservationReferenceFact | None,
    root_account_required: bool,
) -> PreparedLongHorizonRunFacts:
    window_policy = default_long_horizon_context_policy(
        input_budget_tokens=primary_target.context_budget.input_budget_tokens
    )
    rollout_policy = default_rollout_budget_policy()
    child_policy = default_child_rollout_policy()
    status_policy = default_rollout_status_hint_policy()
    feasibility = evaluate_rollout_budget_feasibility(
        execution_profile_kind=(
            "host_root" if root_account_required else "subagent_child"
        ),
        execution_profile_id=("host_root" if root_account_required else "child"),
        primary_target_slot=primary_target.model_role,
        primary_target=primary_target,
        summarizer_target_slot=summarizer_target.model_role,
        summarizer_target=summarizer_target,
        policy=rollout_policy,
    )
    if not feasibility.feasible:
        raise LongHorizonRunConfigurationError(
            "resolved target pair leaves no exploration allowance"
        )
    account_payload = {
        "account_id": account_id,
        "owner_runtime_session_id": account_owner_runtime_session_id,
        "root_run_id": account_owner_run_id,
        "policy": rollout_policy,
        "total_budget_milliunits": feasibility.total_rollout_budget_milliunits,
        "finalization_reserve_milliunits": (
            feasibility.finalization_reserve_milliunits
        ),
        "finalization_agent_reserve_milliunits": (
            feasibility.finalization_agent_reserve_milliunits
        ),
        "finalization_compaction_reserve_milliunits": (
            feasibility.finalization_compaction_reserve_milliunits
        ),
        "finalization_tool_reserve_milliunits": (
            feasibility.finalization_tool_reserve_milliunits
        ),
        "exploration_allowance_milliunits": (
            feasibility.exploration_allowance_milliunits
        ),
    }
    root_account = (
        RolloutBudgetAccountFact(
            **account_payload,
            semantic_fingerprint=context_fingerprint(
                "rollout-budget-account:v1", account_payload
            ),
        )
        if root_account_required
        else None
    )
    window_id = f"context_window:{uuid4().hex}"
    window_open_event_id = f"context_window_opened:{uuid4().hex}"
    stable_close_event_id = f"context_window_closed:{uuid4().hex}"
    basis_payload = {
        "basis_kind": "initial_run",
        "run_start_event_id": run_start_event_id,
        "source_compaction_started_event_id": None,
        "source_compaction_plan_fingerprint": None,
        "source_through_sequence_at_compaction": None,
        "summarized_pair_groups_fingerprint": None,
        "retained_pair_groups_fingerprint": None,
    }
    basis = ContextWindowTranscriptBasisFact(
        **basis_payload,
        basis_fingerprint=context_fingerprint(
            "context-window-transcript-basis:v1", basis_payload
        ),
    )
    window_payload = {
        "contract_version": "context-window:v1",
        "window_id": window_id,
        "run_id": run_id,
        "generation": 1,
        "previous_window_id": None,
        "open_reason": ContextWindowOpenReason.INITIAL_RUN,
        "transcript_basis": basis,
        "source_through_sequence_at_open": source_through_sequence_at_open,
        "resolved_model_target_fingerprint": primary_target.target_fingerprint,
        "input_budget_tokens": primary_target.context_budget.input_budget_tokens,
        "token_estimator_fingerprint": primary_target.token_estimator.estimator_fingerprint,
        "window_policy_fingerprint": window_policy.policy_fingerprint,
        "initial_projection_generation": 0,
        "initial_projection_unit_count": initial_projection_unit_count,
        "initial_projection_state_fingerprint": initial_projection_state_fingerprint,
        "stable_close_event_id": stable_close_event_id,
        "source_compaction_id": None,
        "source_summary_artifact_id": None,
        "source_summary_fingerprint": None,
    }
    semantic_payload = {
        key: value
        for key, value in window_payload.items()
        if key not in {"window_id", "stable_close_event_id"}
    }
    window_semantic_fingerprint = context_fingerprint(
        "context-window-semantic:v1", semantic_payload
    )
    window_fact_payload = {
        **window_payload,
        "window_semantic_fingerprint": window_semantic_fingerprint,
    }
    window = ContextWindowFact(
        **window_fact_payload,
        window_fact_fingerprint=context_fingerprint(
            "context-window-fact:v1", window_fact_payload
        ),
    )
    contract_payload = {
        "contract_version": "run-long-horizon:v1",
        "rollout_account_id": account_id,
        "rollout_account_owner_runtime_session_id": (
            account_owner_runtime_session_id
        ),
        "rollout_account_owner_run_id": account_owner_run_id,
        "inherited_rollout_reservation": inherited_rollout_reservation,
        "initial_window_id": window_id,
        "initial_window_open_event_id": window_open_event_id,
        "window_policy": window_policy,
        "window_compaction_summarizer_target": summarizer_target,
        "rollout_policy": rollout_policy,
        "child_rollout_policy": child_policy,
        "rollout_status_hint_policy": status_policy,
        "subagent_graph_reducer_contract": graph_reducer_contract,
    }
    contract = RunLongHorizonContractFact(
        **contract_payload,
        contract_fingerprint=context_fingerprint(
            "run-long-horizon:v1", contract_payload
        ),
    )
    return PreparedLongHorizonRunFacts(
        contract=contract,
        initial_window=window,
        root_account=root_account,
        opening_batch_id=f"long_horizon_opening_batch:{uuid4().hex}",
    )
    ResolvedChildRolloutBudgetFact,
    RolloutBudgetStateFact,
    RolloutReservationFact,
