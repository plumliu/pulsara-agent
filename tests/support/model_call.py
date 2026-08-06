"""Central model-target fixtures for hard-cut runtime tests."""

from __future__ import annotations

from tests.support.runtime_owner import runtime_session_for_test

import asyncio

from dataclasses import dataclass, replace
from typing import AsyncIterator
from uuid import uuid4
from weakref import WeakKeyDictionary

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.config import LLMConfig, ModelSlotConfig
from pulsara_agent.llm.control_contract import (
    CURRENT_MODEL_CALL_CONTROL_DOWNSTREAM_CONTRACT,
)
from pulsara_agent.llm.adapters.mock import MockTransport
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.provider import ProviderProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.resolution import resolve_model_call, resolve_model_target
from pulsara_agent.primitives.model_call import (
    ContextBudgetReportEvent,
    CompactionTargetEstimateFact,
    ModelCallPurpose,
    ModelContextLimits,
    ResolvedModelCallFact,
    ResolvedModelTargetFact,
    ModelTokenUsageFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.context import (
    ContextCompileInputFailureFact,
    ContextInputFailureReasonCode,
    context_fingerprint,
)
from pulsara_agent.primitives.context_input_commit import (
    ContextCompileInputCommitFact,
    ContextCompileSourceReferenceSetFact,
    RunSeedProjectionBaseReferenceFact,
)
from pulsara_agent.primitives.run_boundary import (
    ModelStreamRecoveryPlanFact,
    RunExecutionActivationFact,
)
from pulsara_agent.primitives.frozen import StableEventIdentityFact, build_frozen_fact
from pulsara_agent.primitives.terminal_projection import (
    ModelCallTerminalProjectionEndReferenceFact,
    ModelTerminalProjectionSemanticJoinFact,
    TerminalProjectionReferenceFact,
)


def compaction_completed_contract_fields(
    *,
    estimated_tokens_before: int = 10_000,
    estimated_tokens_after: int = 100,
) -> dict[str, object]:
    target = test_resolved_target_fact()
    summarizer = test_resolved_call_fact(
        purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    )
    summary_actual = min(estimated_tokens_after, 50)
    target_estimate = CompactionTargetEstimateFact(
        estimate_scope="transcript_only",
        basis_context_id=None,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=None,
        transcript_tokens_before=estimated_tokens_before,
        estimated_tokens_before=estimated_tokens_before,
        summary_tokens_reserved=max(summary_actual, 256),
        retained_transcript_tokens=estimated_tokens_after - summary_actual,
        protected_transcript_tokens=0,
        summary_tokens_actual=summary_actual,
        transcript_tokens_after=estimated_tokens_after,
        estimated_tokens_after=estimated_tokens_after,
        predicted_post_target_reached=None,
    )
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "target_estimate": target_estimate,
        "summarizer_call": summarizer,
        "summarizer_context_id": "context:test-compaction",
        "summarizer_input_estimated_tokens": 64,
        "summarizer_input_budget_tokens": summarizer.target.context_budget.input_budget_tokens,
        "summarizer_usage_status": "missing",
        "summarizer_usage": None,
        "summarizer_estimated_input_tokens": 64,
        "summarizer_reported_model_id": None,
        "predicted_post_target_reached": None,
        "started_event_id": "context_compaction_started:test",
    }


def compaction_started_contract_fields(
    *,
    estimated_tokens_before: int = 10_000,
) -> dict[str, object]:
    target = test_resolved_target_fact()
    summarizer = test_resolved_call_fact(
        purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    )
    target_estimate = CompactionTargetEstimateFact(
        estimate_scope="transcript_only",
        basis_context_id=None,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=None,
        transcript_tokens_before=estimated_tokens_before,
        estimated_tokens_before=estimated_tokens_before,
        summary_tokens_reserved=256,
        retained_transcript_tokens=0,
        protected_transcript_tokens=0,
        summary_tokens_actual=None,
        transcript_tokens_after=None,
        estimated_tokens_after=None,
        predicted_post_target_reached=None,
    )
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "target_estimate": target_estimate,
        "summarizer_call": summarizer,
        "summarizer_context_id": "context:test-compaction",
        "summarizer_input_estimated_tokens": 64,
        "summarizer_input_budget_tokens": summarizer.target.context_budget.input_budget_tokens,
        "terminal_event_id": "context_compaction_terminal:test",
    }


def compaction_failed_contract_fields() -> dict[str, object]:
    target = test_resolved_target_fact()
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "failure_stage": "planning",
        "termination_kind": "failed",
    }


