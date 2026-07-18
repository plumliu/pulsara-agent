from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from pulsara_agent.event import (
    CapabilityGateDecisionEvent,
    ContextProjectionRewritePageEvent,
    ContextWindowClosedEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionFailedEvent,
    ContextWindowOpenedEvent,
    CustomEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallControlDispositionResolvedEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RunEndEvent,
    RunStartEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
    PhysicalOperationReservationSettledEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.llm.drafts import (
    ProviderErrorDraft,
    SanitizedProviderSemanticEnvelope,
    build_semantic_draft,
)
from pulsara_agent.llm.accounting import build_model_reservation_settlement_event
from pulsara_agent.llm.input import LLMMessage, MessageRole
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.runtime import LLMRuntime
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.adapters.mock import MockTransport
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.sanitizing_transport import sanitize_provider_failure
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    TranscriptCompileInput,
    TranscriptMessageFact,
    TranscriptProjectionWindowFact,
    TranscriptTextBlockFact,
    TranscriptToolCallFact,
    TranscriptToolResultRefFact,
    ToolInteractionPairFact,
    canonical_utc_timestamp,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.long_horizon import (
    ContextWindowCloseReason,
    ContextWindowFact,
    ContextWindowOpenReason,
    ContextWindowTranscriptBasisFact,
    ModelCallReservationQuoteFact,
    RolloutBudgetAccountFact,
    RolloutBudgetBucket,
    RolloutBudgetStateFact,
    RolloutPhase,
    RolloutReservationFact,
    RolloutTransitionReason,
    RolloutUsageChargeFact,
    RolloutStatusHintPolicyFact,
    ToolObservationProjectionFact,
    ToolObservationProjectionRewriteEntryFact,
    ToolObservationProtectionFact,
    ToolObservationRepresentation,
    calculate_model_call_reservation,
    default_long_horizon_context_policy,
    default_rollout_budget_policy,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    ToolResultContentFact,
    ToolResultRenderUnit,
    ToolResultRollupSemanticsFact,
    ToolResultStateFact,
    ToolResultTextContentFact,
)
from pulsara_agent.primitives.model_call import (
    DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT,
    ModelCallPurpose,
    ModelTokenUsageFact,
    ResolvedModelTargetFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.authority_materialization import PhysicalOperationKind
from pulsara_agent.llm.recovery import ModelStreamRecoveryService
from pulsara_agent.llm.segment import ModelStreamSegmentAccumulator
from pulsara_agent.llm.terminal_projection import (
    ModelTerminalProjectionReducer,
    build_default_terminal_projection_contract_bundle,
)
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.llm.control_recovery import (
    ModelCallControlDispositionRecoveryService,
    ModelCallControlRecoveryStructuralError,
)
from pulsara_agent.primitives.model_call import ModelCallControlDisposition
from pulsara_agent.primitives.run_lifecycle import (
    RunStopReason,
    RunTerminalizationKind,
)
from pulsara_agent.runtime.long_horizon.rollout import (
    RolloutReducerContractError,
    fold_rollout_budget,
    initial_rollout_budget_state,
    rollout_state_with_phase,
)
from pulsara_agent.runtime.long_horizon.coordinator import (
    phase_for_settled_exploration,
    plan_root_model_admission,
    plan_root_tool_admission,
    rollout_bucket_remaining,
)
from pulsara_agent.runtime.long_horizon.status import (
    derive_rollout_status_shadow,
)
from pulsara_agent.runtime.long_horizon.store import LongHorizonStateStore
from pulsara_agent.runtime.authority_materialization import (
    LedgerMaterializationAccountStore,
    LedgerMaterializationCoordinator,
    build_default_authority_materialization_contract_bundle,
)
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventAuthorityView,
    ContextEventSlice,
    FrozenStoredEvent,
    event_reference_from_stored,
)
from pulsara_agent.runtime.context_input.live import _read_live_primary_event_slice
from pulsara_agent.runtime.tool_action import (
    ToolActionClassifierRegistry,
    default_tool_action_classifier_registry,
    fixed_tool_action_policy,
)
from pulsara_agent.runtime.tool_execution import ToolExecutionTerminalDrainBlocked
from pulsara_agent.primitives.long_horizon import LongHorizonActionClass
from pulsara_agent.message import ToolResultState
from pulsara_agent.tools import ToolCall
from tests.conftest import tool_result_end_contract_fields
from pulsara_agent.runtime.long_horizon.projection_reducer import (
    ContextProjectionReducerError,
    ContextWindowProjectionReducer,
    build_projection_state,
)
from pulsara_agent.runtime.long_horizon.projection import (
    LongHorizonPreparationBoundExceeded,
    ProjectionTargetUnreachable,
    advance_compile_attempt_index,
    advance_safe_point_revision,
    plan_deterministic_projection_rewrite,
    plan_new_result_ingest,
)
from pulsara_agent.runtime.long_horizon.rollup import (
    InMemoryPreparedObservationRollupCache,
    default_observation_rollup_renderer_registry,
    materialize_observation_rollup,
    prepared_observation_rollup_cache_key,
    prepare_observation_rollup_artifact,
)
from pulsara_agent.runtime.context_input.policy import resolve_context_compile_policy
from pulsara_agent.runtime.context_input.render import (
    apply_tool_observation_projection,
    prepare_tool_result_render_input,
    render_prepared_tool_result_units,
)
from pulsara_agent.runtime.context_input.compiler import lower_transcript_for_context
from pulsara_agent.runtime.state import LoopBudget
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.capability.result_semantics import build_unknown_result_semantics
from pulsara_agent.runtime.long_horizon.context_budget import (
    measure_long_horizon_context_budget,
)
from pulsara_agent.runtime.long_horizon.window import (
    ContextWindowChainState,
    fold_context_window_chain,
)
from pulsara_agent.runtime.long_horizon.window_compaction import (
    build_compacted_context_window,
    build_window_compaction_plan,
    parse_window_compaction_summary,
    prepare_window_compaction_source_document,
    window_compaction_identity,
)
from pulsara_agent.runtime.long_horizon.window_compaction_service import (
    ContextWindowCompactionService,
    PendingWindowCompactionError,
    WindowCompactionRequest,
)
from pulsara_agent.inspector.service import _context_window_projection
from tests.support.model_call import (
    model_call_start_fields,
    model_call_end_fields,
    model_terminal_projection_end_reference_fixture,
    test_resolved_call,
    test_resolved_target_fact,
    test_llm_config,
)
from tests.support.runtime_session import in_memory_runtime_session
from tests.conftest import open_test_root_rollout_run


CTX = EventContext(
    run_id="run:long-horizon",
    turn_id="turn:long-horizon",
    reply_id="reply:long-horizon",
)


def test_safe_point_revision_and_compile_attempt_index_have_independent_bounds() -> None:
    policy = default_long_horizon_context_policy(input_budget_tokens=64_000)
    compile_attempt_index = 0
    safe_point_revision = 0

    for _ in range(policy.max_compile_attempts_per_model_call):
        compile_attempt_index = advance_compile_attempt_index(
            compile_attempt_index,
            policy=policy,
        )
    safe_point_revision = advance_safe_point_revision(
        safe_point_revision,
        policy=policy,
    )
    assert safe_point_revision == 1
    with pytest.raises(LongHorizonPreparationBoundExceeded) as compile_exhausted:
        advance_compile_attempt_index(compile_attempt_index, policy=policy)
    assert (
        compile_exhausted.value.reason_code.value
        == "context_compile_attempts_exhausted"
    )

    compile_attempt_index = 1
    safe_point_revision = 0
    for _ in range(policy.max_safe_point_revisions):
        safe_point_revision = advance_safe_point_revision(
            safe_point_revision,
            policy=policy,
        )
    compile_attempt_index = advance_compile_attempt_index(
        compile_attempt_index,
        policy=policy,
    )
    assert compile_attempt_index == 2
    with pytest.raises(LongHorizonPreparationBoundExceeded) as revision_exhausted:
        advance_safe_point_revision(safe_point_revision, policy=policy)
    assert (
        revision_exhausted.value.reason_code.value
        == "long_horizon_preparation_cycle_exceeded"
    )


def _basis() -> ContextWindowTranscriptBasisFact:
    payload = {
        "basis_kind": "initial_run",
        "run_start_event_id": "run-start:long-horizon",
        "source_compaction_started_event_id": None,
        "source_compaction_plan_fingerprint": None,
        "source_through_sequence_at_compaction": None,
        "summarized_pair_groups_fingerprint": None,
        "retained_pair_groups_fingerprint": None,
    }
    return ContextWindowTranscriptBasisFact(
        **payload,
        basis_fingerprint=context_fingerprint(
            "context-window-transcript-basis:v1", payload
        ),
    )


def _window(
    *,
    window_id: str = "window:one",
    generation: int = 1,
    previous_window_id: str | None = None,
    stable_close_event_id: str = "window-close:one",
    target: ResolvedModelTargetFact | None = None,
) -> ContextWindowFact:
    target = target or test_resolved_target_fact()
    policy = default_long_horizon_context_policy(
        input_budget_tokens=target.context_budget.input_budget_tokens
    )
    payload = {
        "contract_version": "context-window:v1",
        "window_id": window_id,
        "run_id": CTX.run_id,
        "generation": generation,
        "previous_window_id": previous_window_id,
        "open_reason": ContextWindowOpenReason.INITIAL_RUN,
        "transcript_basis": _basis(),
        "source_through_sequence_at_open": 0,
        "resolved_model_target_fingerprint": target.target_fingerprint,
        "input_budget_tokens": target.context_budget.input_budget_tokens,
        "token_estimator_fingerprint": target.token_estimator.estimator_fingerprint,
        "window_policy_fingerprint": policy.policy_fingerprint,
        "initial_projection_generation": 0,
        "initial_projection_unit_count": 0,
        "initial_projection_state_fingerprint": context_fingerprint(
            "context-window-projection-state:v1",
            {
                "projection_generation": 0,
                "unit_projections": (),
                "rollups": (),
            },
        ),
        "stable_close_event_id": stable_close_event_id,
        "source_compaction_id": None,
        "source_summary_artifact_id": None,
        "source_summary_fingerprint": None,
    }
    semantic_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"window_id", "stable_close_event_id"}
    }
    semantic = context_fingerprint("context-window-semantic:v1", semantic_payload)
    fact_payload = {**payload, "window_semantic_fingerprint": semantic}
    return ContextWindowFact(
        **fact_payload,
        window_fact_fingerprint=context_fingerprint(
            "context-window-fact:v1", fact_payload
        ),
    )


def _event_ref(event_id: str, sequence: int) -> ContextEventReferenceFact:
    return ContextEventReferenceFact(
        runtime_session_id="runtime:long-horizon",
        event_id=event_id,
        sequence=sequence,
        event_type="test_event",
        payload_fingerprint="sha256:" + f"{sequence:064x}"[-64:],
    )


def _message(**payload) -> TranscriptMessageFact:
    payload = dict(payload)
    for field_name in ("created_at_utc", "finished_at_utc"):
        if payload[field_name] is not None:
            payload[field_name] = canonical_utc_timestamp(payload[field_name])
    return TranscriptMessageFact(
        **payload,
        message_fingerprint=context_fingerprint("transcript-message:v1", payload),
    )


