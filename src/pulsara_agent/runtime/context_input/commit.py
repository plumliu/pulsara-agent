"""Factories for the compact ContextCompiled dispatch authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context_input_audit_storage import (
    ContextInputAuditComponentKind,
    ContextInputAuditComponentOwnership,
    MAX_AUDIT_COMPONENT_REFERENCES,
    MAX_AUDIT_INLINE_ITEM_BYTES,
    MAX_AUDIT_PAGES,
    MAX_AUDIT_PAGE_CANONICAL_BYTES,
    MAX_AUDIT_PLAN_CANONICAL_BYTES,
    MAX_AUDIT_ROOT_CANONICAL_BYTES,
    MAX_AUDIT_TOTAL_INLINE_BYTES,
    MAX_AUDIT_TOTAL_PAGE_CANONICAL_BYTES,
)
from pulsara_agent.primitives.context_input_commit import (
    CheckpointProjectionBaseReferenceFact,
    ContextCompileInputCommitFact,
    ContextCompileSourceReferenceSetFact,
    ContextInputAuditExpectationFact,
    RunSeedProjectionBaseReferenceFact,
    build_context_input_audit_expectation,
)
from pulsara_agent.primitives.context import LongHorizonContextAttributionFact
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    ProviderInputPreparationInstallFact,
)
from pulsara_agent.primitives.transcript_projection import (
    CheckpointProjectionBaseFact,
    RunSeedProjectionBaseFact,
    TranscriptProjectionBaseFact,
)

if TYPE_CHECKING:
    from pulsara_agent.event import ContextCompiledEvent


CONTEXT_INPUT_AUDIT_CONTRACT_ID = "pulsara.context-input-audit"
CONTEXT_INPUT_AUDIT_CONTRACT_VERSION = "1"
MAX_CONTEXT_COMPILED_EVENT_CANONICAL_BYTES = 256 * 1024
_CONTEXT_INPUT_AUDIT_REFERENCE_COMPONENTS = frozenset(
    {
        ContextInputAuditComponentKind.SNAPSHOT,
        ContextInputAuditComponentKind.SUBAGENT_GRAPH_SEMANTIC_SOURCE,
        ContextInputAuditComponentKind.SUBAGENT_GRAPH_ACCELERATION,
        ContextInputAuditComponentKind.ORDERED_TRANSCRIPT_PROJECTION_IDENTITY,
        ContextInputAuditComponentKind.PREPARED_PROVIDER_INPUT_PLAN,
        ContextInputAuditComponentKind.CANONICAL_PROVIDER_INPUT_PLAN,
        ContextInputAuditComponentKind.TRANSCRIPT_PROVIDER_PROJECTION,
        ContextInputAuditComponentKind.TRANSCRIPT_AUTHORITY,
        ContextInputAuditComponentKind.TOOL_RESULT_RENDER_POLICY,
        ContextInputAuditComponentKind.ACTIVE_WINDOW,
        ContextInputAuditComponentKind.WINDOW_POLICY,
        ContextInputAuditComponentKind.PROJECTION_STATE,
        ContextInputAuditComponentKind.PROJECTED_TOOL_RESULT_REFS,
        ContextInputAuditComponentKind.PREPARED_ROLLUP_UNITS,
        ContextInputAuditComponentKind.ROLLOUT_STATE,
        ContextInputAuditComponentKind.CONTEXT_BUDGET_DECISION,
        ContextInputAuditComponentKind.PROJECTION_PRESSURE_SHADOW,
        ContextInputAuditComponentKind.PROJECTION_TARGET_UNREACHABLE,
        ContextInputAuditComponentKind.MODEL_START_ATTRIBUTION,
    }
)


def context_input_audit_component_ownership(
    kind: ContextInputAuditComponentKind,
) -> ContextInputAuditComponentOwnership:
    if kind in _CONTEXT_INPUT_AUDIT_REFERENCE_COMPONENTS:
        return ContextInputAuditComponentOwnership.EXISTING_AUTHORITY_REFERENCE
    return ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL


class ContextInputAuditExtractorContract(BaseModel):
    """Immutable process contract for one closed audit component extractor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_kind: ContextInputAuditComponentKind
    ownership: ContextInputAuditComponentOwnership
    extractor_id: str = Field(min_length=1, max_length=192)
    extractor_version: str = Field(min_length=1, max_length=32)
    extractor_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _identity(self) -> "ContextInputAuditExtractorContract":
        expected_id = f"pulsara.context-input-audit.{self.component_kind.value}"
        expected_fingerprint = context_fingerprint(
            "context-input-audit-component-extractor:v1",
            (
                self.component_kind.value,
                self.ownership.value,
                "canonical-json-reference-or-owned-detail:v1",
            ),
        )
        if (
            self.extractor_id != expected_id
            or self.extractor_version != "1"
            or self.extractor_fingerprint != expected_fingerprint
        ):
            raise ValueError("context input audit extractor contract identity drifted")
        return self


