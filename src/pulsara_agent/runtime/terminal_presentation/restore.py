"""Bounded presentation-aware restore of the canonical transcript spine."""

from __future__ import annotations

from dataclasses import dataclass, replace

from pulsara_agent.event_log.protocol import EventLog
from pulsara_agent.llm.terminal_projection import hydrate_terminal_projection_text
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.presentation_checkpoint_storage import (
    PresentationHistorySpineAccelerationFact,
)
from pulsara_agent.primitives.terminal_presentation import (
    CanonicalTranscriptPlacementAnchorReferenceFact,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    projection_references,
)
from pulsara_agent.runtime.authority_materialization.contracts import (
    AuthorityMaterializationContractBundle,
)
from pulsara_agent.runtime.authority_materialization.transcript_restore import (
    RestoredTranscriptProjection,
    restore_transcript_projection,
)
from pulsara_agent.ports.stored_event import JoinedRawStoredEventRangeProof
from pulsara_agent.primitives.terminal_presentation import RestoredRangeFoldResult
from pulsara_agent.runtime.authority_materialization.transcript_tree import (
    TranscriptProjectionMaterializationContracts,
)


@dataclass(frozen=True, slots=True)
class PresentationAwareTranscriptRestore:
    transcript_restore: RestoredTranscriptProjection
    presentation_catch_up_range: JoinedRawStoredEventRangeProof | None
    presentation_catch_up_fold: RestoredRangeFoldResult | None


def restore_transcript_with_presentation_spine(
    *,
    current_restore: RestoredTranscriptProjection,
    acceleration: PresentationHistorySpineAccelerationFact | None,
    event_log: EventLog,
    archive: ArtifactStore,
    runtime_session_id: str,
    requested_through_sequence: int,
    authority_contracts: AuthorityMaterializationContractBundle,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    deadline_monotonic: float | None,
    allow_seedless_test_bootstrap: bool,
) -> PresentationAwareTranscriptRestore:
    """Restore a checkpoint-proved anchor spine and fold one bounded raw suffix.

    The ordinary transcript restore remains the semantic oracle.  This function
    only replaces its process-local store after independently rebuilding the same
    semantic state from the presentation checkpoint high-water and proving exact
    equivalence at the requested high-water.
    """

    if acceleration is None:
        return PresentationAwareTranscriptRestore(
            transcript_restore=current_restore,
            presentation_catch_up_range=None,
            presentation_catch_up_fold=None,
        )
    acceleration.__class__.model_validate(acceleration)
    checkpoint_high_water = acceleration.through_authority_sequence
    if acceleration.runtime_session_id != runtime_session_id:
        raise ValueError("presentation spine acceleration crosses sessions")
    if checkpoint_high_water > requested_through_sequence:
        raise ValueError("presentation checkpoint is ahead of the canonical ledger")

    restored_at_checkpoint = restore_transcript_projection(
        event_log=event_log,
        archive=archive,
        runtime_session_id=runtime_session_id,
        requested_through_sequence=checkpoint_high_water,
        event_domain_binding=authority_contracts.event_domain,
        materialization_contracts=materialization_contracts,
        limits=authority_contracts.limits,
        deadline_monotonic=deadline_monotonic,
        allow_seedless_test_bootstrap=allow_seedless_test_bootstrap,
    )
    anchors = _current_anchor_references(acceleration)
    if tuple(
        item.fact_fingerprint
        for item in restored_at_checkpoint.state_store.stable_entries()
    ) != tuple(
        item.transcript_entry_fact_fingerprint
        for item in acceleration.ordered_entries
        if item.anchor_state_kind == "current"
    ):
        raise ValueError(
            "presentation spine does not match checkpoint transcript leaves"
        )
    restored_at_checkpoint.state_store.restore_placement_spine(anchors)

    proof = None
    restored_fold = None
    if checkpoint_high_water < requested_through_sequence:
        proof = event_log.read_joined_raw_range(
            source_kind="reopen_restore",
            from_sequence_exclusive=checkpoint_high_water,
            through_sequence=requested_through_sequence,
            max_events=authority_contracts.limits.max_unreclaimable_ledger_events,
            max_payload_bytes=(
                authority_contracts.limits.max_unreclaimable_charged_payload_bytes
            ),
            deadline_monotonic=deadline_monotonic,
        )
        if proof is None:
            raise ValueError("presentation restore lost its non-empty ledger suffix")
        _hydrate_tail_projection_documents(
            proof_events=proof.owned_stored_events,
            documents=restored_at_checkpoint.document_registry,
            archive=archive,
            runtime_session_id=runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        restored_fold = restored_at_checkpoint.state_store.fold_restored_range(proof)

    expected = current_restore.state_store.snapshot()
    actual = restored_at_checkpoint.state_store.snapshot()
    if actual != expected:
        raise ValueError(
            "presentation-aware transcript restore is not semantically equivalent"
        )
    if (
        restored_at_checkpoint.state_store.stable_entries()
        != current_restore.state_store.stable_entries()
    ):
        raise ValueError("presentation-aware transcript leaf projection drifted")

    return PresentationAwareTranscriptRestore(
        transcript_restore=replace(
            current_restore,
            state_store=restored_at_checkpoint.state_store,
            document_registry=restored_at_checkpoint.document_registry,
            stable_entries=restored_at_checkpoint.state_store.stable_entries(),
        ),
        presentation_catch_up_range=proof,
        presentation_catch_up_fold=restored_fold,
    )


def _current_anchor_references(
    acceleration: PresentationHistorySpineAccelerationFact,
) -> tuple[CanonicalTranscriptPlacementAnchorReferenceFact, ...]:
    result: list[CanonicalTranscriptPlacementAnchorReferenceFact] = []
    for item in acceleration.ordered_entries:
        if item.anchor_state_kind != "current":
            continue
        reference = CanonicalTranscriptPlacementAnchorReferenceFact(
            schema_version="canonical_transcript_placement_anchor_reference.v1",
            placement_key_contract_id=acceleration.placement_key_contract_id,
            placement_key_contract_version=acceleration.placement_key_contract_version,
            placement_key_contract_fingerprint=(
                acceleration.placement_key_contract_fingerprint
            ),
            transcript_anchor_id=item.transcript_anchor_id,
            stable_anchor_slot_key=item.stable_anchor_slot_key,
            stable_first_spine_coordinate=item.stable_first_spine_coordinate,
            stable_last_spine_coordinate=item.stable_last_spine_coordinate,
            anchor_fingerprint=item.anchor_fingerprint,
            anchor_reference_fingerprint=item.anchor_reference_fingerprint,
        )
        reference.__post_init__()
        result.append(reference)
    return tuple(result)


def _hydrate_tail_projection_documents(
    *,
    proof_events,
    documents,
    archive: ArtifactStore,
    runtime_session_id: str,
    deadline_monotonic: float | None,
) -> None:
    by_fingerprint = {
        reference.reference_fingerprint: reference
        for reference in projection_references(tuple(proof_events))
    }
    for reference in by_fingerprint.values():
        if documents.contains(reference):
            continue
        text = archive.get_text(
            reference.document_artifact_id,
            session_id=runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        documents.register(reference, hydrate_terminal_projection_text(reference, text))


__all__ = [
    "PresentationAwareTranscriptRestore",
    "restore_transcript_with_presentation_spine",
]