def _rollup_unit(
    *,
    ordinal: int,
    text: str,
    path: str = "/workspace/shared.txt",
) -> tuple[
    ToolResultRenderUnit,
    TranscriptMessageFact,
    TranscriptMessageFact,
    ToolInteractionPairFact,
]:
    call_id = f"call:rollup:{ordinal}"
    call_message_id = f"assistant:rollup:{ordinal}"
    result_message_id = f"tool-result:rollup:{ordinal}"
    call_sequence = ordinal * 2 - 1
    result_sequence = ordinal * 2
    call_ref = _event_ref(f"event:call:{ordinal}", call_sequence)
    result_ref = _event_ref(f"event:result:{ordinal}", result_sequence)
    arguments = freeze_json({"path": path})
    call_block = TranscriptToolCallFact(
        tool_call_id=call_id,
        model_tool_name="read_file",
        raw_arguments_json=json.dumps({"path": path}),
        arguments_status="valid_object",
        parsed_arguments=arguments,
        parse_error_code=None,
        state="finished",
        source_events=(call_ref,),
    )
    result_block = TranscriptToolResultRefFact(
        tool_call_id=call_id,
        tool_result_unit_id=f"unit:rollup:{ordinal}",
        source_events=(result_ref,),
    )
    call_message = _message(
        message_id=call_message_id,
        role="assistant",
        name=None,
        run_id=CTX.run_id,
        turn_id=CTX.turn_id,
        reply_id=CTX.reply_id,
        created_at_utc="2026-07-14T00:00:00Z",
        finished_at_utc="2026-07-14T00:00:00Z",
        segment="prior_history",
        blocks=(call_block,),
        source_sequence_start=call_sequence,
        source_sequence_end=call_sequence,
    )
    result_message = _message(
        message_id=result_message_id,
        role="assistant",
        name="read_file",
        run_id=CTX.run_id,
        turn_id=CTX.turn_id,
        reply_id=CTX.reply_id,
        created_at_utc="2026-07-14T00:00:01Z",
        finished_at_utc="2026-07-14T00:00:01Z",
        segment="prior_history",
        blocks=(result_block,),
        source_sequence_start=result_sequence,
        source_sequence_end=result_sequence,
    )
    pair_payload = {
        "tool_call_id": call_id,
        "model_tool_name": "read_file",
        "call_message_id": call_message_id,
        "call_block_index": 0,
        "result_message_id": result_message_id,
        "result_block_index": 0,
        "call_sequence": call_sequence,
        "result_sequence": result_sequence,
        "pairing_status": "completed",
    }
    pair = ToolInteractionPairFact(
        **pair_payload,
        pair_fingerprint=context_fingerprint(
            "tool-interaction-pair:v1", pair_payload
        ),
    )
    content_block = ToolResultTextContentFact(
        block_id=f"text:rollup:{ordinal}",
        text=text,
        chars=len(text),
        content_fingerprint=context_fingerprint("tool-result-text:v1", text),
        source_events=(result_ref,),
    )
    content_payload = {
        "text_blocks": (content_block,),
        "data_blocks": (),
    }
    content = ToolResultContentFact(
        **content_payload,
        content_fingerprint=context_fingerprint(
            "tool-result-content:v1", content_payload
        ),
    )
    semantics = build_unknown_result_semantics(
        result_state=ToolResultStateFact.SUCCESS
    )
    renderer = default_observation_rollup_renderer_registry().resolve_binding(
        renderer_id="pulsara.observation_rollup.canonical",
        renderer_version="v1",
        renderer_contract_fingerprint=(
            semantics.render_profile.render_contract.rollup_renderer_contract_fingerprint
        ),
    )
    rollup_payload = {
        "schema_version": "tool-result-rollup-semantics.v1",
        "rollup_kind": "repeated_file_reads",
        "family_key": context_fingerprint(
            "tool-result-rollup-family:v1",
            {"tool_name": "read_file", "path": path},
        ),
        "evidence_keys": (f"path={path}",),
        "renderer_id": renderer.contract.renderer_id,
        "renderer_version": renderer.contract.renderer_version,
        "renderer_contract_fingerprint": (
            renderer.contract.renderer_contract_fingerprint
        ),
    }
    rollup_semantics = ToolResultRollupSemanticsFact(
        **rollup_payload,
        semantics_fingerprint=context_fingerprint(
            "tool-result-rollup-semantics:v1", rollup_payload
        ),
    )
    unit_payload = {
        "schema_version": "tool-result-unit:v1",
        "unit_id": result_block.tool_result_unit_id,
        "tool_call_id": call_id,
        "model_tool_name": "read_file",
        "descriptor_attribution": None,
        "render_contract_fingerprint": (
            semantics.render_profile.render_contract_fingerprint
        ),
        "render_variant_fingerprint": (
            semantics.render_profile.selected_variant.variant_fingerprint
        ),
        "call_message_id": call_message_id,
        "result_message_id": result_message_id,
        "call_position": (ordinal - 1) * 2,
        "result_position": (ordinal - 1) * 2 + 1,
        "result_state": ToolResultStateFact.SUCCESS,
        "content": content,
        "artifacts": (),
        "observation_timing": ToolObservationTimingFact(
            observed_at_utc=f"2026-07-14T00:00:0{ordinal}Z",
            source_started_at_utc=f"2026-07-14T00:00:0{ordinal}Z",
            source_ended_at_utc=f"2026-07-14T00:00:0{ordinal}Z",
            observation_duration_seconds=0,
            tool_origin="builtin",
            tool_name="read_file",
            tool_call_id=call_id,
        ),
        "terminal_payload_timing": None,
        "render_profile": semantics.render_profile,
        "essential_capture_policy": None,
        "essential": None,
        "rollup_semantics": rollup_semantics,
        "source_sequence_start": result_sequence,
        "source_sequence_end": result_sequence,
        "source_event_ids": (result_ref.event_id,),
    }
    unit = ToolResultRenderUnit(
        **unit_payload,
        unit_fingerprint=context_fingerprint(
            "tool-result-render-unit:v1", unit_payload
        ),
    )
    return unit, call_message, result_message, pair


def _rollup_transcript(
    *, text: str
) -> tuple[TranscriptCompileInput, tuple[ToolResultRenderUnit, ...]]:
    first = _rollup_unit(ordinal=1, text=text)
    second = _rollup_unit(ordinal=2, text=text)
    current_ref = _event_ref("event:current-user", 5)
    current_text = TranscriptTextBlockFact(
        block_id="text:current-user",
        text="summarize prior observations",
        content_fingerprint=context_fingerprint(
            "transcript-text:v1", "summarize prior observations"
        ),
        source_events=(current_ref,),
    )
    current = _message(
        message_id="user:current",
        role="user",
        name=None,
        run_id=CTX.run_id,
        turn_id=CTX.turn_id,
        reply_id=CTX.reply_id,
        created_at_utc="2026-07-14T00:00:05Z",
        finished_at_utc="2026-07-14T00:00:05Z",
        segment="current_user",
        blocks=(current_text,),
        source_sequence_start=5,
        source_sequence_end=5,
    )
    window_payload = {
        "window_kind": "uncompacted",
        "compaction_terminal_ref": None,
        "compaction_summary_artifact_id": None,
        "compacted_through_sequence": None,
        "keep_after_sequence": None,
        "window_compaction_started_ref": None,
        "window_compaction_source_document_artifact_id": None,
        "window_compaction_source_document_fingerprint": None,
        "summarized_message_ids": (),
        "retained_message_ids": (),
        "retained_history_from_sequence": 1,
        "retained_history_through_sequence": 4,
        "protected_run_start_sequence": 1,
        "protected_run_through_sequence": 5,
    }
    projection_window = TranscriptProjectionWindowFact(
        **window_payload,
        window_fingerprint=context_fingerprint(
            "transcript-projection-window:v1", window_payload
        ),
    )
    messages = (first[1], first[2], second[1], second[2], current)
    payload = {
        "schema_version": "transcript-input:v1",
        "runtime_session_id": "runtime:long-horizon",
        "through_sequence": 5,
        "current_user_anchor": current.message_id,
        "projection_window": projection_window,
        "messages": messages,
        "tool_pairs": (first[3], second[3]),
        "compacted_windows": (),
        "stripped_unfinished_call_ids": (),
        "omitted_non_model_block_ids": (),
    }
    return (
        TranscriptCompileInput(
            **payload,
            transcript_fingerprint=context_fingerprint(
                "transcript-compile-input:v1", payload
            ),
        ),
        (first[0], second[0]),
    )


def _unprotected_facts(
    units: tuple[ToolResultRenderUnit, ...],
) -> tuple[ToolObservationProtectionFact, ...]:
    facts = []
    for unit in units:
        payload = {
            "unit_id": unit.unit_id,
            "classes": (),
            "minimum_representation": ToolObservationRepresentation.PAIR_STUB,
        }
        facts.append(
            ToolObservationProtectionFact(
                **payload,
                protection_fingerprint=context_fingerprint(
                    "tool-observation-protection:v1", payload
                ),
            )
        )
    return tuple(facts)


def _protected_facts(
    units: tuple[ToolResultRenderUnit, ...],
    *,
    protection_class: str,
    minimum: ToolObservationRepresentation,
) -> tuple[ToolObservationProtectionFact, ...]:
    facts = []
    for unit in units:
        payload = {
            "unit_id": unit.unit_id,
            "classes": (protection_class,),
            "minimum_representation": minimum,
        }
        facts.append(
            ToolObservationProtectionFact(
                **payload,
                protection_fingerprint=context_fingerprint(
                    "tool-observation-protection:v1", payload
                ),
            )
        )
    return tuple(facts)


def _rewrite_with_protection(
    *,
    protection_class: str,
    minimum: ToolObservationRepresentation,
):
    transcript, units = _rollup_transcript(text="x" * 12_000)
    target = test_resolved_target_fact()
    estimator = PulsaraHeuristicTokenEstimatorV1()
    window = _window(target=target)
    policy = default_long_horizon_context_policy(
        input_budget_tokens=target.context_budget.input_budget_tokens
    )
    render_policy = resolve_context_compile_policy(LoopBudget()).tool_result_basis
    prepared_input = prepare_tool_result_render_input(
        units=units,
        transcript=transcript,
        policy_basis=render_policy,
    )
    base = render_prepared_tool_result_units(
        prepared=prepared_input,
        transcript=transcript,
        token_estimator=estimator,
    )
    protection = _protected_facts(
        units,
        protection_class=protection_class,
        minimum=minimum,
    )
    initial = build_projection_state(
        window=window,
        projection_generation=0,
        through_sequence=0,
        unit_projections=(),
        rollups=(),
    )
    ingested = plan_new_result_ingest(
        event_context=CTX,
        window=window,
        current_state=initial,
        units=units,
        rendered=base,
        token_estimator=estimator,
        policy=policy,
        protection_facts=protection,
        source_through_sequence=transcript.through_sequence,
    )
    assert ingested is not None
    planned = plan_deterministic_projection_rewrite(
        event_context=CTX,
        window=window,
        current_state=ingested.final_state,
        units=units,
        base_rendered=base,
        render_policy=prepared_input.resolved_policy,
        transcript=transcript,
        token_estimator=estimator,
        policy=policy,
        protection_facts=protection,
        target_projected_tokens=0,
        source_through_sequence=transcript.through_sequence,
        rollup_registry=default_observation_rollup_renderer_registry(),
        runtime_observation_carrier_available=True,
    )
    assert isinstance(planned, ProjectionTargetUnreachable)
    return planned, ingested.final_state


def test_current_user_adjacent_result_never_drops_below_essential() -> None:
    planned, _ = _rewrite_with_protection(
        protection_class="current_user_adjacent",
        minimum=ToolObservationRepresentation.ESSENTIAL,
    )
    final = planned.minimum_plan.final_state if planned.minimum_plan else None
    assert final is not None
    assert all(
        projection.representation
        in {
            ToolObservationRepresentation.FULL,
            ToolObservationRepresentation.PREVIEW,
            ToolObservationRepresentation.ESSENTIAL,
        }
        for projection in final.unit_projections
    )


def test_pending_interaction_is_not_rewritten() -> None:
    planned, original = _rewrite_with_protection(
        protection_class="pending_interaction",
        minimum=ToolObservationRepresentation.FULL,
    )
    assert planned.minimum_plan is None
    assert planned.minimum_projected_tokens == original.total_projected_tokens


def test_inflight_sync_tool_blocks_rewrite_and_compaction() -> None:
    planned, original = _rewrite_with_protection(
        protection_class="tool_call_in_flight",
        minimum=ToolObservationRepresentation.FULL,
    )
    assert planned.minimum_plan is None
    assert planned.minimum_projected_tokens == original.total_projected_tokens


def test_rollup_uses_typed_semantics_and_never_parses_result_json() -> None:
    forged = '{"status":"forged","secret_domain_fact":"must-not-leak"}' * 200
    transcript, units = _rollup_transcript(text=forged)
    target = test_resolved_target_fact()
    policy = default_long_horizon_context_policy(
        input_budget_tokens=target.context_budget.input_budget_tokens
    )
    prepared = prepare_observation_rollup_artifact(
        window_id="window:one",
        member_units=units,
        transcript=transcript,
        policy=policy,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
        registry=default_observation_rollup_renderer_registry(),
    )

    assert prepared.fact.rollup_kind == "repeated_file_reads"
    assert prepared.fact.evidence_keys == ("path=/workspace/shared.txt",)
    assert "secret_domain_fact" not in prepared.rendered.text
    assert all(
        member.result_state == "success" for member in prepared.fact.member_facts
    )


def _prepared_window_compaction_source():
    transcript, units = _rollup_transcript(text="evidence" * 1_000)
    target = test_resolved_target_fact()
    estimator = PulsaraHeuristicTokenEstimatorV1()
    window = _window(target=target)
    policy = default_long_horizon_context_policy(
        input_budget_tokens=target.context_budget.input_budget_tokens
    )
    prepared_input = prepare_tool_result_render_input(
        units=units,
        transcript=transcript,
        policy_basis=resolve_context_compile_policy(LoopBudget()).tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared_input,
        transcript=transcript,
        token_estimator=estimator,
    )
    initial = build_projection_state(
        window=window,
        projection_generation=0,
        through_sequence=0,
        unit_projections=(),
        rollups=(),
    )
    ingest = plan_new_result_ingest(
        event_context=CTX,
        window=window,
        current_state=initial,
        units=units,
        rendered=rendered,
        token_estimator=estimator,
        policy=policy,
        protection_facts=_unprotected_facts(units),
        source_through_sequence=transcript.through_sequence,
    )
    assert ingest is not None

    prepared = prepare_window_compaction_source_document(
        compaction_id="window_compaction:test",
        run_id=CTX.run_id,
        window=window,
        projection_state=ingest.final_state,
        transcript=transcript,
        units=units,
        rendered=rendered,
        prepared_rollups=(),
        protection_facts=_unprotected_facts(units),
        source_through_sequence=transcript.through_sequence,
    )

    return prepared, window, ingest.final_state, target


def test_window_compaction_source_document_partitions_complete_pair_groups() -> None:
    prepared, _window_fact, _projection, _target = (
        _prepared_window_compaction_source()
    )

    assert prepared.summarized_unit_ids
    assert prepared.retained_unit_ids == ()
    assert prepared.protected_unit_ids == ()
    assert len(prepared.fact.summarized_pair_group_ids) == 2
    assert prepared.fact.retained_pair_group_ids == ()
    current_user_entry = next(
        entry
        for entry in prepared.fact.entries
        if entry.source_kind == "user_message"
        and entry.model_visible_text == "summarize prior observations"
    )
    assert current_user_entry.source_entry_id in prepared.fact.retained_entry_ids
    assert all(
        set(group.source_entry_ids) <= set(prepared.fact.summarized_entry_ids)
        for group in prepared.fact.pair_groups
    )


def test_window_compaction_summary_rejects_unknown_citation() -> None:
    prepared, _window_fact, _projection, _target = (
        _prepared_window_compaction_source()
    )
    payload = {
        "observed_facts": ["The tool returned evidence."],
        "model_inferences": [],
        "unresolved_questions": [],
        "critical_constraints": [],
        "artifact_locators": [],
        "cited_source_entry_ids": ["window_source:invented"],
    }

    with pytest.raises(ValueError, match="unknown source entry"):
        parse_window_compaction_summary(
            json.dumps(payload),
            source=prepared.fact,
        )