CONTEXT_INPUT_AUDIT_EXTRACTOR_CONTRACTS = tuple(
    ContextInputAuditExtractorContract(
        component_kind=kind,
        ownership=context_input_audit_component_ownership(kind),
        extractor_id=f"pulsara.context-input-audit.{kind.value}",
        extractor_version="1",
        extractor_fingerprint=context_fingerprint(
            "context-input-audit-component-extractor:v1",
            (
                kind.value,
                context_input_audit_component_ownership(kind).value,
                "canonical-json-reference-or-owned-detail:v1",
            ),
        ),
    )
    for kind in ContextInputAuditComponentKind
)
CONTEXT_INPUT_AUDIT_CONTRACT_FINGERPRINT = context_fingerprint(
    "context-input-audit-contract:v1",
    {
        "component_registry": tuple(
            item.value for item in ContextInputAuditComponentKind
        ),
        "component_ownership_registry": tuple(
            item.value for item in ContextInputAuditComponentOwnership
        ),
        "extractor_contracts": CONTEXT_INPUT_AUDIT_EXTRACTOR_CONTRACTS,
        "canonical_json_codec": "pulsara-canonical-json:v1",
        "ordering": "component-registry-ordinal-then-fragment-ordinal:v1",
        "partition": "utf8-safe-plan-scoped-fragments:v1",
        "media_types": (
            "application/vnd.pulsara.context-input-audit-plan+json;version=1",
            "application/vnd.pulsara.context-input-audit-page+json;version=1",
            "application/vnd.pulsara.context-input-audit-root+json;version=1",
        ),
        "limits": {
            "component_references": MAX_AUDIT_COMPONENT_REFERENCES,
            "inline_item_bytes": MAX_AUDIT_INLINE_ITEM_BYTES,
            "total_inline_bytes": MAX_AUDIT_TOTAL_INLINE_BYTES,
            "pages": MAX_AUDIT_PAGES,
            "page_bytes": MAX_AUDIT_PAGE_CANONICAL_BYTES,
            "total_page_bytes": MAX_AUDIT_TOTAL_PAGE_CANONICAL_BYTES,
            "plan_bytes": MAX_AUDIT_PLAN_CANONICAL_BYTES,
            "root_bytes": MAX_AUDIT_ROOT_CANONICAL_BYTES,
        },
    },
)


def build_projection_base_reference(
    base: TranscriptProjectionBaseFact,
) -> RunSeedProjectionBaseReferenceFact | CheckpointProjectionBaseReferenceFact:
    common = base.common
    if isinstance(base, RunSeedProjectionBaseFact):
        return build_frozen_fact(
            RunSeedProjectionBaseReferenceFact,
            schema_version="run_seed_projection_base_reference.v1",
            base_kind="run_seed",
            run_seed_reference=common.run_seed_reference,
            stable_semantic_state_fingerprint=(
                common.stable_semantic_state.state_semantic_fingerprint
            ),
            source_base_fingerprint=base.fact_fingerprint,
        )
    if not isinstance(base, CheckpointProjectionBaseFact):
        raise TypeError("unsupported transcript projection base")
    acceleration = base.checkpoint_acceleration
    return build_frozen_fact(
        CheckpointProjectionBaseReferenceFact,
        schema_version="checkpoint_projection_base_reference.v1",
        base_kind="checkpoint",
        run_seed_reference=common.run_seed_reference,
        checkpoint_id=acceleration.checkpoint_id,
        checkpoint_committed_event_id=(acceleration.checkpoint_committed_event_id),
        checkpoint_root_reference=acceleration.checkpoint_artifact_ref,
        checkpoint_through_sequence=(
            acceleration.checkpoint_candidate_ledger_through_sequence
        ),
        stable_semantic_state_fingerprint=(
            common.stable_semantic_state.state_semantic_fingerprint
        ),
        source_base_fingerprint=base.fact_fingerprint,
    )


