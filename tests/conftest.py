from __future__ import annotations

import builtins
from functools import lru_cache
from uuid import uuid4

import pytest

from pulsara_agent.primitives.permission import PermissionMode, parse_permission_mode
from pulsara_agent.primitives.capability import (
    build_capability_execution_surface_identity,
    build_capability_resolve_basis,
)
from pulsara_agent.primitives.model_call import ModelTokenUsageFact, sha256_fingerprint
from pulsara_agent.primitives.run_boundary import (
    BoundaryTranscriptSnapshotFact,
    NewRunBoundaryFact,
)
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    CurrentUserMessageFact,
    HostRunBoundaryIdentityFact,
    SubagentRunEntryFact,
    text_sha256,
)
from pulsara_agent.primitives.subagent import (
    ChildExplicitResultEvidenceFact,
    ChildNativeTerminalReferenceFact,
    build_child_result_handoff,
    build_child_result_render_policy,
)
from pulsara_agent.runtime.permission import (
    preset_to_policy,
)
from pulsara_agent.capability.result_semantics import (
    build_unknown_result_semantics,
)
from pulsara_agent.capability.result_contracts import generic_result_render_contract
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    ExternalExecutionRequirementReferenceFact,
    ExternalToolCallRequirementFact,
    ExternalToolResultIngressFact,
    FrozenToolResultBlockFact,
    ToolResultExecutionSemanticsFact,
    ToolResultRenderProfileFact,
    ToolResultRenderVariantCode,
    ToolResultStateFact,
)
from pulsara_agent.primitives.context import (
    CapabilityDescriptorRenderAttributionFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.message import ToolResultBlock, ToolResultState
from tests.support import test_resolved_target_fact
from pulsara_agent.primitives.long_horizon import (
    ChildRolloutSubaccountFact,
    ResolvedChildRolloutBudgetFact,
    RolloutReservationReferenceFact,
)
from pulsara_agent.runtime.long_horizon.run_contract import (
    empty_projection_state_fingerprint,
    prepare_child_long_horizon_run,
    prepare_root_long_horizon_run,
)
from pulsara_agent.event_log.transcript_prefix import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
)
from pulsara_agent.primitives.authority_materialization import (
    TranscriptProjectionStableSemanticStateFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.frozen import StableEventIdentityFact
from pulsara_agent.primitives.terminal_projection import (
    ModelCallTerminalProjectionEndReferenceFact,
    ModelTerminalProjectionSemanticJoinFact,
    TerminalProjectionReferenceFact,
    ToolResultTerminalProjectionEndReferenceFact,
    ToolTerminalProjectionSemanticJoinFact,
)
from pulsara_agent.runtime.authority_materialization import (
    build_default_authority_materialization_contract_bundle,
    build_default_transcript_projection_materialization_contracts,
    prepare_run_transcript_seed,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
)
from pulsara_agent.runtime.terminal_projection import ToolResultEndCandidate


@lru_cache(maxsize=64)
def _empty_run_transcript_seed_fields(
    runtime_session_id: str,
    source_through_sequence: int,
) -> dict[str, object]:
    authority = build_default_authority_materialization_contract_bundle()
    stable = build_frozen_fact(
        TranscriptProjectionStableSemanticStateFact,
        schema_version="transcript_projection_stable_semantic_state.v1",
        semantic_source_event_count=0,
        semantic_source_accumulator=EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
        normalized_transcript_fingerprint=context_fingerprint(
            "normalized-transcript-semantic:v1", ()
        ),
    )
    seed = prepare_run_transcript_seed(
        runtime_session_id=runtime_session_id,
        stable_state=stable,
        stable_entries=(),
        ledger_through_sequence=source_through_sequence,
        ledger_continuity_accumulator=EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            authority.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=build_default_transcript_projection_materialization_contracts(
            authority.limits
        ),
    )
    return {
        "run_transcript_seed_semantic": seed.seed_semantic,
        "run_transcript_seed_reference": seed.seed_reference,
    }


def persist_test_run_transcript_seed(runtime_session, *, run_id: str):
    """Persist and register the production-shaped seed for a test RunStart."""

    from time import monotonic

    from pulsara_agent.runtime.authority_materialization import (
        persist_prepared_run_transcript_seed,
        prepare_authority_artifact_write_reservation,
    )

    projection = runtime_session.transcript_projection_state_store.snapshot()
    prepared = prepare_run_transcript_seed(
        runtime_session_id=runtime_session.runtime_session_id,
        stable_state=projection.stable_semantic_state,
        stable_entries=(
            runtime_session.transcript_projection_state_store.stable_entries()
        ),
        ledger_through_sequence=projection.ledger_through_sequence,
        ledger_continuity_accumulator=projection.ledger_continuity_accumulator,
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            runtime_session.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=runtime_session.transcript_projection_materialization_contracts,
    )
    deadline = monotonic() + 30.0
    persist_prepared_run_transcript_seed(
        prepared,
        write_reservation=prepare_authority_artifact_write_reservation(
            operation_id=f"run-seed:{run_id}",
            owner_kind="run_seed_materialization",
            artifacts=prepared.artifacts,
            limits=runtime_session.authority_materialization_contracts.limits,
            absolute_deadline_monotonic=deadline,
        ),
        limits=runtime_session.authority_materialization_contracts.limits,
        archive=runtime_session.archive,
        runtime_session_id=runtime_session.runtime_session_id,
        deadline_monotonic=deadline,
    )
    runtime_session.transcript_projection_checkpoint_service.prepare_run_seed_artifacts(
        run_id=run_id,
        artifact_ids=frozenset(item.artifact_id for item in prepared.artifacts),
    )
    return prepared


def tool_result_end_contract_fields(
    tool_call_id: str,
    *,
    tool_name: str = "test_tool",
    state: ToolResultState | str = ToolResultState.SUCCESS,
    observed_at_utc: str = "2026-01-01T00:00:00Z",
) -> dict[str, object]:
    parsed_state = (
        state if isinstance(state, ToolResultState) else ToolResultState(state)
    )
    semantics = build_unknown_result_semantics(
        result_state=ToolResultStateFact(parsed_state.value)
    )
    return {
        "observation_timing": ToolObservationTimingFact(
            observed_at_utc=observed_at_utc,
            source_started_at_utc=observed_at_utc,
            source_ended_at_utc=observed_at_utc,
            observation_duration_seconds=0,
            freshness="current_tool_observation",
            clock_source="tool_result_events",
            tool_origin="unknown",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        ),
        "render_profile": semantics.render_profile,
        "essential_capture_policy": semantics.essential_capture_policy,
        "essential_result": semantics.essential_result,
        "terminal_payload_timing": semantics.terminal_payload_timing,
        "rollup_semantics": semantics.rollup_semantics,
        "terminal_projection": tool_terminal_projection_end_reference(
            tool_call_id,
            tool_name=tool_name,
            state=parsed_state,
        ),
    }


def tool_terminal_projection_end_reference(
    tool_call_id: str,
    *,
    tool_name: str,
    state: ToolResultState,
) -> ToolResultTerminalProjectionEndReferenceFact:
    semantic = build_frozen_fact(
        ToolTerminalProjectionSemanticJoinFact,
        schema_version="tool_terminal_projection_semantic_join.v1",
        projection_kind="tool_result",
        tool_call_id=tool_call_id,
        model_tool_name=tool_name,
        result_state=ToolResultStateFact(state.value),
        semantic_fingerprint=context_fingerprint(
            "test-tool-projection-semantic:v1",
            (tool_call_id, tool_name, state.value),
        ),
    )
    reference = build_frozen_fact(
        TerminalProjectionReferenceFact,
        schema_version="terminal_projection_reference.v2",
        projection_kind="tool_result",
        semantic_join=semantic,
        document_fact_fingerprint=context_fingerprint(
            "test-tool-projection-document:v1", (tool_call_id, state.value)
        ),
        document_artifact_id=f"test-terminal-projection:tool:{tool_call_id}",
        document_sha256=context_fingerprint(
            "test-tool-projection-bytes:v1", (tool_call_id, state.value)
        ),
        document_byte_count=1,
        document_contract_fingerprint=context_fingerprint(
            "test-terminal-projection-contract:v1", "tool"
        ),
    )
    committed_identity = build_frozen_fact(
        StableEventIdentityFact,
        schema_version="stable_event_identity.v2",
        runtime_session_id="runtime:test",
        event_id=f"test-tool-projection-committed:{tool_call_id}",
        event_type="TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED",
        event_schema_version="1",
        event_schema_fingerprint=context_fingerprint(
            "test-event-schema:v1", "tool-projection"
        ),
        payload_fingerprint=context_fingerprint(
            "test-event-payload:v1", (tool_call_id, state.value)
        ),
    )
    return build_frozen_fact(
        ToolResultTerminalProjectionEndReferenceFact,
        schema_version="tool_result_terminal_projection_end_ref.v2",
        projection_committed_event_identity=committed_identity,
        projection_reference=reference,
    )


def tool_result_end_candidate(
    *,
    event_id: str,
    run_id: str,
    turn_id: str,
    reply_id: str,
    tool_call_id: str,
    tool_name: str,
    state: ToolResultState = ToolResultState.SUCCESS,
    created_at: str = "2026-01-01T00:00:00Z",
) -> ToolResultEndCandidate:
    fields = tool_result_end_contract_fields(
        tool_call_id,
        tool_name=tool_name,
        state=state,
        observed_at_utc=created_at,
    )
    semantics = ToolResultExecutionSemanticsFact(
        render_profile=fields["render_profile"],
        result_state=ToolResultStateFact(state.value),
        essential_capture_policy=fields["essential_capture_policy"],
        essential_result=fields["essential_result"],
        terminal_payload_timing=fields["terminal_payload_timing"],
        rollup_semantics=fields["rollup_semantics"],
    )
    return ToolResultEndCandidate(
        id=event_id,
        run_id=run_id,
        turn_id=turn_id,
        reply_id=reply_id,
        created_at=created_at,
        metadata={},
        tool_call_id=tool_call_id,
        state=state,
        artifacts=(),
        observation_timing=fields["observation_timing"],
        execution_semantics=semantics,
    )


def external_terminal_projection_references(
    ingresses: tuple[ExternalToolResultIngressFact, ...],
) -> tuple[ToolResultTerminalProjectionEndReferenceFact, ...]:
    return tuple(
        tool_terminal_projection_end_reference(
            item.result_block.tool_call_id,
            tool_name=item.result_block.model_tool_name,
            state=ToolResultState(item.result_block.result_state.value),
        )
        for item in ingresses
    )


def model_terminal_projection_end_reference(
    resolved_model_call_id: str,
    *,
    outcome: str = "completed",
    item_count: int = 0,
) -> ModelCallTerminalProjectionEndReferenceFact:
    semantic = build_frozen_fact(
        ModelTerminalProjectionSemanticJoinFact,
        schema_version="model_terminal_projection_semantic_join.v1",
        projection_kind="model_call",
        terminal_outcome=outcome,
        projection_item_count=item_count,
        semantic_fingerprint=context_fingerprint(
            "test-model-projection-semantic:v1",
            (resolved_model_call_id, outcome, item_count),
        ),
    )
    reference = build_frozen_fact(
        TerminalProjectionReferenceFact,
        schema_version="terminal_projection_reference.v2",
        projection_kind="model_call",
        semantic_join=semantic,
        document_fact_fingerprint=context_fingerprint(
            "test-model-projection-document:v1",
            (resolved_model_call_id, outcome, item_count),
        ),
        document_artifact_id=(
            f"test-terminal-projection:model:{resolved_model_call_id}"
        ),
        document_sha256=context_fingerprint(
            "test-model-projection-bytes:v1",
            (resolved_model_call_id, outcome, item_count),
        ),
        document_byte_count=1,
        document_contract_fingerprint=context_fingerprint(
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
        event_schema_fingerprint=context_fingerprint(
            "test-event-schema:v1", "model-projection"
        ),
        payload_fingerprint=context_fingerprint(
            "test-event-payload:v1", (resolved_model_call_id, outcome, item_count)
        ),
    )
    return build_frozen_fact(
        ModelCallTerminalProjectionEndReferenceFact,
        schema_version="model_call_terminal_projection_end_ref.v2",
        projection_committed_event_identity=committed_identity,
        projection_reference=reference,
    )


def external_tool_call_requirement_fact(
    tool_call_id: str,
    *,
    tool_name: str,
    raw_arguments_json: str = "{}",
) -> ExternalToolCallRequirementFact:
    contract = generic_result_render_contract()
    attribution_payload = {
        "owner_runtime_session_id": "runtime:test",
        "exposure_id": "capability-exposure:test",
        "exposure_fact_fingerprint": "exposure-fact:test",
        "descriptor_set_fingerprint": "descriptor-set:test",
        "descriptor_id": f"descriptor:test:{tool_name}",
        "descriptor_fingerprint": f"descriptor-fingerprint:test:{tool_name}",
        "result_render_contract_fingerprint": contract.contract_fingerprint,
        "descriptor_source_event_id": "capability-exposure-event:test",
        "descriptor_source_sequence": 1,
        "descriptor_source_payload_fingerprint": "sha256:" + "1" * 64,
    }
    attribution = CapabilityDescriptorRenderAttributionFact(
        **attribution_payload,
        attribution_fingerprint=context_fingerprint(
            "capability-descriptor-render-attribution:v1", attribution_payload
        ),
    )
    payload = {
        "tool_call_id": tool_call_id,
        "model_tool_name": tool_name,
        "raw_arguments_json": raw_arguments_json,
        "tool_origin": "custom",
        "descriptor_attribution": attribution,
        "result_render_contract": contract,
        "essential_capture_policy": None,
    }
    return ExternalToolCallRequirementFact(
        **payload,
        requirement_fingerprint=context_fingerprint(
            "external-tool-call-requirement:v1", payload
        ),
    )


def external_tool_result_ingress_fact(
    result: ToolResultBlock,
    *,
    requirement: ExternalToolCallRequirementFact | None = None,
    require_event_id: str = "require-external:test",
    require_event_sequence: int = 1,
) -> ExternalToolResultIngressFact:
    requirement = requirement or external_tool_call_requirement_fact(
        result.id, tool_name=result.name
    )
    block_payload = freeze_json(result.model_dump(mode="json"))
    assert hasattr(block_payload, "entries")
    state = ToolResultStateFact(result.state.value)
    frozen_block = FrozenToolResultBlockFact(
        tool_call_id=result.id,
        model_tool_name=result.name,
        result_state=state,
        canonical_block_payload=block_payload,
        block_payload_fingerprint=context_fingerprint(
            "tool-result-block:v1", block_payload
        ),
    )
    timing = ToolObservationTimingFact(
        observed_at_utc="2026-07-09T00:00:00Z",
        source_started_at_utc="2026-07-09T00:00:00Z",
        source_ended_at_utc="2026-07-09T00:00:00Z",
        observation_duration_seconds=0,
        freshness="current_tool_observation",
        clock_source="tool_runtime_metadata",
        tool_origin="unknown",
        tool_name=result.name,
        tool_call_id=result.id,
    )
    variant = next(
        item
        for item in requirement.result_render_contract.allowed_variants
        if item.variant_code is ToolResultRenderVariantCode.EXTERNAL_GENERIC_RESULT
    )
    profile_payload = {
        "profile_version": "tool-result-profile:v1",
        "selected_variant": variant,
        "render_contract": requirement.result_render_contract,
        "tool_origin": "unknown",
        "descriptor_attribution": requirement.descriptor_attribution,
        "render_contract_fingerprint": (
            requirement.result_render_contract.contract_fingerprint
        ),
    }
    profile = ToolResultRenderProfileFact(
        **profile_payload,
        profile_fingerprint=context_fingerprint(
            "tool-result-render-profile:v1", profile_payload
        ),
    )
    semantics = ToolResultExecutionSemanticsFact(
        render_profile=profile,
        result_state=state,
        essential_capture_policy=None,
        essential_result=None,
        terminal_payload_timing=None,
        rollup_semantics=None,
    )
    reference = ExternalExecutionRequirementReferenceFact(
        owner_runtime_session_id="runtime:test",
        require_event_id=require_event_id,
        require_event_sequence=require_event_sequence,
        require_event_payload_fingerprint="sha256:" + "2" * 64,
        tool_call_id=result.id,
        requirement_fingerprint=requirement.requirement_fingerprint,
    )
    payload = {
        "requirement_ref": reference,
        "result_block": frozen_block,
        "observation_timing": timing,
        "execution_semantics": semantics,
    }
    return ExternalToolResultIngressFact(
        **payload,
        ingress_fingerprint=context_fingerprint(
            "external-tool-result-ingress:v1", payload
        ),
    )


def run_start_permission_fields(
    run_id: str,
    *,
    mode: str | PermissionMode = PermissionMode.BYPASS_PERMISSIONS,
    source: str = "session_default",
    user_input: str = "",
    turn_id: str | None = None,
    reply_id: str | None = None,
    mcp_installation_id: str = "mcp_installation:empty",
    mcp_installation_owner_runtime_session_id: str = "runtime:test",
    model_target=None,
    transcript_source_through_sequence: int = 0,
    transcript_source_event_count: int = 0,
) -> dict[str, object]:
    parsed = parse_permission_mode(mode)
    permission_snapshot_id = f"permission_snapshot:{run_id}"
    target = model_target or test_resolved_target_fact()
    from pulsara_agent.runtime.long_horizon.reducer_contract import (
        build_default_subagent_graph_reducer_contract,
    )

    runtime_session_id = mcp_installation_owner_runtime_session_id
    observed_at = "1970-01-01T00:00:00.000000Z"
    resolved_turn_id = turn_id or run_id.replace("run:", "turn:", 1)
    resolved_reply_id = reply_id or run_id.replace("run:", "reply:", 1)
    current_user = CurrentUserMessageFact(
        message_id=f"user-message:{run_id}",
        source_kind=(
            "subagent_task" if source == "child_profile" else "host_user_input"
        ),
        text=user_input,
        observed_at_utc=observed_at,
        content_sha256=text_sha256(user_input),
        source_artifact_id=(
            f"artifact:task:{run_id}" if source == "child_profile" else None
        ),
    )
    graph_contract = build_default_subagent_graph_reducer_contract()
    root_long_horizon = prepare_root_long_horizon_run(
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        run_start_event_id=f"run_start:test:{run_id}",
        primary_target=target,
        summarizer_target=target,
        graph_reducer_contract=graph_contract,
        source_through_sequence_at_open=transcript_source_through_sequence,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    common = {
        "permission_snapshot_id": permission_snapshot_id,
        "permission_mode": parsed.value,
        "permission_policy": preset_to_policy(parsed).to_dict(),
        "permission_snapshot_source": source,
        "model_target": target,
        "subagent_graph_reducer_contract": graph_contract,
        "mcp_installation_id": mcp_installation_id,
        "mcp_installation_owner_runtime_session_id": runtime_session_id,
        "current_user_message": current_user,
        **_empty_run_transcript_seed_fields(
            runtime_session_id,
            transcript_source_through_sequence,
        ),
        "terminal_run_end_event_id": test_run_end_event_id(run_id),
    }
    if source == "child_profile":
        reservation_ref = RolloutReservationReferenceFact(
            owner_runtime_session_id=runtime_session_id,
            reservation_id=f"reservation:test:{run_id}",
            reservation_event_id=f"reservation_event:test:{run_id}",
            reservation_sequence=1,
            reservation_fingerprint=f"sha256:test-reservation:{run_id}",
        )
        budget_payload = {
            "child_profile": "test",
            "child_primary_target_fingerprint": target.target_fingerprint,
            "child_summarizer_target_fingerprint": target.target_fingerprint,
            "child_window_policy_fingerprint": (
                root_long_horizon.contract.window_policy.policy_fingerprint
            ),
            "child_policy_fingerprint": (
                root_long_horizon.contract.child_rollout_policy.policy_fingerprint
            ),
            "child_primary_reservation_quote_semantic_fingerprint": "sha256:test-primary-quote",
            "child_compaction_reservation_quote_semantic_fingerprint": "sha256:test-compaction-quote",
            "one_agent_call_reserve_milliunits": 1,
            "one_compaction_call_reserve_milliunits": 1,
            "tool_reserve_milliunits": 1,
            "profile_limit_milliunits": 3,
            "parent_share_limit_milliunits": 3,
            "max_rollout_milliunits_per_child": 3,
            "parent_account_state_fingerprint": "sha256:test-parent-account",
        }
        resolved_budget = ResolvedChildRolloutBudgetFact(
            **budget_payload,
            resolution_fingerprint=context_fingerprint(
                "resolved-child-rollout-budget:v1", budget_payload
            ),
        )
        child_long_horizon = prepare_child_long_horizon_run(
            child_runtime_session_id=runtime_session_id,
            child_run_id=run_id,
            run_start_event_id=f"run_start:test:{run_id}",
            primary_target=target,
            summarizer_target=target,
            graph_reducer_contract=graph_contract,
            account_id=f"rollout_account:test:parent:{run_id}",
            account_owner_runtime_session_id=runtime_session_id,
            account_owner_run_id=f"parent:{run_id}",
            inherited_rollout_reservation=reservation_ref,
        )
        subaccount_payload = {
            "root_account_id": child_long_horizon.contract.rollout_account_id,
            "parent_reservation": reservation_ref,
            "child_runtime_session_id": runtime_session_id,
            "child_run_id": run_id,
            "resolved_budget": resolved_budget,
            "reserved_milliunits": 3,
        }
        child_subaccount = ChildRolloutSubaccountFact(
            **subaccount_payload,
            subaccount_fingerprint=context_fingerprint(
                "child-rollout-subaccount:v1", subaccount_payload
            ),
        )
        return {
            **common,
            "long_horizon": child_long_horizon.contract,
            "child_rollout_subaccount": child_subaccount,
            "run_entry_kind": "subagent_child",
            "host_run_ingress": None,
            "host_ingress_admission_proof": None,
            "new_run_boundary": None,
            "subagent_run_entry": SubagentRunEntryFact(
                subagent_run_id=run_id,
                subagent_task_id=f"task:{run_id}",
                parent_runtime_session_id=runtime_session_id,
                parent_run_id=f"parent:{run_id}",
                spawn_edge_id=f"edge:{run_id}",
                capability_profile_fingerprint="sha256:test-profile",
                task_artifact_id=f"artifact:task:{run_id}",
                task_observed_at_utc=observed_at,
                child_result_render_policy=build_child_result_render_policy(
                    renderer_version="test:v1",
                    max_summary_chars=4_000,
                    max_artifact_refs=32,
                ),
                permission_snapshot_id=permission_snapshot_id,
                model_target_fingerprint=target.target_fingerprint,
                mcp_installation_id=mcp_installation_id,
                mcp_installation_owner_runtime_session_id=runtime_session_id,
            ),
        }

    identity = HostRunBoundaryIdentityFact(
        boundary_id=f"run_boundary:test:{uuid4().hex}",
        kind="pre_run",
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        turn_id=resolved_turn_id,
        reply_id=resolved_reply_id,
        attempt_number=1,
        observed_at_utc=observed_at,
    )
    surface = build_capability_execution_surface_identity(
        surface_contract_version="test:v1",
        entries=(),
        mcp_installation_id=mcp_installation_id,
    )
    basis = build_capability_resolve_basis(
        basis_id=f"capability_basis:test:{uuid4().hex}",
        basis_kind="initial",
        source_basis_id=None,
        source_basis_fingerprint=None,
        owner=CapabilityExposureOwnerFact(
            owner_kind="host_boundary",
            owner_id=identity.boundary_id,
            host_boundary_kind="pre_run",
            runtime_session_id=runtime_session_id,
            run_id=run_id,
        ),
        workspace_identity_fingerprint="sha256:test-workspace",
        memory_domain_id="memory_domain:test",
        permission_snapshot_id=permission_snapshot_id,
        plan_active=False,
        active_skill_names=(),
        user_intent_fingerprint=sha256_fingerprint("test-user-intent:v1", user_input),
        prior_transcript_fingerprint="sha256:test-prior-transcript",
        mcp_installation_id=mcp_installation_id,
        execution_surface_identity=surface,
    )
    from pulsara_agent.llm.user_carrier import encode_human_input
    from pulsara_agent.primitives.frozen import build_frozen_fact
    from pulsara_agent.primitives.host_ingress import (
        HostIngressAdmissionProofFact,
        HostIngressItemPlacementFact,
        HostRunIngressAttributionFact,
        HostRunIngressSemanticFact,
        HumanRunIngressFact,
    )

    ingress_id = f"host_ingress:test:{run_id}"
    human = encode_human_input(
        user_input,
        causal_occurrence_semantic_fingerprint=context_fingerprint(
            "test-host-ingress-occurrence:v1", (runtime_session_id, run_id)
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
        host_session_id=f"host:test:{runtime_session_id}",
        conversation_id=f"conversation:test:{runtime_session_id}",
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
    admission = build_frozen_fact(
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
            "test-host-ingress-permission:v1", permission_snapshot_id
        ),
        expected_close_intent_revision=0,
        expected_autonomy_chain_state_fingerprint=None,
        proposed_automatic_delivery_ordinal=None,
    )
    return {
        **common,
        "long_horizon": root_long_horizon.contract,
        "child_rollout_subaccount": None,
        "run_entry_kind": "host",
        "host_run_ingress": host_ingress,
        "host_ingress_admission_proof": admission,
        "new_run_boundary": NewRunBoundaryFact(
            identity=identity,
            transcript=BoundaryTranscriptSnapshotFact(
                source_through_sequence=transcript_source_through_sequence,
                source_event_count=transcript_source_event_count,
                compacted_window_id=None,
                checkpoint_compaction_id=None,
                checkpoint_terminal_event_id=None,
                checkpoint_terminal_sequence=None,
                checkpoint_keep_after_sequence=None,
                preflight_compaction_id=None,
                preflight_compaction_terminal_event_id=None,
                preflight_compaction_terminal_sequence=None,
            ),
            model_target_fingerprint=target.target_fingerprint,
            permission_snapshot_id=permission_snapshot_id,
            mcp_installation_id=mcp_installation_id,
            capability_basis=basis,
            degraded_reason_codes=(),
        ),
        "subagent_run_entry": None,
    }


def open_test_root_rollout_run(
    runtime_session,
    *,
    event_context,
    model_target,
    user_input: str = "",
) -> None:
    """Commit the production-shaped root run facts used by main-call tests."""

    from pulsara_agent.event import (
        ContextWindowOpenedEvent,
        RolloutBudgetAccountOpenedEvent,
        RunStartEvent,
    )
    from pulsara_agent.runtime.long_horizon.reducer_contract import (
        build_default_subagent_graph_reducer_contract,
    )
    from pulsara_agent.runtime.long_horizon.run_contract import (
        empty_projection_state_fingerprint,
        prepare_root_long_horizon_run,
    )

    if any(
        isinstance(event, RunStartEvent)
        for event in runtime_session.event_log.iter(run_id=event_context.run_id)
    ):
        return
    run_start_event_id = f"run_start:test:{event_context.run_id}"
    prepared = prepare_root_long_horizon_run(
        runtime_session_id=runtime_session.runtime_session_id,
        run_id=event_context.run_id,
        run_start_event_id=run_start_event_id,
        primary_target=model_target,
        summarizer_target=model_target,
        graph_reducer_contract=build_default_subagent_graph_reducer_contract(),
        source_through_sequence_at_open=0,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    prepared_seed = persist_test_run_transcript_seed(
        runtime_session,
        run_id=event_context.run_id,
    )
    fields = run_start_permission_fields(
        event_context.run_id,
        user_input=user_input,
        turn_id=event_context.turn_id,
        reply_id=event_context.reply_id,
        mcp_installation_owner_runtime_session_id=(runtime_session.runtime_session_id),
        model_target=model_target,
    )
    fields.update(
        long_horizon=prepared.contract,
        run_transcript_seed_semantic=prepared_seed.seed_semantic,
        run_transcript_seed_reference=prepared_seed.seed_reference,
    )
    run_start = RunStartEvent(
        id=run_start_event_id,
        **event_context.event_fields(),
        **fields,
        user_input_chars=len(user_input),
    )
    window_open = ContextWindowOpenedEvent(
        id=prepared.contract.initial_window_open_event_id,
        **event_context.event_fields(),
        window=prepared.initial_window,
        opening_batch_id=prepared.opening_batch_id,
    )
    account = prepared.root_account
    assert account is not None
    account_open = RolloutBudgetAccountOpenedEvent(
        id=f"rollout_budget_account_opened:{account.account_id}",
        **event_context.event_fields(),
        account=account,
    )
    runtime_session.publisher.bind_running_loop()
    result = runtime_session.write_events_from_thread(
        (run_start, window_open, account_open)
    )
    result.require_reduced(f"long_horizon:{runtime_session.runtime_session_id}")
    stored_run_start = next(
        event for event in result.committed_events if isinstance(event, RunStartEvent)
    )
    runtime_session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
        stored_run_start
    )


async def emit_test_accepted_model_reply(
    runtime_session,
    *,
    event_context,
    assistant_text: str,
    user_input: str = "timeline test",
) -> None:
    """Emit one production-shaped accepted assistant reply for integration tests."""

    from pulsara_agent.event import (
        ContextWindowOpenedEvent,
        RolloutBudgetAccountOpenedEvent,
        RunStartEvent,
    )
    from pulsara_agent.llm import LLMRuntime, ModelRole
    from pulsara_agent.llm.adapters.mock import MockTransport
    from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
    from pulsara_agent.llm.control import RunModelCallControlOwner
    from pulsara_agent.llm.input import LLMMessage
    from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
    from pulsara_agent.llm.registry import LLMTransportRegistry
    from pulsara_agent.primitives.model_call import ModelCallPurpose
    from pulsara_agent.runtime.state import LoopState
    from tests.support import (
        bind_test_provider_input_context,
        bind_test_context,
        make_test_run_execution_activation,
        test_llm_config,
        test_llm_context,
    )

    registry = LLMTransportRegistry()
    registry.register(MockTransport(text=assistant_text))
    llm_runtime = LLMRuntime(
        config=test_llm_config(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="mock",
        ),
        registry=registry,
    )
    runtime_session.event_log.ensure_runtime_session_owner()
    target = llm_runtime.resolve_target(role=ModelRole.FLASH)
    run_start_id = f"run_start:test:{event_context.run_id}"
    fields = run_start_permission_fields(
        event_context.run_id,
        user_input=user_input,
        turn_id=event_context.turn_id,
        reply_id=event_context.reply_id,
        mcp_installation_owner_runtime_session_id=runtime_session.runtime_session_id,
        model_target=target.fact,
    )
    long_horizon = prepare_root_long_horizon_run(
        runtime_session_id=runtime_session.runtime_session_id,
        run_id=event_context.run_id,
        run_start_event_id=run_start_id,
        primary_target=target.fact,
        summarizer_target=target.fact,
        graph_reducer_contract=fields["subagent_graph_reducer_contract"],
        source_through_sequence_at_open=0,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    seed = persist_test_run_transcript_seed(
        runtime_session,
        run_id=event_context.run_id,
    )
    fields.update(
        long_horizon=long_horizon.contract,
        run_transcript_seed_semantic=seed.seed_semantic,
        run_transcript_seed_reference=seed.seed_reference,
    )
    committed = await runtime_session.write_events(
        (
            RunStartEvent(
                id=run_start_id,
                **event_context.event_fields(),
                **fields,
                user_input_chars=len(user_input),
            ),
            ContextWindowOpenedEvent(
                id=long_horizon.contract.initial_window_open_event_id,
                **event_context.event_fields(),
                window=long_horizon.initial_window,
                opening_batch_id=long_horizon.opening_batch_id,
            ),
            RolloutBudgetAccountOpenedEvent(
                **event_context.event_fields(),
                account=long_horizon.root_account,
            ),
        )
    )
    stored_start = next(
        event
        for event in committed.committed_events
        if isinstance(event, RunStartEvent)
    )
    runtime_session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
        stored_start
    )

    call = llm_runtime.resolve_call(
        target=target,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    context = bind_test_context(
        call,
        test_llm_context(
            messages=(LLMMessage.user(user_input),),
            context_id=f"context:{event_context.run_id}",
            model_call_index=1,
        ),
    )
    provider_input = await (
        runtime_session.provider_input_generation_coordinator.prepare_one_shot_call(
            call=call,
            context=context,
            event_context=event_context,
            operation_kind="direct_model_call",
            operation_id=call.fact.resolved_model_call_id,
        )
    )
    context = bind_test_provider_input_context(call, provider_input, context)
    activation = make_test_run_execution_activation()
    start_bundle = prepare_model_lifecycle_start_bundle(
        call=call,
        context=context,
        event_context=event_context,
        runtime_session=runtime_session,
        lifecycle_kind="main_assistant_reply",
        run_execution_activation=activation,
        provider_input_start_bundle=provider_input,
    )
    state = LoopState(
        session_id=runtime_session.runtime_session_id,
        run_id=event_context.run_id,
        turn_id=event_context.turn_id,
        reply_id=event_context.reply_id,
    )
    handle = llm_runtime.start_stream(
        call=call,
        context=context,
        event_context=event_context,
        start_bundle=start_bundle,
        commit_port=RuntimeSessionModelStreamEventCommitPort(
            runtime_session=runtime_session,
            state=state,
        ),
        execution_registry=runtime_session.model_stream_execution_registry,
    )
    result = await handle.wait_result()
    owner = RunModelCallControlOwner(
        run_id=event_context.run_id,
        activation=activation,
        segment_id=f"segment:{event_context.run_id}:1",
        segment_generation=activation.segment_generation,
    )
    await owner.resolve_completed_call(
        result=result,
        model_call_index=1,
        event_context=event_context,
        runtime_session=runtime_session,
        state=state,
    )


def test_run_end_event_id(run_id: str) -> str:
    return "run_end:test:" + sha256_fingerprint(
        "test-run-end-id:v1", run_id
    ).removeprefix("sha256:")


def run_end_contract_fields(
    run_id: str,
    *,
    status: str,
    abort_kind: str | None = None,
    recovered: bool = False,
    error_message: str | None = None,
) -> dict[str, object]:
    if status == "finished":
        terminalization_kind = "normal"
        error_message = None
    elif status == "aborted":
        terminalization_kind = (
            "recovered_interrupted"
            if recovered
            else "host_teardown"
            if abort_kind == "host_teardown"
            else "user_stop"
        )
        error_message = None
    elif status == "failed":
        terminalization_kind = "execution_failure"
        error_message = error_message or "synthetic test execution failure"
    else:
        raise ValueError(f"unsupported test RunEnd status: {status}")
    return {
        "id": test_run_end_event_id(run_id),
        "terminalization_kind": terminalization_kind,
        "error_message": error_message,
    }


def subagent_result_handoff_fields(
    *,
    subagent_run_id: str,
    child_runtime_session_id: str,
    child_run_id: str,
    result_id: str,
    summary: str,
    result_artifact_id: str,
    artifact_ids: tuple[str, ...] | list[str],
    result_source: str = "inferred",
    tool_call_count: int = 0,
    token_usage: ModelTokenUsageFact | None = None,
) -> dict[str, object]:
    policy = build_child_result_render_policy(
        renderer_version="test:v1",
        max_summary_chars=4_000,
        max_artifact_refs=32,
    )
    terminal = ChildNativeTerminalReferenceFact(
        child_runtime_session_id=child_runtime_session_id,
        child_run_id=child_run_id,
        terminal_event_id=f"run_end:child:{subagent_run_id}",
        terminal_sequence=4,
        terminal_status="finished",
        terminalization_kind="normal",
        stop_reason="final",
    )
    evidence = (
        ChildExplicitResultEvidenceFact(
            source_result_submitted_event_id=f"submitted:{result_id}",
            source_result_submitted_event_sequence=1,
            child_runtime_session_id=child_runtime_session_id,
            child_run_id=child_run_id,
            source_tool_call_id="call:report-result",
            tool_call_start_event_id="event:tool-call-start",
            tool_call_start_sequence=1,
            tool_result_end_event_id="event:tool-result-end",
            tool_result_end_sequence=3,
        )
        if result_source == "explicit"
        else None
    )
    return {
        "result_handoff": build_child_result_handoff(
            handoff_kind=result_source,  # type: ignore[arg-type]
            policy=policy,
            child_terminal_reference=terminal,
            explicit_evidence=evidence,
            result_id=result_id,
            summary=summary,
            result_artifact_id=result_artifact_id,
            artifact_ids=tuple(artifact_ids),
            token_usage=token_usage,
            usage_status="complete" if token_usage is not None else "missing",
            tool_call_count=tool_call_count,
        )
    }


builtins.run_start_permission_fields = run_start_permission_fields
builtins.run_end_contract_fields = run_end_contract_fields
builtins.subagent_result_handoff_fields = subagent_result_handoff_fields


@pytest.fixture(autouse=True)
def _isolate_user_mcp_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary tests hermetic from ~/.pulsara/mcp.yaml.

    HostCore and HostSession intentionally load user-level MCP servers in
    production.  Unit tests should not inherit the developer's personal MCP
    config: a remote user MCP can make tests slow, flaky, or timing-dependent.
    MCP-specific tests can still override these patched symbols explicitly with
    their own monkeypatches.
    """

    def _empty_configs(*, workspace_root):
        return ()

    monkeypatch.setattr(
        "pulsara_agent.host.core.load_mcp_server_configs", _empty_configs
    )
    monkeypatch.setattr(
        "pulsara_agent.host.session.load_mcp_server_configs", _empty_configs
    )