def test_compacted_window_binds_exact_plan_and_summary() -> None:
    prepared, source_window, projection, _target = (
        _prepared_window_compaction_source()
    )
    call = test_resolved_call(
        purpose=ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY
    )
    policy = default_rollout_budget_policy()
    quote = calculate_model_call_reservation(
        target=call.target.fact,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        policy=policy,
    )
    reservation_payload = {
        "reservation_id": "rollout_reservation:window:test",
        "account_id": "rollout:one",
        "owner_kind": "model_call",
        "owner_id": call.fact.resolved_model_call_id,
        "phase_at_reservation": RolloutPhase.EXPLORATION,
        "budget_bucket": RolloutBudgetBucket.EXPLORATION,
        "reserved_milliunits": quote.reserved_milliunits,
        "model_call_reservation_quote": quote,
        "source_sequence": 1,
    }
    reservation = RolloutReservationFact(
        **reservation_payload,
        semantic_fingerprint=context_fingerprint(
            "rollout-reservation:v1", reservation_payload
        ),
    )
    plan = build_window_compaction_plan(
        compaction_id="window_compaction:test",
        compaction_attempt_index=1,
        run_id=CTX.run_id,
        source_window=source_window,
        source_projection=projection,
        source=prepared,
        source_context_fingerprint="sha256:" + "0" * 64,
        summarizer_call=call.fact,
        rollout_reservation=reservation,
        summarizer_input_manifest_artifact_id="artifact:window-input:test",
        summarizer_input_manifest_fingerprint="sha256:" + "1" * 64,
        source_document_artifact_id="artifact:window-source:test",
        estimated_tokens_before=40_000,
        fixed_new_window_tokens=1_000,
        protected_tail_tokens=1_000,
        summarizer_input_estimated_tokens=2_000,
        post_compaction_target_tokens=32_000,
    )
    cited = prepared.fact.summarized_entry_ids[0]
    summary = parse_window_compaction_summary(
        json.dumps(
            {
                "observed_facts": ["The tool returned evidence."],
                "model_inferences": [],
                "unresolved_questions": [],
                "critical_constraints": ["Keep the current user request."],
                "artifact_locators": [],
                "cited_source_entry_ids": [cited],
            }
        ),
        source=prepared.fact,
    )
    target_window = build_compacted_context_window(
        plan=plan,
        source_window=source_window,
        summary_artifact_id="artifact:window-summary:test",
        summary=summary,
    )

    assert target_window.generation == source_window.generation + 1
    assert target_window.previous_window_id == source_window.window_id
    assert (
        target_window.transcript_basis.source_compaction_plan_fingerprint
        == plan.plan_fingerprint
    )
    assert target_window.source_summary_fingerprint == summary.summary_fingerprint
    assert target_window.stable_close_event_id == plan.stable_target_window_close_event_id


def test_rollup_rewrite_keeps_pairs_and_lowers_inert_observation(
    tmp_path,
) -> None:
    transcript, units = _rollup_transcript(text="x" * 12_000)
    target = test_resolved_target_fact()
    estimator = PulsaraHeuristicTokenEstimatorV1()
    window = _window(target=target)
    policy = default_long_horizon_context_policy(
        input_budget_tokens=target.context_budget.input_budget_tokens
    )
    render_policy = resolve_context_compile_policy(LoopBudget()).tool_result_basis
    prepared_input = prepare_tool_result_render_input(
        units=units,
        transcript=transcript,
        policy_basis=render_policy,
    )
    base = render_prepared_tool_result_units(
        prepared=prepared_input,
        transcript=transcript,
        token_estimator=estimator,
    )
    initial = build_projection_state(
        window=window,
        projection_generation=0,
        through_sequence=0,
        unit_projections=(),
        rollups=(),
    )
    ingested = plan_new_result_ingest(
        event_context=CTX,
        window=window,
        current_state=initial,
        units=units,
        rendered=base,
        token_estimator=estimator,
        policy=policy,
        protection_facts=_unprotected_facts(units),
        source_through_sequence=transcript.through_sequence,
    )
    assert ingested is not None
    planned = plan_deterministic_projection_rewrite(
        event_context=CTX,
        window=window,
        current_state=ingested.final_state,
        units=units,
        base_rendered=base,
        render_policy=prepared_input.resolved_policy,
        transcript=transcript,
        token_estimator=estimator,
        policy=policy,
        protection_facts=_unprotected_facts(units),
        target_projected_tokens=3_000,
        source_through_sequence=transcript.through_sequence,
        rollup_registry=default_observation_rollup_renderer_registry(),
        runtime_observation_carrier_available=True,
    )
    assert planned is not None
    assert not isinstance(planned, ProjectionTargetUnreachable)
    assert len(planned.prepared_rollup_artifacts) == 1
    assert {
        item.representation for item in planned.final_state.unit_projections
    } == {ToolObservationRepresentation.ROLLUP_MEMBER}
    assert planned.final_state.total_projected_tokens == (
        sum(item.estimated_tokens for item in planned.final_state.unit_projections)
        + planned.final_state.rollups[0].estimated_tokens
    )

    runtime_session = in_memory_runtime_session(tmp_path)
    carrier = target.runtime_observation_carrier
    assert carrier is not None
    prepared_rollup = asyncio.run(
        materialize_observation_rollup(
            runtime_session=runtime_session,
            run_id=CTX.run_id,
            prepared=planned.prepared_rollup_artifacts[0],
            carrier=carrier,
        )
    )
    prepared_cache = InMemoryPreparedObservationRollupCache()
    cache_key = prepared_observation_rollup_cache_key(
        durable_rollup_fingerprint=prepared_rollup.rollup.semantic_fingerprint,
        member_unit_fingerprints=tuple(unit.unit_fingerprint for unit in units),
        placement_basis_fingerprint=prepared_rollup.compile_unit.placement_anchor.anchor_fingerprint,
        policy_fingerprint=policy.policy_fingerprint,
        estimator_fingerprint=target.token_estimator.estimator_fingerprint,
        carrier_contract_fingerprint=carrier.contract_fingerprint,
    )
    prepared_cache.put(cache_key, prepared_rollup)
    assert prepared_cache.get(cache_key) is prepared_rollup
    changed_placement_key = prepared_observation_rollup_cache_key(
        durable_rollup_fingerprint=prepared_rollup.rollup.semantic_fingerprint,
        member_unit_fingerprints=tuple(unit.unit_fingerprint for unit in units),
        placement_basis_fingerprint="sha256:changed-placement",
        policy_fingerprint=policy.policy_fingerprint,
        estimator_fingerprint=target.token_estimator.estimator_fingerprint,
        carrier_contract_fingerprint=carrier.contract_fingerprint,
    )
    assert prepared_cache.get(changed_placement_key) is None
    projected = apply_tool_observation_projection(
        units=units,
        rendered=base,
        projection_state=planned.final_state,
        policy=prepared_input.resolved_policy,
        token_estimator=estimator,
    )
    lowered = lower_transcript_for_context(
        transcript=transcript,
        rendered_tool_results=projected,
        prepared_rollups=(prepared_rollup,),
    )
    roles = tuple(message.role for message in lowered.full_messages)
    assert roles.count(MessageRole.TOOL_RESULT) == 2
    observation_index = roles.index(MessageRole.RUNTIME_OBSERVATION)
    assert observation_index > max(
        index for index, role in enumerate(roles) if role is MessageRole.TOOL_RESULT
    )
    assert observation_index < roles.index(MessageRole.USER)
    assert all(
        projection.source_rollup_id == planned.final_state.rollups[0].rollup_id
        for projection in planned.final_state.unit_projections
    )


def _account() -> RolloutBudgetAccountFact:
    target = test_resolved_target_fact()
    policy = default_rollout_budget_policy()
    primary = calculate_model_call_reservation(
        target=target,
        resolved_model_call_id=None,
        policy=policy,
    )
    final_agent = (
        primary.reserved_milliunits * policy.finalization_reserved_model_calls
    )
    final_compaction = (
        primary.reserved_milliunits
        * policy.finalization_reserved_window_compactions
    )
    final_tool = (
        policy.finalization_reserved_tool_cost_units
        * policy.tool_cost_unit_weight_milli
    )
    reserve = final_agent + final_compaction + final_tool
    total = (
        target.context_budget.input_budget_tokens
        * policy.total_input_budget_multiplier_milli
    )
    payload = {
        "account_id": "rollout:one",
        "owner_runtime_session_id": "runtime:long-horizon",
        "root_run_id": CTX.run_id,
        "policy": policy,
        "total_budget_milliunits": total,
        "finalization_reserve_milliunits": reserve,
        "finalization_agent_reserve_milliunits": final_agent,
        "finalization_compaction_reserve_milliunits": final_compaction,
        "finalization_tool_reserve_milliunits": final_tool,
        "exploration_allowance_milliunits": total - reserve,
    }
    return RolloutBudgetAccountFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "rollout-budget-account:v1", payload
        ),
    )


def _model_reservation() -> RolloutReservationFact:
    policy = default_rollout_budget_policy()
    target = test_resolved_target_fact()
    call_id = "model_call:" + "1" * 32
    quote = calculate_model_call_reservation(
        target=target,
        resolved_model_call_id=call_id,
        policy=policy,
    )
    payload = {
        "reservation_id": "reservation:model:one",
        "account_id": "rollout:one",
        "owner_kind": "model_call",
        "owner_id": call_id,
        "phase_at_reservation": RolloutPhase.EXPLORATION,
        "budget_bucket": RolloutBudgetBucket.EXPLORATION,
        "reserved_milliunits": quote.reserved_milliunits,
        "model_call_reservation_quote": quote,
        "source_sequence": 1,
    }
    return RolloutReservationFact(
        **payload,
        semantic_fingerprint=context_fingerprint("rollout-reservation:v1", payload),
    )


def _rollout_state(
    *,
    account: RolloutBudgetAccountFact,
    phase: RolloutPhase = RolloutPhase.EXPLORATION,
    exploration_charged: int = 0,
    final_agent_charged: int = 0,
    final_compaction_charged: int = 0,
    final_tool_charged: int = 0,
    active_reservations: tuple[RolloutReservationFact, ...] = (),
    model_call_count: int = 0,
    tool_call_count: int = 0,
    through_sequence: int = 1,
) -> RolloutBudgetStateFact:
    reserved_by_bucket = {
        bucket: sum(
            item.reserved_milliunits
            for item in active_reservations
            if item.budget_bucket is bucket
        )
        for bucket in RolloutBudgetBucket
    }
    charged = (
        exploration_charged,
        final_agent_charged,
        final_compaction_charged,
        final_tool_charged,
    )
    reserved = tuple(reserved_by_bucket[bucket] for bucket in RolloutBudgetBucket)
    payload = {
        "account_id": account.account_id,
        "phase": phase,
        "charged_milliunits": sum(charged),
        "reserved_milliunits": sum(reserved),
        "exploration_charged_milliunits": charged[0],
        "exploration_reserved_milliunits": reserved[0],
        "finalization_agent_charged_milliunits": charged[1],
        "finalization_agent_reserved_milliunits": reserved[1],
        "finalization_compaction_charged_milliunits": charged[2],
        "finalization_compaction_reserved_milliunits": reserved[2],
        "finalization_tool_charged_milliunits": charged[3],
        "finalization_tool_reserved_milliunits": reserved[3],
        "model_call_count": model_call_count,
        "recovered_incomplete_model_stream_count": 0,
        "model_stream_reconciliation_blocker_count": 0,
        "tool_call_count": tool_call_count,
        "active_reservations": active_reservations,
        "through_sequence": through_sequence,
    }
    return RolloutBudgetStateFact(
        **payload,
        state_fingerprint=context_fingerprint("rollout-budget-state:v1", payload),
    )


def test_phase_transitions_are_monotonic() -> None:
    account = _account()
    initial = initial_rollout_budget_state(account=account, through_sequence=1)
    warning = rollout_state_with_phase(
        initial,
        phase=RolloutPhase.WARNING,
        through_sequence=2,
    )
    assert warning.phase is RolloutPhase.WARNING
    with pytest.raises(RolloutReducerContractError):
        rollout_state_with_phase(warning, phase=RolloutPhase.EXPLORATION)


def test_warning_restricted_finalization_thresholds() -> None:
    account = _account()
    allowance = account.exploration_allowance_milliunits
    policy = account.policy

    def phase_at(ratio_ppm: int) -> RolloutPhase:
        return phase_for_settled_exploration(
            account=account,
            state=_rollout_state(
                account=account,
                exploration_charged=(allowance * ratio_ppm) // 1_000_000,
            ),
        )

    assert phase_at(policy.warning_consumption_ratio_ppm) is RolloutPhase.WARNING
    assert (
        phase_at(policy.restricted_consumption_ratio_ppm)
        is RolloutPhase.RESTRICTED
    )
    assert (
        phase_at(policy.finalization_consumption_ratio_ppm)
        is RolloutPhase.FINALIZATION_ONLY
    )