def build_context_compile_source_references(
    *,
    snapshot: Any,
    prepared_context_input: Any,
    authority_horizon_set_reference: Any,
) -> ContextCompileSourceReferenceSetFact:
    runtime_session_id = snapshot.identity.runtime_session_id
    primary = tuple(
        item
        for item in prepared_context_input.authority_horizons
        if item.runtime_session_id == runtime_session_id
    )
    if len(primary) != 1:
        raise ValueError("context compile requires one primary ledger horizon")
    continuation = snapshot.continuation
    continuation_reference = (
        continuation.resume_boundary if continuation is not None else None
    )
    return build_frozen_fact(
        ContextCompileSourceReferenceSetFact,
        schema_version="context_compile_authority_reference_set.v1",
        run_start_event_reference=snapshot.run_entry.run_start,
        continuation_event_reference=continuation_reference,
        primary_ledger_horizon=primary[0],
        authority_horizon_set_reference=authority_horizon_set_reference,
        transcript_projection_base_reference=build_projection_base_reference(
            prepared_context_input.transcript_projection_evidence.projection_base
        ),
    )


def build_context_compile_input_commit(
    *,
    snapshot: Any,
    prepared_context_input: Any,
    provider_input_planning_bundle: Any,
    provider_neutral_payload_fingerprint: str,
    input_aggregate_fingerprint: str,
    canonical_render_decisions_fingerprint: str,
    final_budget: Any,
) -> ContextCompileInputCommitFact:
    if final_budget.final_payload_estimated_tokens is None:
        raise ValueError("context compile commit requires final payload estimate")
    target = snapshot.resolved_model_call.target
    source_references = build_context_compile_source_references(
        snapshot=snapshot,
        prepared_context_input=prepared_context_input,
        authority_horizon_set_reference=(
            provider_input_planning_bundle.canonical_plan.authority_horizon_set
        ),
    )
    budget_fingerprint = context_fingerprint(
        "context-compile-budget-decision:v1",
        {
            "resolved_model_target_fingerprint": target.target_fingerprint,
            "token_estimator_fingerprint": target.token_estimator.estimator_fingerprint,
            "input_budget_tokens": final_budget.input_budget_tokens,
            "final_payload_estimated_tokens": (
                final_budget.final_payload_estimated_tokens
            ),
        },
    )
    return build_frozen_fact(
        ContextCompileInputCommitFact,
        schema_version="context_compile_input_commit.v1",
        runtime_session_id=snapshot.identity.runtime_session_id,
        run_id=snapshot.identity.run_id,
        context_id=snapshot.identity.context_id,
        resolved_model_call_id=snapshot.resolved_model_call.resolved_model_call_id,
        resolved_model_target_fingerprint=target.target_fingerprint,
        model_call_index=snapshot.identity.model_call_index,
        compile_attempt_index=snapshot.identity.compile_attempt_index,
        context_retry_index=snapshot.identity.context_retry_index,
        source_through_sequence=snapshot.identity.source_through_sequence,
        source_references=source_references,
        snapshot_semantic_fingerprint=snapshot.snapshot_semantic_fingerprint,
        ordered_projection_identity=(
            provider_input_planning_bundle.prepared_plan.ordered_transcript_projection_identity
        ),
        prepared_provider_input_plan_fingerprint=(
            provider_input_planning_bundle.prepared_plan.plan_fingerprint
        ),
        canonical_provider_input_plan_fingerprint=(
            provider_input_planning_bundle.canonical_plan.plan_fingerprint
        ),
        provider_neutral_payload_fingerprint=provider_neutral_payload_fingerprint,
        input_aggregate_fingerprint=input_aggregate_fingerprint,
        canonical_render_decisions_fingerprint=(canonical_render_decisions_fingerprint),
        token_estimator_fingerprint=target.token_estimator.estimator_fingerprint,
        input_budget_tokens=final_budget.input_budget_tokens,
        final_payload_estimated_tokens=final_budget.final_payload_estimated_tokens,
        budget_decision_fingerprint=budget_fingerprint,
    )