def context_compiled_contract_fields(
    *,
    estimated_tokens: int = 123,
    tools_estimated_tokens: int = 42,
    status: str = "compiled",
    non_transcript_baseline_tokens: int | None = None,
    resolved_call: ResolvedModelCallFact | None = None,
    model_call_index: int = 1,
    context_id: str = "context:test",
    run_id: str = "run:test",
    runtime_session_id: str = "runtime:test",
) -> dict[str, object]:
    call = resolved_call or test_resolved_call_fact()
    target = call.target
    baseline = (
        estimated_tokens - max(0, estimated_tokens // 3)
        if non_transcript_baseline_tokens is None
        else non_transcript_baseline_tokens
    )
    transcript = estimated_tokens - baseline
    if transcript < 0:
        raise ValueError("non-transcript baseline exceeds estimated token total")
    sections = max(0, estimated_tokens - tools_estimated_tokens)
    budget = ContextBudgetReportEvent(
        target_fingerprint=target.target_fingerprint,
        resolved_model_call_id=call.resolved_model_call_id,
        measurement_stage="final_payload",
        total_context_tokens=target.limits.total_context_tokens,
        max_input_tokens=target.limits.max_input_tokens,
        max_output_tokens=target.limits.max_output_tokens,
        effective_output_tokens=target.context_budget.effective_output_tokens,
        safety_margin_tokens=target.context_budget.safety_margin_tokens,
        input_budget_tokens=target.context_budget.input_budget_tokens,
        sections_estimated_tokens=sections,
        tools_estimated_tokens=tools_estimated_tokens,
        envelope_estimated_tokens=3,
        allocation_estimated_tokens=sections + tools_estimated_tokens,
        final_payload_estimated_tokens=estimated_tokens,
        non_transcript_baseline_tokens=baseline,
        transcript_estimated_tokens=transcript,
        estimator=target.token_estimator,
    )
    prepared_candidate = None
    semantic_commit = None
    preparation_install = None
    audit_expectation = None
    if status == "compiled":
        (
            prepared_candidate,
            semantic_commit,
            preparation_install,
            audit_expectation,
        ) = _compiled_provider_input_candidate_fixture(
            call,
            budget=budget,
            context_id=context_id,
            model_call_index=model_call_index,
            run_id=run_id,
            runtime_session_id=runtime_session_id,
        )
    failure_stage = None
    input_failure = None
    if status != "compiled":
        failure_stage = "context_budget" if status == "pressure" else "context_compile"
        input_failure = ContextCompileInputFailureFact(
            failure_stage=failure_stage,
            context_id=context_id,
            resolved_model_call_id=call.resolved_model_call_id,
            model_call_index=model_call_index,
            compile_attempt_index=1,
            context_retry_index=0,
            snapshot_id=None,
            source_through_sequence=None,
            available_component_fingerprints=(),
            input_aggregate_fingerprint=None,
            reason_code=(
                ContextInputFailureReasonCode.CONTEXT_BUDGET_EXCEEDED
                if status == "pressure"
                else ContextInputFailureReasonCode.CANDIDATE_INVALID
            ),
        )
    return {
        "status": status,
        "failure_stage": failure_stage,
        "compile_attempt_index": 1,
        "context_retry_index": 0,
        "resolved_call": call,
        "budget": budget,
        "semantic_commit": semantic_commit,
        "provider_input_preparation_install": preparation_install,
        "audit_expectation": audit_expectation,
        "input_failure": input_failure,
    }


def _compiled_provider_input_candidate_fixture(
    call: ResolvedModelCallFact,
    *,
    budget: ContextBudgetReportEvent,
    context_id: str,
    model_call_index: int,
    run_id: str,
    runtime_session_id: str,
):
    """Wrap the one-shot physical fixture in the compact compiled contract."""

    from pulsara_agent.primitives.context import context_fingerprint
    from pulsara_agent.primitives.provider_input import (
        PreparedProviderInputAppendCandidateFact,
        PreparedProviderInputPlanFact,
        ProviderInputPreparationInstallFact,
        ProviderInputCausalValidationResult,
        ProviderOrderedTranscriptProjectionIdentityFact,
        ProviderTranscriptDeltaCommitProofFact,
    )
    from pulsara_agent.runtime.provider_input.causal import (
        CAUSAL_VALIDATION_CONTRACT_FINGERPRINT,
        build_default_resolved_causal_physical_policy,
    )

    bundle = prepared_provider_input_bundle_fixture(
        call,
        context_id=context_id,
        model_call_index=model_call_index,
    )
    candidate = bundle.prepared_candidate
    policy = build_default_resolved_causal_physical_policy()
    empty_wire = context_fingerprint("provider-ordered-transcript-wire:v2:empty", ())
    empty_causal = context_fingerprint(
        "provider-ordered-transcript-causal:v2:empty", ()
    )
    projection_identity = build_frozen_fact(
        ProviderOrderedTranscriptProjectionIdentityFact,
        schema_version="provider_ordered_transcript_projection_identity.v1",
        projection_semantic_fingerprint=context_fingerprint(
            "test-provider-ordered-projection:v1", context_id
        ),
        unit_count=0,
        ordered_wire_semantic_accumulator=empty_wire,
        ordered_causal_semantic_accumulator=empty_causal,
    )
    validation = build_frozen_fact(
        ProviderInputCausalValidationResult,
        schema_version="provider_input_causal_validation_result.v2",
        status="valid",
        projection_identity_fingerprint=projection_identity.identity_fingerprint,
        checked_visible_edge_count=0,
        violation_reason=None,
        violating_projection_indices=(),
        validation_contract_fingerprint=CAUSAL_VALIDATION_CONTRACT_FINGERPRINT,
        resolved_causal_physical_policy_fingerprint=policy.policy_fingerprint,
    )
    frontier = bundle.resulting_core_state.transcript_frontier
    proof = build_frozen_fact(
        ProviderTranscriptDeltaCommitProofFact,
        schema_version="provider_transcript_delta_commit_proof.v1",
        projection_identity_fingerprint=projection_identity.identity_fingerprint,
        predecessor_frontier_fingerprint=(
            frontier.provider_semantic_frontier_fingerprint
        ),
        delta_first_projection_index=None,
        delta_last_projection_index=None,
        ordered_delta_wire_accumulator=empty_wire,
        ordered_delta_causal_accumulator=empty_causal,
        continuation_joins=(),
        resulting_frontier=frontier,
        resolved_causal_physical_policy_fingerprint=policy.policy_fingerprint,
    )
    prepared_plan = build_frozen_fact(
        PreparedProviderInputPlanFact,
        schema_version="prepared_provider_input_plan.v2",
        plan_kind="initial_generation",
        resolved_model_call_id=call.resolved_model_call_id,
        continuity_scope_fingerprint=(
            candidate.preparation_ownership.scope_fingerprint
        ),
        target_generation_id=candidate.generation_id,
        predecessor_core_state_fingerprint=None,
        ordered_transcript_projection_identity=projection_identity,
        causal_validation=validation,
        frame_placement=None,
        transcript_delta_proof=proof,
        source_dispositions=(),
        rollover_intent=None,
        resulting_unit_vector_root_fingerprint=(
            candidate.provider_input_plan.unit_vector_root.reference_fingerprint
        ),
        resolved_causal_physical_policy_fingerprint=policy.policy_fingerprint,
    )
    from pulsara_agent.primitives._context_base import ContextEventReferenceFact
    from pulsara_agent.primitives.context_source import LedgerAuthorityHorizonFact
    from pulsara_agent.primitives.transcript_projection import (
        RunTranscriptSeedReferenceFact,
    )
    from pulsara_agent.runtime.context_input.commit import (
        build_context_input_audit_expectation_for_commit,
    )

    primary_horizon = build_frozen_fact(
        LedgerAuthorityHorizonFact,
        schema_version="ledger_authority_horizon.v1",
        runtime_session_id=runtime_session_id,
        through_sequence=1,
        ledger_event_count_through=1,
        ledger_continuity_accumulator_through=context_fingerprint(
            "test-ledger-continuity:v1", 1
        ),
    )
    seed_reference = build_frozen_fact(
        RunTranscriptSeedReferenceFact,
        schema_version="run_transcript_seed_ref.v1",
        seed_artifact_id="artifact:run-transcript-seed:test",
        seed_artifact_sha256=context_fingerprint("test-run-seed-bytes:v1", context_id),
        seed_artifact_bytes=1,
        seed_semantic_fingerprint=context_fingerprint(
            "test-run-seed-semantic:v1", context_id
        ),
        root_materialization_fingerprint=context_fingerprint(
            "test-run-seed-root:v1", context_id
        ),
        seed_artifact_contract_fingerprint=context_fingerprint(
            "test-run-seed-contract:v1", 1
        ),
        source_runtime_session_id=runtime_session_id,
        source_ledger_through_sequence=1,
        source_ledger_continuity_accumulator=primary_horizon.ledger_continuity_accumulator_through,
        source_checkpoint_id=None,
    )
    base_reference = build_frozen_fact(
        RunSeedProjectionBaseReferenceFact,
        schema_version="run_seed_projection_base_reference.v1",
        base_kind="run_seed",
        run_seed_reference=seed_reference,
        stable_semantic_state_fingerprint=context_fingerprint(
            "test-stable-transcript-state:v1", context_id
        ),
        source_base_fingerprint=context_fingerprint(
            "test-projection-base:v1", context_id
        ),
    )
    run_start_reference = ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id="run-start:test",
        sequence=1,
        event_type="RUN_START",
        payload_fingerprint=context_fingerprint("test-run-start:v1", context_id),
    )
    source_references = build_frozen_fact(
        ContextCompileSourceReferenceSetFact,
        schema_version="context_compile_authority_reference_set.v1",
        run_start_event_reference=run_start_reference,
        continuation_event_reference=None,
        primary_ledger_horizon=primary_horizon,
        authority_horizon_set_reference=(
            candidate.provider_input_plan.authority_horizon_set
        ),
        transcript_projection_base_reference=base_reference,
    )
    budget_decision_fingerprint = context_fingerprint(
        "context-compile-budget-decision:v1",
        {
            "resolved_model_target_fingerprint": call.target.target_fingerprint,
            "token_estimator_fingerprint": call.target.token_estimator.estimator_fingerprint,
            "input_budget_tokens": budget.input_budget_tokens,
            "final_payload_estimated_tokens": budget.final_payload_estimated_tokens,
        },
    )
    semantic_commit = build_frozen_fact(
        ContextCompileInputCommitFact,
        schema_version="context_compile_input_commit.v1",
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        context_id=context_id,
        resolved_model_call_id=call.resolved_model_call_id,
        resolved_model_target_fingerprint=call.target.target_fingerprint,
        model_call_index=model_call_index,
        compile_attempt_index=1,
        context_retry_index=0,
        source_through_sequence=1,
        source_references=source_references,
        snapshot_semantic_fingerprint=context_fingerprint(
            "test-context-snapshot:v1", context_id
        ),
        ordered_projection_identity=projection_identity,
        prepared_provider_input_plan_fingerprint=prepared_plan.plan_fingerprint,
        canonical_provider_input_plan_fingerprint=(
            candidate.provider_input_plan.plan_fingerprint
        ),
        provider_neutral_payload_fingerprint=context_fingerprint(
            "test-provider-neutral-payload:v1", context_id
        ),
        input_aggregate_fingerprint=context_fingerprint(
            "test-context-input-aggregate:v1", context_id
        ),
        canonical_render_decisions_fingerprint=context_fingerprint(
            "test-render-decisions:v1", context_id
        ),
        token_estimator_fingerprint=call.target.token_estimator.estimator_fingerprint,
        input_budget_tokens=budget.input_budget_tokens,
        final_payload_estimated_tokens=budget.final_payload_estimated_tokens,
        budget_decision_fingerprint=budget_decision_fingerprint,
    )
    payload = {
        name: getattr(candidate, name)
        for name in candidate.__class__.model_fields
        if name not in {"schema_version", "candidate_fingerprint"}
    }
    payload.update(
        candidate_kind="compiled_context",
        prepared_plan=prepared_plan,
        semantic_commit_fingerprint=semantic_commit.commit_fingerprint,
        ordered_projection_identity_fingerprint=projection_identity.identity_fingerprint,
        rollover_request=None,
    )
    compiled_candidate = build_frozen_fact(
        PreparedProviderInputAppendCandidateFact,
        schema_version="prepared_provider_input_append_candidate.v3",
        **payload,
    )
    preparation_install = build_frozen_fact(
        ProviderInputPreparationInstallFact,
        schema_version="provider_input_preparation_install.v2",
        semantic_commit_fingerprint=semantic_commit.commit_fingerprint,
        preparation_ownership=compiled_candidate.preparation_ownership,
        prepared_candidate_fingerprint=compiled_candidate.candidate_fingerprint,
        prepared_plan_fingerprint=prepared_plan.plan_fingerprint,
        canonical_provider_input_plan_fingerprint=(
            compiled_candidate.provider_input_plan.plan_fingerprint
        ),
        ordered_projection_identity_fingerprint=projection_identity.identity_fingerprint,
        generation_commit_guard=compiled_candidate.generation_commit_guard,
        rollover_request_fingerprint=None,
    )
    expectation = build_context_input_audit_expectation_for_commit(semantic_commit)
    return compiled_candidate, semantic_commit, preparation_install, expectation


