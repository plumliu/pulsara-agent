"""Typed AP5 authority joining transcript projection and named context facts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from pulsara_agent.memory.foundation.records import ArtifactRecord
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    PreparedContextCandidateSet,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.frozen import PreparedRuntimeValueBase
from pulsara_agent.primitives.transcript_projection import (
    ContextTranscriptAuthorityFact,
    ContextTranscriptProviderSemanticIdentityFact,
    ModelVisibleNamedFactArtifactReferenceFact,
    ModelVisibleNamedFactSelectionEntryFact,
    ModelVisibleNamedFactSelectionFact,
    ModelVisibleNamedFactSemanticSelectionFact,
    TranscriptProviderProjectionFact,
)
from pulsara_agent.runtime.authority_materialization.checkpoint_service import (
    PreparedTranscriptProjectionEvidence,
)
from pulsara_agent.runtime.context_input.event_slice import FrozenStoredEvent
from pulsara_agent.runtime.context_input.provider_projection import (
    PreparedTranscriptProviderProjectionFact,
)
from pulsara_agent.runtime.context_input.transcript import (
    NormalizedContextTranscript,
)


@dataclass(frozen=True, slots=True)
class PreparedNamedFactArtifact:
    reference: ModelVisibleNamedFactArtifactReferenceFact
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        digest = f"sha256:{sha256(self.canonical_bytes).hexdigest()}"
        if (
            self.reference.artifact_sha256 != digest
            or self.reference.artifact_byte_count != len(self.canonical_bytes)
            or self.reference.semantic_content_fingerprint != digest
        ):
            raise ValueError("prepared named-fact artifact identity mismatch")


@dataclass(frozen=True, slots=True)
class PreparedTranscriptProjectionInput(PreparedRuntimeValueBase):
    projection_evidence: PreparedTranscriptProjectionEvidence
    exact_named_authority_facts: tuple[FrozenStoredEvent, ...]
    exact_named_authority_artifacts: tuple[PreparedNamedFactArtifact, ...]
    final_normalized_transcript: NormalizedContextTranscript
    provider_projection: PreparedTranscriptProviderProjectionFact
    authority: ContextTranscriptAuthorityFact
    materialization_fact_fingerprint: str

    def __post_init__(self) -> None:
        evidence = self.projection_evidence
        authority = self.authority
        if (
            authority.projection_base != evidence.projection_base
            or authority.semantic_source != evidence.semantic_source
            or authority.domain_completeness_proof
            != evidence.domain_completeness_proof
            or authority.provider_projection
            != self.provider_projection.projection_fact
            or authority.final_normalized_transcript_fingerprint
            != self.final_normalized_transcript.transcript.transcript_fingerprint
        ):
            raise ValueError("prepared transcript projection authority mismatch")
        expected_delta = tuple(
            (
                item.runtime_session_id,
                item.sequence,
                item.event_id,
                item.event_type,
                item.payload_fingerprint,
            )
            for item in authority.transcript_domain_delta_refs
        )
        actual_delta = tuple(
            (
                item.runtime_session_id,
                item.sequence,
                item.event_id,
                item.event_type,
                item.payload_fingerprint,
            )
            for item in evidence.semantic_delta_events
        )
        if expected_delta != actual_delta:
            raise ValueError("prepared transcript semantic delta mismatch")
        expected_event_refs = tuple(
            sorted(
                {
                    (
                        ref.runtime_session_id,
                        ref.sequence,
                        ref.event_id,
                        ref.event_type,
                        ref.payload_fingerprint,
                    )
                    for entry in authority.named_fact_selection.entries
                    for ref in entry.source_refs
                }
            )
        )
        actual_event_refs = tuple(
            (
                item.runtime_session_id,
                item.sequence,
                item.event_id,
                item.event_type,
                item.payload_fingerprint,
            )
            for item in self.exact_named_authority_facts
        )
        if actual_event_refs != expected_event_refs:
            raise ValueError("prepared named-fact event authority is incomplete")
        expected_artifact_refs = tuple(
            sorted(
                {
                    ref.reference_fingerprint
                    for entry in authority.named_fact_selection.entries
                    for ref in entry.source_artifact_refs
                }
            )
        )
        actual_artifact_refs = tuple(
            sorted(
                item.reference.reference_fingerprint
                for item in self.exact_named_authority_artifacts
            )
        )
        if actual_artifact_refs != expected_artifact_refs:
            raise ValueError("prepared named-fact artifact authority is incomplete")
        expected_fingerprint = _prepared_projection_input_fingerprint(
            evidence=evidence,
            exact_named_authority_facts=self.exact_named_authority_facts,
            exact_named_authority_artifacts=(
                self.exact_named_authority_artifacts
            ),
            normalized=self.final_normalized_transcript,
            provider_projection=self.provider_projection,
            authority=authority,
        )
        if self.materialization_fact_fingerprint != expected_fingerprint:
            raise ValueError("prepared transcript projection fingerprint mismatch")


def prepare_named_fact_artifact(
    *,
    record: ArtifactRecord,
    canonical_bytes: bytes,
) -> PreparedNamedFactArtifact:
    digest = f"sha256:{sha256(canonical_bytes).hexdigest()}"
    if record.digest != digest or record.size_bytes != len(canonical_bytes):
        raise ValueError("named-fact artifact read differs from stored identity")
    metadata = record.metadata or {}
    contract = metadata.get("artifact_contract_fingerprint")
    if not isinstance(contract, str) or not contract.startswith("sha256:"):
        contract = context_fingerprint(
            "model-visible-named-fact-artifact-contract:v1",
            {"media_type": record.media_type},
        )
    reference = build_frozen_fact(
        ModelVisibleNamedFactArtifactReferenceFact,
        schema_version="model_visible_named_fact_artifact_ref.v1",
        artifact_id=record.id,
        artifact_sha256=digest,
        artifact_byte_count=len(canonical_bytes),
        semantic_content_fingerprint=digest,
        artifact_contract_fingerprint=contract,
    )
    return PreparedNamedFactArtifact(
        reference=reference,
        canonical_bytes=canonical_bytes,
    )


def build_model_visible_named_fact_selection(
    *,
    semantic_selection: ModelVisibleNamedFactSemanticSelectionFact,
    prepared_candidates: PreparedContextCandidateSet,
    prepared_artifacts: tuple[PreparedNamedFactArtifact, ...],
    fallback_source_ref: ContextEventReferenceFact,
) -> ModelVisibleNamedFactSelectionFact:
    candidates = {
        entry.candidate.source_instance_id: entry.candidate
        for entry in prepared_candidates.entries
    }
    artifacts = {item.reference.artifact_id: item for item in prepared_artifacts}
    if len(artifacts) != len(prepared_artifacts):
        raise ValueError("prepared named-fact artifact IDs are not unique")
    entries: list[ModelVisibleNamedFactSelectionEntryFact] = []
    for semantic in semantic_selection.selected_items:
        try:
            candidate = candidates[semantic.semantic_key]
        except KeyError as exc:
            raise ValueError("named semantic item has no prepared candidate") from exc
        if candidate.source_kind != semantic.source_kind:
            raise ValueError("named semantic item source kind drifted")
        artifact_refs = []
        for artifact_id in candidate.source_artifact_ids:
            try:
                prepared = artifacts[artifact_id]
            except KeyError as exc:
                raise ValueError(
                    "named semantic item artifact was not read-confirmed"
                ) from exc
            artifact_refs.append(prepared.reference)
        source_refs = candidate.source_fact_refs or (fallback_source_ref,)
        entries.append(
            build_frozen_fact(
                ModelVisibleNamedFactSelectionEntryFact,
                schema_version="model_visible_named_fact_selection_entry.v1",
                semantic_identity=semantic,
                source_refs=tuple(
                    sorted(
                        source_refs,
                        key=lambda item: (
                            item.runtime_session_id,
                            item.sequence,
                            item.event_id,
                        ),
                    )
                ),
                source_artifact_refs=tuple(
                    sorted(artifact_refs, key=lambda item: item.artifact_id)
                ),
            )
        )
    return build_frozen_fact(
        ModelVisibleNamedFactSelectionFact,
        schema_version="model_visible_named_fact_selection.v2",
        semantic_selection=semantic_selection,
        entries=tuple(entries),
    )


def build_context_transcript_authority(
    *,
    evidence: PreparedTranscriptProjectionEvidence,
    final_normalized_transcript_fingerprint: str,
    provider_projection: TranscriptProviderProjectionFact,
    named_fact_selection: ModelVisibleNamedFactSelectionFact,
) -> ContextTranscriptAuthorityFact:
    base = evidence.projection_base.common
    provider_identity = build_frozen_fact(
        ContextTranscriptProviderSemanticIdentityFact,
        schema_version="context_transcript_provider_semantic_identity.v2",
        projection_base_semantic_fingerprint=(
            base.semantic_identity.semantic_fingerprint
        ),
        semantic_source_fingerprint=(
            evidence.semantic_source.semantic_source_fingerprint
        ),
        stable_state_semantic_fingerprint=(
            base.stable_semantic_state.state_semantic_fingerprint
        ),
        final_normalized_transcript_fingerprint=(
            final_normalized_transcript_fingerprint
        ),
        invocation_provider_projection_semantic_fingerprint=(
            provider_projection.semantic_identity.semantic_fingerprint
        ),
        named_facts_semantic_fingerprint=(
            named_fact_selection.semantic_selection.named_facts_semantic_fingerprint
        ),
    )
    delta_refs = tuple(
        ContextEventReferenceFact(
            runtime_session_id=event.runtime_session_id,
            event_id=event.event_id,
            sequence=event.sequence,
            event_type=event.event_type,
            payload_fingerprint=event.payload_fingerprint,
        )
        for event in evidence.semantic_delta_events
    )
    return build_frozen_fact(
        ContextTranscriptAuthorityFact,
        schema_version="context_transcript_authority.v6",
        projection_base=evidence.projection_base,
        semantic_source=evidence.semantic_source,
        provider_semantic_identity=provider_identity,
        provider_projection=provider_projection,
        named_fact_selection=named_fact_selection,
        final_normalized_transcript_fingerprint=(
            final_normalized_transcript_fingerprint
        ),
        transcript_domain_delta_refs=delta_refs,
        domain_completeness_proof=evidence.domain_completeness_proof,
    )


def prepare_transcript_projection_input(
    *,
    evidence: PreparedTranscriptProjectionEvidence,
    normalized: NormalizedContextTranscript,
    provider_projection: PreparedTranscriptProviderProjectionFact,
    semantic_selection: ModelVisibleNamedFactSemanticSelectionFact,
    prepared_candidates: PreparedContextCandidateSet,
    prepared_artifacts: tuple[PreparedNamedFactArtifact, ...],
    fallback_source_ref: ContextEventReferenceFact,
    authority_events: tuple[FrozenStoredEvent, ...],
) -> PreparedTranscriptProjectionInput:
    """Join every AP5 transcript input under one immutable proof owner."""

    named_selection = build_model_visible_named_fact_selection(
        semantic_selection=semantic_selection,
        prepared_candidates=prepared_candidates,
        prepared_artifacts=prepared_artifacts,
        fallback_source_ref=fallback_source_ref,
    )
    authority = build_context_transcript_authority(
        evidence=evidence,
        final_normalized_transcript_fingerprint=(
            normalized.transcript.transcript_fingerprint
        ),
        provider_projection=provider_projection.projection_fact,
        named_fact_selection=named_selection,
    )
    events_by_key: dict[tuple[str, str], FrozenStoredEvent] = {}
    for event in authority_events:
        key = (event.runtime_session_id, event.event_id)
        existing = events_by_key.get(key)
        if existing is not None and existing != event:
            raise ValueError("named-fact authority event identity is ambiguous")
        events_by_key[key] = event
    named_refs = {
        (
            ref.runtime_session_id,
            ref.sequence,
            ref.event_id,
            ref.event_type,
            ref.payload_fingerprint,
        )
        for entry in named_selection.entries
        for ref in entry.source_refs
    }
    exact_events: list[FrozenStoredEvent] = []
    for runtime_session_id, sequence, event_id, event_type, payload in sorted(
        named_refs
    ):
        try:
            event = events_by_key[(runtime_session_id, event_id)]
        except KeyError as exc:
            raise ValueError("named-fact authority event was not frozen") from exc
        if (
            event.sequence != sequence
            or event.event_type != event_type
            or event.payload_fingerprint != payload
        ):
            raise ValueError("named-fact authority event drifted")
        exact_events.append(event)
    selected_artifact_ids = {
        ref.artifact_id
        for entry in named_selection.entries
        for ref in entry.source_artifact_refs
    }
    artifacts_by_id = {
        item.reference.artifact_id: item for item in prepared_artifacts
    }
    if len(artifacts_by_id) != len(prepared_artifacts):
        raise ValueError("prepared named-fact artifact IDs are not unique")
    try:
        exact_artifacts = tuple(
            artifacts_by_id[artifact_id]
            for artifact_id in sorted(selected_artifact_ids)
        )
    except KeyError as exc:
        raise ValueError("selected named-fact artifact was not prepared") from exc
    fingerprint = _prepared_projection_input_fingerprint(
        evidence=evidence,
        exact_named_authority_facts=tuple(exact_events),
        exact_named_authority_artifacts=exact_artifacts,
        normalized=normalized,
        provider_projection=provider_projection,
        authority=authority,
    )
    return PreparedTranscriptProjectionInput(
        projection_evidence=evidence,
        exact_named_authority_facts=tuple(exact_events),
        exact_named_authority_artifacts=exact_artifacts,
        final_normalized_transcript=normalized,
        provider_projection=provider_projection,
        authority=authority,
        materialization_fact_fingerprint=fingerprint,
    )


def _prepared_projection_input_fingerprint(
    *,
    evidence: PreparedTranscriptProjectionEvidence,
    exact_named_authority_facts: tuple[FrozenStoredEvent, ...],
    exact_named_authority_artifacts: tuple[PreparedNamedFactArtifact, ...],
    normalized: NormalizedContextTranscript,
    provider_projection: PreparedTranscriptProviderProjectionFact,
    authority: ContextTranscriptAuthorityFact,
) -> str:
    return context_fingerprint(
        "prepared-transcript-projection-input:v1",
        {
            "projection_base": evidence.projection_base.fact_fingerprint,
            "semantic_source": evidence.semantic_source.semantic_source_fingerprint,
            "domain_proof": (
                evidence.domain_completeness_proof.completeness_fingerprint
            ),
            "semantic_delta": tuple(
                item.envelope_fingerprint for item in evidence.semantic_delta_events
            ),
            "named_events": tuple(
                item.envelope_fingerprint for item in exact_named_authority_facts
            ),
            "named_artifacts": tuple(
                item.reference.reference_fingerprint
                for item in exact_named_authority_artifacts
            ),
            "transcript": normalized.transcript.transcript_fingerprint,
            "provider_projection": (
                provider_projection.projection_fact.fact_fingerprint
            ),
            "authority": authority.fact_fingerprint,
        },
    )


__all__ = [
    "PreparedNamedFactArtifact",
    "PreparedTranscriptProjectionInput",
    "build_context_transcript_authority",
    "build_model_visible_named_fact_selection",
    "prepare_transcript_projection_input",
    "prepare_named_fact_artifact",
]