def build_context_input_aggregate_fingerprint(
    *,
    snapshot: Any,
    prepared_context_input: Any,
    prepared_transcript_projection: Any,
    provider_input_planning_bundle: Any,
    active_window: Any,
    window_policy: Any,
    projection_state: Any,
    prepared_rollups: tuple[Any, ...],
    rollout_state: Any,
    context_budget_decision: Any,
    projection_target_unreachable: Any | None,
) -> str:
    """Hash the semantic authorities without serializing an audit manifest."""

    transcript = prepared_transcript_projection.final_normalized_transcript.transcript
    provider_projection = (
        prepared_transcript_projection.provider_projection.projection_fact
    )
    authority = prepared_transcript_projection.authority
    ordered = provider_input_planning_bundle.prepared_plan
    return context_fingerprint(
        "context-compile-input-aggregate:v9",
        (
            snapshot.snapshot_semantic_fingerprint,
            transcript.transcript_fingerprint,
            provider_projection.semantic_identity.semantic_fingerprint,
            authority.provider_semantic_identity.provider_semantic_fingerprint,
            prepared_context_input.prepared_tool_results.render_input_fingerprint,
            prepared_context_input.prepared_candidates.candidate_set_fingerprint,
            ordered.ordered_transcript_projection_identity.identity_fingerprint,
            ordered.plan_fingerprint,
            provider_input_planning_bundle.canonical_plan.plan_fingerprint,
            active_window.window_semantic_fingerprint,
            window_policy.policy_fingerprint,
            projection_state.state_semantic_fingerprint,
            tuple(item.prepared_fingerprint for item in prepared_rollups),
            rollout_state.state_fingerprint,
            context_budget_decision.decision_fingerprint,
            (
                projection_target_unreachable.audit_fingerprint
                if projection_target_unreachable is not None
                else None
            ),
            snapshot.identity.compiler_contract_version,
        ),
    )


def build_long_horizon_context_attribution(
    *,
    run_contract_fingerprint: str,
    active_window: Any,
    projection_state: Any,
    projection_rewrite_event_refs: tuple[Any, ...],
    rollout_account_owner_runtime_session_id: str,
    rollout_state: Any,
    subagent_graph_semantic_source: Any,
    context_budget_decision: Any,
) -> LongHorizonContextAttributionFact:
    payload = {
        "schema_version": "long-horizon-context-attribution:v1",
        "run_contract_fingerprint": run_contract_fingerprint,
        "window_id": active_window.window_id,
        "window_generation": active_window.generation,
        "window_semantic_fingerprint": active_window.window_semantic_fingerprint,
        "projection_generation": projection_state.projection_generation,
        "projection_state_fingerprint": projection_state.state_semantic_fingerprint,
        "projection_rewrite_event_refs": projection_rewrite_event_refs,
        "rollout_account_id": rollout_state.account_id,
        "rollout_account_owner_runtime_session_id": (
            rollout_account_owner_runtime_session_id
        ),
        "rollout_state_through_sequence": rollout_state.through_sequence,
        "rollout_phase": rollout_state.phase,
        "rollout_state_fingerprint": rollout_state.state_fingerprint,
        "subagent_graph_semantic_source": subagent_graph_semantic_source,
        "budget_decision": context_budget_decision,
        "summary_artifact_id": active_window.source_summary_artifact_id,
        "summary_content_sha256": active_window.source_summary_fingerprint,
    }
    return LongHorizonContextAttributionFact(
        **payload,
        attribution_fingerprint=context_fingerprint(
            "long-horizon-context-attribution:v1", payload
        ),
    )