def model_call_start_fields(
    *,
    event_id: str | None = None,
    context_id: str = "context:test",
    model_call_index: int | None = 1,
    resolved_call: ResolvedModelCallFact | None = None,
    lifecycle_kind: str = "main_assistant_reply",
    pre_send_estimated_input_tokens: int = 0,
) -> dict[str, object]:
    call = resolved_call or test_resolved_call_fact()
    event_id = event_id or f"model_call_start:{uuid4().hex}"
    main = lifecycle_kind == "main_assistant_reply"
    activation = make_test_run_execution_activation()
    contract = CURRENT_MODEL_CALL_CONTROL_DOWNSTREAM_CONTRACT
    recovery_payload = {
        "schema_version": "model_stream_recovery_plan.v1",
        "lifecycle_kind": lifecycle_kind,
        "model_call_start_event_id": event_id,
        "stable_model_call_end_event_id": f"model_call_end:{call.resolved_model_call_id}",
        "reply_start_event_id": (
            f"reply_start:{call.resolved_model_call_id}" if main else None
        ),
        "stable_reply_end_event_id": (
            f"reply_end:{call.resolved_model_call_id}" if main else None
        ),
        "reservation_id": None,
        "reservation_quote_fingerprint": None,
        "stable_settlement_event_id": None,
        "window_compaction_started_event_id": None,
        "pre_send_estimated_input_tokens": pre_send_estimated_input_tokens,
        "run_execution_activation": activation if main else None,
        "control_downstream_predicate_contract": contract if main else None,
    }
    recovery_plan = ModelStreamRecoveryPlanFact(
        **recovery_payload,
        recovery_plan_fingerprint=sha256_fingerprint(
            "model-stream-recovery-plan:v1",
            {
                **recovery_payload,
                "run_execution_activation": (
                    activation.model_dump(mode="json") if main else None
                ),
                "control_downstream_predicate_contract": (
                    contract.model_dump(mode="json") if main else None
                ),
            },
        ),
    )
    return {
        "id": event_id,
        "resolved_call": call,
        "context_id": context_id,
        "model_call_index": model_call_index,
        "recovery_plan": recovery_plan,
        "provider_input_reference": committed_provider_input_reference_fixture(
            call,
            context_id=context_id,
            model_call_index=model_call_index,
        ),
    }


def committed_provider_input_reference_fixture(
    call: ResolvedModelCallFact,
    *,
    context_id: str,
    model_call_index: int | None,
):
    """Build the minimum legal one-shot carrier for schema-level tests."""

    return prepared_provider_input_bundle_fixture(
        call,
        context_id=context_id,
        model_call_index=model_call_index,
    ).committed_reference


def prepared_provider_input_bundle_fixture(
    call: ResolvedModelCallFact,
    *,
    context_id: str,
    model_call_index: int | None,
    event_context: EventContext | None = None,
    runtime_session_id: str = "runtime:test",
):
    """Build one immutable provider-input lifecycle fixture."""

    from dataclasses import replace as dataclass_replace

    from pulsara_agent.primitives.provider_input import OneShotGenerationScopeFact
    from pulsara_agent.runtime.provider_input.planner import (
        plan_one_shot_provider_input,
    )
    from pulsara_agent.runtime.provider_input.store import ProviderInputGenerationStore

    runtime_call = dataclass_replace(
        test_resolved_call(purpose=call.purpose),
        fact=call,
    )
    context = LLMContext(
        messages=(LLMMessage.user("[test provider input]"),),
        context_id=context_id,
        resolved_model_call_id=call.resolved_model_call_id,
        target_fingerprint=call.target.target_fingerprint,
        model_call_index=model_call_index,
        compiler_estimated_input_tokens=(0 if model_call_index is not None else None),
    )
    scope = build_frozen_fact(
        OneShotGenerationScopeFact,
        schema_version="one_shot_generation_scope.v1",
        operation_kind="direct_model_call",
        operation_id=call.resolved_model_call_id,
        attempt_index=0,
    )
    resolved_event_context = event_context or EventContext(
        run_id="run:test",
        turn_id="turn:test",
        reply_id="reply:test",
    )
    store = ProviderInputGenerationStore(runtime_session_id=runtime_session_id)
    return plan_one_shot_provider_input(
        call=runtime_call,
        context=context,
        generation_snapshot=store.snapshot(scope.scope_fingerprint),
        event_context=resolved_event_context,
        runtime_session_id=runtime_session_id,
        operation_kind="direct_model_call",
        operation_id=call.resolved_model_call_id,
        attempt_index=0,
        clock_observed_at_utc="2026-01-01T00:00:00Z",
    )


