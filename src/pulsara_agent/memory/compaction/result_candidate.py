"""Immutable extraction result and candidate-outbox plan factories."""

from __future__ import annotations

from pulsara_agent.event import (
    ContextCompactionMemoryExtractionCompletedEvent,
    EventContext,
)
from pulsara_agent.event_log.serialization import freeze_event_write_candidate
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    PooledMemoryCandidate,
    candidate_payload_fingerprint,
    pooled_candidate_row_fingerprint,
)
from pulsara_agent.memory.candidates.projection_outbox import (
    CandidateProjectionOutboxRow,
)
from pulsara_agent.memory.compaction.parser import (
    ParsedCompactionMemoryExtractionOutput,
)
from pulsara_agent.primitives._context_base import (
    canonical_utc_timestamp,
    context_fingerprint,
)
from pulsara_agent.primitives.compaction import (
    CompactionMemoryEvidenceNodeFact,
    CompactionMemoryExtractionCandidateAttributionFact,
    CompactionMemoryPreferenceCandidatePayloadFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    CandidateProjectionOutboxItemFact,
    CandidateProjectionProducerKind,
)
from pulsara_agent.primitives.memory_candidate import (
    PreferenceCandidate,
    ValidCandidatePayload,
    build_memory_candidate_semantic,
)
from pulsara_agent.projection_jobs.compaction_memory import (
    CandidateOutboxPlanFact,
    CandidateOutboxPlanItemFact,
    CompactionMemoryExtractionOccurrenceAttributionFact,
    CompactionMemoryExtractionResultCandidateFact,
    CompactionMemoryExtractionResultSemanticFact,
    DurableProjectionEventWriteCandidateFact,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionKind,
    LeasedDurableProjectionJob,
    ProjectionJobResultOwnerFact,
)


CANDIDATE_OUTBOX_LOWERING_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-candidate-outbox-lowering-contract:v1",
    {
        "source": "stored-extraction-completed-event",
        "physical_defaults": "forbidden",
        "producer_kind": "compaction_memory_extraction",
    },
)


def _accumulate(domain: str, values: tuple[str, ...]) -> str:
    current = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        current = context_fingerprint(f"{domain}:step", (current, value))
    return current


def extraction_completed_event_id(
    *, runtime_session_id: str, request_event_id: str, job_id: str, target_key: str
) -> str:
    digest = context_fingerprint(
        "compaction-memory-extraction-completed-event-id:v1",
        (runtime_session_id, request_event_id, job_id, target_key),
    ).removeprefix("sha256:")
    return f"context_compaction_memory_extraction_completed:{digest}"


def projection_job_result_owner(
    lease: LeasedDurableProjectionJob,
) -> ProjectionJobResultOwnerFact:
    return build_frozen_fact(
        ProjectionJobResultOwnerFact,
        schema_version="projection_job_result_owner.v1",
        owner_kind="durable_projection_job",
        job_id=lease.job.job_id,
        job_semantic_fingerprint=lease.job.job_semantic_fingerprint,
        job_candidate_fingerprint=lease.job_candidate_fingerprint,
        source_event_reference_fingerprint=(
            lease.job.source_event_reference.reference_fingerprint
        ),
    )


