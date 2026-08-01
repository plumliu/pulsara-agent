"""Bounded manifest hydration and exact human-evidence selection."""

from __future__ import annotations

from pulsara_agent.event_log.serialization import build_raw_stored_event_envelope

from dataclasses import dataclass
from hashlib import sha256
from typing import Callable

from pulsara_agent.event.events import RunStartEvent
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    freeze_event_write_candidate,
)
from pulsara_agent.memory.compaction.sanitizer import (
    SANITIZER_CONTRACT_FINGERPRINT,
    sanitize_compaction_evidence,
)
from pulsara_agent.memory.compaction.manifest import (
    MANIFEST_ARTIFACT_CONTRACT_FINGERPRINT,
    MANIFEST_SELECTION_PROJECTION_CONTRACT_FINGERPRINT,
    MANIFEST_SOURCE_SELECTION_CONTRACT_FINGERPRINT,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.compaction import (
    COMPACTION_MEMORY_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT,
    COMPACTION_MEMORY_INPUT_PROJECTION_CONTRACT_FINGERPRINT,
    CompactionHumanEvidenceArtifactSelectionProjectionFact,
    CompactionHumanEvidenceInlineSelectionProjectionFact,
    CompactionHumanEvidenceManifestPageFact,
    CompactionHumanEvidenceManifestAttributionFact,
    CompactionHumanEvidenceManifestReferenceFact,
    CompactionHumanEvidenceManifestRootFact,
    CompactionHumanEvidenceManifestSemanticFact,
    CompactionMemoryEvidenceAttributionFact,
    CompactionMemoryEvidenceInputProjectionFact,
    CompactionMemoryEvidenceNodeFact,
    CompactionMemoryEvidenceSemanticFact,
    CompactionMemoryEvidenceSetSemanticFact,
    CompactionMemoryExtractionInputAttributionFact,
    CompactionMemoryExtractionInputDocumentFact,
    CompactionMemoryExtractionInputSemanticFact,
    CompactionPostCompletionExtensionLinkFact,
    ResolvedExtractionInputBudgetAttributionFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionSourceEventReferenceFact,
)


EXTRACTION_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT = (
    COMPACTION_MEMORY_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT
)
EXTRACTION_INPUT_PROJECTION_CONTRACT_FINGERPRINT = (
    COMPACTION_MEMORY_INPUT_PROJECTION_CONTRACT_FINGERPRINT
)
EXTRACTION_INPUT_CODEC_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-input-codec:v1",
    {"encoding": "canonical-json", "node_text": "full-sanitized-message"},
)


@dataclass(frozen=True, slots=True)
class ExactHumanEvidenceSource:
    event: RunStartEvent
    stored_reference: GovernanceStoredEventReferenceFact


@dataclass(frozen=True, slots=True)
class SelectedCompactionMemoryExtractionInput:
    document: CompactionMemoryExtractionInputDocumentFact
    ordered_nodes: tuple[CompactionMemoryEvidenceNodeFact, ...]
    canonical_input_utf8: str
    source_eligible_leaf_count: int
    permanent_omission_count: int
    permanent_omission_semantic_accumulator: str
    permanent_omission_attribution_accumulator: str


def _accumulate(domain: str, values: tuple[str, ...]) -> str:
    current = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        current = context_fingerprint(f"{domain}:step", (current, value))
    return current


def compaction_evidence_node_id(*, semantic, attribution) -> str:
    """Return the canonical graph identity for one extracted human message."""

    return "evidence:" + context_fingerprint(
        "compaction-memory-evidence-node-id:v1",
        {
            "source_event_reference": (
                attribution.source_event_reference.reference_fingerprint
            ),
            "message_id": attribution.source_message_id,
            "semantic": semantic.evidence_semantic_fingerprint,
            "sanitizer": SANITIZER_CONTRACT_FINGERPRINT,
        },
    )