def make_test_run_execution_activation() -> RunExecutionActivationFact:
    activation_payload = {
        "schema_version": "run_execution_activation.v1",
        "activation_owner_kind": "host_run_boundary",
        "activation_owner_id": "boundary:test",
        "segment_generation": 1,
    }
    return RunExecutionActivationFact(
        **activation_payload,
        activation_fingerprint=sha256_fingerprint(
            "run-execution-activation:v1", activation_payload
        ),
    )


def model_call_end_fields(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_input_tokens: int | None = None,
    resolved_call: ResolvedModelCallFact | None = None,
) -> dict[str, object]:
    call = resolved_call or test_resolved_call_fact()
    usage = ModelTokenUsageFact(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    return {
        "resolved_model_call_id": call.resolved_model_call_id,
        "target_fingerprint": call.target.target_fingerprint,
        "reported_model_id": call.target.model_id,
        "outcome": "completed",
        "provider_dispatch_status": "dispatched",
        "usage_status": "reported",
        "usage": usage,
        "estimated_input_tokens": (
            input_tokens if estimated_input_tokens is None else estimated_input_tokens
        ),
        "terminal_projection": model_terminal_projection_end_reference_fixture(
            call.resolved_model_call_id,
            outcome="completed",
        ),
    }


def model_terminal_projection_end_reference_fixture(
    resolved_model_call_id: str,
    *,
    outcome: str,
    item_count: int = 0,
) -> ModelCallTerminalProjectionEndReferenceFact:
    semantic_fingerprint = sha256_fingerprint(
        "test-model-projection-semantic:v1",
        (resolved_model_call_id, outcome, item_count),
    )
    semantic_join = ModelTerminalProjectionSemanticJoinFact(
        schema_version="model_terminal_projection_semantic_join.v1",
        projection_kind="model_call",
        terminal_outcome=outcome,
        projection_item_count=item_count,
        semantic_fingerprint=semantic_fingerprint,
    )
    reference = build_frozen_fact(
        TerminalProjectionReferenceFact,
        schema_version="terminal_projection_reference.v2",
        projection_kind="model_call",
        semantic_join=semantic_join,
        document_fact_fingerprint=sha256_fingerprint(
            "test-model-projection-document:v1",
            (resolved_model_call_id, outcome, item_count),
        ),
        document_artifact_id=f"test-terminal-projection:model:{resolved_model_call_id}",
        document_sha256=sha256_fingerprint(
            "test-model-projection-bytes:v1",
            (resolved_model_call_id, outcome, item_count),
        ),
        document_byte_count=1,
        document_contract_fingerprint=sha256_fingerprint(
            "test-terminal-projection-contract:v1", "model"
        ),
    )
    committed_identity = build_frozen_fact(
        StableEventIdentityFact,
        schema_version="stable_event_identity.v2",
        runtime_session_id="runtime:test",
        event_id=f"test-model-projection-committed:{resolved_model_call_id}",
        event_type="MODEL_CALL_TERMINAL_PROJECTION_COMMITTED",
        event_schema_version="1",
        event_schema_fingerprint=sha256_fingerprint(
            "test-event-schema:v1", "model-projection"
        ),
        payload_fingerprint=sha256_fingerprint(
            "test-event-payload:v1", (resolved_model_call_id, outcome, item_count)
        ),
    )
    return build_frozen_fact(
        ModelCallTerminalProjectionEndReferenceFact,
        schema_version="model_call_terminal_projection_end_ref.v2",
        projection_committed_event_identity=committed_identity,
        projection_reference=reference,
    )


def _test_run_activation_service(agent):
    service = _TEST_ACTIVATION_SERVICES.get(agent)
    if service is not None:
        return service

    from pulsara_agent.runtime.run_execution.interaction_transition import (
        RuntimeInteractionTransitionService,
    )
    from pulsara_agent.runtime.run_execution.service import RunActivationService

    registry = agent.run_execution_registry
    if registry is None:
        raise RuntimeError("test activation service requires a run registry")

    async def unexpected_resume_commit(_prepared):
        raise RuntimeError("test support has no Host resume-boundary commit owner")

    transition = RuntimeInteractionTransitionService(
        registry=registry,
        event_log=runtime_session_for_test(agent).event_log,
        runtime_session_id=runtime_session_for_test(agent).runtime_session_id,
        commit_resume_boundary=unexpected_resume_commit,
        classify_write_failure=lambda _exc: "other",
    )
    service = RunActivationService(
        registry=registry,
        event_log=runtime_session_for_test(agent).event_log,
        agent_runtime=agent,
        runtime_session_id=runtime_session_for_test(agent).runtime_session_id,
    )
    service.bind_interaction_transition_port(transition)
    _TEST_ACTIVATION_SERVICES[agent] = service
    return service


async def run_agent_task(agent, user_input: str, **kwargs):
    """Commit a test Host entry and run it through the production activation owner."""

    _prepare_test_host_run_entry(agent, user_input, kwargs)
    draft, committed, _stored = await _commit_test_host_run_entry(
        agent, user_input, kwargs
    )
    service = _test_run_activation_service(agent)
    dispatch = service.start_result_activation(
        run_id=committed.run_start_event.run_id,
        host_session_id="host:test-support",
        result_factory=lambda: agent.run_committed_entry(draft, committed),
    )
    from pulsara_agent.ports.run_execution import RunSegmentInstallBlocked

    if isinstance(dispatch, RunSegmentInstallBlocked):
        raise RuntimeError(f"test activation was blocked: {dispatch.reason}")
    return await _wait_test_activation_result(
        agent,
        dispatch,
        legacy_state=draft.state,
    )


async def commit_test_run_owner(agent, user_input: str, **kwargs):
    """Commit and promote a test run without starting its activation driver."""

    _prepare_test_host_run_entry(agent, user_input, kwargs)
    return await _commit_test_host_run_entry(agent, user_input, kwargs)


def stream_agent_task(agent, user_input: str, **kwargs):
    """Return a test-owned entry stream feeding the committed production API."""

    _prepare_test_host_run_entry(agent, user_input, kwargs)

    async def _stream():
        draft, committed, stored = await _commit_test_host_run_entry(
            agent, user_input, kwargs
        )
        service = _test_run_activation_service(agent)
        dispatch = service.start_stream_activation(
            run_id=committed.run_start_event.run_id,
            host_session_id="host:test-support",
            stream_factory=lambda: agent.stream_committed_entry(draft, committed),
        )
        from pulsara_agent.ports.run_execution import RunSegmentInstallBlocked

        if isinstance(dispatch, RunSegmentInstallBlocked):
            raise RuntimeError(f"test activation was blocked: {dispatch.reason}")
        if dispatch.observer is None:
            raise RuntimeError("test stream activation lacks its observer")
        try:
            for event in stored:
                yield event
            async for event in dispatch.observer:
                yield event
            await _wait_test_activation_result(
                agent,
                dispatch,
                legacy_state=draft.state,
            )
        finally:
            await dispatch.observer.aclose()

    return _stream()


async def request_test_run_stop(agent, run_id: str):
    """Exercise run control independently from the detachable observer."""

    from pulsara_agent.runtime.recovery import AbortKind

    return await _test_run_activation_service(agent).request_active_stop(
        run_id,
        AbortKind.USER_STOP,
    )


async def _commit_test_host_run_entry(agent, user_input: str, kwargs: dict):
    from pulsara_agent.event import (
        ContextWindowOpenedEvent,
        EventContext,
        RolloutBudgetAccountOpenedEvent,
    )
    from pulsara_agent.runtime.run_entry import (
        CommittedHostRunEntry,
        install_run_working_set,
    )
    from pulsara_agent.runtime.execution_handles import RunExecutionHandleSet
    from pulsara_agent.runtime.run_execution.prepared import PreparedRunActivationOwner
    from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
    from pulsara_agent.ports.run_execution import (
        build_prepared_run_owner_reservation_key,
    )
    from pulsara_agent.runtime.session import EventPublicationAfterCommitError
    from pulsara_agent.runtime.long_horizon.run_contract import (
        empty_projection_state_fingerprint,
        prepare_root_long_horizon_run,
    )

    state = kwargs["state"]
    target = kwargs["run_model_target"]
    prepared = kwargs["_prepared_test_host_run_authority"]
    if agent._subagent_parent_features_enabled and agent.subagent_runtime is not None:
        await agent.subagent_runtime.repair_dangling_children()
    run_start_event_id = f"run_start:test:{uuid4().hex}"
    long_horizon = prepare_root_long_horizon_run(
        runtime_session_id=runtime_session_for_test(agent).runtime_session_id,
        run_id=state.run_id,
        run_start_event_id=run_start_event_id,
        primary_target=target.fact,
        summarizer_target=agent.llm_runtime.resolve_target(role=ModelRole.FLASH).fact,
        graph_reducer_contract=(
            runtime_session_for_test(
                agent
            ).subagent_graph_checkpoint_service.reducer_binding.contract
        ),
        source_through_sequence_at_open=(
            runtime_session_for_test(agent).event_log.next_sequence() - 1
        ),
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    draft = await agent.prepare_run_draft(
        state,
        run_model_target=target,
        permission_snapshot=state.permission_snapshot,
        current_user_message=prepared["current_user_message_fact"],
        run_start_event_id=run_start_event_id,
        terminal_run_end_event_id=state.terminal_run_end_event_id,
        capability_basis=state.execution_resources.capability_resolve_basis.fact,
        frozen_execution_surface=(
            state.execution_resources.frozen_capability_execution_surface
        ),
        host_run_ingress=prepared["host_run_ingress"],
        host_ingress_admission_proof=prepared["host_ingress_admission_proof"],
        new_run_boundary=prepared["new_run_boundary_fact"],
        subagent_run_entry=None,
        long_horizon=long_horizon,
        child_rollout_subaccount=None,
        prior_messages=kwargs.get("prior_messages"),
    )
    registry = agent.run_execution_registry
    if registry is None:
        registry = RunExecutionRegistry()
        agent.bind_run_execution_registry(registry)
    reservation_key = build_prepared_run_owner_reservation_key(
        runtime_session_id=runtime_session_for_test(agent).runtime_session_id,
        run_id=state.run_id,
        run_start_event_id=run_start_event_id,
    )
    frozen_surface = state.execution_resources.frozen_capability_execution_surface
    if frozen_surface is None:
        raise RuntimeError("test run entry lost its frozen execution surface")
    execution_handles = RunExecutionHandleSet(
        handle_id=f"test_execution_handles:{uuid4().hex}",
        handle_generation=1,
        owner=reservation_key,
        state="boundary_owned",
        mcp_installation=runtime_session_for_test(agent).mcp_installation_id,
        capability_runtime=agent.capability_runtime,
        tool_registry=agent.tool_executor.registry,
        frozen_execution_surface=frozen_surface,
    )
    registry.reserve_prepared(
        key=reservation_key,
        execution_handles=execution_handles,
        reservation_generation=1,
    )
    current_task = asyncio.current_task()
    if current_task is None:
        raise RuntimeError("test run entry requires an asyncio task owner")
    prepared_activation = PreparedRunActivationOwner(
        run_id=state.run_id,
        boundary_id=draft.run_start_event.new_run_boundary.identity.boundary_id,
        owner_task=current_task,
        generation=1,
        _working_state=state,
    )
    audits = runtime_session_for_test(agent).pending_mcp_installation_audit_events(
        EventContext(
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
    )
    event_context = EventContext(
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
    )
    account = long_horizon.root_account
    assert account is not None
    window_open = ContextWindowOpenedEvent(
        id=long_horizon.contract.initial_window_open_event_id,
        **event_context.event_fields(),
        window=long_horizon.initial_window,
        opening_batch_id=long_horizon.opening_batch_id,
    )
    account_open = RolloutBudgetAccountOpenedEvent(
        id=f"rollout_budget_account_opened:{account.account_id}",
        **event_context.event_fields(),
        account=account,
    )
    try:
        stored = tuple(
            await runtime_session_for_test(agent).commit_accepted_events(
                (draft.run_start_event, window_open, account_open, *audits),
            )
        )
    except EventPublicationAfterCommitError as exc:
        runtime_session_for_test(agent).acknowledge_committed_mcp_installation_audits(
            exc.result.committed_events
        )
        registry.release_prepared(reservation_key, outcome="unknown")
        raise
    except BaseException:
        registry.release_prepared(reservation_key, outcome="none")
        prepared_activation.release()
        raise
    runtime_session_for_test(agent).acknowledge_committed_mcp_installation_audits(
        stored
    )
    run_start = stored[0]
    assert run_start.sequence is not None
    assert draft.run_start_event.new_run_boundary is not None
    committed = CommittedHostRunEntry(
        run_start_event=run_start,
        run_start_sequence=run_start.sequence,
        committed_through_sequence=stored[-1].sequence or run_start.sequence,
        publication_status="completed",
        boundary_id=draft.run_start_event.new_run_boundary.identity.boundary_id,
        committed_audit_event_ids=tuple(event.id for event in stored[3:]),
    )
    owner = registry.promote_committed_entry(
        reservation_key=reservation_key,
        committed=committed,
        run_start_envelope=runtime_session_for_test(
            agent
        ).event_log.read_raw_events_by_id((run_start.id,))[0],
        prepared_activation=prepared_activation,
    )
    state.execution_resources.capability_execution_borrow_authority = (
        owner.execution_handles.borrow_authority
    )
    state.execution_resources.capability_execution_borrow_kind = "parent"
    runtime_session_for_test(
        agent
    ).transcript_projection_checkpoint_service.adopt_committed_run_seed(run_start)
    install_run_working_set(
        state,
        committed,
        plan_snapshot=prepared["host_run_boundary_plan"],
        capability_resolve_basis=state.execution_resources.capability_resolve_basis,
        frozen_execution_surface=(
            state.execution_resources.frozen_capability_execution_surface
        ),
    )
    return draft, committed, stored


def _prepare_test_host_run_entry(agent, user_input: str, kwargs: dict) -> None:
    """Provide the typed Host run-entry contract for direct component tests."""

    from pulsara_agent.event.events import utc_now
    from pulsara_agent.capability.types import (
        CapabilityExecutionSurfaceSnapshotContext,
    )
    from pulsara_agent.primitives.capability import build_capability_resolve_basis
    from pulsara_agent.primitives.model_call import sha256_fingerprint
    from pulsara_agent.primitives.run_boundary import (
        BoundaryTranscriptSnapshotFact,
        NewRunBoundaryFact,
        PlanWorkflowStateFact,
    )
    from pulsara_agent.primitives.run_entry import (
        CapabilityExposureOwnerFact,
        CurrentUserMessageFact,
        HostRunBoundaryIdentityFact,
        text_sha256,
    )
    from pulsara_agent.llm.user_carrier import encode_human_input
    from pulsara_agent.primitives.host_ingress import (
        HostIngressAdmissionProofFact,
        HostIngressItemPlacementFact,
        HostRunIngressAttributionFact,
        HostRunIngressSemanticFact,
        HumanRunIngressFact,
    )
    from pulsara_agent.tools.registry import build_tool_binding_contract
    from pulsara_agent.primitives.permission import preset_permission_policy_fact
    from pulsara_agent.runtime.run_entry import CapabilityResolveBasis

    _ensure_test_postgres_runtime_owner(agent)
    state = kwargs.setdefault("state", agent.new_state())
    _TEST_RUN_STATES.setdefault(agent, {})[state.run_id] = state
    target = kwargs.setdefault("run_model_target", agent.resolve_run_model_target())
    permission = agent._capture_run_permission_snapshot(state)
    observed_at = utc_now()
    boundary = HostRunBoundaryIdentityFact(
        boundary_id=f"run_boundary:test:{uuid4().hex}",
        kind="pre_run",
        runtime_session_id=runtime_session_for_test(agent).runtime_session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        attempt_number=1,
        observed_at_utc=observed_at,
    )
    owner = CapabilityExposureOwnerFact(
        owner_kind="host_boundary",
        owner_id=boundary.boundary_id,
        host_boundary_kind="pre_run",
        runtime_session_id=boundary.runtime_session_id,
        run_id=boundary.run_id,
    )
    for tool_name in agent.tool_executor.registry.names():
        if agent.tool_executor.registry.binding_contract(tool_name) is None:
            agent.tool_executor.registry.bind_contract(
                build_tool_binding_contract(
                    tool_name=tool_name,
                    origin="custom",
                    contract_id=f"test.direct.{tool_name}",
                    contract_version="v1",
                )
            )
    frozen_surface = agent.capability_runtime.freeze_execution_surface(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=runtime_session_for_test(agent).workspace_root,
            workspace_kind=agent.workspace_kind,
            available_tool_names=frozenset(agent.tool_executor.registry.names()),
            mcp_installation_id=runtime_session_for_test(agent).mcp_installation_id,
        ),
        tool_registry=agent.tool_executor.registry,
        archive=runtime_session_for_test(agent).archive,
        runtime_session_id=runtime_session_for_test(agent).runtime_session_id,
        owner_id=boundary.boundary_id,
    )
    surface = frozen_surface.identity
    basis = build_capability_resolve_basis(
        basis_id=f"capability_basis:test:{uuid4().hex}",
        basis_kind="initial",
        source_basis_id=None,
        source_basis_fingerprint=None,
        owner=owner,
        workspace_identity_fingerprint=sha256_fingerprint(
            "test-workspace:v1", str(runtime_session_for_test(agent).workspace_root)
        ),
        memory_domain_id="memory_domain:test",
        permission_snapshot_id=permission.snapshot_id,
        plan_active=False,
        active_skill_names=tuple(sorted(kwargs.get("active_skill_names") or ())),
        user_intent_fingerprint=sha256_fingerprint("test-user-intent:v1", user_input),
        prior_transcript_fingerprint=sha256_fingerprint(
            "test-prior-transcript:v1",
            [
                message.model_dump(mode="json")
                for message in (kwargs.get("prior_messages") or ())
            ],
        ),
        mcp_installation_id=surface.mcp_installation_id,
        execution_surface_identity=surface,
    )
    transcript = BoundaryTranscriptSnapshotFact(
        source_through_sequence=0,
        source_event_count=0,
        compacted_window_id=None,
        checkpoint_compaction_id=None,
        checkpoint_terminal_event_id=None,
        checkpoint_terminal_sequence=None,
        checkpoint_keep_after_sequence=None,
        preflight_compaction_id=None,
        preflight_compaction_terminal_event_id=None,
        preflight_compaction_terminal_sequence=None,
    )
    current_user = CurrentUserMessageFact(
        message_id=f"user-message:{state.run_id}",
        source_kind="host_user_input",
        text=user_input,
        observed_at_utc=observed_at,
        content_sha256=text_sha256(user_input),
        source_artifact_id=None,
    )
    ingress_id = f"host_ingress:test:{state.run_id}"
    human = encode_human_input(
        user_input,
        causal_occurrence_semantic_fingerprint=context_fingerprint(
            "test-host-ingress-occurrence:v1",
            (runtime_session_for_test(agent).runtime_session_id, state.run_id),
        ),
    ).semantic_fact
    placement = build_frozen_fact(
        HostIngressItemPlacementFact,
        schema_version="host_ingress_item_placement.v1",
        item_kind="human_input",
        item_semantic_fingerprint=human.semantic_fingerprint,
        accepted_ingress_ordinal=1,
        item_ordinal=0,
    )
    ingress_semantic = build_frozen_fact(
        HostRunIngressSemanticFact,
        schema_version="host_run_ingress_semantic.v1",
        ordered_current_input_semantic_fingerprints=(human.semantic_fingerprint,),
    )
    ingress_attribution = build_frozen_fact(
        HostRunIngressAttributionFact,
        schema_version="host_run_ingress_attribution.v1",
        ingress_id=ingress_id,
        host_session_id=f"host:test:{runtime_session_for_test(agent).runtime_session_id}",
        conversation_id=None,
        observed_at_utc=observed_at,
        ingress_semantic_fingerprint=(ingress_semantic.ingress_semantic_fingerprint),
        ordered_item_placements=(placement,),
    )
    host_ingress = build_frozen_fact(
        HumanRunIngressFact,
        schema_version="human_run_ingress.v1",
        semantic_identity=ingress_semantic,
        attribution=ingress_attribution,
        human_message=human,
        attached_runtime_notifications=(),
    )
    host_admission = build_frozen_fact(
        HostIngressAdmissionProofFact,
        schema_version="host_ingress_admission_proof.v1",
        admission_id=ingress_id,
        admission_generation=1,
        ingress_fact_fingerprint=host_ingress.fact_fingerprint,
        selected_ingress_item_ids=(ingress_id,),
        selected_notification_head_fingerprints=(),
        expected_host_state_generation=0,
        expected_permission_policy_revision=0,
        expected_permission_policy_fingerprint=context_fingerprint(
            "test-host-ingress-permission:v1", permission.snapshot_id
        ),
        expected_close_intent_revision=0,
        expected_autonomy_chain_state_fingerprint=None,
        proposed_automatic_delivery_ordinal=None,
    )
    state.permission_snapshot = permission
    state.run_model_target = target
    state.execution_resources.current_user_message_fact = current_user
    state.terminal_run_end_event_id = f"run_end:test:{uuid4().hex}"
    state.execution_resources.frozen_capability_execution_surface = frozen_surface
    state.execution_resources.capability_resolve_basis = CapabilityResolveBasis(
        fact=basis,
        user_input=user_input,
        prior_messages=tuple(
            message.model_copy(deep=True)
            for message in (kwargs.get("prior_messages") or ())
        ),
        active_skill_names=frozenset(kwargs.get("active_skill_names") or ()),
        workspace_root=runtime_session_for_test(agent).workspace_root,
        memory_domain_id="memory_domain:test",
    )
    kwargs["_prepared_test_host_run_authority"] = {
        "current_user_message_fact": current_user,
        "host_run_ingress": host_ingress,
        "host_ingress_admission_proof": host_admission,
        "new_run_boundary_fact": NewRunBoundaryFact(
            identity=boundary,
            transcript=transcript,
            model_target_fingerprint=target.fact.target_fingerprint,
            permission_snapshot_id=permission.snapshot_id,
            mcp_installation_id=surface.mcp_installation_id,
            capability_basis=basis,
            degraded_reason_codes=(),
        ),
        "host_run_boundary_plan": PlanWorkflowStateFact(
            workflow_id=None,
            active=False,
            pending_entry_audit=False,
            revision=0,
            entered_event_id=None,
            entered_event_sequence=None,
            entry_run_id=None,
            entry_turn_id=None,
            entry_reply_id=None,
            stored_default_permission=preset_permission_policy_fact(
                permission.permission_mode
            ),
            accepted_plan_artifact_id=None,
        ),
    }


def _ensure_test_postgres_runtime_owner(agent) -> None:
    """Mirror the production Host's durable session-owner precondition.

    Direct component tests intentionally bypass HostCore/SessionManifestStore,
    but PostgreSQL artifacts still require their runtime session owner to exist
    before the pre-RunStart capability surface is frozen.
    """

    from pulsara_agent.memory import PostgresArtifactStore

    archive = runtime_session_for_test(agent).archive
    if not isinstance(archive, PostgresArtifactStore):
        return

    event_log = runtime_session_for_test(agent).event_log
    ensure_owner = getattr(event_log, "ensure_runtime_session_owner", None)
    if ensure_owner is None:
        raise TypeError("PostgreSQL test runtime lacks a verified session-owner port")
    ensure_owner()


@dataclass(frozen=True, slots=True)
class _ContractOnlyTransport:
    api: str
    binding_id: str = "test.contract_only"
    contract_version: str = "v1"

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        if False:
            yield  # pragma: no cover


def test_model_limits(
    *,
    total_context_tokens: int = 256_000,
    max_input_tokens: int = 256_000,
    max_output_tokens: int = 8_192,
    default_output_tokens: int = 8_000,
    input_safety_margin_tokens: int = 64_000,
) -> ModelContextLimits:
    return ModelContextLimits(
        total_context_tokens=total_context_tokens,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        default_output_tokens=default_output_tokens,
        input_safety_margin_tokens=input_safety_margin_tokens,
    )


def test_model_slot(
    model_id: str,
    *,
    limits: ModelContextLimits | None = None,
) -> ModelSlotConfig:
    return ModelSlotConfig(model_id=model_id, limits=limits or test_model_limits())


def test_llm_config(
    *,
    api_key: str,
    base_url: str,
    pro_model: str,
    flash_model: str,
    api: str = "openai_responses",
    provider: str = "custom",
    provider_profile: ProviderProfile | None = None,
    retry: LLMRetryConfig = LLMRetryConfig(),
    openai_sdk_max_retries: int | None = None,
    pro_limits: ModelContextLimits | None = None,
    flash_limits: ModelContextLimits | None = None,
) -> LLMConfig:
    """Build a production-shaped config while keeping terse test call sites."""

    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        pro=test_model_slot(pro_model, limits=pro_limits),
        flash=test_model_slot(flash_model, limits=flash_limits),
        api=api,
        provider=provider,
        provider_profile=provider_profile,
        retry=retry,
        openai_sdk_max_retries=openai_sdk_max_retries,
    )


def test_resolved_target_fact(
    *,
    model_id: str = "test-pro",
    role: ModelRole = ModelRole.PRO,
    limits: ModelContextLimits | None = None,
) -> ResolvedModelTargetFact:
    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model=model_id if role is ModelRole.PRO else "test-pro",
        flash_model=model_id if role is ModelRole.FLASH else "test-flash",
        api="mock",
        pro_limits=limits,
        flash_limits=limits,
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="test"))
    return resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=None,
    ).fact