def build_preference_candidate_attributions(
    *,
    parsed: ParsedCompactionMemoryExtractionOutput,
    nodes: tuple[CompactionMemoryEvidenceNodeFact, ...],
    scope: str,
    job_id: str,
    request_event_id: str,
    extraction_contract_fingerprint: str,
    created_at_utc: str,
) -> tuple[CompactionMemoryExtractionCandidateAttributionFact, ...]:
    by_id = {node.evidence_node_id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("extraction evidence node IDs are not unique")
    canonical_created = canonical_utc_timestamp(created_at_utc)
    attributions: list[CompactionMemoryExtractionCandidateAttributionFact] = []
    for ordinal, proposal in enumerate(parsed.output.candidates):
        evidence = tuple(by_id[item] for item in proposal.evidence_node_ids)
        semantic = build_memory_candidate_semantic(
            kind="Preference",
            scope=scope,
            statement=proposal.statement,
        )
        occurrence = context_fingerprint(
            "compaction-memory-preference-candidate-occurrence:v1",
            {
                "job_id": job_id,
                "request_event_id": request_event_id,
                "candidate_ordinal": ordinal,
                "candidate_semantic_fingerprint": semantic.semantic_fingerprint,
                "ordered_evidence_node_ids": proposal.evidence_node_ids,
                "extraction_contract_fingerprint": extraction_contract_fingerprint,
            },
        )
        occurrence_suffix = occurrence.removeprefix("sha256:")
        candidate_id = f"candidate:compaction-memory:{occurrence_suffix}"
        entry_id = f"pool:compaction-memory:{occurrence_suffix}"
        payload = build_frozen_fact(
            CompactionMemoryPreferenceCandidatePayloadFact,
            schema_version="compaction_memory_preference_candidate_payload.v1",
            kind="Preference",
            candidate_id=candidate_id,
            statement=proposal.statement,
            scope=scope,
            evidence_ids=proposal.evidence_node_ids,
            source_authority="conversation_evidence",
            verification_status="inferred",
            candidate_semantic=semantic,
        )
        attributions.append(
            build_frozen_fact(
                CompactionMemoryExtractionCandidateAttributionFact,
                schema_version=(
                    "compaction_memory_extraction_candidate_attribution.v1"
                ),
                candidate_entry_id=entry_id,
                candidate_ordinal=ordinal,
                candidate_payload=payload,
                candidate_occurrence_fingerprint=occurrence,
                candidate_created_at_utc=canonical_created,
                ordered_evidence_node_ids=proposal.evidence_node_ids,
                ordered_evidence_semantic_fingerprints=tuple(
                    item.semantic.evidence_semantic_fingerprint for item in evidence
                ),
            )
        )
    return tuple(attributions)


def build_extraction_completed_event(
    *,
    runtime_session_id: str,
    event_context: EventContext,
    created_at_utc: str,
    lease: LeasedDurableProjectionJob,
    result_semantic: CompactionMemoryExtractionResultSemanticFact,
    occurrence_attribution: CompactionMemoryExtractionOccurrenceAttributionFact,
    candidate_attributions: tuple[
        CompactionMemoryExtractionCandidateAttributionFact, ...
    ],
) -> ContextCompactionMemoryExtractionCompletedEvent:
    request_id = lease.job.source_event_reference.event_id
    return ContextCompactionMemoryExtractionCompletedEvent(
        id=extraction_completed_event_id(
            runtime_session_id=runtime_session_id,
            request_event_id=request_id,
            job_id=lease.job.job_id,
            target_key=lease.job.target_key,
        ),
        created_at=canonical_utc_timestamp(created_at_utc),
        **event_context.event_fields(),
        result_semantic=result_semantic,
        occurrence_attribution=occurrence_attribution,
        ordered_candidate_attributions=candidate_attributions,
    )


def _pooled_candidate(
    *,
    runtime_session_id: str,
    event: ContextCompactionMemoryExtractionCompletedEvent,
    attribution: CompactionMemoryExtractionCandidateAttributionFact,
) -> PooledMemoryCandidate:
    source = attribution.candidate_payload
    payload = ValidCandidatePayload(
        candidate=PreferenceCandidate(
            candidate_id=source.candidate_id,
            statement=source.statement,
            scope=source.scope,
            evidence_ids=source.evidence_ids,
            source_authority=source.source_authority,
            verification_status=source.verification_status,
        )
    )
    return PooledMemoryCandidate(
        entry_id=attribution.candidate_entry_id,
        payload=payload,
        candidate_semantic=source.candidate_semantic,
        origin=CandidateOrigin.COMPACTION,
        source_session_id=runtime_session_id,
        source_run_id=event.run_id,
        source_turn_id=event.turn_id,
        source_reply_id=event.reply_id,
        source_tool_call_id=None,
        user_quote=None,
        quoted_evidence_locator=None,
        source_event_id=event.id,
        source_artifact_id=None,
        intent_fingerprint=attribution.candidate_occurrence_fingerprint,
        metadata={
            "producer_kind": "compaction_memory_extraction",
            "candidate_ordinal": attribution.candidate_ordinal,
            "candidate_attribution_fingerprint": attribution.attribution_fingerprint,
        },
        created_at=attribution.candidate_created_at_utc,
    )


def lower_extraction_candidate_outbox_rows(
    *,
    runtime_session_id: str,
    event: ContextCompactionMemoryExtractionCompletedEvent,
) -> tuple[CandidateProjectionOutboxRow, ...]:
    from pulsara_agent.event_log.serialization import stable_event_identity

    producer = stable_event_identity(event, runtime_session_id=runtime_session_id)
    rows: list[CandidateProjectionOutboxRow] = []
    for attribution in event.ordered_candidate_attributions:
        candidate = _pooled_candidate(
            runtime_session_id=runtime_session_id,
            event=event,
            attribution=attribution,
        )
        rows.append(
            CandidateProjectionOutboxRow(
                item=build_frozen_fact(
                    CandidateProjectionOutboxItemFact,
                    schema_version="candidate_projection_outbox_item.v1",
                    producer_kind=(
                        CandidateProjectionProducerKind.COMPACTION_MEMORY_EXTRACTION
                    ),
                    producer_event_identity=producer,
                    candidate_entry_id=candidate.entry_id,
                    candidate_index=attribution.candidate_ordinal,
                    candidate_payload=candidate.payload,
                    candidate_semantic_fingerprint=(
                        attribution.candidate_payload.candidate_semantic.semantic_fingerprint
                    ),
                    candidate_payload_fingerprint=candidate_payload_fingerprint(
                        candidate.payload
                    ),
                    candidate_attribution_fingerprint=(
                        attribution.attribution_fingerprint
                    ),
                ),
                candidate=candidate,
            )
        )
    return tuple(rows)


def _outbox_plan(
    *,
    event: ContextCompactionMemoryExtractionCompletedEvent,
    rows: tuple[CandidateProjectionOutboxRow, ...],
) -> CandidateOutboxPlanFact:
    if len(rows) != len(event.ordered_candidate_attributions):
        raise ValueError("extraction event/outbox row cardinality drifted")
    items = tuple(
        build_frozen_fact(
            CandidateOutboxPlanItemFact,
            schema_version="candidate_outbox_plan_item.v1",
            candidate_ordinal=attribution.candidate_ordinal,
            candidate_entry_id=attribution.candidate_entry_id,
            candidate_attribution_fingerprint=attribution.attribution_fingerprint,
            expected_projection_item_fingerprint=row.item.item_fingerprint,
            expected_physical_row_fingerprint=pooled_candidate_row_fingerprint(
                row.candidate
            ),
        )
        for attribution, row in zip(
            event.ordered_candidate_attributions, rows, strict=True
        )
    )
    return build_frozen_fact(
        CandidateOutboxPlanFact,
        schema_version="candidate_outbox_plan.v1",
        producer_event_id=event.id,
        ordered_items=items,
        item_count=len(items),
        ordered_item_accumulator=_accumulate(
            "candidate-outbox-plan-item-accumulator:v1",
            tuple(item.item_fingerprint for item in items),
        ),
        lowering_contract_fingerprint=(
            CANDIDATE_OUTBOX_LOWERING_CONTRACT_FINGERPRINT
        ),
    )


def build_result_candidate(
    *,
    runtime_session_id: str,
    lease: LeasedDurableProjectionJob,
    event: ContextCompactionMemoryExtractionCompletedEvent,
    intended_target_head_revision: int,
    expected_target_head_fingerprint: str | None,
    permanent_automatic_omission_count: int,
    permanent_automatic_omission_semantic_accumulator: str,
    permanent_automatic_omission_attribution_accumulator: str,
) -> CompactionMemoryExtractionResultCandidateFact:
    if lease.job.projection_kind is not DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION:
        raise ValueError("result candidate requires an extraction job")
    if event.id != extraction_completed_event_id(
        runtime_session_id=runtime_session_id,
        request_event_id=lease.job.source_event_reference.event_id,
        job_id=lease.job.job_id,
        target_key=lease.job.target_key,
    ):
        raise ValueError("extraction result event ID drifted")
    frozen = freeze_event_write_candidate(event)
    producer = build_frozen_fact(
        DurableProjectionEventWriteCandidateFact,
        schema_version="durable_projection_event_write_candidate.v1",
        event_id=frozen.event_id,
        event_type=frozen.event_type,
        event_schema_version=frozen.event_schema_version,
        event_schema_fingerprint=frozen.event_schema_fingerprint,
        event_domain_contract_fingerprint=frozen.event_domain_contract_fingerprint,
        canonical_unsequenced_payload_utf8=frozen.canonical_payload_bytes.decode(
            "utf-8"
        ),
        canonical_payload_sha256=frozen.payload_fingerprint,
        canonical_payload_utf8_bytes=len(frozen.canonical_payload_bytes),
    )
    rows = lower_extraction_candidate_outbox_rows(
        runtime_session_id=runtime_session_id,
        event=event,
    )
    plan = _outbox_plan(event=event, rows=rows)
    owner = projection_job_result_owner(lease)
    result_candidate_id = (
        "compaction-memory-result-candidate:"
        + context_fingerprint(
            "compaction-memory-extraction-result-candidate-id:v1",
            (
                owner.owner_fingerprint,
                event.id,
                event.result_semantic.result_semantic_fingerprint,
            ),
        ).removeprefix("sha256:")
    )
    receipt_id = (
        "compaction-memory-result-receipt:"
        + context_fingerprint(
            "compaction-memory-extraction-result-receipt-id:v1",
            (
                owner.owner_fingerprint,
                event.id,
                lease.job.target_key,
                event.result_semantic.result_semantic_fingerprint,
            ),
        ).removeprefix("sha256:")
    )
    return build_frozen_fact(
        CompactionMemoryExtractionResultCandidateFact,
        schema_version="compaction_memory_extraction_result_candidate.v1",
        result_candidate_id=result_candidate_id,
        result_owner=owner,
        job_id=lease.job.job_id,
        target_key=lease.job.target_key,
        completed_event_id=event.id,
        producer_event_candidate=producer,
        result_semantic_fingerprint=(
            event.result_semantic.result_semantic_fingerprint
        ),
        receipt_id=receipt_id,
        intended_target_head_revision=intended_target_head_revision,
        expected_target_head_fingerprint=expected_target_head_fingerprint,
        candidate_outbox_plan=plan,
        permanent_automatic_omission_count=(
            permanent_automatic_omission_count
        ),
        permanent_automatic_omission_semantic_accumulator=(
            permanent_automatic_omission_semantic_accumulator
        ),
        permanent_automatic_omission_attribution_accumulator=(
            permanent_automatic_omission_attribution_accumulator
        ),
    )


def validate_result_candidate_outbox_plan(
    *,
    runtime_session_id: str,
    candidate: CompactionMemoryExtractionResultCandidateFact,
    event: ContextCompactionMemoryExtractionCompletedEvent,
) -> tuple[CandidateProjectionOutboxRow, ...]:
    rows = lower_extraction_candidate_outbox_rows(
        runtime_session_id=runtime_session_id,
        event=event,
    )
    actual = _outbox_plan(event=event, rows=rows)
    if actual != candidate.candidate_outbox_plan:
        raise ValueError("extraction result outbox plan exact rebind failed")
    return rows


__all__ = [
    "CANDIDATE_OUTBOX_LOWERING_CONTRACT_FINGERPRINT",
    "build_extraction_completed_event",
    "build_preference_candidate_attributions",
    "build_result_candidate",
    "extraction_completed_event_id",
    "lower_extraction_candidate_outbox_rows",
    "projection_job_result_owner",
    "validate_result_candidate_outbox_plan",
]
