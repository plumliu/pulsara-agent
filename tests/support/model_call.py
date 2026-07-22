"""Central model-target fixtures for hard-cut runtime tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import AsyncIterator
from uuid import uuid4

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
    ContextCompileInputAuditFact,
    context_fingerprint,
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
    manifest_reference = None
    prepared_plan_fingerprint = None
    prepared_candidate_fingerprint = None
    if status == "compiled":
        prepared_candidate, manifest_reference = (
            _compiled_provider_input_candidate_fixture(
                call,
                context_id=context_id,
                model_call_index=model_call_index,
            )
        )
        assert prepared_candidate.prepared_plan is not None
        prepared_plan_fingerprint = prepared_candidate.prepared_plan.plan_fingerprint
        prepared_candidate_fingerprint = prepared_candidate.candidate_fingerprint
    return {
        "status": status,
        "failure_stage": "context_compile" if status == "failed" else None,
        "compile_attempt_index": 1,
        "context_retry_index": 0,
        "resolved_call": call,
        "budget": budget,
        "input_audit": ContextCompileInputAuditFact(
            snapshot_id="context_snapshot:test",
            snapshot_semantic_fingerprint="sha256:" + "1" * 64,
            snapshot_fact_fingerprint="sha256:" + "2" * 64,
            snapshot_schema_version="context-snapshot:v1",
            compiler_contract_version="context-compiler-input:v1",
            source_runtime_session_id="runtime:test",
            authority_from_sequence=1,
            source_through_sequence=1,
            authority_slice_plan_fingerprint="sha256:" + "3" * 64,
            transcript_projection_window_fingerprint="sha256:" + "4" * 64,
            run_start_event_id="run-start:test",
            run_start_sequence=1,
            continuation_event_id=None,
            continuation_sequence=None,
            continuation_count=0,
            resolved_model_call_id=call.resolved_model_call_id,
            model_call_index=model_call_index,
            compile_attempt_index=1,
            context_retry_index=0,
            transcript_fingerprint="sha256:" + "5" * 64,
            transcript_message_count=1,
            transcript_pair_count=0,
            tool_result_units_fingerprint="sha256:" + "6" * 64,
            tool_result_unit_count=0,
            tool_result_render_policy_fingerprint="sha256:" + "7" * 64,
            tool_result_render_input_fingerprint="sha256:" + "8" * 64,
            prepared_candidate_set_fingerprint="sha256:" + "9" * 64,
            section_candidate_count=1,
            input_aggregate_fingerprint="sha256:" + "a" * 64,
            input_manifest_artifact_id=(
                manifest_reference.input_manifest_artifact_id
                if manifest_reference is not None
                else "context-input-manifest:test"
            ),
            input_manifest_fingerprint="sha256:" + "b" * 64,
            long_horizon_attribution_fingerprint="sha256:" + "e" * 64,
            input_manifest_write_outcome="stored",
        ),
        "provider_neutral_payload_fingerprint": (
            "sha256:" + "c" * 64 if status == "compiled" else None
        ),
        "canonical_render_decisions_fingerprint": (
            "sha256:" + "d" * 64 if status == "compiled" else None
        ),
        "prepared_provider_input": prepared_candidate,
        "manifest_projection_reference": manifest_reference,
        "prepared_provider_input_plan_fingerprint": prepared_plan_fingerprint,
        "prepared_provider_input_candidate_fingerprint": (
            prepared_candidate_fingerprint
        ),
    }


def _compiled_provider_input_candidate_fixture(
    call: ResolvedModelCallFact,
    *,
    context_id: str,
    model_call_index: int,
):
    """Wrap the one-shot physical fixture in the compiled manifest contract."""

    from pulsara_agent.primitives.context import context_fingerprint
    from pulsara_agent.primitives.provider_input import (
        ContextInputManifestProjectionReferenceFact,
        PreparedProviderInputAppendCandidateFact,
        PreparedProviderInputPlanFact,
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
    empty_wire = context_fingerprint(
        "provider-ordered-transcript-wire:v2:empty", ()
    )
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
    manifest_reference = build_frozen_fact(
        ContextInputManifestProjectionReferenceFact,
        schema_version="context_input_manifest_projection_reference.v1",
        context_id=context_id,
        input_manifest_artifact_id=f"context-input-manifest:test:{context_id}",
        input_manifest_content_fingerprint=context_fingerprint(
            "test-context-input-manifest-content:v1", context_id
        ),
        input_manifest_fact_fingerprint=context_fingerprint(
            "test-context-input-manifest-fact:v1", context_id
        ),
        projection_identity=projection_identity,
    )
    payload = {
        name: getattr(candidate, name)
        for name in candidate.__class__.model_fields
        if name not in {"schema_version", "candidate_fingerprint"}
    }
    payload.update(
        candidate_kind="compiled_manifest",
        prepared_plan=prepared_plan,
        manifest_projection_reference=manifest_reference,
        rollover_request=None,
    )
    return (
        build_frozen_fact(
            PreparedProviderInputAppendCandidateFact,
            schema_version="prepared_provider_input_append_candidate.v2",
            **payload,
        ),
        manifest_reference,
    )


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


async def run_agent_task(agent, user_input: str, **kwargs):
    """Commit a test-owned Host entry, then invoke the production committed API."""

    _prepare_test_host_run_entry(agent, user_input, kwargs)
    draft, committed, _stored = await _commit_test_host_run_entry(
        agent, user_input, kwargs
    )
    try:
        return await agent.run_committed_entry(draft, committed)
    finally:
        state = kwargs["state"]
        working_set = state.run_working_set
        if (
            state.status.value != "waiting_user"
            and working_set is not None
            and working_set.model_call_control_owner is not None
        ):
            await working_set.model_call_control_owner.retire()
            working_set.model_call_control_owner = None


def stream_agent_task(agent, user_input: str, **kwargs):
    """Return a test-owned entry stream feeding the committed production API."""

    _prepare_test_host_run_entry(agent, user_input, kwargs)

    async def _stream():
        draft, committed, stored = await _commit_test_host_run_entry(
            agent, user_input, kwargs
        )
        try:
            for event in stored:
                yield event
            async for event in agent.stream_committed_entry(draft, committed):
                yield event
        finally:
            state = kwargs["state"]
            working_set = state.run_working_set
            if (
                state.status.value != "waiting_user"
                and working_set is not None
                and working_set.model_call_control_owner is not None
            ):
                await working_set.model_call_control_owner.retire()
                working_set.model_call_control_owner = None

    return _stream()


async def _commit_test_host_run_entry(agent, user_input: str, kwargs: dict):
    from pulsara_agent.event import (
        ContextWindowOpenedEvent,
        EventContext,
        RolloutBudgetAccountOpenedEvent,
    )
    from pulsara_agent.runtime.run_entry import (
        CommittedHostRunEntry,
        install_run_working_set,
        prepare_agent_run_draft,
    )
    from pulsara_agent.runtime.session import EventPublicationAfterCommitError
    from pulsara_agent.runtime.long_horizon.run_contract import (
        empty_projection_state_fingerprint,
        prepare_root_long_horizon_run,
    )

    state = kwargs["state"]
    target = kwargs["run_model_target"]
    if (
        agent._subagent_parent_features_enabled
        and agent.subagent_runtime is not None
        and not agent._subagent_dangling_repair_done
    ):
        await agent.subagent_runtime.repair_dangling_children()
        agent._subagent_dangling_repair_done = True
    run_start_event_id = f"run_start:test:{uuid4().hex}"
    long_horizon = prepare_root_long_horizon_run(
        runtime_session_id=agent.runtime_session.runtime_session_id,
        run_id=state.run_id,
        run_start_event_id=run_start_event_id,
        primary_target=target.fact,
        summarizer_target=agent.llm_runtime.resolve_target(role=ModelRole.FLASH).fact,
        graph_reducer_contract=(
            agent.runtime_session.subagent_graph_checkpoint_service.reducer_binding.contract
        ),
        source_through_sequence_at_open=(
            agent.runtime_session.event_log.next_sequence() - 1
        ),
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    draft = await prepare_agent_run_draft(
        agent,
        state,
        run_model_target=target,
        permission_snapshot=state.permission_snapshot,
        current_user_message=state.scratchpad["current_user_message_fact"],
        run_start_event_id=run_start_event_id,
        terminal_run_end_event_id=state.scratchpad["terminal_run_end_event_id"],
        capability_basis=state.scratchpad["capability_resolve_basis"].fact,
        frozen_execution_surface=state.scratchpad[
            "frozen_capability_execution_surface"
        ],
        host_run_ingress=state.scratchpad["host_run_ingress"],
        host_ingress_admission_proof=state.scratchpad[
            "host_ingress_admission_proof"
        ],
        new_run_boundary=state.scratchpad["new_run_boundary_fact"],
        subagent_run_entry=None,
        long_horizon=long_horizon,
        child_rollout_subaccount=None,
        prior_messages=kwargs.get("prior_messages"),
    )
    audits = agent.runtime_session.pending_mcp_installation_audit_events(
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
            await agent.runtime_session.emit_many(
                (draft.run_start_event, window_open, account_open, *audits),
                state=state,
            )
        )
    except EventPublicationAfterCommitError as exc:
        agent.runtime_session.acknowledge_committed_mcp_installation_audits(
            exc.result.committed_events
        )
        raise
    agent.runtime_session.acknowledge_committed_mcp_installation_audits(stored)
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
    agent.runtime_session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
        run_start
    )
    install_run_working_set(
        state,
        committed,
        plan_snapshot=state.scratchpad["host_run_boundary_plan"],
        capability_resolve_basis=state.scratchpad["capability_resolve_basis"],
        frozen_execution_surface=state.scratchpad[
            "frozen_capability_execution_surface"
        ],
    )
    from pulsara_agent.llm.control import RunModelCallControlOwner

    working_set = state.run_working_set
    assert working_set is not None
    activation_payload = {
        "schema_version": "run_execution_activation.v1",
        "activation_owner_kind": "host_run_boundary",
        "activation_owner_id": draft.run_start_event.new_run_boundary.identity.boundary_id,
        "segment_generation": 1,
    }
    activation = RunExecutionActivationFact(
        **activation_payload,
        activation_fingerprint=sha256_fingerprint(
            "run-execution-activation:v1", activation_payload
        ),
    )
    working_set.run_execution_activation = activation
    working_set.process_segment_id = f"test_segment:{state.run_id}:1"
    working_set.model_call_control_owner = RunModelCallControlOwner(
        run_id=state.run_id,
        activation=activation,
        segment_id=working_set.process_segment_id,
        segment_generation=1,
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
    target = kwargs.setdefault("run_model_target", agent.resolve_run_model_target())
    permission = agent._capture_run_permission_snapshot(state)
    observed_at = utc_now()
    boundary = HostRunBoundaryIdentityFact(
        boundary_id=f"run_boundary:test:{uuid4().hex}",
        kind="pre_run",
        runtime_session_id=agent.runtime_session.runtime_session_id,
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
            workspace_root=agent.runtime_session.workspace_root,
            workspace_kind=agent.workspace_kind,
            available_tool_names=frozenset(agent.tool_executor.registry.names()),
            mcp_installation_id=agent.runtime_session.mcp_installation_id,
        ),
        tool_registry=agent.tool_executor.registry,
        archive=agent.runtime_session.archive,
        runtime_session_id=agent.runtime_session.runtime_session_id,
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
            "test-workspace:v1", str(agent.runtime_session.workspace_root)
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
            (agent.runtime_session.runtime_session_id, state.run_id),
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
        host_session_id=f"host:test:{agent.runtime_session.runtime_session_id}",
        conversation_id=None,
        observed_at_utc=observed_at,
        ingress_semantic_fingerprint=(
            ingress_semantic.ingress_semantic_fingerprint
        ),
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
    state.scratchpad.update(
        {
            "current_user_message_fact": current_user,
            "host_run_ingress": host_ingress,
            "host_ingress_admission_proof": host_admission,
            "terminal_run_end_event_id": f"run_end:test:{uuid4().hex}",
            "new_run_boundary_fact": NewRunBoundaryFact(
                identity=boundary,
                transcript=transcript,
                model_target_fingerprint=target.fact.target_fingerprint,
                permission_snapshot_id=permission.snapshot_id,
                mcp_installation_id=surface.mcp_installation_id,
                capability_basis=basis,
                degraded_reason_codes=(),
            ),
            "frozen_capability_execution_surface": frozen_surface,
            "capability_resolve_basis": CapabilityResolveBasis(
                fact=basis,
                user_input=user_input,
                prior_messages=tuple(
                    message.model_copy(deep=True)
                    for message in (kwargs.get("prior_messages") or ())
                ),
                active_skill_names=frozenset(kwargs.get("active_skill_names") or ()),
                workspace_root=agent.runtime_session.workspace_root,
                memory_domain_id="memory_domain:test",
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
    )


def _ensure_test_postgres_runtime_owner(agent) -> None:
    """Mirror the production Host's durable session-owner precondition.

    Direct component tests intentionally bypass HostCore/SessionManifestStore,
    but PostgreSQL artifacts still require their runtime session owner to exist
    before the pre-RunStart capability surface is frozen.
    """

    from pulsara_agent.memory import PostgresArtifactStore

    archive = agent.runtime_session.archive
    if not isinstance(archive, PostgresArtifactStore):
        return

    import psycopg

    with psycopg.connect(archive.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sessions (id, workspace_root)
                values (%s, %s)
                on conflict (id) do nothing
                """,
                (
                    agent.runtime_session.runtime_session_id,
                    str(agent.runtime_session.workspace_root),
                ),
            )


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
            state=None,
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
stream_agent_task.__test__ = False
