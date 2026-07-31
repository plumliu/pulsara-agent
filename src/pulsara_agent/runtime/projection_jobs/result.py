"""Pure factories for durable projection results, receipts, and target heads."""

from __future__ import annotations

from typing import cast

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionAppliedResultReceiptFact,
    DurableProjectionArtifactResultDocumentReferenceFact,
    DurableProjectionCanonicalMutationReferenceFact,
    DurableProjectionGraphResultDocumentReferenceFact,
    DurableProjectionJobCandidateFact,
    DurableProjectionKind,
    DurableProjectionResultDocumentReferenceFact,
    DurableProjectionResultReceiptFact,
    DurableProjectionSourceEventReferenceFact,
    DurableProjectionSupersededResultReceiptFact,
    DurableProjectionTargetHeadFact,
    DurableProjectionTargetUpdatePolicy,
    LeasedDurableProjectionJob,
    PreparedDurableProjectionArtifactDocumentFact,
    PreparedDurableProjectionDocumentFact,
    PreparedDurableProjectionGraphDocumentFact,
    PreparedDurableProjectionGraphRelationFact,
    PreparedDurableProjectionResultFact,
    ProjectionResultCanonicalMutationOwnerFact,
    ProjectionJobResultOwnerFact,
    DurableProjectionResultOwner,
    DurableProjectionResultSemanticFact,
    build_projection_fact,
    durable_result_receipt_reference,
)


def validate_prepared_job_result(
    *,
    candidate: DurableProjectionJobCandidateFact,
    lease: LeasedDurableProjectionJob,
    prepared: PreparedDurableProjectionResultFact,
) -> None:
    """Rebind a prepared result to the exact leased job and its content."""

    owner = prepared.result_owner
    job = candidate.job_semantic
    if not isinstance(owner, ProjectionJobResultOwnerFact):
        raise ValueError("prepared projection result is not job-owned")
    if (
        lease.job != job
        or lease.job_candidate_fingerprint != candidate.candidate_fingerprint
        or lease.activation_fingerprint != candidate.activation_fingerprint
        or lease.seed_contract_fingerprint != candidate.seed_contract_fingerprint
        or owner.job_id != job.job_id
        or owner.job_semantic_fingerprint != job.job_semantic_fingerprint
        or owner.job_candidate_fingerprint != candidate.candidate_fingerprint
        or owner.source_event_reference_fingerprint
        != job.source_event_reference.reference_fingerprint
        or prepared.result_semantic.projection_kind is not job.projection_kind
    ):
        raise ValueError("prepared projection result owner drifted")
    document_semantics = tuple(
        document_semantic_fingerprint(item) for item in prepared.ordered_documents
    )
    mutation_semantics = tuple(
        item.mutation_semantic.mutation_semantic_fingerprint
        for item in prepared.canonical_mutation_candidates
    )
    if (
        prepared.result_semantic.ordered_document_semantic_fingerprints
        != document_semantics
        or prepared.result_semantic.ordered_canonical_mutation_semantic_fingerprints
        != mutation_semantics
    ):
        raise ValueError("prepared projection result semantic vector drifted")
    mutation_owner = projection_result_mutation_owner(
        prepared=prepared,
        source_event_reference=job.source_event_reference,
    )
    for mutation in prepared.canonical_mutation_candidates:
        if mutation.source_owner_fingerprint != mutation_owner.owner_fingerprint:
            raise ValueError("projection mutation owner drifted")


def projection_result_mutation_owner(
    *,
    prepared: PreparedDurableProjectionResultFact,
    source_event_reference: DurableProjectionSourceEventReferenceFact,
) -> ProjectionResultCanonicalMutationOwnerFact:
    """Derive the non-recursive canonical-mutation owner from result semantic."""

    return build_projection_result_mutation_owner(
        result_owner=prepared.result_owner,
        result_semantic=prepared.result_semantic,
        source_event_reference=source_event_reference,
    )