def _read_verified_text(
    archive: ArtifactStore,
    reference,
    *,
    runtime_session_id: str,
    deadline_monotonic: float,
) -> str:
    content = archive.get_text(
        reference.artifact_id,
        session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    encoded = content.encode("utf-8")
    if (
        len(encoded) != reference.content_bytes
        or sha256(encoded).hexdigest() != reference.content_sha256
    ):
        raise ValueError("manifest artifact content authority mismatch")
    return content


def _load_manifest_root(
    *,
    reference: CompactionHumanEvidenceManifestReferenceFact,
    archive: ArtifactStore,
    runtime_session_id: str,
    deadline_monotonic: float,
) -> CompactionHumanEvidenceManifestRootFact:
    root_text = _read_verified_text(
        archive,
        reference.paged_manifest_root_reference,
        runtime_session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    root = CompactionHumanEvidenceManifestRootFact.model_validate_json(root_text)
    if (
        reference.paged_manifest_root_reference.artifact_kind
        != "compaction-human-evidence-manifest-root"
        or reference.paged_manifest_root_reference.artifact_contract_fingerprint
        != MANIFEST_ARTIFACT_CONTRACT_FINGERPRINT
    ):
        raise ValueError("manifest root reference contract mismatch")
    semantic = build_frozen_fact(
        CompactionHumanEvidenceManifestSemanticFact,
        schema_version="compaction_human_evidence_manifest_semantic.v1",
        eligible_leaf_count=root.eligible_leaf_count,
        ordered_semantic_accumulator=root.ordered_semantic_accumulator,
        transitive_leaf_coverage_fingerprint=(
            root.transitive_leaf_coverage_fingerprint
        ),
        selection_contract_fingerprint=(root.source_selection_contract_fingerprint),
    )
    attribution = build_frozen_fact(
        CompactionHumanEvidenceManifestAttributionFact,
        schema_version="compaction_human_evidence_manifest_attribution.v1",
        manifest_semantic_fingerprint=semantic.manifest_semantic_fingerprint,
        runtime_session_id=root.runtime_session_id,
        selection_window_attribution=root.selection_window_attribution,
        transcript_cursor_fingerprint=root.transcript_cursor_fingerprint,
        transcript_cursor_generation=root.transcript_cursor_generation,
        verified_through_sequence=root.verified_through_sequence,
        ledger_continuity_accumulator=root.ledger_continuity_accumulator,
        domain_completeness_proof_fingerprint=(
            root.domain_completeness_proof_fingerprint
        ),
        ordered_leaf_attribution_accumulator=(root.ordered_attribution_accumulator),
        ordered_selection_projection_accumulator=(
            root.ordered_selection_projection_accumulator
        ),
        selection_projection_contract_fingerprint=(
            root.selection_projection_contract_fingerprint
        ),
        paged_manifest_root_reference=(reference.paged_manifest_root_reference),
    )
    if (
        root.runtime_session_id != runtime_session_id
        or root.source_selection_contract_fingerprint
        != MANIFEST_SOURCE_SELECTION_CONTRACT_FINGERPRINT
        or root.selection_projection_contract_fingerprint
        != MANIFEST_SELECTION_PROJECTION_CONTRACT_FINGERPRINT
        or semantic.manifest_semantic_fingerprint
        != reference.manifest_semantic_fingerprint
        or attribution.attribution_fingerprint
        != reference.manifest_attribution_fingerprint
    ):
        raise ValueError("manifest reference semantic/attribution rebind failed")
    return root


def _read_manifest_page(
    *,
    root: CompactionHumanEvidenceManifestRootFact,
    page_index: int,
    archive: ArtifactStore,
    runtime_session_id: str,
    deadline_monotonic: float,
) -> CompactionHumanEvidenceManifestPageFact:
    page_reference = root.ordered_page_references[page_index]
    if (
        page_reference.artifact_kind != "compaction-human-evidence-manifest-page"
        or page_reference.artifact_contract_fingerprint
        != MANIFEST_ARTIFACT_CONTRACT_FINGERPRINT
    ):
        raise ValueError("manifest page reference contract mismatch")
    page_text = _read_verified_text(
        archive,
        page_reference,
        runtime_session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    page = CompactionHumanEvidenceManifestPageFact.model_validate_json(page_text)
    if page.page_index != page_index:
        raise ValueError("manifest page order mismatch")
    expected_semantic = _accumulate(
        "compaction-human-evidence-page-semantic:v1",
        tuple(item.semantic_fingerprint for item in page.ordered_leaf_semantics),
    )
    expected_attribution = _accumulate(
        "compaction-human-evidence-page-attribution:v1",
        tuple(item.attribution_fingerprint for item in page.ordered_leaf_attributions),
    )
    expected_projection = _accumulate(
        "compaction-human-evidence-page-selection:v1",
        tuple(
            item.selection_projection_fingerprint
            for item in page.ordered_selection_projections
        ),
    )
    if (
        page.semantic_accumulator != expected_semantic
        or page.attribution_accumulator != expected_attribution
        or page.selection_projection_accumulator != expected_projection
    ):
        raise ValueError("manifest page accumulator mismatch")
    return page


def _validate_manifest_pages(
    *,
    root: CompactionHumanEvidenceManifestRootFact,
    archive: ArtifactStore,
    runtime_session_id: str,
    deadline_monotonic: float,
) -> None:
    semantic_accumulator = context_fingerprint(
        "compaction-human-evidence-manifest-semantic:v1:empty", ()
    )
    attribution_accumulator = context_fingerprint(
        "compaction-human-evidence-manifest-attribution:v1:empty", ()
    )
    projection_accumulator = context_fingerprint(
        "compaction-human-evidence-manifest-selection:v1:empty", ()
    )
    transitive_coverage = context_fingerprint(
        "compaction-human-evidence-transitive-coverage:v1:empty", ()
    )
    count = 0
    first_sequence: int | None = None
    last_sequence: int | None = None
    for page_index in range(root.page_count):
        page = _read_manifest_page(
            root=root,
            page_index=page_index,
            archive=archive,
            runtime_session_id=runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        for semantic, attribution, projection in zip(
            page.ordered_leaf_semantics,
            page.ordered_leaf_attributions,
            page.ordered_selection_projections,
            strict=True,
        ):
            semantic_accumulator = context_fingerprint(
                "compaction-human-evidence-manifest-semantic:v1:step",
                (semantic_accumulator, semantic.semantic_fingerprint),
            )
            attribution_accumulator = context_fingerprint(
                "compaction-human-evidence-manifest-attribution:v1:step",
                (attribution_accumulator, attribution.attribution_fingerprint),
            )
            projection_accumulator = context_fingerprint(
                "compaction-human-evidence-manifest-selection:v1:step",
                (
                    projection_accumulator,
                    projection.selection_projection_fingerprint,
                ),
            )
            transitive_coverage = context_fingerprint(
                "compaction-human-evidence-transitive-coverage:v1:step",
                (transitive_coverage, semantic.semantic_fingerprint),
            )
            first_sequence = (
                attribution.source_sequence
                if first_sequence is None
                else first_sequence
            )
            last_sequence = attribution.source_sequence
            count += 1
    if (
        count != root.eligible_leaf_count
        or semantic_accumulator != root.ordered_semantic_accumulator
        or attribution_accumulator != root.ordered_attribution_accumulator
        or projection_accumulator != root.ordered_selection_projection_accumulator
        or transitive_coverage != root.transitive_leaf_coverage_fingerprint
        or first_sequence != root.first_source_sequence
        or last_sequence != root.last_source_sequence
    ):
        raise ValueError("manifest root/page coverage mismatch")


def _build_node(
    *,
    semantic,
    attribution,
    projection: CompactionHumanEvidenceInlineSelectionProjectionFact,
    resolved: ExactHumanEvidenceSource,
) -> CompactionMemoryEvidenceNodeFact:
    source = resolved.event
    if source.id != attribution.exact_run_start_event_reference.event_id:
        raise ValueError("selected evidence resolved another RunStart")
    if source.current_user_message.message_id != attribution.message_id:
        raise ValueError("selected evidence message identity mismatch")
    sanitized = sanitize_compaction_evidence(source.current_user_message.text)
    if (
        sanitized.text != projection.sanitized_full_text
        or sanitized.text_sha256 != projection.sanitized_full_text_sha256
        or sanitized.text_utf8_bytes != projection.sanitized_full_text_utf8_bytes
    ):
        raise ValueError("selected evidence sanitizer rebind failed")
    evidence_semantic = build_frozen_fact(
        CompactionMemoryEvidenceSemanticFact,
        schema_version="compaction_memory_evidence_semantic.v1",
        source_kind="direct_human_input",
        sanitized_full_message_text=sanitized.text,
        sanitized_full_message_sha256=sanitized.text_sha256,
        sanitized_full_message_utf8_bytes=sanitized.text_utf8_bytes,
        sanitizer_contract_fingerprint=SANITIZER_CONTRACT_FINGERPRINT,
    )
    input_projection = build_frozen_fact(
        CompactionMemoryEvidenceInputProjectionFact,
        schema_version="compaction_memory_evidence_input_projection.v1",
        projection_kind="full",
        evidence_semantic_fingerprint=evidence_semantic.evidence_semantic_fingerprint,
        projected_text=sanitized.text,
        projected_text_sha256=sanitized.text_sha256,
        projected_text_utf8_bytes=sanitized.text_utf8_bytes,
        projection_contract_fingerprint=(
            EXTRACTION_INPUT_PROJECTION_CONTRACT_FINGERPRINT
        ),
    )
    source_ref = resolved.stored_reference
    expected_ref = attribution.exact_run_start_event_reference
    rebound_envelope = build_raw_stored_event_envelope(
        event=source,
        runtime_session_id=expected_ref.runtime_session_id,
        schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
    )
    unsequenced_candidate = freeze_event_write_candidate(
        source.model_copy(update={"sequence": None})
    )
    if (
        source_ref.stable_identity.runtime_session_id != expected_ref.runtime_session_id
        or source_ref.stable_identity.event_id != expected_ref.event_id
        or source_ref.stable_identity.event_type != expected_ref.event_type
        or source_ref.stable_identity.payload_fingerprint
        != unsequenced_candidate.payload_fingerprint
        or source_ref.sequence != expected_ref.sequence
        or source_ref.sequence != attribution.source_sequence
        or source.sequence != expected_ref.sequence
        or rebound_envelope.payload_fingerprint != expected_ref.payload_fingerprint
        or source_ref.stored_envelope_fingerprint
        != rebound_envelope.envelope_fingerprint
    ):
        raise ValueError("selected evidence stored authority rebind failed")
    evidence_attribution = build_frozen_fact(
        CompactionMemoryEvidenceAttributionFact,
        schema_version="compaction_memory_evidence_attribution.v1",
        evidence_semantic_fingerprint=evidence_semantic.evidence_semantic_fingerprint,
        source_event_reference=source_ref,
        source_run_id=source.run_id,
        source_turn_id=source.turn_id,
        source_reply_id=source.reply_id,
        source_message_id=source.current_user_message.message_id,
        original_text_sha256=source.current_user_message.content_sha256,
        original_text_utf8_bytes=len(source.current_user_message.text.encode("utf-8")),
        source_wire_semantic_fingerprint=(
            semantic.message_provider_semantic_fingerprint
        ),
        ordered_redaction_audits=sanitized.audits,
    )
    evidence_node_id = compaction_evidence_node_id(
        semantic=evidence_semantic,
        attribution=evidence_attribution,
    )
    return build_frozen_fact(
        CompactionMemoryEvidenceNodeFact,
        schema_version="compaction_memory_evidence_node.v1",
        evidence_node_id=evidence_node_id,
        semantic=evidence_semantic,
        input_projection=input_projection,
        attribution=evidence_attribution,
    )


def select_compaction_memory_extraction_input(
    *,
    runtime_session_id: str,
    compaction_id: str,
    extension_link: CompactionPostCompletionExtensionLinkFact,
    request_event_reference: GovernanceStoredEventReferenceFact,
    durable_job_id: str,
    durable_job_source_reference: DurableProjectionSourceEventReferenceFact,
    manifest_reference: CompactionHumanEvidenceManifestReferenceFact,
    archive: ArtifactStore,
    exact_source_resolver: Callable[
        [ContextEventReferenceFact], ExactHumanEvidenceSource
    ],
    resolved_budget: ResolvedExtractionInputBudgetAttributionFact,
    token_estimator: Callable[[str], int],
    prompt_contract_fingerprint: str,
    extraction_contract_fingerprint: str,
    deadline_monotonic: float,
    maximum_nodes: int = 256,
) -> SelectedCompactionMemoryExtractionInput:
    """Select complete messages only and exact-rebind at most 256 RunStart events."""

    if maximum_nodes < 1 or maximum_nodes > 256:
        raise ValueError("extraction evidence node bound is invalid")
    root = _load_manifest_root(
        reference=manifest_reference,
        archive=archive,
        runtime_session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    _validate_manifest_pages(
        root=root,
        archive=archive,
        runtime_session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    remaining_tokens = resolved_budget.usable_evidence_tokens
    remaining_bytes = resolved_budget.maximum_physical_input_utf8_bytes
    selected: list[tuple[object, object, object]] = []
    omitted_semantics: list[str] = []
    omitted_attributions: list[str] = []
    for page_index in range(root.page_count - 1, -1, -1):
        page = _read_manifest_page(
            root=root,
            page_index=page_index,
            archive=archive,
            runtime_session_id=runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        page_rows = zip(
            page.ordered_leaf_semantics,
            page.ordered_leaf_attributions,
            page.ordered_selection_projections,
            strict=True,
        )
        for semantic, attribution, projection in reversed(tuple(page_rows)):
            if len(selected) >= maximum_nodes:
                omitted_semantics.append(semantic.semantic_fingerprint)
                omitted_attributions.append(attribution.attribution_fingerprint)
                continue
            if isinstance(
                projection, CompactionHumanEvidenceArtifactSelectionProjectionFact
            ):
                omitted_semantics.append(semantic.semantic_fingerprint)
                omitted_attributions.append(attribution.attribution_fingerprint)
                continue
            if not isinstance(
                projection, CompactionHumanEvidenceInlineSelectionProjectionFact
            ):
                raise ValueError("unknown manifest selection projection")
            tokens = token_estimator(projection.sanitized_full_text)
            encoded_bytes = projection.sanitized_full_text_utf8_bytes
            if tokens < 0:
                raise ValueError("token estimator returned a negative value")
            if tokens > remaining_tokens or encoded_bytes > remaining_bytes:
                omitted_semantics.append(semantic.semantic_fingerprint)
                omitted_attributions.append(attribution.attribution_fingerprint)
                continue
            selected.append((semantic, attribution, projection))
            remaining_tokens -= tokens
            remaining_bytes -= encoded_bytes

    selected.sort(key=lambda item: item[1].source_sequence)
    nodes: list[CompactionMemoryEvidenceNodeFact] = []
    for semantic, attribution, projection in selected:
        resolved = exact_source_resolver(attribution.exact_run_start_event_reference)
        nodes.append(
            _build_node(
                semantic=semantic,
                attribution=attribution,
                projection=projection,
                resolved=resolved,
            )
        )

    evidence_semantics = tuple(node.semantic for node in nodes)
    semantic_accumulator = _accumulate(
        "compaction-memory-evidence-set-semantic:v1",
        tuple(item.evidence_semantic_fingerprint for item in evidence_semantics),
    )
    evidence_set = build_frozen_fact(
        CompactionMemoryEvidenceSetSemanticFact,
        schema_version="compaction_memory_evidence_set_semantic.v1",
        ordered_evidence_semantics=evidence_semantics,
        ordered_input_projection_fingerprints=tuple(
            node.input_projection.projection_fingerprint for node in nodes
        ),
        evidence_count=len(nodes),
        ordered_evidence_semantic_accumulator=semantic_accumulator,
        selection_contract_fingerprint=(
            EXTRACTION_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT
        ),
        sanitizer_contract_fingerprint=SANITIZER_CONTRACT_FINGERPRINT,
        input_projection_contract_fingerprint=(
            EXTRACTION_INPUT_PROJECTION_CONTRACT_FINGERPRINT
        ),
    )
    semantic = build_frozen_fact(
        CompactionMemoryExtractionInputSemanticFact,
        schema_version="compaction_memory_extraction_input_semantic.v1",
        evidence_set=evidence_set,
        prompt_contract_fingerprint=prompt_contract_fingerprint,
        input_codec_contract_fingerprint=EXTRACTION_INPUT_CODEC_CONTRACT_FINGERPRINT,
        extraction_contract_fingerprint=extraction_contract_fingerprint,
    )
    omission_semantic_accumulator = _accumulate(
        "compaction-memory-permanent-omission-semantic:v1",
        tuple(omitted_semantics),
    )
    omission_attribution_accumulator = _accumulate(
        "compaction-memory-permanent-omission-attribution:v1",
        tuple(omitted_attributions),
    )
    attribution = build_frozen_fact(
        CompactionMemoryExtractionInputAttributionFact,
        schema_version="compaction_memory_extraction_input_attribution.v1",
        compaction_id=compaction_id,
        extension_link=extension_link,
        request_event_reference=request_event_reference,
        durable_job_id=durable_job_id,
        durable_job_source_reference_fingerprint=(
            durable_job_source_reference.reference_fingerprint
        ),
        human_evidence_manifest_reference=manifest_reference,
        ordered_evidence_attributions=tuple(node.attribution for node in nodes),
        resolved_input_budget_attribution=resolved_budget,
        permanent_omission_count=len(omitted_semantics),
        permanent_omission_semantic_accumulator=omission_semantic_accumulator,
        permanent_omission_attribution_accumulator=omission_attribution_accumulator,
    )
    document = build_frozen_fact(
        CompactionMemoryExtractionInputDocumentFact,
        schema_version="compaction_memory_extraction_input_document.v1",
        semantic=semantic,
        attribution=attribution,
    )
    canonical_input = canonical_json_bytes(
        {
            "schema_version": "compaction_memory_extraction_model_input.v1",
            "evidence_nodes": tuple(
                {
                    "evidence_node_id": node.evidence_node_id,
                    "text": node.input_projection.projected_text,
                }
                for node in nodes
            ),
        }
    ).decode("utf-8")
    return SelectedCompactionMemoryExtractionInput(
        document=document,
        ordered_nodes=tuple(nodes),
        canonical_input_utf8=canonical_input,
        source_eligible_leaf_count=root.eligible_leaf_count,
        permanent_omission_count=len(omitted_semantics),
        permanent_omission_semantic_accumulator=omission_semantic_accumulator,
        permanent_omission_attribution_accumulator=omission_attribution_accumulator,
    )


def restore_selected_compaction_memory_extraction_input(
    document: CompactionMemoryExtractionInputDocumentFact,
) -> SelectedCompactionMemoryExtractionInput:
    """Rebuild the immutable model input view from its confirmed document."""

    semantics = document.semantic.evidence_set.ordered_evidence_semantics
    attributions = document.attribution.ordered_evidence_attributions
    projection_fingerprints = (
        document.semantic.evidence_set.ordered_input_projection_fingerprints
    )
    if not (len(semantics) == len(attributions) == len(projection_fingerprints)):
        raise ValueError("extraction input evidence cardinality drifted")
    nodes: list[CompactionMemoryEvidenceNodeFact] = []
    for semantic, attribution, expected_projection_fingerprint in zip(
        semantics,
        attributions,
        projection_fingerprints,
        strict=True,
    ):
        projection = build_frozen_fact(
            CompactionMemoryEvidenceInputProjectionFact,
            schema_version="compaction_memory_evidence_input_projection.v1",
            projection_kind="full",
            evidence_semantic_fingerprint=semantic.evidence_semantic_fingerprint,
            projected_text=semantic.sanitized_full_message_text,
            projected_text_sha256=semantic.sanitized_full_message_sha256,
            projected_text_utf8_bytes=semantic.sanitized_full_message_utf8_bytes,
            projection_contract_fingerprint=(
                EXTRACTION_INPUT_PROJECTION_CONTRACT_FINGERPRINT
            ),
        )
        if projection.projection_fingerprint != expected_projection_fingerprint:
            raise ValueError("extraction input projection fingerprint drifted")
        evidence_node_id = compaction_evidence_node_id(
            semantic=semantic,
            attribution=attribution,
        )
        nodes.append(
            build_frozen_fact(
                CompactionMemoryEvidenceNodeFact,
                schema_version="compaction_memory_evidence_node.v1",
                evidence_node_id=evidence_node_id,
                semantic=semantic,
                input_projection=projection,
                attribution=attribution,
            )
        )
    canonical_input = canonical_json_bytes(
        {
            "schema_version": "compaction_memory_extraction_model_input.v1",
            "evidence_nodes": tuple(
                {
                    "evidence_node_id": node.evidence_node_id,
                    "text": node.input_projection.projected_text,
                }
                for node in nodes
            ),
        }
    ).decode("utf-8")
    return SelectedCompactionMemoryExtractionInput(
        document=document,
        ordered_nodes=tuple(nodes),
        canonical_input_utf8=canonical_input,
        source_eligible_leaf_count=(
            len(nodes) + document.attribution.permanent_omission_count
        ),
        permanent_omission_count=document.attribution.permanent_omission_count,
        permanent_omission_semantic_accumulator=(
            document.attribution.permanent_omission_semantic_accumulator
        ),
        permanent_omission_attribution_accumulator=(
            document.attribution.permanent_omission_attribution_accumulator
        ),
    )


__all__ = [
    "EXTRACTION_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT",
    "EXTRACTION_INPUT_CODEC_CONTRACT_FINGERPRINT",
    "EXTRACTION_INPUT_PROJECTION_CONTRACT_FINGERPRINT",
    "ExactHumanEvidenceSource",
    "SelectedCompactionMemoryExtractionInput",
    "restore_selected_compaction_memory_extraction_input",
    "select_compaction_memory_extraction_input",
]
