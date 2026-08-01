from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

from hashlib import sha256
from time import monotonic

import pytest

from pulsara_agent.event import (
    ContextCompactionCompletedEvent,
    ContextCompactionMemoryExtractionCompletedEvent,
    ContextCompactionMemoryExtractionRequestedEvent,
    ContextCompactionStartedEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    RunEndEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.runtime.projection_jobs.compaction_budget import (
    DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
    build_background_budget_genesis,
    reserve_background_budget,
    settle_background_budget,
)
from pulsara_agent.runtime.projection_jobs.compaction_memory_driver import (
    _EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT,
    _INPUT_ARTIFACT_CONTRACT_FINGERPRINT,
)
from pulsara_agent.projection_jobs.compaction_memory_policy import (
    build_default_compaction_memory_extraction_policy,
)
from pulsara_agent.memory.compaction.extension import (
    build_compaction_memory_extraction_contract,
)
from pulsara_agent.memory.compaction.parser import (
    PARSER_CONTRACT_FINGERPRINT,
    parse_compaction_memory_extraction_output,
)
from pulsara_agent.memory.compaction.result_candidate import (
    build_preference_candidate_attributions,
    lower_extraction_candidate_outbox_rows,
)
from pulsara_agent.memory.governance.evidence import (
    GovernanceSourceEvidenceBuilder,
)
from pulsara_agent.memory.governance.executor import (
    GovernanceDecisionExecutionIdentity,
    MemoryGovernanceExecutor,
)
from pulsara_agent.memory.candidates.pool import (
    InMemoryCandidatePool,
    PostgresCandidatePool,
    SubmitAsIsDecision,
    WriteSucceededOutcome,
)
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.memory.canonical.ledger import CanonicalMemoryLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.graph import InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.compaction import (
    CompactionMemoryExtractionModelInputAttributionFact,
    CompactionPostCompletionExtensionLinkFact,
    CompactionPostCompletionExtensionRequestedFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    CompactionExtractionGovernanceSourceSemanticFact,
    GovernanceEvidenceArtifactReferenceFact,
    GovernanceEvidenceBuildStatus,
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.primitives.long_horizon import (
    calculate_model_call_reservation,
    default_rollout_budget_policy,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.projection_jobs.compaction_memory import (
    CompactionMemoryExtractionOccurrenceAttributionFact,
    CompactionMemoryModelResultAttributionFact,
    CompactionMemoryValidCandidatesResultSemanticFact,
)
from pulsara_agent.settings import StorageConfig
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TranscriptProjectionDocumentRegistry,
    TranscriptProjectionStateStore,
)
from pulsara_agent.runtime.projection_jobs.source import source_event_reference
from tests.support.model_call import (
    compaction_completed_contract_fields,
    compaction_started_contract_fields,
    model_call_end_fields,
    model_call_start_fields,
    test_resolved_call_fact,
)
from tests.support.memory_uow import fake_memory_uow_factory, postgres_memory_uow
from tests.support.event_write import restore_transcript_projection_fixture
from tests.support.postgres import verified_postgres_provider
from tests.test_compaction_memory_extraction_evidence import _select, _world


def _stored_reference(world, event_id: str) -> GovernanceStoredEventReferenceFact:
    raw = world.log.read_raw_events_by_id(
        (event_id,), deadline_monotonic=monotonic() + 10.0
    )
    assert len(raw) == 1
    event = decode_raw_stored_event_envelope(raw[0], DEFAULT_EVENT_SCHEMA_REGISTRY)
    return build_frozen_fact(
        GovernanceStoredEventReferenceFact,
        schema_version="governance_stored_event_reference.v1",
        stable_identity=stable_event_identity(
            event,
            runtime_session_id=world.runtime_session_id,
        ),
        sequence=raw[0].sequence,
        stored_envelope_fingerprint=raw[0].envelope_fingerprint,
    )


@pytest.mark.parametrize(
    "human_text",
    (
        "Remember that I prefer compact, factual progress reports.",
        "Remember this complete preference: " + "structured-detail " * 240,
    ),
    ids=("short-message", "message-over-legacy-2000-char-limit"),
)
@pytest.mark.postgres
def test_compaction_governance_rebinds_complete_sanitized_human_message(
    human_text: str,
) -> None:
    world = _world(
        (("human", human_text),),
        runtime_session_id=context_fingerprint(
            "test-compaction-governance-evidence-runtime:v1", human_text
        ),
    )
    human_start = next(
        event for event in world.log.iter() if isinstance(event, RunStartEvent)
    )
    context = EventContext(
        run_id=human_start.run_id,
        turn_id=human_start.turn_id,
        reply_id=human_start.reply_id,
    )
    compaction_id = "context_compaction:governance-evidence"
    completed_id = "context_compaction_completed:governance-evidence"
    request_id = "context_compaction_memory_extraction_requested:governance-evidence"
    extension_contract_fingerprint = context_fingerprint(
        "test-compaction-memory-extension:v1", "governance-evidence"
    )
    link = build_frozen_fact(
        CompactionPostCompletionExtensionLinkFact,
        schema_version="compaction_post_completion_extension_link.v1",
        compaction_id=compaction_id,
        completed_event_id=completed_id,
        request_event_id=request_id,
        extension_contract_fingerprint=extension_contract_fingerprint,
    )
    disposition = build_frozen_fact(
        CompactionPostCompletionExtensionRequestedFact,
        schema_version="compaction_post_completion_extension_requested.v1",
        disposition_kind="requested",
        extension_link=link,
    )
    started_fields = compaction_started_contract_fields()
    started_fields["terminal_event_id"] = completed_id
    started = ContextCompactionStartedEvent(
        id="context_compaction_started:governance-evidence",
        **context.event_fields(),
        **started_fields,
        compaction_id=compaction_id,
        trigger="manual",
        reason="manual",
        window_number=1,
        window_id="context_window:governance-evidence",
        threshold_tokens=8_000,
        through_sequence=world.authority.ledger_through_sequence,
        keep_after_sequence=world.authority.ledger_through_sequence,
    )
    completed_fields = compaction_completed_contract_fields()
    completed_fields["started_event_id"] = started.id
    completed = ContextCompactionCompletedEvent(
        id=completed_id,
        **context.event_fields(),
        **completed_fields,
        compaction_id=compaction_id,
        trigger="manual",
        reason="manual",
        window_number=1,
        window_id="context_window:governance-evidence",
        summary_artifact_id="artifact:summary:governance-evidence",
        summary_chars=20,
        threshold_tokens=8_000,
        through_sequence=world.authority.ledger_through_sequence,
        keep_after_sequence=world.authority.ledger_through_sequence,
        post_completion_extension_dispositions=(disposition,),
    )
    target = test_resolved_call_fact(
        purpose=ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION
    ).target
    extraction_contract = build_compaction_memory_extraction_contract()
    policy = build_default_compaction_memory_extraction_policy(model_target=target)
    request = ContextCompactionMemoryExtractionRequestedEvent(
        id=request_id,
        **context.event_fields(),
        extension_link=link,
        human_evidence_manifest_reference=world.plan.reference,
        memory_domain_id="memory-domain:governance-evidence",
        resolved_scope="ctx:user",
        extraction_contract=extraction_contract,
        extraction_policy=policy,
        business_occurrence_fingerprint=context_fingerprint(
            "test-compaction-memory-request-occurrence:v1", request_id
        ),
        event_semantic_fingerprint=context_fingerprint(
            "test-compaction-memory-request-semantic:v1",
            world.plan.reference.manifest_semantic_fingerprint,
        ),
    )
    world.log.extend((started, completed, request))
    request_reference = _stored_reference(world, request.id)
    request_raw = world.log.read_raw_events_by_id(
        (request.id,), deadline_monotonic=monotonic() + 10.0
    )[0]
    durable_source = source_event_reference(request_raw)
    selected, exact_reads = _select(
        world,
        request_reference_override=request_reference,
        durable_source_reference_override=durable_source,
        extension_link_override=link,
    )
    assert exact_reads == 1
    assert selected.ordered_nodes[0].semantic.sanitized_full_message_text == human_text

    input_bytes = canonical_json_bytes(selected.document.model_dump(mode="json"))
    input_digest = sha256(input_bytes).hexdigest()
    input_artifact_id = f"compaction-memory-extraction-input:{input_digest}"
    world.archive.put_text_if_absent_or_confirm_identical(
        input_artifact_id,
        input_bytes.decode("utf-8"),
        session_id=world.runtime_session_id,
        run_id=None,
        media_type="application/json",
        semantic_metadata={
            "input_document_fingerprint": selected.document.document_fingerprint
        },
        deadline_monotonic=monotonic() + 10.0,
    )
    input_reference = build_frozen_fact(
        GovernanceEvidenceArtifactReferenceFact,
        schema_version="governance_evidence_artifact_reference.v1",
        artifact_kind="compaction_memory_extraction_input",
        artifact_id=input_artifact_id,
        media_type="application/json",
        content_sha256=input_digest,
        content_bytes=len(input_bytes),
        artifact_contract_id="pulsara.compaction-memory-extraction.input",
        artifact_contract_version="1",
        artifact_contract_fingerprint=_INPUT_ARTIFACT_CONTRACT_FINGERPRINT,
    )

    call = test_resolved_call_fact(
        purpose=ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION
    )
    quote = calculate_model_call_reservation(
        target=call.target,
        resolved_model_call_id=call.resolved_model_call_id,
        policy=default_rollout_budget_policy(),
    )
    account = build_background_budget_genesis(
        runtime_session_id=world.runtime_session_id
    )
    reserve = reserve_background_budget(
        account=account,
        policy=DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
        reservation_id="background-reservation:governance-evidence",
        extraction_job_id="projection-job:evidence",
        operation_id="operation:governance-evidence",
        dispatch_attempt_ordinal=1,
        quote=quote,
    )
    assert reserve.reservation is not None and reserve.failure is None
    reservation = reserve.reservation
    model_input_attribution = build_frozen_fact(
        CompactionMemoryExtractionModelInputAttributionFact,
        schema_version="compaction_memory_extraction_model_input_attribution.v1",
        extraction_job_id="projection-job:evidence",
        dispatch_attempt_ordinal=1,
        request_event_reference=request_reference,
        input_artifact_reference=input_reference,
        input_semantic_fingerprint=selected.document.semantic.input_semantic_fingerprint,
        input_document_fingerprint=selected.document.document_fingerprint,
        resolved_input_budget_attribution_fingerprint=(
            selected.document.attribution.resolved_input_budget_attribution.attribution_fingerprint
        ),
        background_budget_reservation=reservation,
        extraction_contract_fingerprint=extraction_contract.contract_fingerprint,
    )
    start_fields = model_call_start_fields(
        event_id="model_call_start:governance-evidence",
        context_id="context:governance-evidence",
        model_call_index=None,
        resolved_call=call,
        lifecycle_kind="direct_internal_call",
    )
    model_start = ModelCallStartEvent(
        **context.event_fields(),
        **start_fields,
        compaction_memory_extraction_input_attribution=model_input_attribution,
    )
    world.log.append(model_start)
    model_end = ModelCallEndEvent(
        id=model_start.recovery_plan.stable_model_call_end_event_id,
        **context.event_fields(),
        **model_call_end_fields(
            input_tokens=32,
            output_tokens=16,
            resolved_call=call,
        ),
    )
    world.log.append(model_end)
    settled = settle_background_budget(
        account=reserve.account,
        policy=DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
        reservation=reservation,
        model_end=model_end,
    )
    assert settled.settlement is not None

    parsed = parse_compaction_memory_extraction_output(
        '{"schema_version":"compaction_memory_extraction_output.v1",'
        '"candidates":[{"kind":"Preference",'
        '"statement":"User prefers compact, factual progress reports.",'
        f'"evidence_node_ids":["{selected.ordered_nodes[0].evidence_node_id}"]}},'
        '{"kind":"Preference",'
        '"statement":"User prefers final answers without decorative emoji.",'
        f'"evidence_node_ids":["{selected.ordered_nodes[0].evidence_node_id}"]}}]}}',
        allowed_evidence_node_ids=(selected.ordered_nodes[0].evidence_node_id,),
    )
    candidate_attributions = build_preference_candidate_attributions(
        parsed=parsed,
        nodes=selected.ordered_nodes,
        scope=request.resolved_scope,
        job_id="projection-job:evidence",
        request_event_id=request.id,
        extraction_contract_fingerprint=extraction_contract.contract_fingerprint,
        created_at_utc=model_end.created_at,
    )
    result_semantic = build_frozen_fact(
        CompactionMemoryValidCandidatesResultSemanticFact,
        schema_version="compaction_memory_valid_candidates_result_semantic.v1",
        outcome_kind="valid_candidates",
        input_semantic_fingerprint=selected.document.semantic.input_semantic_fingerprint,
        evidence_set_semantic_fingerprint=(
            selected.document.semantic.evidence_set.evidence_set_semantic_fingerprint
        ),
        terminal_projection_semantic_fingerprint=(
            model_end.terminal_projection.projection_reference.semantic_join.semantic_fingerprint
        ),
        parser_contract_fingerprint=PARSER_CONTRACT_FINGERPRINT,
        extraction_semantic_contract_fingerprint=(
            _EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT
        ),
        ordered_candidate_semantic_fingerprints=tuple(
            sorted(
                item.candidate_payload.candidate_semantic_fingerprint
                for item in candidate_attributions
            )
        ),
    )
    outcome_attribution = build_frozen_fact(
        CompactionMemoryModelResultAttributionFact,
        schema_version="compaction_memory_model_result_attribution.v1",
        outcome_kind="valid_candidates",
        input_artifact_reference=input_reference,
        input_semantic_fingerprint=selected.document.semantic.input_semantic_fingerprint,
        resolved_input_budget=(
            selected.document.attribution.resolved_input_budget_attribution
        ),
        model_call_start_event_reference=_stored_reference(world, model_start.id),
        model_call_end_event_reference=_stored_reference(world, model_end.id),
        model_terminal_projection_reference=(
            model_end.terminal_projection.projection_reference
        ),
        parsed_output_semantic_fingerprint=parsed.semantic_fingerprint,
        dispatch_attempt_ordinal=1,
        background_budget_reservation=reservation,
        background_budget_settlement=settled.settlement,
    )
    occurrence = build_frozen_fact(
        CompactionMemoryExtractionOccurrenceAttributionFact,
        schema_version="compaction_memory_extraction_occurrence_attribution.v1",
        compaction_id=compaction_id,
        extension_link=link,
        request_event_reference=request_reference,
        durable_job_id="projection-job:evidence",
        durable_job_source_reference=durable_source,
        human_evidence_manifest_reference=world.plan.reference,
        outcome_attribution=outcome_attribution,
    )
    result = ContextCompactionMemoryExtractionCompletedEvent(
        id="context_compaction_memory_extraction_completed:governance-evidence",
        created_at=model_end.created_at,
        **context.event_fields(),
        result_semantic=result_semantic,
        occurrence_attribution=occurrence,
        ordered_candidate_attributions=candidate_attributions,
    )
    world.log.append(result)
    candidates = tuple(
        row.candidate
        for row in lower_extraction_candidate_outbox_rows(
            runtime_session_id=world.runtime_session_id,
            event=result,
        )
    )
    candidate, second_candidate = candidates

    reducer = TranscriptProjectionStateStore(
        runtime_session_id=world.runtime_session_id,
        documents=TranscriptProjectionDocumentRegistry(),
    )
    restore_transcript_projection_fixture(event_log=world.log, reducer=reducer)
    evidence_builder = GovernanceSourceEvidenceBuilder(
        runtime_session_id=world.runtime_session_id,
        event_log=world.log,
        archive=world.archive,
    )
    authority = reducer.capture_governance_authority_snapshot()
    preparation = evidence_builder.prepare(candidate=candidate, authority=authority)
    second_preparation = evidence_builder.prepare(
        candidate=second_candidate,
        authority=authority,
    )

    assert preparation.result.status is GovernanceEvidenceBuildStatus.FULL, (
        preparation.result.stable_reason_code,
        preparation.rejection.stable_reason_code
        if preparation.rejection is not None
        else None,
    )
    assert preparation.candidate_snapshot is not None
    assert second_preparation.result.status is GovernanceEvidenceBuildStatus.FULL
    assert second_preparation.candidate_snapshot is not None
    semantic = preparation.candidate_snapshot.source_evidence_semantic
    assert isinstance(semantic, CompactionExtractionGovernanceSourceSemanticFact)
    assert tuple(item.text for item in semantic.ordered_evidence_semantics) == (
        human_text,
    )
    prompt = preparation.candidate_snapshot.prompt_projection.model_visible_payload
    assert tuple(item.text for item in prompt.ordered_evidence_texts) == (human_text,)
    assert tuple(item.field_code for item in prompt.ordered_evidence_texts) == (
        "canonical_sanitized_user_message",
    )
    quote_attribution = preparation.candidate_snapshot.source_evidence_attribution.quoted_evidence_attributions[
        0
    ]
    assert quote_attribution.start_char is None
    assert quote_attribution.end_char is None

    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    for item in candidates:
        pool.append_candidate(item)
    write_service = MemoryWriteService(
        CanonicalMemoryLedger(
            graph=graph,
            gate=MemoryWriteGate(),
            graph_id="graph:governance-evidence",
        )
    )
    executor = MemoryGovernanceExecutor(
        candidate_pool=pool,
        memory_write_service=write_service,
        event_log=world.log,
        event_commit_port=world.log.extend,
        graph=graph,
        graph_id="graph:governance-evidence",
        runtime_session_id=world.runtime_session_id,
        memory_write_uow_factory=fake_memory_uow_factory(
            graph=graph,
            candidate_pool=pool,
            memory_write_service=write_service,
            graph_id="graph:governance-evidence",
        ),
        allowed_write_scopes=frozenset({request.resolved_scope}),
    )
    applied = executor.apply_decision(
        SubmitAsIsDecision(
            target_entry_id=candidate.entry_id,
            reason="verified compaction evidence",
        ),
        governance_batch_id="governance:compaction-evidence",
        candidate_snapshots={candidate.entry_id: preparation.candidate_snapshot},
        execution_identity=GovernanceDecisionExecutionIdentity(
            batch_input_fingerprint="sha256:compaction-evidence-batch",
            batch_input_reference_fingerprint="sha256:compaction-evidence-reference",
            governance_model_call_id="model_call:compaction-evidence-governance",
            decision_index=0,
            allowed_candidate_entry_ids=frozenset({candidate.entry_id}),
            allowed_scopes=frozenset({request.resolved_scope}),
        ),
    )
    assert isinstance(applied.decision_record.write_outcome, WriteSucceededOutcome)
    assert graph.has_jsonld(
        selected.ordered_nodes[0].evidence_node_id,
        graph_id="graph:governance-evidence",
    )
    second_applied = executor.apply_decision(
        SubmitAsIsDecision(
            target_entry_id=second_candidate.entry_id,
            reason="reuse verified compaction evidence",
        ),
        governance_batch_id="governance:compaction-evidence-second",
        candidate_snapshots={
            second_candidate.entry_id: second_preparation.candidate_snapshot
        },
        execution_identity=GovernanceDecisionExecutionIdentity(
            batch_input_fingerprint="sha256:compaction-evidence-batch-second",
            batch_input_reference_fingerprint=(
                "sha256:compaction-evidence-reference-second"
            ),
            governance_model_call_id="model_call:compaction-evidence-governance-second",
            decision_index=1,
            allowed_candidate_entry_ids=frozenset({second_candidate.entry_id}),
            allowed_scopes=frozenset({request.resolved_scope}),
        ),
    )
    assert isinstance(
        second_applied.decision_record.write_outcome,
        WriteSucceededOutcome,
    )

    dsn = StorageConfig.from_env().postgres_dsn
    provider = verified_postgres_provider(dsn)
    postgres_graph_id = context_fingerprint(
        "test-compaction-governance-evidence-graph:v1", human_text
    )
    postgres_graph = PostgresGraphStore(connection_provider=provider)
    postgres_pool = PostgresCandidatePool(connection_provider=provider)
    postgres_event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id=world.runtime_session_id,
    )
    postgres_event_log.ensure_runtime_session_owner()
    source_run_events = tuple(
        event.model_copy(update={"sequence": None})
        for event in world.log.iter(run_id=human_start.run_id)
        if isinstance(event, (RunStartEvent, RunEndEvent))
    )
    postgres_event_log.extend(source_run_events)
    for item in candidates:
        postgres_pool.append_candidate(item)
    postgres_executor = MemoryGovernanceExecutor(
        candidate_pool=postgres_pool,
        memory_write_service=MemoryWriteService(
            CanonicalMemoryLedger(
                graph=postgres_graph,
                gate=MemoryWriteGate(),
                graph_id=postgres_graph_id,
            )
        ),
        event_log=world.log,
        event_commit_port=world.log.extend,
        graph=postgres_graph,
        graph_id=postgres_graph_id,
        runtime_session_id=world.runtime_session_id,
        memory_write_uow_factory=lambda: postgres_memory_uow(
            connection_provider=provider,
            runtime_session_id=world.runtime_session_id,
            archive=PostgresArtifactStore(connection_provider=provider),
            graph_id=postgres_graph_id,
        ),
        allowed_write_scopes=frozenset({request.resolved_scope}),
    )
    postgres_applied = postgres_executor.apply_decision(
        SubmitAsIsDecision(
            target_entry_id=candidate.entry_id,
            reason="verified compaction evidence in PostgreSQL",
        ),
        governance_batch_id=context_fingerprint(
            "test-compaction-governance-evidence-batch:v1", human_text
        ),
        candidate_snapshots={candidate.entry_id: preparation.candidate_snapshot},
        execution_identity=GovernanceDecisionExecutionIdentity(
            batch_input_fingerprint=context_fingerprint(
                "test-compaction-governance-evidence-input:v1", human_text
            ),
            batch_input_reference_fingerprint=context_fingerprint(
                "test-compaction-governance-evidence-reference:v1", human_text
            ),
            governance_model_call_id=context_fingerprint(
                "test-compaction-governance-evidence-call:v1", human_text
            ),
            decision_index=0,
            allowed_candidate_entry_ids=frozenset({candidate.entry_id}),
            allowed_scopes=frozenset({request.resolved_scope}),
        ),
    )
    assert isinstance(
        postgres_applied.decision_record.write_outcome,
        WriteSucceededOutcome,
    )
    assert postgres_graph.has_jsonld(
        selected.ordered_nodes[0].evidence_node_id,
        graph_id=postgres_graph_id,
    )
    assert postgres_graph.has_jsonld(
        postgres_applied.decision_record.write_outcome.memory_id,
        graph_id=postgres_graph_id,
    )
    postgres_second_applied = postgres_executor.apply_decision(
        SubmitAsIsDecision(
            target_entry_id=second_candidate.entry_id,
            reason="reuse verified compaction evidence in PostgreSQL",
        ),
        governance_batch_id=context_fingerprint(
            "test-compaction-governance-evidence-second-batch:v1", human_text
        ),
        candidate_snapshots={
            second_candidate.entry_id: second_preparation.candidate_snapshot
        },
        execution_identity=GovernanceDecisionExecutionIdentity(
            batch_input_fingerprint=context_fingerprint(
                "test-compaction-governance-evidence-second-input:v1", human_text
            ),
            batch_input_reference_fingerprint=context_fingerprint(
                "test-compaction-governance-evidence-second-reference:v1", human_text
            ),
            governance_model_call_id=context_fingerprint(
                "test-compaction-governance-evidence-second-call:v1", human_text
            ),
            decision_index=1,
            allowed_candidate_entry_ids=frozenset({second_candidate.entry_id}),
            allowed_scopes=frozenset({request.resolved_scope}),
        ),
    )
    assert isinstance(
        postgres_second_applied.decision_record.write_outcome,
        WriteSucceededOutcome,
    )
    assert postgres_graph.has_jsonld(
        postgres_second_applied.decision_record.write_outcome.memory_id,
        graph_id=postgres_graph_id,
    )