def build_projection_result_mutation_owner(
    *,
    result_owner: DurableProjectionResultOwner,
    result_semantic: DurableProjectionResultSemanticFact,
    source_event_reference: DurableProjectionSourceEventReferenceFact,
) -> ProjectionResultCanonicalMutationOwnerFact:
    if isinstance(result_owner, ProjectionJobResultOwnerFact):
        if (
            result_owner.source_event_reference_fingerprint
            != source_event_reference.reference_fingerprint
        ):
            raise ValueError("projection result mutation source drifted")
    else:
        if result_owner.source_event_reference != source_event_reference:
            raise ValueError("pre-activation mutation source drifted")
    return cast(
        ProjectionResultCanonicalMutationOwnerFact,
        build_projection_fact(
            ProjectionResultCanonicalMutationOwnerFact,
            schema_version="projection_result_canonical_mutation_owner.v1",
            owner_kind="projection_result",
            result_owner=result_owner,
            projection_kind=result_semantic.projection_kind,
            source_event_reference=source_event_reference,
            projection_result_semantic_fingerprint=(
                result_semantic.result_semantic_fingerprint
            ),
        ),
    )


def document_semantic_fingerprint(
    document: PreparedDurableProjectionDocumentFact,
) -> str:
    if isinstance(document, PreparedDurableProjectionGraphRelationFact):
        return document.relation_reference.relation_semantic_fingerprint
    return document.document_semantic_fingerprint


def durable_document_reference(
    document: PreparedDurableProjectionDocumentFact,
) -> DurableProjectionResultDocumentReferenceFact:
    if isinstance(document, PreparedDurableProjectionArtifactDocumentFact):
        if (
            document.content_sha256 != document.artifact_reference.content_sha256
            or document.content_utf8_bytes
            != document.artifact_reference.content_utf8_bytes
        ):
            raise ValueError("projection artifact content reference drifted")
        return cast(
            DurableProjectionArtifactResultDocumentReferenceFact,
            build_projection_fact(
                DurableProjectionArtifactResultDocumentReferenceFact,
                schema_version=(
                    "durable_projection_artifact_result_document_reference.v1"
                ),
                document_kind="artifact",
                semantic_document_id=document.semantic_document_id,
                document_semantic_fingerprint=(document.document_semantic_fingerprint),
                media_type=document.media_type,
                content_codec_contract_fingerprint=(
                    document.content_codec_contract_fingerprint
                ),
                metadata_contract_fingerprint=(document.metadata_contract_fingerprint),
                artifact_reference=document.artifact_reference,
            ),
        )
    if isinstance(document, PreparedDurableProjectionGraphDocumentFact):
        return cast(
            DurableProjectionGraphResultDocumentReferenceFact,
            build_projection_fact(
                DurableProjectionGraphResultDocumentReferenceFact,
                schema_version=(
                    "durable_projection_graph_result_document_reference.v1"
                ),
                document_kind="graph_document",
                graph_id=document.graph_id,
                semantic_document_id=document.semantic_document_id,
                graph_document_type=document.graph_document_type,
                document_semantic_fingerprint=(document.document_semantic_fingerprint),
                canonical_json_sha256=document.canonical_json_sha256,
                canonical_json_utf8_bytes=document.canonical_json_utf8_bytes,
                jsonld_codec_contract_fingerprint=(
                    document.jsonld_codec_contract_fingerprint
                ),
            ),
        )
    return document.relation_reference


def canonical_mutation_references(
    prepared: PreparedDurableProjectionResultFact,
) -> tuple[DurableProjectionCanonicalMutationReferenceFact, ...]:
    return tuple(
        cast(
            DurableProjectionCanonicalMutationReferenceFact,
            build_projection_fact(
                DurableProjectionCanonicalMutationReferenceFact,
                schema_version=("durable_projection_canonical_mutation_reference.v1"),
                mutation_id=item.mutation_id,
                mutation_semantic_fingerprint=(
                    item.mutation_semantic.mutation_semantic_fingerprint
                ),
                ordered_surface_delivery_identity_fingerprints=tuple(
                    context_fingerprint(
                        "canonical-mutation-surface-delivery-identity:v1",
                        {
                            "mutation_id": item.mutation_id,
                            "surface": surface.value,
                            "surface_plan_fingerprint": (item.surface_plan_fingerprint),
                        },
                    )
                    for surface in item.requested_surfaces
                ),
            ),
        )
        for item in prepared.canonical_mutation_candidates
    )