def build_context_input_audit_expectation_for_commit(
    commit: ContextCompileInputCommitFact,
) -> ContextInputAuditExpectationFact:
    return build_context_input_audit_expectation(
        semantic_commit_fingerprint=commit.commit_fingerprint,
        audit_contract_id=CONTEXT_INPUT_AUDIT_CONTRACT_ID,
        audit_contract_version=CONTEXT_INPUT_AUDIT_CONTRACT_VERSION,
        audit_contract_fingerprint=CONTEXT_INPUT_AUDIT_CONTRACT_FINGERPRINT,
    )


def build_provider_input_preparation_install(
    *,
    semantic_commit: ContextCompileInputCommitFact,
    provider_input_start_bundle: Any,
) -> ProviderInputPreparationInstallFact:
    candidate = provider_input_start_bundle.prepared_candidate
    prepared_plan = candidate.prepared_plan
    if candidate.candidate_kind != "compiled_context" or prepared_plan is None:
        raise ValueError("compiled context requires a compiled provider candidate")
    if candidate.semantic_commit_fingerprint != semantic_commit.commit_fingerprint:
        raise ValueError("provider candidate semantic commit drifted")
    if (
        candidate.ordered_projection_identity_fingerprint
        != semantic_commit.ordered_projection_identity.identity_fingerprint
        or prepared_plan.plan_fingerprint
        != semantic_commit.prepared_provider_input_plan_fingerprint
    ):
        raise ValueError("provider candidate plan/projection drifted")
    rollover = candidate.rollover_request
    return build_frozen_fact(
        ProviderInputPreparationInstallFact,
        schema_version="provider_input_preparation_install.v2",
        semantic_commit_fingerprint=semantic_commit.commit_fingerprint,
        preparation_ownership=candidate.preparation_ownership,
        prepared_candidate_fingerprint=candidate.candidate_fingerprint,
        prepared_plan_fingerprint=prepared_plan.plan_fingerprint,
        canonical_provider_input_plan_fingerprint=(
            candidate.provider_input_plan.plan_fingerprint
        ),
        ordered_projection_identity_fingerprint=(
            semantic_commit.ordered_projection_identity.identity_fingerprint
        ),
        generation_commit_guard=candidate.generation_commit_guard,
        rollover_request_fingerprint=(
            rollover.request_fingerprint if rollover is not None else None
        ),
    )


def build_context_compiled_event(**event_fields: Any) -> "ContextCompiledEvent":
    """Build and freeze the sole bounded ContextCompiled write candidate.

    The model validator protects the logical DTO bound.  Freezing through the
    active event-domain registry is the second, physical check: it measures the
    exact bytes that the EventLog writer will receive, including the canonical
    event wrapper and the current schema binding.
    """

    # Local imports keep the primitives/provider-input dependency DAG acyclic.
    from pulsara_agent.event import ContextCompiledEvent
    from pulsara_agent.event_log.serialization import freeze_event_write_candidate

    event = ContextCompiledEvent(**event_fields)
    candidate = freeze_event_write_candidate(event)
    if (
        len(candidate.canonical_payload_bytes)
        > MAX_CONTEXT_COMPILED_EVENT_CANONICAL_BYTES
    ):
        raise ValueError("ContextCompiled candidate exceeds 256 KiB")
    return event


__all__ = [
    "CONTEXT_INPUT_AUDIT_CONTRACT_FINGERPRINT",
    "CONTEXT_INPUT_AUDIT_CONTRACT_ID",
    "CONTEXT_INPUT_AUDIT_CONTRACT_VERSION",
    "CONTEXT_INPUT_AUDIT_EXTRACTOR_CONTRACTS",
    "ContextInputAuditExtractorContract",
    "MAX_CONTEXT_COMPILED_EVENT_CANONICAL_BYTES",
    "build_context_compiled_event",
    "build_context_compile_input_commit",
    "build_context_input_aggregate_fingerprint",
    "build_context_compile_source_references",
    "build_context_input_audit_expectation_for_commit",
    "build_provider_input_preparation_install",
    "build_long_horizon_context_attribution",
    "build_projection_base_reference",
    "context_input_audit_component_ownership",
]