def test_resolved_call_fact(
    *,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
) -> ResolvedModelCallFact:
    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model="test-pro",
        flash_model="test-flash",
        api="mock",
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="test"))
    role = (
        ModelRole.PRO
        if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        else ModelRole.FLASH
    )
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=None,
    )
    return resolve_model_call(target=target, purpose=purpose).fact


def test_resolved_call(
    *,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
    limits: ModelContextLimits | None = None,
    options: LLMOptions | None = None,
    provider_profile: ProviderProfile | None = None,
):
    """Return a runtime call for component tests that do not own an LLM runtime."""

    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model="test-pro",
        flash_model="test-flash",
        api="mock",
        provider_profile=provider_profile,
        pro_limits=limits,
        flash_limits=limits,
    )
    role = (
        ModelRole.PRO
        if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        else ModelRole.FLASH
    )
    return resolve_test_call(config, role=role, purpose=purpose, options=options)


def resolve_test_call(
    config: LLMConfig,
    *,
    role: ModelRole = ModelRole.PRO,
    options: LLMOptions | None = None,
    transport=None,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
):
    registry = LLMTransportRegistry()
    registry.register(transport or _ContractOnlyTransport(api=config.api))
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=options,
    )
    return resolve_model_call(target=target, purpose=purpose)


def bind_test_context(
    call,
    context: LLMContext,
    *,
    context_id: str | None = None,
    model_call_index: int | None = None,
) -> LLMContext:
    index = model_call_index
    if index is None and call.fact.context_mode == "compiled":
        index = context.model_call_index if context.model_call_index is not None else 1
    bound = replace(
        context,
        context_id=context_id or context.context_id or "context:test",
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=index,
    )
    if (
        call.fact.context_mode == "compiled"
        and bound.compiler_estimated_input_tokens is None
    ):
        bound = replace(
            bound,
            compiler_estimated_input_tokens=(
                call.target.token_estimator.estimate_context(bound).total_input_tokens
            ),
        )
    return bound