def applied_result_receipt(
    *,
    lease: LeasedDurableProjectionJob,
    prepared: PreparedDurableProjectionResultFact,
    target_head_revision: int,
) -> DurableProjectionAppliedResultReceiptFact:
    job = lease.job
    return applied_result_receipt_for_source(
        prepared=prepared,
        target_key=job.target_key,
        source_event_reference=job.source_event_reference,
        target_head_revision=target_head_revision,
    )


def applied_result_receipt_for_source(
    *,
    prepared: PreparedDurableProjectionResultFact,
    target_key: str,
    source_event_reference: DurableProjectionSourceEventReferenceFact,
    target_head_revision: int,
) -> DurableProjectionAppliedResultReceiptFact:
    """Build the same immutable receipt for job or pre-activation owners."""

    receipt_id = "projection-result-receipt:" + context_fingerprint(
        "durable-projection-applied-result-receipt-id:v1",
        {
            "projection_kind": (prepared.result_semantic.projection_kind.value),
            "target_key": target_key,
            "source_event_reference_fingerprint": (
                source_event_reference.reference_fingerprint
            ),
            "result_semantic_fingerprint": (
                prepared.result_semantic.result_semantic_fingerprint
            ),
        },
    )
    return cast(
        DurableProjectionAppliedResultReceiptFact,
        build_projection_fact(
            DurableProjectionAppliedResultReceiptFact,
            schema_version="durable_projection_applied_result_receipt.v1",
            receipt_kind="applied",
            receipt_id=receipt_id,
            result_owner=prepared.result_owner,
            result_semantic=prepared.result_semantic,
            target_key=target_key,
            source_event_reference_fingerprint=(
                source_event_reference.reference_fingerprint
            ),
            source_sequence=source_event_reference.sequence,
            target_head_revision=target_head_revision,
            result_document_references=tuple(
                durable_document_reference(item) for item in prepared.ordered_documents
            ),
            canonical_mutation_references=canonical_mutation_references(prepared),
        ),
    )


def superseded_result_receipt(
    *,
    lease: LeasedDurableProjectionJob,
    prepared: PreparedDurableProjectionResultFact,
    effective: DurableProjectionAppliedResultReceiptFact,
) -> DurableProjectionSupersededResultReceiptFact:
    if (
        lease.job.handler_contract.target_update_policy
        is not DurableProjectionTargetUpdatePolicy.FULL_REPLACEMENT
        or effective.source_sequence <= lease.job.source_event_reference.sequence
    ):
        raise ValueError("projection result cannot be superseded")
    return superseded_result_receipt_for_source(
        prepared=prepared,
        projection_kind=lease.job.projection_kind,
        target_key=lease.job.target_key,
        source_event_reference=lease.job.source_event_reference,
        effective=effective,
    )


def superseded_result_receipt_for_source(
    *,
    prepared: PreparedDurableProjectionResultFact,
    projection_kind: DurableProjectionKind,
    target_key: str,
    source_event_reference: DurableProjectionSourceEventReferenceFact,
    effective: DurableProjectionAppliedResultReceiptFact,
) -> DurableProjectionSupersededResultReceiptFact:
    """Build a full-replacement supersession receipt for either owner."""

    if prepared.result_semantic.projection_kind is not projection_kind:
        raise ValueError("projection result cannot be superseded")
    return superseded_result_receipt_for_owner(
        candidate_result_owner=prepared.result_owner,
        projection_kind=projection_kind,
        target_key=target_key,
        source_event_reference=source_event_reference,
        effective=effective,
    )


