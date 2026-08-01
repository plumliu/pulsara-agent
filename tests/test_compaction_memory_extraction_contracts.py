from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

import pickle
from dataclasses import dataclass
from time import monotonic

import pytest
from pydantic import ValidationError

from pulsara_agent.event import EventContext, RunEndEvent, RunStartEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.candidates.main_agent_builder import (
    build_main_agent_memory_candidate_payload,
)
from pulsara_agent.memory.governance.dedupe import candidate_fingerprint
from pulsara_agent.memory.compaction.extension import (
    build_compaction_memory_extraction_contract,
)
from pulsara_agent.memory.compaction.result_candidate import (
    build_preference_candidate_attributions,
)
from pulsara_agent.runtime.projection_jobs.compaction_budget import (
    resolve_extraction_input_budget,
)
from pulsara_agent.memory.compaction.evidence import (
    ExactHumanEvidenceSource,
    select_compaction_memory_extraction_input,
)
from pulsara_agent.memory.compaction.manifest import (
    build_human_evidence_manifest_plan,
)
from pulsara_agent.memory.compaction.parser import (
    CompactionMemoryExtractionOutputError,
    parse_compaction_memory_extraction_output,
)
from pulsara_agent.memory.compaction.sanitizer import (
    sanitize_compaction_evidence,
)
from pulsara_agent.ports.compaction_extensions import (
    CompactionPostCompletionExtensionPrivateHandleIdentity,
    PreparedCompactionPostCompletionExtensionIntent,
    PreparedCompactionPostCompletionExtensionIntentIdentity,
)
from pulsara_agent.primitives._context_base import context_fingerprint, thaw_json
from pulsara_agent.primitives.compaction import (
    CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT,
    DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
    EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT,
    CompactionMemoryEvidenceAttributionFact,
    CompactionMemoryExtractionInputAttributionFact,
    CompactionMemoryExtractionInputDocumentFact,
    CompactionHumanEvidenceManifestPageFact,
    CompactionPostCompletionExtensionContractFact,
    CompactionPostCompletionExtensionLinkFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.memory_candidate import (
    PreferenceCandidate,
    ValidCandidatePayload,
)
from pulsara_agent.primitives.governance_evidence import (
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.projection_jobs.compaction_memory import (
    CandidateOutboxPlanFact,
    CandidateOutboxPlanItemFact,
    CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact,
    CompactionMemoryNoEligibleEvidenceResultSemanticFact,
)
from pulsara_agent.projection_jobs.compaction_memory_policy import (
    build_default_compaction_memory_extraction_policy,
    compaction_memory_delivery_policy_from_request,
    compaction_memory_retry_delay_seconds,
)
from pulsara_agent.projection_jobs.contracts import DurableProjectionKind
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TranscriptProjectionDocumentRegistry,
    TranscriptProjectionStateStore,
)
from pulsara_agent.runtime.projection_jobs.source import source_event_reference
from pulsara_agent.runtime.projection_jobs.registry import (
    DURABLE_PROJECTION_TRIGGER_REGISTRY,
)
from tests.conftest import run_end_contract_fields, run_start_permission_fields
from tests.support.event_write import restore_transcript_projection_fixture
from tests.support.model_call import test_model_limits, test_resolved_target_fact


def _runtime_fact(cls, fingerprint_field: str, domain: str, **payload):
    payload[fingerprint_field] = context_fingerprint(domain, payload)
    return cls(**payload)


def _selected_input(
    texts: tuple[str, ...],
    *,
    total_context_tokens: int = 32_000,
):
    runtime_session_id = "runtime:compaction-memory-contracts"
    log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    committed = []
    for index, text in enumerate(texts):
        context = EventContext(
            run_id=f"run:evidence:{index}",
            turn_id=f"turn:evidence:{index}",
            reply_id=f"reply:evidence:{index}",
        )
        committed.extend(
            log.extend(
                (
                    RunStartEvent(
                        **context.event_fields(),
                        **run_start_permission_fields(
                            context.run_id,
                            user_input=text,
                        ),
                        user_input_chars=len(text),
                        metadata={"user_input": text},
                    ),
                    RunEndEvent(
                        **context.event_fields(),
                        **run_end_contract_fields(
                            context.run_id,
                            status="finished",
                        ),
                        status="finished",
                        stop_reason="final",
                    ),
                )
            )
        )
    reducer = TranscriptProjectionStateStore(
        runtime_session_id=runtime_session_id,
        documents=TranscriptProjectionDocumentRegistry(),
    )
    restore_transcript_projection_fixture(event_log=log, reducer=reducer)
    authority = reducer.capture_governance_authority_snapshot()
    through = authority.ledger_through_sequence
    plan = build_human_evidence_manifest_plan(
        runtime_session_id=runtime_session_id,
        authority_snapshot=authority,
        previous_keep_after_sequence=0,
        current_keep_after_sequence=through,
        current_through_sequence=through,
        predecessor_completed_event_id=None,
        event_lookup=log.get_by_id,
    )
    archive = InMemoryArchiveStore()
    for artifact in plan.artifacts:
        archive.put_text_if_absent_or_confirm_identical(
            artifact.reference.artifact_id,
            artifact.content,
            session_id=runtime_session_id,
            run_id=None,
            media_type=artifact.reference.media_type,
            semantic_metadata=thaw_json(artifact.semantic_metadata),
            deadline_monotonic=monotonic() + 10.0,
        )

    raw_by_id = {
        envelope.event_id: envelope
        for envelope in log.read_raw_events_by_id(
            tuple(event.id for event in committed),
            deadline_monotonic=monotonic() + 10.0,
        )
    }

    def resolve(reference):
        envelope = raw_by_id[reference.event_id]
        event = decode_raw_stored_event_envelope(
            envelope, DEFAULT_EVENT_SCHEMA_REGISTRY
        )
        assert isinstance(event, RunStartEvent)
        stored = build_frozen_fact(
            GovernanceStoredEventReferenceFact,
            schema_version="governance_stored_event_reference.v1",
            stable_identity=stable_event_identity(
                event,
                runtime_session_id=runtime_session_id,
            ),
            sequence=envelope.sequence,
            stored_envelope_fingerprint=envelope.envelope_fingerprint,
        )
        return ExactHumanEvidenceSource(event=event, stored_reference=stored)

    first = next(
        envelope
        for envelope in raw_by_id.values()
        if envelope.event_type == "RUN_START"
    )
    request_reference = resolve(
        next(
            ref
            for entry in authority.reducer_evidence_snapshot.stable_entries
            for ref in entry.source_event_refs
            if ref.event_id == first.event_id
        )
    ).stored_reference
    target = test_resolved_target_fact(
        limits=test_model_limits(
            total_context_tokens=total_context_tokens,
            max_input_tokens=total_context_tokens,
            max_output_tokens=1_000,
            default_output_tokens=512,
            input_safety_margin_tokens=128,
        )
    )
    budget = resolve_extraction_input_budget(
        target=target,
        static_prompt_tokens=32,
    ).budget
    link = build_frozen_fact(
        CompactionPostCompletionExtensionLinkFact,
        schema_version="compaction_post_completion_extension_link.v1",
        compaction_id="compaction:contracts",
        completed_event_id="compaction-completed:contracts",
        request_event_id="compaction-request:contracts",
        extension_contract_fingerprint=context_fingerprint(
            "test-extension-contract:v1", "contracts"
        ),
    )
    selected = select_compaction_memory_extraction_input(
        runtime_session_id=runtime_session_id,
        compaction_id=link.compaction_id,
        extension_link=link,
        request_event_reference=request_reference,
        durable_job_id="projection-job:contracts",
        durable_job_source_reference=source_event_reference(first),
        manifest_reference=plan.reference,
        archive=archive,
        exact_source_resolver=resolve,
        resolved_budget=budget,
        token_estimator=lambda value: max(1, len(value.encode("utf-8")) // 4),
        prompt_contract_fingerprint=context_fingerprint(
            "test-extraction-prompt:v1", "contracts"
        ),
        extraction_contract_fingerprint=context_fingerprint(
            "test-extraction-contract:v1", "contracts"
        ),
        deadline_monotonic=monotonic() + 10.0,
    )
    return plan, selected


@pytest.mark.parametrize(
    "payload",
    (
        '{"schema_version":"compaction_memory_extraction_output.v1",'
        '"schema_version":"compaction_memory_extraction_output.v1","candidates":[]}',
        '{"schema_version":"compaction_memory_extraction_output.v1",'
        '"candidates":[],"extra":true}',
        '{"schema_version":"compaction_memory_extraction_output.v1",'
        '"candidates":[{"kind":"Preference","statement":NaN,'
        '"evidence_node_ids":["e:1"]}]}',
        'Here is the result: {"schema_version":'
        '"compaction_memory_extraction_output.v1","candidates":[]}',
    ),
)
def test_extraction_parser_rejects_noncanonical_json(payload: str) -> None:
    with pytest.raises(CompactionMemoryExtractionOutputError):
        parse_compaction_memory_extraction_output(
            payload,
            allowed_evidence_node_ids=("e:1",),
        )


def test_extraction_parser_accepts_empty_and_rejects_bad_evidence() -> None:
    parsed = parse_compaction_memory_extraction_output(
        '{"schema_version":"compaction_memory_extraction_output.v1","candidates":[]}',
        allowed_evidence_node_ids=("e:1", "e:2"),
    )
    assert parsed.output.candidates == ()

    for refs in (("missing",), ("e:1", "e:1"), ("e:2", "e:1")):
        payload = (
            '{"schema_version":"compaction_memory_extraction_output.v1",'
            '"candidates":[{"kind":"Preference","statement":"Use uv",'
            f'"evidence_node_ids":{list(refs)!r}'
            "}]}"
        ).replace("'", '"')
        with pytest.raises(CompactionMemoryExtractionOutputError):
            parse_compaction_memory_extraction_output(
                payload,
                allowed_evidence_node_ids=("e:1", "e:2"),
            )


def test_request_policy_is_the_extraction_execution_authority() -> None:
    policy = build_default_compaction_memory_extraction_policy(
        model_target=test_resolved_target_fact()
    )
    delivery = compaction_memory_delivery_policy_from_request(policy)
    seed = DURABLE_PROJECTION_TRIGGER_REGISTRY.resolve(
        DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION
    )

    assert policy.maximum_attempts == 3
    assert delivery.retry_policy.maximum_attempts == policy.maximum_attempts
    assert delivery.retry_policy.policy_fingerprint == (policy.retry_policy_fingerprint)
    assert delivery.physical_policy.maximum_physical_attempt_seconds == (
        policy.provider_timeout_seconds
    )
    assert delivery.retry_policy.lease_duration_seconds == (
        policy.lease_duration_seconds
    )
    assert policy.input_budget_policy_fingerprint == (
        EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT
    )
    assert policy.background_work_budget_policy_fingerprint == (
        DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY.policy_fingerprint
    )
    assert seed.delivery_policy == delivery


def test_candidate_semantic_identity_is_shared_across_producers() -> None:
    statement = "Keep progress reports concise."
    scope = "ctx:user"
    main_payload = build_main_agent_memory_candidate_payload(
        runtime_session_id="runtime:shared-candidate-semantic",
        tool_call_id="tool-call:shared-candidate-semantic",
        tool_name="remember_preference",
        arguments={
            "statement": statement,
            "scope": scope,
            "source_authority": "conversation_evidence",
            "verification_status": "inferred",
        },
    )
    assert isinstance(main_payload, ValidCandidatePayload)
    reflection = PreferenceCandidate(
        candidate_id="candidate:reflection:shared-semantic",
        statement=statement,
        scope=scope,
        source_authority="conversation_evidence",
        verification_status="inferred",
    )
    _plan, selected = _selected_input(("I prefer concise progress reports.",))
    node_id = selected.ordered_nodes[0].evidence_node_id
    parsed = parse_compaction_memory_extraction_output(
        '{"schema_version":"compaction_memory_extraction_output.v1",'
        '"candidates":[{"kind":"Preference",'
        f'"statement":"{statement}","evidence_node_ids":["{node_id}"]}}]}}',
        allowed_evidence_node_ids=(node_id,),
    )
    extraction_contract = build_compaction_memory_extraction_contract()
    compaction = build_preference_candidate_attributions(
        parsed=parsed,
        nodes=selected.ordered_nodes,
        scope=scope,
        job_id="projection-job:shared-candidate-semantic",
        request_event_id="extraction-request:shared-candidate-semantic",
        extraction_contract_fingerprint=extraction_contract.contract_fingerprint,
        created_at_utc="2026-07-28T00:00:00Z",
    )[0]

    main = main_payload.candidate
    shared = compaction.candidate_payload.candidate_semantic
    assert main.candidate_id != reflection.candidate_id
    assert compaction.candidate_payload.candidate_id not in {
        main.candidate_id,
        reflection.candidate_id,
    }
    assert candidate_fingerprint(main) == candidate_fingerprint(reflection)
    assert candidate_fingerprint(main) == shared.semantic_fingerprint


def test_shared_candidate_semantic_preserves_case_and_internal_whitespace() -> None:
    left = PreferenceCandidate(
        candidate_id="candidate:left",
        statement="Likes  Tea",
        scope="ctx:user",
        source_authority="conversation_evidence",
        verification_status="inferred",
    )
    right = PreferenceCandidate(
        candidate_id="candidate:right",
        statement=" likes tea ",
        scope="ctx:user",
        source_authority="conversation_evidence",
        verification_status="inferred",
    )

    assert candidate_fingerprint(left) != candidate_fingerprint(right)


def test_compaction_memory_retry_schedule_is_closed_and_jitter_free() -> None:
    policy = build_default_compaction_memory_extraction_policy(
        model_target=test_resolved_target_fact()
    )
    delivery = compaction_memory_delivery_policy_from_request(policy)

    assert tuple(
        compaction_memory_retry_delay_seconds(
            delivery,
            dispatch_attempt_ordinal=ordinal,
        )
        for ordinal in (1, 2, 3)
    ) == (1.0, 2.0, 4.0)


def test_sanitizer_handles_overlapping_secret_rules_without_leaking() -> None:
    first = sanitize_compaction_evidence("API_KEY=sk-abcdefghijk")
    second = sanitize_compaction_evidence("API_KEY=sk-zyxwvutsrqp")

    assert first.text == second.text == "[REDACTED:credential-assignment]"
    assert "abcdefghijk" not in first.text
    assert first.text_sha256 == second.text_sha256
    assert first.audits[0].sanitizer_rule_id == "credential-assignment"


def test_evidence_semantic_excludes_original_secret_and_occurrence() -> None:
    _plan_a, selected_a = _selected_input(("API_KEY=sk-abcdefghijk",))
    _plan_b, selected_b = _selected_input(("API_KEY=sk-zyxwvutsrqp",))

    node_a = selected_a.ordered_nodes[0]
    node_b = selected_b.ordered_nodes[0]
    assert node_a.semantic.evidence_semantic_fingerprint == (
        node_b.semantic.evidence_semantic_fingerprint
    )
    assert node_a.attribution.attribution_fingerprint != (
        node_b.attribution.attribution_fingerprint
    )
    assert "sk-" not in selected_a.canonical_input_utf8


def test_input_semantic_ignores_resolved_target_when_selection_is_equal() -> None:
    _plan_a, selected_a = _selected_input(
        ("I prefer uv for Python environments.",),
        total_context_tokens=32_000,
    )
    _plan_b, selected_b = _selected_input(
        ("I prefer uv for Python environments.",),
        total_context_tokens=64_000,
    )

    assert selected_a.document.semantic.input_semantic_fingerprint == (
        selected_b.document.semantic.input_semantic_fingerprint
    )
    assert selected_a.document.attribution.attribution_fingerprint != (
        selected_b.document.attribution.attribution_fingerprint
    )


def test_oversized_message_is_omitted_whole_without_head_tail() -> None:
    _plan, selected = _selected_input(("ordinary words " * 700,))

    assert selected.ordered_nodes == ()
    assert selected.source_eligible_leaf_count == 1
    assert selected.permanent_omission_count == 1
    assert "head_tail" not in selected.canonical_input_utf8


def test_no_call_results_require_canonical_empty_evidence() -> None:
    common = {
        "evidence_set_semantic_fingerprint": (
            CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT
        ),
        "extraction_semantic_contract_fingerprint": "sha256:extraction",
    }
    build_frozen_fact(
        CompactionMemoryNoEligibleEvidenceResultSemanticFact,
        schema_version="compaction_memory_no_eligible_result_semantic.v1",
        outcome_kind="no_eligible_evidence",
        **common,
    )
    build_frozen_fact(
        CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact,
        schema_version=(
            "compaction_memory_input_budget_unsatisfiable_result_semantic.v1"
        ),
        outcome_kind="input_budget_unsatisfiable",
        failure_kind="no_complete_evidence_message_fits",
        budget_selection_contract_fingerprint="sha256:budget",
        **common,
    )
    with pytest.raises(ValidationError, match="canonical empty"):
        build_frozen_fact(
            CompactionMemoryNoEligibleEvidenceResultSemanticFact,
            schema_version="compaction_memory_no_eligible_result_semantic.v1",
            outcome_kind="no_eligible_evidence",
            evidence_set_semantic_fingerprint="sha256:not-empty",
            extraction_semantic_contract_fingerprint="sha256:extraction",
        )


def test_evidence_and_outbox_accumulators_reject_caller_drift() -> None:
    plan, selected = _selected_input(("Remember that I prefer concise reports.",))
    page = plan.pages[0]
    with pytest.raises(ValidationError, match="accumulator"):
        build_frozen_fact(
            CompactionHumanEvidenceManifestPageFact,
            schema_version="compaction_human_evidence_manifest_page.v1",
            page_index=page.page_index,
            ordered_leaf_semantics=page.ordered_leaf_semantics,
            ordered_leaf_attributions=page.ordered_leaf_attributions,
            ordered_selection_projections=page.ordered_selection_projections,
            first_source_sequence=page.first_source_sequence,
            last_source_sequence=page.last_source_sequence,
            semantic_accumulator="sha256:wrong",
            attribution_accumulator=page.attribution_accumulator,
            selection_projection_accumulator=page.selection_projection_accumulator,
        )
    with pytest.raises(ValidationError, match="accumulator"):
        build_frozen_fact(
            type(selected.document.semantic.evidence_set),
            schema_version="compaction_memory_evidence_set_semantic.v1",
            ordered_evidence_semantics=(
                selected.document.semantic.evidence_set.ordered_evidence_semantics
            ),
            ordered_input_projection_fingerprints=(
                selected.document.semantic.evidence_set.ordered_input_projection_fingerprints
            ),
            evidence_count=selected.document.semantic.evidence_set.evidence_count,
            ordered_evidence_semantic_accumulator="sha256:wrong",
            selection_contract_fingerprint=(
                selected.document.semantic.evidence_set.selection_contract_fingerprint
            ),
            sanitizer_contract_fingerprint=(
                selected.document.semantic.evidence_set.sanitizer_contract_fingerprint
            ),
            input_projection_contract_fingerprint=(
                selected.document.semantic.evidence_set.input_projection_contract_fingerprint
            ),
        )

    item = build_frozen_fact(
        CandidateOutboxPlanItemFact,
        schema_version="candidate_outbox_plan_item.v1",
        candidate_ordinal=0,
        candidate_entry_id="candidate-entry:1",
        candidate_attribution_fingerprint="sha256:attribution",
        expected_projection_item_fingerprint="sha256:projection",
        expected_physical_row_fingerprint="sha256:physical",
    )
    with pytest.raises(ValidationError, match="accumulator"):
        build_frozen_fact(
            CandidateOutboxPlanFact,
            schema_version="candidate_outbox_plan.v1",
            producer_event_id="extraction-completed:1",
            ordered_items=(item,),
            item_count=1,
            ordered_item_accumulator="sha256:wrong",
            lowering_contract_fingerprint="sha256:lowering",
        )


def test_input_document_rejects_semantic_attribution_cross_pair() -> None:
    _plan, selected = _selected_input(("Use short release notes.",))
    original = selected.document.attribution.ordered_evidence_attributions[0]
    wrong = build_frozen_fact(
        CompactionMemoryEvidenceAttributionFact,
        schema_version="compaction_memory_evidence_attribution.v1",
        evidence_semantic_fingerprint="sha256:other",
        source_event_reference=original.source_event_reference,
        source_run_id=original.source_run_id,
        source_turn_id=original.source_turn_id,
        source_reply_id=original.source_reply_id,
        source_message_id=original.source_message_id,
        original_text_sha256=original.original_text_sha256,
        original_text_utf8_bytes=original.original_text_utf8_bytes,
        source_wire_semantic_fingerprint=original.source_wire_semantic_fingerprint,
        ordered_redaction_audits=original.ordered_redaction_audits,
    )
    original_attribution = selected.document.attribution
    attribution = build_frozen_fact(
        CompactionMemoryExtractionInputAttributionFact,
        schema_version="compaction_memory_extraction_input_attribution.v1",
        compaction_id=original_attribution.compaction_id,
        extension_link=original_attribution.extension_link,
        request_event_reference=original_attribution.request_event_reference,
        durable_job_id=original_attribution.durable_job_id,
        durable_job_source_reference_fingerprint=(
            original_attribution.durable_job_source_reference_fingerprint
        ),
        human_evidence_manifest_reference=(
            original_attribution.human_evidence_manifest_reference
        ),
        ordered_evidence_attributions=(wrong,),
        resolved_input_budget_attribution=(
            original_attribution.resolved_input_budget_attribution
        ),
        permanent_omission_count=original_attribution.permanent_omission_count,
        permanent_omission_semantic_accumulator=(
            original_attribution.permanent_omission_semantic_accumulator
        ),
        permanent_omission_attribution_accumulator=(
            original_attribution.permanent_omission_attribution_accumulator
        ),
    )
    with pytest.raises(ValidationError, match="semantic/attribution"):
        build_frozen_fact(
            CompactionMemoryExtractionInputDocumentFact,
            schema_version="compaction_memory_extraction_input_document.v1",
            semantic=selected.document.semantic,
            attribution=attribution,
        )


@dataclass
class _LiveHandle:
    identity: CompactionPostCompletionExtensionPrivateHandleIdentity
    active: bool = True


def test_live_extension_intent_is_not_pydantic_or_serializable() -> None:
    contract = build_frozen_fact(
        CompactionPostCompletionExtensionContractFact,
        schema_version="compaction_post_completion_extension_contract.v1",
        extension_id="test.extension",
        extension_version="1",
        request_event_type="CONTEXT_COMPACTION_MEMORY_EXTRACTION_REQUESTED",
        request_event_schema_fingerprint="sha256:event-schema",
        source_manifest_contract_fingerprint="sha256:manifest",
        admission_policy_fingerprint="sha256:admission",
    )
    handle_identity = _runtime_fact(
        CompactionPostCompletionExtensionPrivateHandleIdentity,
        "identity_fingerprint",
        "compaction-post-completion-extension-private-handle-identity:v1",
        extension_id=contract.extension_id,
        handle_id="handle:1",
        generation=1,
        manifest_preparation_identity_fingerprint="sha256:manifest-preparation",
    )
    intent_identity = _runtime_fact(
        PreparedCompactionPostCompletionExtensionIntentIdentity,
        "intent_fingerprint",
        "prepared-compaction-post-completion-extension-intent:v1",
        extension_contract_fingerprint=contract.contract_fingerprint,
        completed_event_id="completed:1",
        request_event_id="request:1",
        extension_link_id="sha256:link",
        business_occurrence_fingerprint="sha256:occurrence",
        private_handle_identity_fingerprint=handle_identity.identity_fingerprint,
    )
    intent = PreparedCompactionPostCompletionExtensionIntent(
        identity=intent_identity,
        extension_contract=contract,
        private_handle=_LiveHandle(handle_identity),
    )

    assert not hasattr(intent, "model_dump")
    with pytest.raises(TypeError, match="not serializable"):
        pickle.dumps(intent)