def bind_test_provider_input_context(
    call,
    provider_input,
    context: LLMContext,
) -> LLMContext:
    """Hydrate a prepared carrier and freeze its final compiled estimate.

    Production compiled calls receive this estimate from the context compiler.
    Tests that intentionally route a compiled call through the one-shot planner
    must recompute it after the planner appends its per-invocation clock.
    """

    bound = provider_input.carrier.to_llm_context(context)
    if call.fact.context_mode == "compiled":
        bound = replace(
            bound,
            compiler_estimated_input_tokens=(
                call.target.token_estimator.estimate_context(bound).total_input_tokens
            ),
        )
    return bound


async def start_test_direct_model_stream(
    runtime,
    *,
    call,
    context: LLMContext,
    event_context: EventContext,
    runtime_session,
):
    """Start a direct model call through the production durable lifecycle."""

    provider_input = await runtime_session.provider_input_generation_coordinator.prepare_one_shot_call(
        call=call,
        context=context,
        event_context=event_context,
        operation_kind="direct_model_call",
        operation_id=call.fact.resolved_model_call_id,
    )
    context = bind_test_provider_input_context(call, provider_input, context)
    bundle = prepare_model_lifecycle_start_bundle(
        call=call,
        context=context,
        event_context=event_context,
        runtime_session=runtime_session,
        lifecycle_kind="direct_internal_call",
        provider_input_start_bundle=provider_input,
    )
    return runtime.start_stream(
        call=call,
        context=context,
        event_context=event_context,
        start_bundle=bundle,
        commit_port=RuntimeSessionModelStreamEventCommitPort(
            runtime_session=runtime_session,
        ),
        execution_registry=runtime_session.model_stream_execution_registry,
    )