def superseded_result_receipt_for_owner(
    *,
    candidate_result_owner: DurableProjectionResultOwner,
    projection_kind: DurableProjectionKind,
    target_key: str,
    source_event_reference: DurableProjectionSourceEventReferenceFact,
    effective: DurableProjectionAppliedResultReceiptFact,
) -> DurableProjectionSupersededResultReceiptFact:
    """Build a receipt when an applied head already dominates a candidate."""

    if (
        effective.result_semantic.projection_kind is not projection_kind
        or effective.target_key != target_key
        or effective.source_sequence <= source_event_reference.sequence
    ):
        raise ValueError("projection result cannot be superseded")
    if isinstance(candidate_result_owner, ProjectionJobResultOwnerFact):
        if (
            candidate_result_owner.source_event_reference_fingerprint
            != source_event_reference.reference_fingerprint
        ):
            raise ValueError("superseded job owner source drifted")
    elif (
        candidate_result_owner.projection_kind is not projection_kind
        or candidate_result_owner.source_event_reference != source_event_reference
    ):
        raise ValueError("superseded pre-activation owner source drifted")
    effective_reference = durable_result_receipt_reference(effective)
    receipt_id = "projection-result-receipt:" + context_fingerprint(
        "durable-projection-superseded-result-receipt-id:v1",
        {
            "projection_kind": projection_kind.value,
            "target_key": target_key,
            "candidate_owner_fingerprint": (candidate_result_owner.owner_fingerprint),
            "candidate_source_event_reference_fingerprint": (
                source_event_reference.reference_fingerprint
            ),
            "effective_applied_receipt_fingerprint": (effective.receipt_fingerprint),
        },
    )
    return cast(
        DurableProjectionSupersededResultReceiptFact,
        build_projection_fact(
            DurableProjectionSupersededResultReceiptFact,
            schema_version=("durable_projection_superseded_result_receipt.v1"),
            receipt_kind="superseded",
            receipt_id=receipt_id,
            candidate_result_owner=candidate_result_owner,
            projection_kind=projection_kind,
            target_key=target_key,
            candidate_source_event_reference_fingerprint=(
                source_event_reference.reference_fingerprint
            ),
            candidate_source_sequence=source_event_reference.sequence,
            effective_applied_result_receipt_reference=effective_reference,
            target_head_revision=effective.target_head_revision,
        ),
    )


def target_head_from_applied_receipt(
    receipt: DurableProjectionAppliedResultReceiptFact,
) -> DurableProjectionTargetHeadFact:
    return cast(
        DurableProjectionTargetHeadFact,
        build_projection_fact(
            DurableProjectionTargetHeadFact,
            schema_version="durable_projection_target_head.v1",
            projection_kind=receipt.result_semantic.projection_kind,
            target_key=receipt.target_key,
            applied_source_sequence=receipt.source_sequence,
            applied_source_event_reference_fingerprint=(
                receipt.source_event_reference_fingerprint
            ),
            applied_result_receipt_reference=(
                durable_result_receipt_reference(receipt)
            ),
            head_revision=receipt.target_head_revision,
        ),
    )


def exact_applied_head_receipt(
    *,
    head: DurableProjectionTargetHeadFact,
    receipt: DurableProjectionResultReceiptFact,
) -> DurableProjectionAppliedResultReceiptFact:
    if (
        not isinstance(receipt, DurableProjectionAppliedResultReceiptFact)
        or durable_result_receipt_reference(receipt)
        != head.applied_result_receipt_reference
        or receipt.result_semantic.projection_kind is not head.projection_kind
        or receipt.target_key != head.target_key
        or receipt.source_sequence != head.applied_source_sequence
        or receipt.source_event_reference_fingerprint
        != head.applied_source_event_reference_fingerprint
        or receipt.target_head_revision != head.head_revision
        or target_head_from_applied_receipt(receipt) != head
    ):
        raise ValueError("projection target head receipt rebind failed")
    return receipt


__all__ = [
    "applied_result_receipt",
    "applied_result_receipt_for_source",
    "build_projection_result_mutation_owner",
    "canonical_mutation_references",
    "document_semantic_fingerprint",
    "durable_document_reference",
    "exact_applied_head_receipt",
    "projection_result_mutation_owner",
    "superseded_result_receipt",
    "superseded_result_receipt_for_source",
    "superseded_result_receipt_for_owner",
    "target_head_from_applied_receipt",
    "validate_prepared_job_result",
]