def test_exploration_admission_unreachable_transitions_to_finalization_with_actual_call() -> None:
    account = _account()
    reservation = _model_reservation()
    quote = reservation.model_call_reservation_quote
    assert quote is not None
    state = _rollout_state(
        account=account,
        phase=RolloutPhase.RESTRICTED,
        exploration_charged=account.exploration_allowance_milliunits - 1,
    )
    plan = plan_root_model_admission(
        account=account,
        state=state,
        quote=quote,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    assert plan.action == "transition"
    assert plan.transition_to is RolloutPhase.FINALIZATION_ONLY
    assert plan.reason_code is RolloutTransitionReason.EXPLORATION_ADMISSION_UNREACHABLE


def test_active_reclaimable_exploration_reservation_prevents_early_unreachable_transition() -> None:
    account = _account()
    reservation = _model_reservation()
    quote = reservation.model_call_reservation_quote
    assert quote is not None
    state = _rollout_state(
        account=account,
        phase=RolloutPhase.RESTRICTED,
        exploration_charged=(
            account.exploration_allowance_milliunits
            - reservation.reserved_milliunits
        ),
        active_reservations=(reservation,),
    )
    plan = plan_root_model_admission(
        account=account,
        state=state,
        quote=quote,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    assert plan.action == "blocked"
    assert plan.budget_bucket is RolloutBudgetBucket.EXPLORATION


def test_exploration_cannot_borrow_any_finalization_bucket() -> None:
    account = _account()
    reservation = _model_reservation()
    quote = reservation.model_call_reservation_quote
    assert quote is not None
    state = _rollout_state(
        account=account,
        phase=RolloutPhase.RESTRICTED,
        exploration_charged=account.exploration_allowance_milliunits - 1,
    )
    assert account.finalization_reserve_milliunits > quote.reserved_milliunits
    plan = plan_root_model_admission(
        account=account,
        state=state,
        quote=quote,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    assert plan.action == "transition"
    assert plan.transition_to is RolloutPhase.FINALIZATION_ONLY


def test_finalization_reserve_preserves_two_calls() -> None:
    account = _account()
    quote = _model_reservation().model_call_reservation_quote
    assert quote is not None
    state = _rollout_state(account=account, phase=RolloutPhase.FINALIZATION_ONLY)
    first = plan_root_model_admission(
        account=account,
        state=state,
        quote=quote,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    assert first.action == "admit"
    assert first.budget_bucket is RolloutBudgetBucket.FINALIZATION_AGENT

    after_one = _rollout_state(
        account=account,
        phase=RolloutPhase.FINALIZATION_ONLY,
        final_agent_charged=quote.reserved_milliunits,
    )
    second = plan_root_model_admission(
        account=account,
        state=after_one,
        quote=quote,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    assert second.action == "admit"

    after_two = _rollout_state(
        account=account,
        phase=RolloutPhase.FINALIZATION_ONLY,
        final_agent_charged=quote.reserved_milliunits * 2,
    )
    exhausted = plan_root_model_admission(
        account=account,
        state=after_two,
        quote=quote,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    assert exhausted.action == "transition"
    assert exhausted.transition_to is RolloutPhase.EXHAUSTED
    assert exhausted.reason_code is RolloutTransitionReason.FINALIZATION_AGENT_UNAVAILABLE


def test_finalization_reserve_separately_preserves_one_window_compaction() -> None:
    account = _account()
    quote = _model_reservation().model_call_reservation_quote
    assert quote is not None
    state = _rollout_state(account=account, phase=RolloutPhase.FINALIZATION_ONLY)
    admitted = plan_root_model_admission(
        account=account,
        state=state,
        quote=quote,
        purpose=ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY,
    )
    assert admitted.action == "admit"
    assert admitted.budget_bucket is RolloutBudgetBucket.FINALIZATION_COMPACTION

    spent = _rollout_state(
        account=account,
        phase=RolloutPhase.FINALIZATION_ONLY,
        final_compaction_charged=quote.reserved_milliunits,
    )
    exhausted = plan_root_model_admission(
        account=account,
        state=spent,
        quote=quote,
        purpose=ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY,
    )
    assert exhausted.action == "transition"
    assert exhausted.transition_to is RolloutPhase.EXHAUSTED
    assert exhausted.reason_code is RolloutTransitionReason.WINDOW_COMPACTION_UNAVAILABLE


def test_finalization_reserve_separately_preserves_synthesis_and_verification_tools() -> None:
    account = _account()
    state = _rollout_state(account=account, phase=RolloutPhase.FINALIZATION_ONLY)
    plan = plan_root_tool_admission(
        account=account,
        state=state,
        attempted_tool_call_count=2,
    )
    assert plan.action == "admit"
    assert plan.budget_bucket is RolloutBudgetBucket.FINALIZATION_TOOL
    assert rollout_bucket_remaining(
        account=account,
        state=state,
        bucket=RolloutBudgetBucket.FINALIZATION_TOOL,
    ) == account.finalization_tool_reserve_milliunits


def test_emergency_counter_is_not_reported_as_normal_budget_exhaustion() -> None:
    account = _account()
    quote = _model_reservation().model_call_reservation_quote
    assert quote is not None
    state = _rollout_state(
        account=account,
        model_call_count=account.policy.emergency_model_call_limit,
    )
    plan = plan_root_model_admission(
        account=account,
        state=state,
        quote=quote,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    assert plan.action == "transition"
    assert plan.transition_to is RolloutPhase.EMERGENCY_HARD_STOP
    assert plan.reason_code is RolloutTransitionReason.EMERGENCY_CIRCUIT_BREAKER


def test_window_generation_is_contiguous() -> None:
    opened = ContextWindowOpenedEvent(
        **CTX.event_fields(),
        sequence=1,
        window=_window(),
        opening_batch_id="batch:one",
    )
    closed = ContextWindowClosedEvent(
        id="window-close:one",
        **CTX.event_fields(),
        sequence=2,
        window_id="window:one",
        window_generation=1,
        close_reason=ContextWindowCloseReason.RUN_FINISHED,
        final_projection_generation=0,
        final_projection_state_fingerprint=(
            opened.window.initial_projection_state_fingerprint
        ),
        source_through_sequence=1,
        next_window_id=None,
        compaction_terminal_event_id=None,
    )
    state = fold_context_window_chain(
        (opened, closed), initial=ContextWindowChainState.empty(run_id=CTX.run_id)
    )
    assert state.consistent
    assert state.ordered_window_ids == ("window:one",)
    assert state.active_window_id is None


def test_window_rejects_two_active_windows() -> None:
    first = ContextWindowOpenedEvent(
        **CTX.event_fields(), sequence=1, window=_window(), opening_batch_id="batch:one"
    )
    duplicate = first.model_copy(
        update={"id": "window-open:two", "sequence": 2, "opening_batch_id": "batch:two"}
    )
    state = fold_context_window_chain(
        (first, duplicate), initial=ContextWindowChainState.empty(run_id=CTX.run_id)
    )
    assert not state.consistent
    assert state.diagnostics[-1].code == "context_window_multiple_open"


def test_projection_rank_never_increases() -> None:
    projection_payload = {
        "schema_version": "tool_observation_projection.v1",
        "window_id": "window:one",
        "projection_generation": 2,
        "unit_id": "unit:one",
        "tool_call_id": "call:one",
        "tool_result_event_id": "result:one",
        "tool_result_sequence": 9,
        "tool_name": "read_file",
        "representation": ToolObservationRepresentation.FULL,
        "representation_rank": 60,
        "rendered_fragment_artifact_id": None,
        "rendered_fragment_fingerprint": "fragment:one",
        "estimated_tokens": 10,
        "primary_artifact_id": None,
        "essential_envelope_fingerprint": "essential:one",
        "observation_timing_fingerprint": "timing:one",
        "source_rollup_id": None,
        "protected_reason_codes": (),
        "decision_reason_code": "new_result_ingested",
    }
    projection = ToolObservationProjectionFact(
        **projection_payload,
        semantic_fingerprint=context_fingerprint(
            "tool-observation-projection:v1", projection_payload
        ),
    )
    with pytest.raises(ValueError, match="cannot increase"):
        ToolObservationProjectionRewriteEntryFact(
            unit_id="unit:one",
            from_representation=ToolObservationRepresentation.PREVIEW,
            to_projection=projection,
        )


def _projection_fact(
    *,
    unit_id: str,
    generation: int,
    result_sequence: int,
) -> ToolObservationProjectionFact:
    payload = {
        "schema_version": "tool_observation_projection.v1",
        "window_id": "window:one",
        "projection_generation": generation,
        "unit_id": unit_id,
        "tool_call_id": f"call:{unit_id}",
        "tool_result_event_id": f"result:{unit_id}",
        "tool_result_sequence": result_sequence,
        "tool_name": "read_file",
        "representation": ToolObservationRepresentation.FULL,
        "representation_rank": 60,
        "rendered_fragment_artifact_id": None,
        "rendered_fragment_fingerprint": f"fragment:{unit_id}",
        "estimated_tokens": 10,
        "primary_artifact_id": None,
        "essential_envelope_fingerprint": f"essential:{unit_id}",
        "observation_timing_fingerprint": f"timing:{unit_id}",
        "source_rollup_id": None,
        "protected_reason_codes": (),
        "decision_reason_code": "new_result_ingested",
    }
    return ToolObservationProjectionFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "tool-observation-projection:v1", payload
        ),
    )


def _projection_page(
    *,
    projection: ToolObservationProjectionFact,
    page_index: int,
    page_count: int,
    final_state_fingerprint: str,
    from_generation: int = 0,
) -> ContextProjectionRewritePageEvent:
    return ContextProjectionRewritePageEvent(
        id=f"rewrite:one:page:{page_index}",
        **CTX.event_fields(),
        sequence=page_index + 2,
        rewrite_id="rewrite:one",
        window_id="window:one",
        from_projection_generation=from_generation,
        to_projection_generation=from_generation + 1,
        source_through_sequence=3,
        page_index=page_index,
        page_count=page_count,
        entries=(
            ToolObservationProjectionRewriteEntryFact(
                unit_id=projection.unit_id,
                from_representation=None,
                to_projection=projection,
            ),
        ),
        rollups=(),
        plan_fingerprint="plan:one",
        final_state_fingerprint=final_state_fingerprint,
        reason_code="new_result_ingested",
    )


def _budget_context(*, call, tool_result_text: str) -> LLMContext:
    return LLMContext(
        messages=(
            LLMMessage.user("inspect the result"),
            LLMMessage.tool_result(
                tool_result_text,
                tool_call_id="call:one",
            ),
        ),
        context_id="context:long-horizon-budget",
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=1,
        system_prompt="runtime facts",
    )


def test_long_horizon_budget_uses_exact_compiler_message_breakdown() -> None:
    call = test_resolved_call()
    window = _window(target=call.target.fact)
    context = _budget_context(call=call, tool_result_text="result body" * 200)
    estimate = call.target.token_estimator.estimate_context(context)
    projection = _projection_fact(unit_id="one", generation=1, result_sequence=2)
    projection_state = build_projection_state(
        window=window,
        projection_generation=1,
        through_sequence=2,
        unit_projections=(projection,),
        rollups=(),
    )
    policy = default_long_horizon_context_policy(
        input_budget_tokens=call.target.context_budget.input_budget_tokens
    )

    measurement = measure_long_horizon_context_budget(
        call=call,
        context=context,
        estimate=estimate,
        window=window,
        projection_state=projection_state,
        policy=policy,
    )

    empty_result = LLMMessage.tool_result("", tool_call_id="call:one")
    result_index = 1
    expected_projected = (
        estimate.message_tokens_by_index[result_index]
        - call.target.token_estimator.estimate_message(empty_result)
    )
    decision = measurement.decision
    assert decision.projected_tool_tokens_before == expected_projected
    assert (
        decision.fixed_non_result_tokens + decision.projected_tool_tokens_before
        == estimate.total_input_tokens
    )
    assert decision.estimator_fingerprint == (
        call.target.fact.token_estimator.estimator_fingerprint
    )
    assert decision.decision == "within_soft_target"
    assert decision.final_input_tokens_after == estimate.total_input_tokens


def test_long_horizon_soft_projection_target_is_not_a_renderer_hard_cap() -> None:
    call = test_resolved_call()
    window = _window(target=call.target.fact)
    input_budget = call.target.context_budget.input_budget_tokens
    policy = default_long_horizon_context_policy(input_budget_tokens=input_budget)
    projected_chars = input_budget * 4 * 3 // 10
    context = _budget_context(call=call, tool_result_text="x" * projected_chars)
    estimate = call.target.token_estimator.estimate_context(context)
    projection = _projection_fact(unit_id="one", generation=1, result_sequence=2)
    projection_state = build_projection_state(
        window=window,
        projection_generation=1,
        through_sequence=2,
        unit_projections=(projection,),
        rollups=(),
    )

    measurement = measure_long_horizon_context_budget(
        call=call,
        context=context,
        estimate=estimate,
        window=window,
        projection_state=projection_state,
        policy=policy,
    )

    assert measurement.soft_target_exceeded
    assert not measurement.window_trigger_exceeded
    assert measurement.decision.decision == "projection_rewrite"
    assert measurement.decision.projected_tool_tokens_after is None


def test_l1_projection_unit_limit_is_diagnostic_only() -> None:
    call = test_resolved_call()
    window = _window(target=call.target.fact)
    context = _budget_context(call=call, tool_result_text="small result")
    estimate = call.target.token_estimator.estimate_context(context)
    policy = default_long_horizon_context_policy(
        input_budget_tokens=call.target.context_budget.input_budget_tokens
    )
    projections = tuple(
        _projection_fact(unit_id=f"unit-{index}", generation=1, result_sequence=index + 2)
        for index in range(policy.max_projection_units_per_window + 1)
    )
    projection_state = build_projection_state(
        window=window,
        projection_generation=1,
        through_sequence=len(projections) + 1,
        unit_projections=projections,
        rollups=(),
    )

    measurement = measure_long_horizon_context_budget(
        call=call,
        context=context,
        estimate=estimate,
        window=window,
        projection_state=projection_state,
        policy=policy,
    )

    assert measurement.decision.unit_count_limit_exceeded
    assert measurement.pressure_shadow.unit_count_limit_exceeded
    assert measurement.pressure_shadow.enforcement_mode == "diagnostic_only"
    assert measurement.decision.decision == "within_soft_target"


def test_projection_reducer_applies_one_atomic_multi_page_generation() -> None:
    window = _window()
    first = _projection_fact(unit_id="one", generation=1, result_sequence=2)
    second = _projection_fact(unit_id="two", generation=1, result_sequence=3)
    final_state = build_projection_state(
        window=window,
        projection_generation=1,
        through_sequence=3,
        unit_projections=(first, second),
        rollups=(),
    )
    opened = ContextWindowOpenedEvent(
        **CTX.event_fields(),
        sequence=1,
        window=window,
        opening_batch_id="batch:one",
    )
    pages = (
        _projection_page(
            projection=first,
            page_index=0,
            page_count=2,
            final_state_fingerprint=final_state.state_semantic_fingerprint,
        ),
        _projection_page(
            projection=second,
            page_index=1,
            page_count=2,
            final_state_fingerprint=final_state.state_semantic_fingerprint,
        ),
    )
    reducer = ContextWindowProjectionReducer()
    reducer.apply_committed((opened, *pages))

    assert reducer.state(window.window_id) == final_state

    reducer.apply_committed(
        (
            ContextWindowClosedEvent(
                id=window.stable_close_event_id,
                **CTX.event_fields(),
                sequence=4,
                window_id=window.window_id,
                window_generation=window.generation,
                close_reason=ContextWindowCloseReason.RUN_FINISHED,
                final_projection_generation=1,
                final_projection_state_fingerprint=(
                    final_state.state_semantic_fingerprint
                ),
                source_through_sequence=3,
                next_window_id=None,
                compaction_terminal_event_id=None,
            ),
        )
    )
    assert reducer.active_state(CTX.run_id) is None


def test_projection_reducer_rejects_incomplete_committed_page_batch() -> None:
    window = _window()
    first = _projection_fact(unit_id="one", generation=1, result_sequence=2)
    final_state = build_projection_state(
        window=window,
        projection_generation=1,
        through_sequence=3,
        unit_projections=(first,),
        rollups=(),
    )
    reducer = ContextWindowProjectionReducer()
    reducer.apply_committed(
        (
            ContextWindowOpenedEvent(
                **CTX.event_fields(),
                sequence=1,
                window=window,
                opening_batch_id="batch:one",
            ),
        )
    )

    with pytest.raises(ContextProjectionReducerError, match="without all declared"):
        reducer.apply_committed(
            (
                _projection_page(
                    projection=first,
                    page_index=0,
                    page_count=2,
                    final_state_fingerprint=final_state.state_semantic_fingerprint,
                ),
            )
        )


def test_projection_reducer_rejects_stale_generation_cas() -> None:
    window = _window()
    projection = _projection_fact(unit_id="one", generation=2, result_sequence=2)
    final_state = build_projection_state(
        window=window,
        projection_generation=2,
        through_sequence=3,
        unit_projections=(projection,),
        rollups=(),
    )
    reducer = ContextWindowProjectionReducer()
    reducer.apply_committed(
        (
            ContextWindowOpenedEvent(
                **CTX.event_fields(),
                sequence=1,
                window=window,
                opening_batch_id="batch:one",
            ),
        )
    )

    with pytest.raises(ContextProjectionReducerError, match="generation CAS"):
        reducer.apply_committed(
            (
                _projection_page(
                    projection=projection,
                    page_index=0,
                    page_count=1,
                    final_state_fingerprint=final_state.state_semantic_fingerprint,
                    from_generation=1,
                ),
            )
        )


def test_rollout_model_reservation_and_settlement_use_exact_quote() -> None:
    account = _account()
    reservation = _model_reservation()
    opened = RolloutBudgetAccountOpenedEvent(
        **CTX.event_fields(), sequence=1, account=account
    )
    created = RolloutBudgetReservationCreatedEvent(
        **CTX.event_fields(), sequence=2, reservation=reservation
    )
    quote = reservation.model_call_reservation_quote
    assert isinstance(quote, ModelCallReservationQuoteFact)
    charge_payload = {
        "accounting_basis": "reserved_missing_usage",
        "reported_input_tokens": None,
        "reported_cached_input_tokens": None,
        "reported_output_tokens": None,
        "pre_send_estimated_input_tokens": 100,
        "physical_input_token_upper_bound": quote.physical_input_token_upper_bound,
        "output_token_upper_bound": quote.output_token_upper_bound,
        "charged_output_tokens": quote.output_token_upper_bound,
        "charged_milliunits": quote.reserved_milliunits,
        "reservation_quote_fact_fingerprint": quote.quote_fact_fingerprint,
        "policy_fingerprint": quote.policy_fingerprint,
    }
    charge = RolloutUsageChargeFact(
        **charge_payload,
        charge_fingerprint=context_fingerprint(
            "rollout-usage-charge:v1", charge_payload
        ),
    )
    settled = RolloutBudgetReservationSettledEvent(
        **CTX.event_fields(),
        sequence=3,
        reservation_id=reservation.reservation_id,
        charged_milliunits=charge.charged_milliunits,
        usage_status="reserved_missing_usage",
        usage_charge=charge,
        source_model_call_end_event_id="model-end:one",
        source_tool_result_event_id=None,
        child_usage_handoff=None,
    )
    folded_account, state = fold_rollout_budget((opened, created, settled))
    assert folded_account == account
    assert state is not None
    assert state.model_call_count == 1
    assert state.charged_milliunits == quote.reserved_milliunits
    assert state.active_reservations == ()


def test_rollout_state_snapshot_freezes_state_and_reducer_high_water_together() -> None:
    account = _account()
    store = LongHorizonStateStore(
        (
            RolloutBudgetAccountOpenedEvent(
                **CTX.event_fields(),
                sequence=1,
                account=account,
            ),
        )
    )

    through_sequence, state = store.rollout_state_snapshot(account.account_id)

    assert through_sequence == 1
    assert state is not None
    assert state.through_sequence == through_sequence


def _fold_model_settlement(
    settlement: RolloutBudgetReservationSettledEvent,
) -> None:
    account = _account()
    reservation = _model_reservation()
    _account_fact, state = fold_rollout_budget(
        (
            RolloutBudgetAccountOpenedEvent(
                **CTX.event_fields(), sequence=1, account=account
            ),
            RolloutBudgetReservationCreatedEvent(
                **CTX.event_fields(), sequence=2, reservation=reservation
            ),
            settlement.model_copy(update={"sequence": 3}),
        )
    )
    assert state is not None
    assert state.active_reservations == ()


def _model_end(
    *,
    usage: ModelTokenUsageFact | None,
    outcome: str = "completed",
    dispatch: str = "dispatched",
    estimated_input_tokens: int = 100,
) -> ModelCallEndEvent:
    call_id = _model_reservation().owner_id
    target = test_resolved_target_fact()
    return ModelCallEndEvent(
        **CTX.event_fields(),
        id=f"model-end:{outcome}:{dispatch}:{usage is not None}",
        resolved_model_call_id=call_id,
        target_fingerprint=target.target_fingerprint,
        reported_model_id=target.model_id if dispatch == "dispatched" else None,
        outcome=outcome,
        provider_dispatch_status=dispatch,
        usage_status="reported" if usage is not None else "missing",
        usage=usage,
        estimated_input_tokens=estimated_input_tokens,
        terminal_projection=model_terminal_projection_end_reference_fixture(
            call_id,
            outcome=outcome,
        ),
    )


def test_rollout_account_weighted_usage_math() -> None:
    account = _account()
    reservation = _model_reservation()
    usage = ModelTokenUsageFact(
        input_tokens=100,
        cached_input_tokens=40,
        output_tokens=10,
        total_tokens=110,
    )
    settlement = build_model_reservation_settlement_event(
        event_context=CTX,
        account=account,
        reservation=reservation,
        model_end=_model_end(usage=usage),
    )
    expected = (
        60 * account.policy.non_cached_input_weight_milli
        + 40 * account.policy.cached_input_weight_milli
        + 10 * account.policy.output_weight_milli
    )
    assert settlement.charged_milliunits == expected
    _fold_model_settlement(settlement)


def test_cached_input_is_subset_of_input_and_charged_once() -> None:
    account = _account()
    reservation = _model_reservation()
    usage = ModelTokenUsageFact(
        input_tokens=100,
        cached_input_tokens=100,
        output_tokens=0,
        total_tokens=100,
    )
    settlement = build_model_reservation_settlement_event(
        event_context=CTX,
        account=account,
        reservation=reservation,
        model_end=_model_end(usage=usage),
    )
    assert settlement.charged_milliunits == (
        100 * account.policy.cached_input_weight_milli
    )
    _fold_model_settlement(settlement)


def test_missing_usage_settles_full_physical_reservation_quote() -> None:
    account = _account()
    reservation = _model_reservation()
    settlement = build_model_reservation_settlement_event(
        event_context=CTX,
        account=account,
        reservation=reservation,
        model_end=_model_end(usage=None, outcome="provider_error"),
    )
    assert settlement.usage_status == "reserved_missing_usage"
    assert settlement.charged_milliunits == reservation.reserved_milliunits
    _fold_model_settlement(settlement)


def test_missing_usage_never_uses_stream_chars_or_cached_discount() -> None:
    account = _account()
    reservation = _model_reservation()
    settlement = build_model_reservation_settlement_event(
        event_context=CTX,
        account=account,
        reservation=reservation,
        model_end=_model_end(
            usage=None,
            outcome="provider_error",
            estimated_input_tokens=1,
        ),
    )
    assert settlement.usage_charge is not None
    assert settlement.usage_charge.reported_cached_input_tokens is None
    assert settlement.charged_milliunits == reservation.reserved_milliunits


def test_start_committed_but_provider_not_dispatched_settles_zero_not_full_quote() -> None:
    account = _account()
    reservation = _model_reservation()
    settlement = build_model_reservation_settlement_event(
        event_context=CTX,
        account=account,
        reservation=reservation,
        model_end=_model_end(
            usage=None,
            outcome="runtime_error",
            dispatch="not_started",
        ),
    )
    assert settlement.usage_status == "not_started_zero"
    assert settlement.charged_milliunits == 0
    _fold_model_settlement(settlement)


def test_cancelled_call_with_reported_usage_uses_reported_settlement() -> None:
    account = _account()
    reservation = _model_reservation()
    usage = ModelTokenUsageFact(
        input_tokens=7,
        cached_input_tokens=2,
        output_tokens=3,
        total_tokens=10,
    )
    settlement = build_model_reservation_settlement_event(
        event_context=CTX,
        account=account,
        reservation=reservation,
        model_end=_model_end(usage=usage, outcome="cancelled"),
    )
    assert settlement.usage_status == "provider_reported_usage"
    assert settlement.charged_milliunits < reservation.reserved_milliunits
    _fold_model_settlement(settlement)


def test_rollout_reducer_rejects_self_consistent_wrong_weighted_charge() -> None:
    account = _account()
    reservation = _model_reservation()
    quote = reservation.model_call_reservation_quote
    assert quote is not None and quote.quote_fact_fingerprint is not None
    charge_payload = {
        "accounting_basis": "provider_reported_usage",
        "reported_input_tokens": 10,
        "reported_cached_input_tokens": 5,
        "reported_output_tokens": 1,
        "pre_send_estimated_input_tokens": 10,
        "physical_input_token_upper_bound": quote.physical_input_token_upper_bound,
        "output_token_upper_bound": quote.output_token_upper_bound,
        "charged_output_tokens": 1,
        "charged_milliunits": 1,
        "reservation_quote_fact_fingerprint": quote.quote_fact_fingerprint,
        "policy_fingerprint": account.policy.policy_fingerprint,
    }
    charge = RolloutUsageChargeFact(
        **charge_payload,
        charge_fingerprint=context_fingerprint(
            "rollout-usage-charge:v1", charge_payload
        ),
    )
    settlement = RolloutBudgetReservationSettledEvent(
        **CTX.event_fields(),
        reservation_id=reservation.reservation_id,
        charged_milliunits=1,
        usage_status="provider_reported_usage",
        usage_charge=charge,
        source_model_call_end_event_id="model-end:tampered",
        source_tool_result_event_id=None,
        child_usage_handoff=None,
    )
    with pytest.raises(
        RolloutReducerContractError,
        match="charge arithmetic mismatch",
    ):
        _fold_model_settlement(settlement)


def test_rollout_settlement_cannot_exceed_reservation() -> None:
    account = _account()
    reservation = _model_reservation()
    opened = RolloutBudgetAccountOpenedEvent(
        **CTX.event_fields(), sequence=1, account=account
    )
    created = RolloutBudgetReservationCreatedEvent(
        **CTX.event_fields(), sequence=2, reservation=reservation
    )
    invalid = RolloutBudgetReservationSettledEvent.model_construct(
        **CTX.event_fields(),
        id="settlement:invalid",
        created_at="2026-07-14T00:00:00Z",
        sequence=3,
        type="ROLLOUT_BUDGET_RESERVATION_SETTLED",
        reservation_id=reservation.reservation_id,
        charged_milliunits=reservation.reserved_milliunits + 1,
        usage_status="tool_terminal",
        usage_charge=None,
        source_model_call_end_event_id=None,
        source_tool_result_event_id="tool-result:one",
        metadata={},
    )
    with pytest.raises(RolloutReducerContractError, match="exceeds reservation"):
        fold_rollout_budget((opened, created, invalid))


def _tool_reservation(
    *,
    call_id: str,
    reservation_id: str,
    action_class: LongHorizonActionClass,
    source_sequence: int,
) -> RolloutReservationFact:
    policy = default_rollout_budget_policy()
    amount = policy.tool_cost_unit_weight_milli
    payload = {
        "reservation_id": reservation_id,
        "account_id": _account().account_id,
        "owner_kind": "tool_call",
        "owner_id": call_id,
        "phase_at_reservation": RolloutPhase.EXPLORATION,
        "budget_bucket": RolloutBudgetBucket.EXPLORATION,
        "reserved_milliunits": amount,
        "model_call_reservation_quote": None,
        "source_sequence": source_sequence,
    }
    del action_class
    return RolloutReservationFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "rollout-reservation:v1", payload
        ),
    )


def _status_policy() -> RolloutStatusHintPolicyFact:
    payload = {
        "schema_version": "rollout-status-hint-policy:v1",
        "recent_tool_call_window": 16,
        "minimum_equivalent_outcome_occurrences": 3,
        "max_recurrence_entries": 4,
    }
    return RolloutStatusHintPolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint(
            "rollout-status-hint-policy:v1", payload
        ),
    )


def _append_settled_tool(
    events: list,
    *,
    call_id: str,
    action_class: LongHorizonActionClass,
    arguments: dict[str, object],
    text_chunks: tuple[str, ...],
    observed_at: str,
) -> None:
    sequence = len(events) + 1
    reservation = _tool_reservation(
        call_id=call_id,
        reservation_id=f"reservation:{call_id}",
        action_class=action_class,
        source_sequence=sequence - 1,
    )
    events.append(
        RolloutBudgetReservationCreatedEvent(
            **CTX.event_fields(), sequence=sequence, reservation=reservation
        )
    )
    policy = fixed_tool_action_policy(action_class)
    registry: ToolActionClassifierRegistry = default_tool_action_classifier_registry()
    classification = registry.classify(
        call=ToolCall(id=call_id, name="search_files", arguments=arguments),
        descriptor_id="descriptor:search_files",
        descriptor_fingerprint="descriptor-fingerprint:search_files",
        policy=policy,
    )
    events.append(
        CapabilityGateDecisionEvent(
            **CTX.event_fields(),
            sequence=len(events) + 1,
            tool_call_id=call_id,
            tool_name="search_files",
            descriptor_id="descriptor:search_files",
            decision="allow",
            action_classification=classification,
        )
    )
    for chunk in text_chunks:
        events.append(
            ToolResultTextDeltaEvent(
                **CTX.event_fields(),
                sequence=len(events) + 1,
                tool_call_id=call_id,
                delta=chunk,
            )
        )
    terminal = ToolResultEndEvent(
        **CTX.event_fields(),
        id=f"tool-result-end:{call_id}",
        sequence=len(events) + 1,
        created_at=observed_at,
        tool_call_id=call_id,
        state=ToolResultState.SUCCESS,
        **tool_result_end_contract_fields(
            call_id,
            tool_name="search_files",
            observed_at_utc=observed_at,
        ),
    )
    events.append(terminal)
    events.append(
        RolloutBudgetReservationSettledEvent(
            **CTX.event_fields(),
            sequence=len(events) + 1,
            reservation_id=reservation.reservation_id,
            charged_milliunits=reservation.reserved_milliunits,
            usage_status="tool_terminal",
            usage_charge=None,
            source_model_call_end_event_id=None,
            source_tool_result_event_id=terminal.id,
            child_usage_handoff=None,
        )
    )


def _status_slice(events: list) -> ContextEventSlice:
    frozen = tuple(
        FrozenStoredEvent.from_stored_event(
            event,
            runtime_session_id="runtime:long-horizon",
        )
        for event in events
    )
    return ContextEventSlice(
        runtime_session_id="runtime:long-horizon",
        from_sequence=1,
        through_sequence=len(frozen),
        events=frozen,
        event_ids_fingerprint=context_fingerprint(
            "context-event-slice-ids:v1",
            tuple(event.event_id for event in frozen),
        ),
        event_payloads_fingerprint=context_fingerprint(
            "context-event-slice-payloads:v1",
            tuple(event.payload_fingerprint for event in frozen),
        ),
    )


def test_exact_recurrence_allows_interleaved_hydration_and_control_calls() -> None:
    account = _account()
    events: list = [
        RolloutBudgetAccountOpenedEvent(
            **CTX.event_fields(), sequence=1, account=account
        )
    ]
    _append_settled_tool(
        events,
        call_id="call:search:one",
        action_class=LongHorizonActionClass.EVIDENCE_ACQUISITION,
        arguments={"query": "same"},
        text_chunks=("same-result",),
        observed_at="2026-01-01T00:00:01Z",
    )
    _append_settled_tool(
        events,
        call_id="call:hydrate",
        action_class=LongHorizonActionClass.EVIDENCE_HYDRATION,
        arguments={"query": "artifact"},
        text_chunks=("hydrated",),
        observed_at="2026-01-01T00:00:02Z",
    )
    _append_settled_tool(
        events,
        call_id="call:search:two",
        action_class=LongHorizonActionClass.EVIDENCE_ACQUISITION,
        arguments={"query": "same"},
        text_chunks=("same-", "result"),
        observed_at="2026-01-01T00:00:03Z",
    )
    _append_settled_tool(
        events,
        call_id="call:search:three",
        action_class=LongHorizonActionClass.EVIDENCE_ACQUISITION,
        arguments={"query": "same"},
        text_chunks=("same-result",),
        observed_at="2026-01-01T00:00:04Z",
    )

    shadow = derive_rollout_status_shadow(
        event_slice=_status_slice(events),
        account_id=account.account_id,
        policy=_status_policy(),
    )

    assert shadow.model_visible is False
    assert shadow.settled_tool_call_count == 4
    assert len(shadow.recurrence) == 1
    recurrence = shadow.recurrence[0]
    assert recurrence.action_occurrence_count == 3
    assert recurrence.equivalent_terminal_outcome_count == 3
    assert len(recurrence.source_event_refs) == 3


def test_same_action_with_different_terminal_outcomes_does_not_trigger_hint() -> None:
    account = _account()
    events: list = [
        RolloutBudgetAccountOpenedEvent(
            **CTX.event_fields(), sequence=1, account=account
        )
    ]
    for index, result in enumerate(("one", "two", "three"), start=1):
        _append_settled_tool(
            events,
            call_id=f"call:changed:{index}",
            action_class=LongHorizonActionClass.EXTERNAL_ACTION,
            arguments={"query": "same"},
            text_chunks=(result,),
            observed_at=f"2026-01-01T00:00:0{index}Z",
        )

    shadow = derive_rollout_status_shadow(
        event_slice=_status_slice(events),
        account_id=account.account_id,
        policy=_status_policy(),
    )

    assert shadow.settled_tool_call_count == 3
    assert shadow.recurrence == ()


def test_tool_terminal_unknown_keeps_single_terminal_owner_and_blocks_close(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    reservation = _tool_reservation(
        call_id="call:unknown-terminal",
        reservation_id="reservation:unknown-terminal",
        action_class=LongHorizonActionClass.EXTERNAL_ACTION,
        source_sequence=1,
    )
    registry = runtime_session.tool_execution_terminal_registry
    registry.install_admitted_batch(
        run_id=CTX.run_id,
        reservations=(reservation,),
    )
    terminal = ToolResultEndEvent(
        **CTX.event_fields(),
        id="tool-result-end:unknown-terminal",
        tool_call_id=reservation.owner_id,
        state=ToolResultState.ERROR,
        **tool_result_end_contract_fields(
            reservation.owner_id,
            tool_name="search_files",
            state=ToolResultState.ERROR,
        ),
    )
    settlement = RolloutBudgetReservationSettledEvent(
        **CTX.event_fields(),
        id="rollout-settlement:unknown-terminal",
        reservation_id=reservation.reservation_id,
        charged_milliunits=reservation.reserved_milliunits,
        usage_status="tool_terminal",
        usage_charge=None,
        source_model_call_end_event_id=None,
        source_tool_result_event_id=terminal.id,
        child_usage_handoff=None,
    )
    registry.freeze_terminal(
        run_id=CTX.run_id,
        reservation=reservation,
        candidates=(terminal, settlement),
    )
    registry.mark_commit_outcome_unknown(
        run_id=CTX.run_id,
        reservation=reservation,
    )
    session_type = type(runtime_session)
    original_confirm = session_type.confirm_event_batch_from_thread

    def confirmation_unavailable(self, candidates, **kwargs):
        if self is runtime_session:
            raise OSError("injected confirmation outage")
        return original_confirm(self, candidates, **kwargs)

    monkeypatch.setattr(
        session_type,
        "confirm_event_batch_from_thread",
        confirmation_unavailable,
    )

    with pytest.raises(ToolExecutionTerminalDrainBlocked):
        asyncio.run(registry.drain_pending(deadline_monotonic=0.0))

    owner = registry.owner_for_call(
        run_id=CTX.run_id,
        tool_call_id=reservation.owner_id,
    )
    assert owner is not None
    assert owner.attempt_generation == 1
    assert registry.active_owner_count() == 1


def test_quote_rejects_target_or_policy_drift() -> None:
    quote = calculate_model_call_reservation(
        target=test_resolved_target_fact(),
        resolved_model_call_id="model_call:" + "2" * 32,
        policy=default_rollout_budget_policy(),
    )
    with pytest.raises(ValueError, match="amount mismatch"):
        ModelCallReservationQuoteFact.model_validate(
            {
                **quote.model_dump(mode="json"),
                "reserved_milliunits": quote.reserved_milliunits + 1,
            }
        )


def test_direct_model_stream_recovery_writes_stable_runtime_error_end() -> None:
    call = test_resolved_call(purpose=ModelCallPurpose.MEMORY_REFLECTION)
    log = InMemoryEventLog()
    start = ModelCallStartEvent(
        **CTX.event_fields(),
        **model_call_start_fields(
            resolved_call=call.fact,
            model_call_index=None,
            lifecycle_kind="direct_internal_call",
            pre_send_estimated_input_tokens=17,
        ),
    )
    log.extend((start,))

    report = ModelStreamRecoveryService(
        event_log=log,
        archive=InMemoryArchiveStore(),
        allow_unbootstrapped_test_events=True,
    ).repair_incomplete_model_streams()

    assert report.repaired[0].terminal_outcome == "runtime_error"
    ends = [event for event in log.iter() if isinstance(event, ModelCallEndEvent)]
    assert len(ends) == 1
    assert ends[0].id == start.recovery_plan.stable_model_call_end_event_id
    assert ends[0].estimated_input_tokens == 17
    assert ends[0].provider_dispatch_status == "dispatched"
    assert not any(isinstance(event, ReplyEndEvent) for event in log.iter())


def test_model_stream_recovery_atomically_settles_physical_reservation() -> None:
    contracts = build_default_authority_materialization_contract_bundle()
    log = InMemoryEventLog(runtime_session_id="runtime:model-recovery")
    store = LedgerMaterializationAccountStore(
        state=None,
        charge_contract=contracts.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id=log.runtime_session_id,
        event_log=log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    coordinator.bootstrap_genesis(
        context=CTX,
        business_events=(
            CustomEvent(
                id="event:model-recovery-genesis",
                **CTX.event_fields(),
                name="model-recovery-genesis",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=(
            contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.LEDGER_GENESIS
            ).contract
        ),
        register_transcript_consumer=True,
    )
    call = test_resolved_call(purpose=ModelCallPurpose.MEMORY_REFLECTION)
    start = ModelCallStartEvent(
        **CTX.event_fields(),
        **model_call_start_fields(
            resolved_call=call.fact,
            model_call_index=None,
            lifecycle_kind="direct_internal_call",
            pre_send_estimated_input_tokens=17,
        ),
    )
    coordinator.reserve_and_commit_dispatch(
        context=CTX,
        business_events=(start,),
        reservation_id=f"model_physical:{call.fact.resolved_model_call_id[-96:]}",
        owner_id=call.fact.resolved_model_call_id,
        burst_contract=(
            contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.MODEL_CALL
            ).contract
        ),
    )

    report = ModelStreamRecoveryService(
        event_log=log,
        archive=InMemoryArchiveStore(),
    ).repair_incomplete_model_streams()

    assert report.repaired[0].terminal_outcome == "runtime_error"
    account = log.read_materialization_account_state()
    assert account is not None
    assert account.active_reservations == ()
    settlements = tuple(
        event
        for event in log.iter()
        if isinstance(event, PhysicalOperationReservationSettledEvent)
    )
    assert len(settlements) == 1
    assert settlements[0].settlement.reservation_id == (
        f"model_physical:{call.fact.resolved_model_call_id[-96:]}"
    )


def test_main_model_stream_recovery_closes_reply_envelope() -> None:
    call = test_resolved_call()
    log = InMemoryEventLog()
    start = ModelCallStartEvent(
        **CTX.event_fields(),
        **model_call_start_fields(
            resolved_call=call.fact,
            pre_send_estimated_input_tokens=23,
        ),
    )
    reply_start = ReplyStartEvent(
        id=start.recovery_plan.reply_start_event_id,
        **CTX.event_fields(),
        name="assistant",
    )
    log.extend((reply_start, start))

    ModelStreamRecoveryService(
        event_log=log,
        archive=InMemoryArchiveStore(),
        allow_unbootstrapped_test_events=True,
    ).repair_incomplete_model_streams()

    events = log.iter()
    end = next(event for event in events if isinstance(event, ModelCallEndEvent))
    reply_end = next(event for event in events if isinstance(event, ReplyEndEvent))
    assert end.outcome == "runtime_error"
    assert reply_end.id == start.recovery_plan.stable_reply_end_event_id
    assert reply_end.model_terminal_outcome == end.outcome
    assert end.sequence is not None and reply_end.sequence == end.sequence + 1


def test_model_stream_recovery_preserves_durable_provider_error_winner() -> None:
    call = test_resolved_call(purpose=ModelCallPurpose.MEMORY_REFLECTION)
    log = InMemoryEventLog()
    start = ModelCallStartEvent(
        **CTX.event_fields(),
        **model_call_start_fields(
            resolved_call=call.fact,
            model_call_index=None,
            lifecycle_kind="direct_internal_call",
        ),
    )
    draft = build_semantic_draft(
        ProviderErrorDraft,
        transport_sequence_index=0,
        error=sanitize_provider_failure(message="provider unavailable"),
    )
    source_before = sha256_fingerprint(
        "model-stream-sanitized-source:v2", "empty"
    )
    source_after = sha256_fingerprint(
        "model-stream-sanitized-source-receipt:v2",
        {
            "source_accumulator_before": source_before,
            "transport_sequence_index": 0,
            "draft_kind": draft.draft_kind,
            "draft_fingerprint": draft.draft_fingerprint,
        },
    )
    envelope = SanitizedProviderSemanticEnvelope(
        envelope_id="provider-error-envelope",
        draft=draft,
        proposed_transport_sequence_index=0,
        source_accumulator_before=source_before,
        source_accumulator_after=source_after,
        accepted_at_monotonic_ns=0,
        adapter_source_payload_bytes=1,
        counts_as_adapter_source_item=False,
    )
    prepared = ModelStreamSegmentAccumulator(
        resolved_model_call_id=call.fact.resolved_model_call_id,
        model_call_start_event_id=start.id,
        context=CTX,
    ).push(envelope)
    assert len(prepared) == 1
    provider_error = prepared[0].event
    log.extend((start, provider_error))

    report = ModelStreamRecoveryService(
        event_log=log,
        archive=InMemoryArchiveStore(),
        allow_unbootstrapped_test_events=True,
    ).repair_incomplete_model_streams()

    assert report.repaired[0].terminal_outcome == "provider_error"
    end = next(
        event for event in log.iter() if isinstance(event, ModelCallEndEvent)
    )
    assert end.outcome == "provider_error"


def _commit_completed_main_model_call(
    log: InMemoryEventLog,
    archive: InMemoryArchiveStore,
) -> ModelCallStartEvent:
    call = test_resolved_call()
    start = ModelCallStartEvent(
        **CTX.event_fields(),
        **model_call_start_fields(resolved_call=call.fact),
    )
    reply_start = ReplyStartEvent(
        id=start.recovery_plan.reply_start_event_id,
        **CTX.event_fields(),
        name="assistant",
    )
    committed_prefix = log.extend((reply_start, start))
    committed_start = next(
        event for event in committed_prefix if isinstance(event, ModelCallStartEvent)
    )
    usage = ModelTokenUsageFact(input_tokens=0, output_tokens=0, total_tokens=0)
    reducer = ModelTerminalProjectionReducer(
        runtime_session_id=log.runtime_session_id,
        start_event=committed_start,
        contracts=build_default_terminal_projection_contract_bundle(),
        model_stream_semantic_domain_contract_fingerprint=(
            build_default_authority_materialization_contract_bundle()
            .event_domain.contract.transcript_semantic_domain_contract_fingerprint
        ),
        segment_policy_contract_fingerprint=(
            DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT.contract_fingerprint
        ),
    )
    projection = reducer.prepare_terminal(
        event_context=CTX,
        terminal_outcome="completed",
        usage_report=TransportUsageReport(
            usage_status="reported",
            usage=usage,
            reported_model_id=call.fact.target.model_id,
        ),
    )
    archive.put_text(
        projection.projection_reference.document_artifact_id,
        projection.canonical_document_bytes.decode("utf-8"),
        session_id=log.runtime_session_id,
        run_id=CTX.run_id,
        media_type="application/vnd.pulsara.terminal-projection+json; version=2",
    )
    end_fields = model_call_end_fields(resolved_call=call.fact)
    end = ModelCallEndEvent(
        id=committed_start.recovery_plan.stable_model_call_end_event_id,
        **CTX.event_fields(),
        **{**end_fields, "terminal_projection": projection.end_reference},
    )
    reply_end = ReplyEndEvent(
        id=committed_start.recovery_plan.stable_reply_end_event_id,
        **CTX.event_fields(),
        model_terminal_outcome="completed",
    )
    log.extend((projection.committed_event, end, reply_end))
    return committed_start


def test_control_recovery_suppresses_completed_call_without_downstream() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    start = _commit_completed_main_model_call(log, archive)

    report = ModelCallControlDispositionRecoveryService(
        event_log=log,
        archive=archive,
        allow_unbootstrapped_test_events=True,
    ).repair_missing_dispositions()

    assert report.recovered[0].source == "recovered_suppression"
    dispositions = tuple(
        event
        for event in log.iter()
        if isinstance(event, ModelCallControlDispositionResolvedEvent)
    )
    assert len(dispositions) == 1
    assert (
        dispositions[0].disposition
        is ModelCallControlDisposition.SUPPRESSED_BY_RECOVERY
    )
    assert dispositions[0].model_call_start_event_id == start.id


def test_control_recovery_rejects_run_end_without_prior_disposition() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _commit_completed_main_model_call(log, archive)
    log.append(
        RunEndEvent(
            **CTX.event_fields(),
            status="finished",
            stop_reason=RunStopReason.FINAL,
            terminalization_kind=RunTerminalizationKind.NORMAL,
        )
    )

    with pytest.raises(
        ModelCallControlRecoveryStructuralError,
        match="downstream control facts",
    ):
        ModelCallControlDispositionRecoveryService(
            event_log=log,
            archive=archive,
            allow_unbootstrapped_test_events=True,
        ).repair_missing_dispositions()


def _service_window_compaction_transcript(
    *, runtime_session, run_start, window_open
) -> TranscriptCompileInput:
    prior_ref = event_reference_from_stored(
        run_start,
        runtime_session_id=runtime_session.runtime_session_id,
    )
    current_ref = event_reference_from_stored(
        window_open,
        runtime_session_id=runtime_session.runtime_session_id,
    )
    current_text = "Investigate the evidence and report the result."
    assistant_text = "Prior observed evidence: " + "verified-data " * 400
    current_block = TranscriptTextBlockFact(
        block_id="text:service-current-user",
        text=current_text,
        content_fingerprint=context_fingerprint(
            "transcript-text:v1", current_text
        ),
        source_events=(current_ref,),
    )
    assistant_block = TranscriptTextBlockFact(
        block_id="text:service-assistant-tail",
        text=assistant_text,
        content_fingerprint=context_fingerprint(
            "transcript-text:v1", assistant_text
        ),
        source_events=(prior_ref,),
    )
    current = _message(
        message_id="user:service-current",
        role="user",
        name=None,
        run_id=CTX.run_id,
        turn_id=CTX.turn_id,
        reply_id=CTX.reply_id,
        created_at_utc=window_open.created_at,
        finished_at_utc=window_open.created_at,
        segment="current_user",
        blocks=(current_block,),
        source_sequence_start=current_ref.sequence,
        source_sequence_end=current_ref.sequence,
    )
    assistant = _message(
        message_id="assistant:service-tail",
        role="assistant",
        name=None,
        run_id=CTX.run_id,
        turn_id=CTX.turn_id,
        reply_id=CTX.reply_id,
        created_at_utc=run_start.created_at,
        finished_at_utc=run_start.created_at,
        segment="prior_history",
        blocks=(assistant_block,),
        source_sequence_start=prior_ref.sequence,
        source_sequence_end=prior_ref.sequence,
    )
    through_sequence = runtime_session.event_log.next_sequence() - 1
    window_payload = {
        "window_kind": "uncompacted",
        "compaction_terminal_ref": None,
        "compaction_summary_artifact_id": None,
        "compacted_through_sequence": None,
        "keep_after_sequence": None,
        "window_compaction_started_ref": None,
        "window_compaction_source_document_artifact_id": None,
        "window_compaction_source_document_fingerprint": None,
        "summarized_message_ids": (),
        "retained_message_ids": (),
        "retained_history_from_sequence": 1,
        "retained_history_through_sequence": through_sequence,
        "protected_run_start_sequence": 1,
        "protected_run_through_sequence": through_sequence,
    }
    projection_window = TranscriptProjectionWindowFact(
        **window_payload,
        window_fingerprint=context_fingerprint(
            "transcript-projection-window:v1", window_payload
        ),
    )
    payload = {
        "schema_version": "transcript-input:v1",
        "runtime_session_id": runtime_session.runtime_session_id,
        "through_sequence": through_sequence,
        "current_user_anchor": current.message_id,
        "projection_window": projection_window,
        "messages": (assistant, current),
        "tool_pairs": (),
        "compacted_windows": (),
        "stripped_unfinished_call_ids": (),
        "omitted_non_model_block_ids": (),
    }
    return TranscriptCompileInput(
        **payload,
        transcript_fingerprint=context_fingerprint(
            "transcript-compile-input:v1", payload
        ),
    )


class _BlockingSummaryTransport:
    api = "mock"
    binding_id = "test.mock"
    contract_version = "v1"

    def __init__(self, *, text: str) -> None:
        self.text = text
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, *, call, context, event_context):
        self.started.set()
        await self.release.wait()
        async for event in MockTransport(text=self.text).stream(
            call=call,
            context=context,
            event_context=event_context,
        ):
            yield event


async def _service_window_compaction_fixture(
    tmp_path,
    *,
    invalid_citation: bool,
    blocking: bool = False,
    external_llm_runtime: LLMRuntime | None = None,
):
    config = None
    if external_llm_runtime is None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="mock",
        )
        bootstrap_registry = LLMTransportRegistry()
        bootstrap_registry.register(MockTransport(text="bootstrap"))
        bootstrap_runtime = LLMRuntime(config=config, registry=bootstrap_registry)
    else:
        bootstrap_runtime = external_llm_runtime
    target = bootstrap_runtime.resolve_target(role=ModelRole.PRO)
    runtime_session = in_memory_runtime_session(tmp_path)
    open_test_root_rollout_run(
        runtime_session,
        event_context=CTX,
        model_target=target.fact,
    )
    events = tuple(runtime_session.event_log.iter(run_id=CTX.run_id))
    run_start = next(event for event in events if isinstance(event, RunStartEvent))
    window_open = next(
        event for event in events if isinstance(event, ContextWindowOpenedEvent)
    )
    transcript = _service_window_compaction_transcript(
        runtime_session=runtime_session,
        run_start=run_start,
        window_open=window_open,
    )
    chain = runtime_session.long_horizon_state_store.window_state(CTX.run_id)
    assert chain is not None and chain.active_window_id is not None
    source_window = chain.windows[chain.active_window_id]
    projection = runtime_session.long_horizon_state_store.projection_state(
        source_window.window_id
    )
    assert projection is not None
    prepared_render = prepare_tool_result_render_input(
        units=(),
        transcript=transcript,
        policy_basis=resolve_context_compile_policy(LoopBudget()).tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared_render,
        transcript=transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )
    compaction_id, _ = window_compaction_identity(
        run_id=CTX.run_id,
        source_window=source_window,
        source_projection=projection,
        source_through_sequence=transcript.through_sequence,
        attempt_index=1,
    )
    source = prepare_window_compaction_source_document(
        compaction_id=compaction_id,
        run_id=CTX.run_id,
        window=source_window,
        projection_state=projection,
        transcript=transcript,
        units=(),
        rendered=rendered,
        prepared_rollups=(),
        protection_facts=(),
        source_through_sequence=transcript.through_sequence,
    )
    citation = (
        "source-entry:not-present"
        if invalid_citation
        else source.fact.summarized_entry_ids[0]
    )
    summary_text = json.dumps(
        {
            "observed_facts": ["The prior evidence was retained."],
            "model_inferences": [],
            "unresolved_questions": ["Produce the final answer."],
            "critical_constraints": ["Keep the user request."],
            "artifact_locators": [],
            "cited_source_entry_ids": [citation],
        }
    )
    blocking_transport = None
    if external_llm_runtime is None:
        assert config is not None
        registry = LLMTransportRegistry()
        blocking_transport = (
            _BlockingSummaryTransport(text=summary_text) if blocking else None
        )
        registry.register(blocking_transport or MockTransport(text=summary_text))
        llm_runtime = LLMRuntime(config=config, registry=registry)
    else:
        llm_runtime = external_llm_runtime
    llm_runtime.rebind_target(
        run_start.long_horizon.window_compaction_summarizer_target
    )
    service = ContextWindowCompactionService(
        runtime_session=runtime_session,
        llm_runtime=llm_runtime,
    )
    runtime_session.window_compaction_service = service
    request = WindowCompactionRequest(
        event_context=CTX,
        state=LoopState(
            session_id=runtime_session.runtime_session_id,
            run_id=CTX.run_id,
        ),
        run_contract=run_start.long_horizon,
        source_window=source_window,
        source_projection=projection,
        transcript=transcript,
        tool_result_units=(),
        rendered_tool_results=rendered,
        prepared_rollups=(),
        protection_facts=(),
        source_through_sequence=transcript.through_sequence,
        source_context_fingerprint=context_fingerprint(
            "service-window-source-context:v1", transcript.transcript_fingerprint
        ),
        estimated_tokens_before=20_000,
        non_transcript_baseline_tokens=1_000,
        transcript_tokens_before=4_000,
    )
    return runtime_session, service, request, source_window, blocking_transport


async def _close_window_compaction_fixture(runtime_session, service) -> None:
    deadline = asyncio.get_running_loop().time() + 1
    await service.drain_pending(deadline_monotonic=deadline)
    await runtime_session.context_input_io_service.drain_pending(
        deadline_monotonic=deadline
    )
    await asyncio.sleep(0)
    runtime_session.close()


async def _window_compaction_service_atomically_switches_active_window(
    tmp_path,
) -> None:
    runtime_session, service, request, source_window, _transport = (
        await _service_window_compaction_fixture(tmp_path, invalid_citation=False)
    )
    try:
        outcome = await service.compact(request)
        assert outcome.status == "compacted"
        committed = tuple(runtime_session.event_log.iter(run_id=CTX.run_id))
        completed = next(
            event
            for event in committed
            if isinstance(event, ContextWindowCompactionCompletedEvent)
        )
        terminal_index = committed.index(completed)
        assert isinstance(committed[terminal_index + 1], ContextWindowClosedEvent)
        assert isinstance(committed[terminal_index + 2], ContextWindowOpenedEvent)
        chain = runtime_session.long_horizon_state_store.window_state(CTX.run_id)
        assert chain is not None and chain.active_window_id is not None
        active_window = chain.windows[chain.active_window_id]
        assert active_window.generation == source_window.generation + 1
        assert active_window.previous_window_id == source_window.window_id
        assert runtime_session.archive.get_text(
            completed.summary_artifact_id,
            session_id=runtime_session.runtime_session_id,
        )
        projection = _context_window_projection(
            committed,
            _ArtifactLookupStore(runtime_session),
        )
        assert [item["generation"] for item in projection["windows"]] == [1, 2]
        assert [item["status"] for item in projection["windows"]] == [
            "closed",
            "active",
        ]
        assert projection["compactions"][0]["status"] == "completed"
        assert projection["compactions"][0]["summary_artifact_present"] is True
        assert projection["diagnostics"] == []
    finally:
        await _close_window_compaction_fixture(runtime_session, service)


def test_window_compaction_service_atomically_switches_active_window(tmp_path) -> None:
    asyncio.run(_window_compaction_service_atomically_switches_active_window(tmp_path))


async def _compacted_window_live_authority_uses_bounded_delta(tmp_path) -> None:
    runtime_session, service, request, _source_window, _transport = (
        await _service_window_compaction_fixture(tmp_path, invalid_citation=False)
    )
    try:
        outcome = await service.compact(request)
        assert outcome.status == "compacted"
        start = runtime_session.long_horizon_state_store.run_start(CTX.run_id)
        chain = runtime_session.long_horizon_state_store.window_state(CTX.run_id)
        assert start is not None
        assert chain is not None and chain.active_window_id is not None
        active = chain.windows[chain.active_window_id]
        source_through = (
            active.transcript_basis.source_through_sequence_at_compaction
        )
        assert source_through is not None
        working_set = SimpleNamespace(
            run_start_event_id=start.id,
            run_start_sequence=start.sequence,
            plan_snapshot=SimpleNamespace(
                entered_event_id=None,
                entered_event_sequence=None,
            ),
            effective_exposure_event_ref=None,
            latest_committed_resume_boundary_ref=None,
        )
        authority = await _read_live_primary_event_slice(
            runtime_session=runtime_session,
            working_set=working_set,
        )
        assert authority.primary_slice.from_sequence == source_through + 1
        assert all(
            item.sequence > source_through
            for item in authority.primary_slice.events
        )
        assert isinstance(authority.view, ContextEventAuthorityView)
        assert authority.view.event_by_id(start.id).sequence == start.sequence
        assert all(
            item.event_id != start.id for item in authority.primary_slice.events
        )
    finally:
        await _close_window_compaction_fixture(runtime_session, service)


def test_compacted_window_live_authority_uses_bounded_delta(tmp_path) -> None:
    asyncio.run(_compacted_window_live_authority_uses_bounded_delta(tmp_path))


async def _window_compaction_invalid_citation_keeps_old_window_open(
    tmp_path,
) -> None:
    runtime_session, service, request, source_window, _transport = (
        await _service_window_compaction_fixture(tmp_path, invalid_citation=True)
    )
    try:
        outcome = await service.compact(request)
        assert outcome.status == "failed"
        assert outcome.reason_code == "context_window_compaction_summary_validation_failed"
        chain = runtime_session.long_horizon_state_store.window_state(CTX.run_id)
        assert chain is not None and chain.active_window_id is not None
        assert chain.active_window_id == source_window.window_id
        assert not any(
            isinstance(event, ContextWindowClosedEvent)
            for event in runtime_session.event_log.iter(run_id=CTX.run_id)
        )
    finally:
        await _close_window_compaction_fixture(runtime_session, service)


def test_window_compaction_invalid_citation_keeps_old_window_open(tmp_path) -> None:
    asyncio.run(_window_compaction_invalid_citation_keeps_old_window_open(tmp_path))


def test_window_compaction_terminal_none_retains_owner_until_same_batch_commits(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulsara_agent.runtime.session import EventCommitError

    async def scenario() -> None:
        runtime_session, service, request, _source_window, _transport = (
            await _service_window_compaction_fixture(
                tmp_path,
                invalid_citation=False,
            )
        )
        session_type = type(runtime_session)
        original_write = session_type.write_events
        terminal_attempted = asyncio.Event()
        block_terminal = True

        async def fail_terminal_precommit(self, events, **kwargs):
            if block_terminal and any(
                isinstance(
                    event,
                    (
                        ContextWindowCompactionCompletedEvent,
                        ContextWindowCompactionFailedEvent,
                    ),
                )
                for event in events
            ):
                terminal_attempted.set()
                raise EventCommitError("synthetic window terminal pre-commit failure")
            return await original_write(self, events, **kwargs)

        monkeypatch.setattr(
            session_type,
            "write_events",
            fail_terminal_precommit,
        )
        waiter = asyncio.create_task(service.compact(request))
        try:
            await asyncio.wait_for(terminal_attempted.wait(), timeout=1)
            await asyncio.sleep(0.02)
            assert waiter.done() is False
            assert service.pending_count == 1
            assert not any(
                isinstance(
                    event,
                    (
                        ContextWindowCompactionCompletedEvent,
                        ContextWindowCompactionFailedEvent,
                    ),
                )
                for event in runtime_session.event_log.iter()
            )
            block_terminal = False
            outcome = await asyncio.wait_for(waiter, timeout=1)
            assert outcome.status == "compacted"
        finally:
            block_terminal = False
            if not waiter.done():
                await asyncio.wait_for(waiter, timeout=1)
            await _close_window_compaction_fixture(runtime_session, service)

    asyncio.run(scenario())


def test_window_compaction_source_stale_does_not_write_failure_or_charge_circuit(
    tmp_path,
) -> None:
    async def scenario() -> None:
        runtime_session, service, request, _source_window, _transport = (
            await _service_window_compaction_fixture(
                tmp_path,
                invalid_citation=False,
            )
        )
        try:
            await runtime_session.emit(
                CustomEvent(
                    **CTX.event_fields(),
                    name="unrelated_background_fact",
                    payload={"kind": "unrelated"},
                )
            )
            first = await service.compact(request)
            second = await service.compact(request)
            assert first.status == second.status == "source_stale"
            assert not any(
                isinstance(event, ContextWindowCompactionFailedEvent)
                for event in runtime_session.event_log.iter()
            )
        finally:
            await _close_window_compaction_fixture(runtime_session, service)

    asyncio.run(scenario())


async def _window_compaction_waiter_cancellation_detaches_from_owner(tmp_path) -> None:
    runtime_session, service, request, source_window, transport = (
        await _service_window_compaction_fixture(
            tmp_path,
            invalid_citation=False,
            blocking=True,
        )
    )
    assert transport is not None
    waiter = asyncio.create_task(service.compact(request))
    try:
        await asyncio.wait_for(transport.started.wait(), timeout=1)
        assert service.pending_count == 1
        assert any(
            event.type.value == "CONTEXT_WINDOW_COMPACTION_STARTED"
            for event in runtime_session.event_log.iter(run_id=CTX.run_id)
        )
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert service.pending_count == 1
        with pytest.raises(PendingWindowCompactionError):
            await service.drain_pending(
                deadline_monotonic=asyncio.get_running_loop().time() + 0.001
            )
        transport.release.set()
        await service.drain_pending(
            deadline_monotonic=asyncio.get_running_loop().time() + 1
        )
        chain = runtime_session.long_horizon_state_store.window_state(CTX.run_id)
        assert chain is not None and chain.active_window_id is not None
        assert chain.active_window_id != source_window.window_id
    finally:
        transport.release.set()
        if not waiter.done():
            waiter.cancel()
        await _close_window_compaction_fixture(runtime_session, service)


def test_window_compaction_waiter_cancellation_detaches_from_owner(tmp_path) -> None:
    asyncio.run(_window_compaction_waiter_cancellation_detaches_from_owner(tmp_path))


class _ArtifactLookupStore:
    def __init__(self, runtime_session) -> None:
        self.runtime_session = runtime_session

    def artifact(self, artifact_id: str):
        text = self.runtime_session.archive.get_text(
            artifact_id,
            session_id=self.runtime_session.runtime_session_id,
        )
        return {"id": artifact_id} if text is not None else None


async def _window_compaction_same_run_join_uses_one_owner(tmp_path) -> None:
    runtime_session, service, request, _source_window, transport = (
        await _service_window_compaction_fixture(
            tmp_path,
            invalid_citation=False,
            blocking=True,
        )
    )
    assert transport is not None
    first = asyncio.create_task(service.compact(request))
    second: asyncio.Task | None = None
    try:
        await asyncio.wait_for(transport.started.wait(), timeout=1)
        second = asyncio.create_task(service.compact(request))
        await asyncio.sleep(0)
        assert not second.done()
        assert service.pending_count == 1
        transport.release.set()
        first_outcome, second_outcome = await asyncio.gather(first, second)
        assert first_outcome == second_outcome
        assert sum(
            event.type.value == "CONTEXT_WINDOW_COMPACTION_STARTED"
            for event in runtime_session.event_log.iter(run_id=CTX.run_id)
        ) == 1
    finally:
        transport.release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await _close_window_compaction_fixture(runtime_session, service)


def test_window_compaction_same_run_join_uses_one_owner(tmp_path) -> None:
    asyncio.run(_window_compaction_same_run_join_uses_one_owner(tmp_path))


def test_real_llm_long_horizon_window_compaction_continues_same_run(
    tmp_path,
) -> None:
    import os
    from pathlib import Path

    from pulsara_agent.llm import build_llm_runtime
    from pulsara_agent.settings import PulsaraSettings

    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip(
            "Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider."
        )
    if os.getenv("PULSARA_RUN_LONG_HORIZON_DOGFOOD") != "1":
        pytest.skip(
            "Set PULSARA_RUN_LONG_HORIZON_DOGFOOD=1 to run long-horizon dogfood."
        )
    env_file = Path(".env")
    settings = (
        PulsaraSettings.from_env_file(env_file)
        if env_file.exists()
        else PulsaraSettings.from_env()
    )
    evidence = asyncio.run(
        _real_window_compaction_same_run(
            tmp_path,
            llm_runtime=build_llm_runtime(settings.llm),
        )
    )
    print(
        "\nREAL_LLM_LONG_HORIZON_WINDOW_COMPACTION="
        + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    )
    assert evidence["status"] == "compacted", evidence
    assert evidence["source_run_id"] == evidence["target_run_id"], evidence
    assert evidence["target_generation"] == evidence["source_generation"] + 1
    assert evidence["summary_artifact_id"], evidence


async def _real_window_compaction_same_run(
    tmp_path,
    *,
    llm_runtime: LLMRuntime,
) -> dict[str, object]:
    runtime_session, service, request, source_window, _transport = (
        await _service_window_compaction_fixture(
            tmp_path,
            invalid_citation=False,
            external_llm_runtime=llm_runtime,
        )
    )
    try:
        outcomes = []
        for _attempt in range(2):
            outcome = await service.compact(request)
            outcomes.append(outcome)
            if outcome.status == "compacted":
                break
        committed = tuple(runtime_session.event_log.iter(run_id=CTX.run_id))
        completed = next(
            (
                event
                for event in committed
                if isinstance(event, ContextWindowCompactionCompletedEvent)
            ),
            None,
        )
        failures = tuple(
            event
            for event in committed
            if isinstance(event, ContextWindowCompactionFailedEvent)
        )
        target = outcome.target_window
        return {
            "status": outcome.status,
            "attempt_statuses": [item.status for item in outcomes],
            "attempt_reason_codes": [item.reason_code for item in outcomes],
            "source_run_id": source_window.run_id,
            "target_run_id": target.run_id if target is not None else None,
            "source_generation": source_window.generation,
            "target_generation": target.generation if target is not None else None,
            "summary_artifact_id": (
                completed.summary_artifact_id if completed is not None else None
            ),
            "failure_events": [
                {
                    "failure_stage": event.failure_stage,
                    "reason_code": event.reason_code,
                    "retryable": event.retryable,
                }
                for event in failures
            ],
            "window_open_count": sum(
                isinstance(event, ContextWindowOpenedEvent) for event in committed
            ),
        }
    finally:
        await _close_window_compaction_fixture(runtime_session, service)


async def _window_compaction_restart_repairs_started_without_terminal(tmp_path) -> None:
    runtime_session, service, request, source_window, _transport = (
        await _service_window_compaction_fixture(tmp_path, invalid_citation=False)
    )
    recovery_session = None
    try:
        outcome = await service.compact(request)
        assert outcome.status == "compacted"
        committed = tuple(runtime_session.event_log.iter(run_id=CTX.run_id))
        completed_index = next(
            index
            for index, event in enumerate(committed)
            if isinstance(event, ContextWindowCompactionCompletedEvent)
        )
        interrupted_prefix = committed[:completed_index]
        assert any(
            event.type.value == "ROLLOUT_BUDGET_RESERVATION_SETTLED"
            for event in interrupted_prefix
        )

        interrupted_log = InMemoryEventLog(
            runtime_session_id=runtime_session.runtime_session_id
        )
        interrupted_log.extend(
            event.model_copy(update={"sequence": None})
            for event in interrupted_prefix
        )
        recovery_session = in_memory_runtime_session(
            tmp_path,
            event_log=interrupted_log,
            archive=runtime_session.archive,
            runtime_session_id=runtime_session.runtime_session_id,
        )
        recovery_service = ContextWindowCompactionService(
            runtime_session=recovery_session,
            llm_runtime=service.llm_runtime,
        )
        recovered = await recovery_service.recover_interrupted(
            state=LoopState(
                session_id=recovery_session.runtime_session_id,
                run_id=CTX.run_id,
            )
        )
        assert len(recovered) == 1
        assert recovered[0].reason_code == (
            "context_window_compaction_recovered_interrupted"
        )
        assert recovered[0].source_window_id == source_window.window_id
        assert not any(
            isinstance(event, ContextWindowClosedEvent)
            for event in recovery_session.event_log.iter(run_id=CTX.run_id)
        )
        chain = recovery_session.long_horizon_state_store.window_state(CTX.run_id)
        assert chain is not None
        assert chain.active_window_id == source_window.window_id

        reopened_service = ContextWindowCompactionService(
            runtime_session=recovery_session,
            llm_runtime=service.llm_runtime,
        )
        assert await reopened_service.recover_interrupted() == ()
    finally:
        if recovery_session is not None:
            recovery_session.close()
        await _close_window_compaction_fixture(runtime_session, service)


def test_window_compaction_restart_repairs_started_without_terminal(tmp_path) -> None:
    asyncio.run(_window_compaction_restart_repairs_started_without_terminal(tmp_path))