def test_llm_context(**kwargs) -> LLMContext:
    """Build a structurally complete context before a test binds its real call."""

    kwargs.setdefault("context_id", "context:test-unbound")
    kwargs.setdefault("resolved_model_call_id", f"model_call:{'0' * 32}")
    kwargs.setdefault("target_fingerprint", f"sha256:{'0' * 64}")
    kwargs.setdefault("model_call_index", None)
    return LLMContext(**kwargs)


test_model_limits.__test__ = False
test_model_slot.__test__ = False
test_llm_config.__test__ = False
test_resolved_target_fact.__test__ = False
test_resolved_call_fact.__test__ = False
test_resolved_call.__test__ = False
resolve_test_call.__test__ = False
bind_test_context.__test__ = False
test_llm_context.__test__ = False
model_call_start_fields.__test__ = False
model_call_end_fields.__test__ = False
context_compiled_contract_fields.__test__ = False
compaction_completed_contract_fields.__test__ = False
compaction_started_contract_fields.__test__ = False
compaction_failed_contract_fields.__test__ = False
run_agent_task.__test__ = False
commit_test_run_owner.__test__ = False
stream_agent_task.__test__ = False
request_test_run_stop.__test__ = False
_TEST_RUN_STATES: WeakKeyDictionary = WeakKeyDictionary()
_TEST_ACTIVATION_SERVICES: WeakKeyDictionary = WeakKeyDictionary()


def test_owned_run_state(agent, run_id: str):
    """Return a test-owned working state without widening production results."""

    states = _TEST_RUN_STATES.get(agent, {})
    state = states.get(run_id)
    if state is None:
        raise KeyError(f"test run state is unavailable: {run_id}")
    owner = agent.run_execution_registry.require(run_id)
    if (
        owner.lifecycle == "suspended"
        and not state.pending_tool_calls
        and (state.pending_interaction_kind is None)
    ):
        service = _test_run_activation_service(agent)
        transition = service._interaction_transition_port
        if transition is None:
            raise RuntimeError("test activation service lacks interaction owner")
        return transition.hydrate_suspended_working_state(run_id=run_id)
    return state


test_owned_run_state.__test__ = False


def test_owned_terminalization_state(agent, run_id: str):
    """Borrow a suspended test run through its stable finalization owner.

    Direct AgentRuntime component tests intentionally omit the Host continuation
    boundary.  They must still make the process-state ownership transfer explicit
    instead of weakening the production finalization guard.
    """

    state = test_owned_run_state(agent, run_id)
    owner = agent.run_execution_registry.require(run_id)
    if owner.lifecycle == "suspended":
        agent.run_execution_registry.transfer_suspension_state_to_finalization(run_id)
    return state


test_owned_terminalization_state.__test__ = False


async def resume_test_agent_after_approval(agent, run_id: str, resolution):
    """Drive a direct Agent component resume through a real activation owner."""

    from pulsara_agent.runtime.run_execution.owner import (
        ActiveRunSuspension,
        NoActiveSuspension,
    )
    from pulsara_agent.ports.run_execution import RunSegmentInstallBlocked

    state = test_owned_run_state(agent, run_id)
    pending_by_id = {call.id: call for call in state.pending_tool_calls}
    decisions_by_id = {
        decision.tool_call_id: decision for decision in resolution.decisions
    }
    unknown_ids = set(decisions_by_id).difference(pending_by_id)
    if unknown_ids:
        raise ValueError(
            f"approval resolution referenced unknown tool calls: {sorted(unknown_ids)}"
        )
    missing_ids = set(pending_by_id).difference(decisions_by_id)
    if missing_ids:
        raise ValueError(
            f"approval resolution missing decisions for tool calls: {sorted(missing_ids)}"
        )
    owner = agent.run_execution_registry.require(run_id)
    suspension = owner.suspension_slot
    if not isinstance(suspension, ActiveRunSuspension):
        raise RuntimeError("test resume requires one active suspension owner")
    carrier = suspension.resources.state_carrier
    pending_token = (
        f"test-resume-pending:{owner.identity.owner_fingerprint}:"
        f"{owner.next_segment_generation + 1}"
    )
    carrier.transfer(
        expected_owner_token=suspension.resources.state_owner_token,
        new_owner_token=pending_token,
    )
    owner.pending_activation_state = carrier
    owner.pending_activation_owner_token = pending_token
    owner.suspension_slot = NoActiveSuspension()
    owner.lifecycle = "initializing"

    service = _test_run_activation_service(agent)
    dispatch = service.start_result_activation(
        run_id=run_id,
        host_session_id="host:test-support",
        result_factory=lambda: agent.resume_after_approval(state, resolution),
    )
    if isinstance(dispatch, RunSegmentInstallBlocked):
        raise RuntimeError(f"test resume activation was blocked: {dispatch.reason}")
    try:
        return await _wait_test_activation_result(
            agent,
            dispatch,
            legacy_state=state,
        )
    except BaseException:
        # This test-only path bypasses the production transition service so it
        # can exercise malformed resolutions. Restore the immutable suspension
        # when validation rejects before any durable continuation exists.
        active = owner.active_segment
        if (
            active is not None
            and active.state_carrier is carrier
            and active.state_owner_token is not None
        ):
            carrier.transfer(
                expected_owner_token=active.state_owner_token,
                new_owner_token=suspension.resources.state_owner_token,
            )
            active.state_carrier = None
            active.state_owner_token = None
            if active.execution_handle_borrow is not None:
                active.execution_handle_borrow.release()
                active.execution_handle_borrow = None
            owner.active_segment = None
            owner.suspension_slot = suspension
            owner.lifecycle = "suspended"
        raise


resume_test_agent_after_approval.__test__ = False


async def _wait_test_activation_result(agent, dispatch, *, legacy_state=None):
    """Adapt the production closed outcome at the test-support boundary only."""

    from pulsara_agent.ports.run_execution import (
        RunReconciliationRequired,
        RunSuspendedOutcome,
        RunTerminalOutcome,
        RunTerminalOutputPending,
        RunTerminalizationPending,
    )
    from pulsara_agent.runtime.agent import agent_run_result_from_terminal_outcome

    outcome = await dispatch.wait_activation()
    if isinstance(outcome, RunTerminalOutcome):
        if legacy_state is not None:
            return agent.result_from_owned_state(legacy_state)
        return agent_run_result_from_terminal_outcome(outcome)
    if isinstance(outcome, (RunTerminalizationPending, RunTerminalOutputPending)):
        terminal = await dispatch.run_handle.wait_run_completion()
        if legacy_state is not None:
            return agent.result_from_owned_state(legacy_state)
        return agent_run_result_from_terminal_outcome(terminal)
    if isinstance(outcome, RunSuspendedOutcome):
        # Legacy unit tests inspect the mutable state after suspension. This
        # compatibility view is deliberately confined to tests/support;
        # production Host and child callers consume the closed authority.
        return agent.result_from_owned_state(
            legacy_state
            if legacy_state is not None
            else test_owned_run_state(agent, outcome.owner_identity.run_id)
        )
    if isinstance(outcome, RunReconciliationRequired):
        raise RuntimeError(
            f"test activation requires reconciliation: {outcome.diagnostic_code}"
        )
    raise TypeError(f"unsupported activation outcome: {type(outcome).__name__}")
