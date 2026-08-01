"""Canonical transcript/audit to renderer-neutral history projection."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from typing import Iterable, Literal

from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_history import (
    AfterTranscriptLeafAuditAnchorFact,
    AssistantMessageCell,
    AuditCell,
    BeforeTranscriptLeafAuditAnchorFact,
    CanonicalTranscriptHistorySourceFact,
    DurableAuditHistorySourceFact,
    DurableHistoryCell,
    LedgerSequenceAuditAnchorFact,
    PresentationDataContentBlockFact,
    PresentationHistoryEntryFact,
    PresentationHistoryPlacementKeyFact,
    PresentationHistoryPlacementKeyContractFact,
    PresentationHistoryTailFoldSegmentFact,
    PresentationHistoryTailMutationFact,
    PresentationTextContentBlockFact,
    ToolTerminalCell,
    UpsertPresentationHistoryEntryMutationFact,
    UserPromptCell,
    RemovePresentationHistoryEntryMutationFact,
    build_presentation_history_placement_key,
)
from pulsara_agent.primitives.presentation_checkpoint_storage import (
    PresentationHistorySpineAccelerationFact,
    PresentationHistorySpineEntryAccelerationFact,
)
from pulsara_agent.primitives.storage_frozen import build_frozen_storage_fact
from pulsara_agent.primitives.terminal_presentation import (
    CanonicalTranscriptLeafChangeFact,
    CanonicalTranscriptPlacementAnchorReferenceFact,
    CanonicalTranscriptPlacementAnchorTombstoneFact,
    CommittedPresentationTapEntry,
    RestoredRangeFoldResult,
    TranscriptAuditDispositionFact,
)
from pulsara_agent.ports.stored_event import JoinedRawStoredEventRangeProof
from pulsara_agent.primitives.terminal_projection import (
    CanonicalToolResultDataBlockSemanticFact,
    CanonicalToolResultTextBlockSemanticFact,
    ModelDataBlockSemanticFact,
    ModelProviderErrorSemanticFact,
    ModelTextBlockSemanticFact,
    ModelThinkingBlockSemanticFact,
    ModelToolCallBlockSemanticFact,
    TerminalArtifactContentReferenceFact,
    TerminalInlineContentFact,
    ToolTerminalProjectionPayloadFact,
)
from pulsara_agent.primitives.transcript_projection import (
    InlineNormalizedMessageContentFact,
    NormalizedMessageContentArtifactFact,
    NormalizedMessageContentArtifactReferenceFact,
    TerminalProjectionMessageContentRefFact,
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionLeafEntryReferenceFact,
    TranscriptProviderDataPlaceholderSemanticFact,
    TranscriptProviderTextBlockSemanticFact,
    TranscriptProviderThinkingBlockSemanticFact,
    TranscriptProviderToolCallBlockSemanticFact,
    TranscriptProviderToolResultRefSemanticFact,
    TranscriptToolPairLeafEntryFact,
    TranscriptToolResultLeafEntryFact,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
    TRANSCRIPT_PROJECTION_REDUCER_ID,
    TRANSCRIPT_PROJECTION_REDUCER_VERSION,
    TranscriptProjectionDocumentRegistry,
)
from pulsara_agent.runtime.terminal_presentation.policy import (
    ExtractedDurableAuditCell,
    PresentationAuditExtractorBinding,
    PresentationPurposePolicyRegistry,
)


_TRANSCRIPT_DISPOSITION_EXTRACTOR_ID = (
    "pulsara.presentation.transcript-audit-disposition"
)
_TRANSCRIPT_DISPOSITION_EXTRACTOR_VERSION = "1"
_TRANSCRIPT_DISPOSITION_EXTRACTOR_FINGERPRINT = context_fingerprint(
    "presentation-transcript-audit-disposition-extractor:v1",
    (
        "suppressed_model_output",
        "recovered_transcript",
        "rejected_transcript_candidate",
    ),
)


@dataclass(frozen=True, slots=True)
class PresentationProjectionApplyResult:
    from_sequence_exclusive: int
    through_sequence: int
    base_projection_revision: int
    resulting_projection_revision: int
    ordered_segments: tuple[PresentationHistoryTailFoldSegmentFact, ...]
    ordered_mutations: tuple[PresentationHistoryTailMutationFact, ...]
    resulting_entry_count: int
    resulting_entry_accumulator: str
    result_fingerprint: str


@dataclass(frozen=True, slots=True)
class PresentationProjectionSnapshot:
    runtime_session_id: str
    through_authority_sequence: int
    projection_revision: int
    ordered_entries: tuple[PresentationHistoryEntryFact, ...]
    ordered_tail_segments: tuple[PresentationHistoryTailFoldSegmentFact, ...]
    canonical_spine_fingerprint: str
    entry_accumulator: str
    ordered_entries_complete: bool
    spine_acceleration: PresentationHistorySpineAccelerationFact
    snapshot_fingerprint: str


@dataclass(slots=True)
class _AnchorState:
    reference: CanonicalTranscriptPlacementAnchorReferenceFact
    first_ordering_boundary_sequence: int
    last_ordering_boundary_sequence: int
    current_leaf_fact_fingerprint: str | None
    tombstone_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class _KnownEntryIdentity:
    history_entry_id: str
    placement_key: PresentationHistoryPlacementKeyFact
    entry_fingerprint: str
    transcript_anchor_slot_key: str | None


class PresentationHistoryProjectionOwner:
    """Single owner for global placement of transcript and durable-audit cells."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        placement_contract: PresentationHistoryPlacementKeyContractFact,
        purpose_policy: PresentationPurposePolicyRegistry,
        audit_extractor: PresentationAuditExtractorBinding,
        transcript_documents: TranscriptProjectionDocumentRegistry,
        archive: ArtifactStore,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.placement_contract = placement_contract
        self.purpose_policy = purpose_policy
        self.audit_extractor = audit_extractor
        self.transcript_documents = transcript_documents
        self.archive = archive
        self._lock = RLock()
        self._through_sequence = 0
        self._projection_revision = 0
        self._entries_by_id: dict[str, PresentationHistoryEntryFact] = {}
        self._known_entry_identities: dict[str, _KnownEntryIdentity] = {}
        self._entry_id_by_anchor_slot: dict[str, str] = {}
        self._anchors_by_slot: dict[str, _AnchorState] = {}
        self._anchor_slot_by_leaf_fingerprint: dict[str, str] = {}
        self._transcript_entry_fingerprint_by_slot: dict[str, str] = {}
        self._tail_segments: list[PresentationHistoryTailFoldSegmentFact] = []
        self._ordered_entry_set_complete = True

    @property
    def through_sequence(self) -> int:
        with self._lock:
            return self._through_sequence

    @property
    def projection_revision(self) -> int:
        with self._lock:
            return self._projection_revision

    def apply_committed_tap_entry(
        self, entry: CommittedPresentationTapEntry
    ) -> PresentationProjectionApplyResult:
        entry.__post_init__()
        if entry.runtime_session_id != self.runtime_session_id:
            raise ValueError("presentation projection tap entry crosses sessions")
        with self._lock:
            if entry.source_first_sequence != self._through_sequence + 1:
                raise ValueError("presentation projection tap input is not contiguous")
            before = self._capture_state()
            base_revision = self._projection_revision
            try:
                result = self._apply_entry_unlocked(entry, base_revision=base_revision)
            except BaseException:
                self._restore_state(before)
                raise
            return result

    def apply_restored_range(
        self,
        range_proof: JoinedRawStoredEventRangeProof,
        fold_result: RestoredRangeFoldResult,
    ) -> PresentationProjectionApplyResult:
        """Apply historical authority without pretending it was one commit batch."""

        range_proof.__post_init__()
        if range_proof.runtime_session_id != self.runtime_session_id:
            raise ValueError("presentation restored range crosses sessions")
        if (
            fold_result.source_range_proof_fingerprint
            != range_proof.range_proof_fingerprint
            or fold_result.source_first_sequence
            != range_proof.from_sequence_exclusive + 1
            or fold_result.source_last_sequence != range_proof.through_sequence
        ):
            raise ValueError("presentation restored range/fold join mismatch")
        with self._lock:
            if range_proof.from_sequence_exclusive != self._through_sequence:
                raise ValueError("presentation restored range is not contiguous")
            before = self._capture_state()
            base_revision = self._projection_revision
            try:
                return self._apply_source_unlocked(
                    raw_envelopes=range_proof.raw_stored_envelopes,
                    fold=fold_result.fold_delta,
                    base_revision=base_revision,
                )
            except BaseException:
                self._restore_state(before)
                raise

    def snapshot(self) -> PresentationProjectionSnapshot:
        with self._lock:
            ordered = self._ordered_entries_unlocked()
            accumulator = _entry_accumulator(ordered)
            spine = _spine_fingerprint(self._anchors_by_slot.values())
            acceleration = self._spine_acceleration_unlocked(spine)
            payload = {
                "runtime_session_id": self.runtime_session_id,
                "through_authority_sequence": self._through_sequence,
                "projection_revision": self._projection_revision,
                "ordered_entry_fingerprints": tuple(
                    item.entry_fingerprint for item in ordered
                ),
                "ordered_tail_segment_fingerprints": tuple(
                    item.segment_fingerprint for item in self._tail_segments
                ),
                "canonical_spine_fingerprint": spine,
                "entry_accumulator": accumulator,
                "ordered_entries_complete": self._ordered_entry_set_complete,
            }
            return PresentationProjectionSnapshot(
                runtime_session_id=self.runtime_session_id,
                through_authority_sequence=self._through_sequence,
                projection_revision=self._projection_revision,
                ordered_entries=ordered,
                ordered_tail_segments=tuple(self._tail_segments),
                canonical_spine_fingerprint=spine,
                entry_accumulator=accumulator,
                ordered_entries_complete=self._ordered_entry_set_complete,
                spine_acceleration=acceleration,
                snapshot_fingerprint=context_fingerprint(
                    "presentation-projection-snapshot:v1", payload
                ),
            )

    def acknowledge_checkpoint(
        self,
        *,
        through_sequence: int,
        projection_revision: int,
    ) -> None:
        """Consume only the checkpoint-covered tail prefix.

        Checkpoint persistence is allowed to overlap later live commits.  The
        reducer therefore cannot clear the whole tail after a FULL result; it
        must retain every one-sequence segment after the frozen candidate cut.
        The checkpoint owns the client-visible revision even when its consumed
        prefix contains only no-op segments.
        """

        with self._lock:
            if through_sequence > self._through_sequence:
                raise ValueError("presentation checkpoint moved past live authority")
            if projection_revision < self._projection_revision:
                raise ValueError("presentation checkpoint revision moved backwards")
            self._tail_segments = [
                item
                for item in self._tail_segments
                if item.through_sequence > through_sequence
            ]
            self._projection_revision = projection_revision

    def _apply_entry_unlocked(
        self,
        entry: CommittedPresentationTapEntry,
        *,
        base_revision: int,
    ) -> PresentationProjectionApplyResult:
        return self._apply_source_unlocked(
            raw_envelopes=entry.raw_stored_envelopes,
            fold=entry.canonical_fold_result.fold_delta,
            base_revision=base_revision,
        )

    def _apply_source_unlocked(
        self,
        *,
        raw_envelopes,
        fold,
        base_revision: int,
    ) -> PresentationProjectionApplyResult:
        changes_by_sequence = _group_leaf_changes_by_sequence(
            fold.ordered_leaf_changes,
            fallback_sequence=fold.through_sequence,
        )
        disposition_by_sequence: dict[int, list[TranscriptAuditDispositionFact]] = {}
        for item in fold.ordered_audit_dispositions:
            disposition_by_sequence.setdefault(item.source_sequence, []).append(item)
        transition_pool = list(fold.ordered_placement_transition_proofs)
        # The compound tap stores raw envelopes, while the live fold result's
        # receipt owns decoded events.  Decode only on this observational lane;
        # normal storage encoding is never repeated.
        from pulsara_agent.event_log.historical_decoder import (
            decode_raw_stored_event_envelope,
        )
        from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY

        raw_by_sequence = {
            raw.sequence: (
                decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY),
                raw,
            )
            for raw in raw_envelopes
        }
        source_first_sequence = raw_envelopes[0].sequence
        source_last_sequence = raw_envelopes[-1].sequence
        all_mutations: list[PresentationHistoryTailMutationFact] = []
        segments: list[PresentationHistoryTailFoldSegmentFact] = []
        for sequence in range(source_first_sequence, source_last_sequence + 1):
            mutations: list[PresentationHistoryTailMutationFact] = []
            same_sequence_leaf_changes = changes_by_sequence.get(sequence, ())
            for change in same_sequence_leaf_changes:
                mutations.extend(
                    self._apply_leaf_change(
                        change,
                        transition_pool=transition_pool,
                        source_sequence=sequence,
                        fold_delta_fingerprint=fold.fold_delta_fingerprint,
                    )
                )
            event, raw = raw_by_sequence[sequence]
            policy = self.purpose_policy.resolve(raw)
            extracted = self.audit_extractor.extract(
                runtime_session_id=self.runtime_session_id,
                event=event,
                envelope=raw,
                policy=policy,
            )
            for audit in extracted:
                mutations.append(
                    self._upsert_audit(
                        audit,
                        source_sequence=sequence,
                        same_sequence_leaf_changes=same_sequence_leaf_changes,
                    )
                )
            for disposition in disposition_by_sequence.get(sequence, ()):
                mutations.append(
                    self._upsert_transcript_disposition(
                        disposition,
                        raw=raw,
                    )
                )
            mutations.sort(
                key=lambda item: item.placement_key.canonical_comparable_key_bytes
            )
            segment = _build_segment(
                runtime_session_id=self.runtime_session_id,
                raw=raw,
                mutations=tuple(mutations),
                transcript_fold_delta_fingerprint=fold.fold_delta_fingerprint,
                policy_fingerprint=policy.policy_fingerprint,
            )
            self._tail_segments.append(segment)
            segments.append(segment)
            all_mutations.extend(mutations)
            self._through_sequence = sequence
        if any(changes_by_sequence.values()):
            consumed = sum(len(items) for items in changes_by_sequence.values())
            if consumed != len(fold.ordered_leaf_changes):
                raise ValueError(
                    "presentation projection did not consume all leaf changes"
                )
        # Live tail application advances authority, not the client-visible root
        # revision.  Exactly one confirmed checkpoint/root swap advances that
        # revision, independent of physical receipt grouping or tap batching.
        ordered = self._ordered_entries_unlocked()
        accumulator = _entry_accumulator(ordered)
        payload = {
            "from_sequence_exclusive": source_first_sequence - 1,
            "through_sequence": source_last_sequence,
            "base_projection_revision": base_revision,
            "resulting_projection_revision": self._projection_revision,
            "ordered_segment_fingerprints": tuple(
                item.segment_fingerprint for item in segments
            ),
            "ordered_mutation_fingerprints": tuple(
                item.mutation_fingerprint for item in all_mutations
            ),
            "resulting_entry_count": len(ordered),
            "resulting_entry_accumulator": accumulator,
        }
        return PresentationProjectionApplyResult(
            from_sequence_exclusive=source_first_sequence - 1,
            through_sequence=source_last_sequence,
            base_projection_revision=base_revision,
            resulting_projection_revision=self._projection_revision,
            ordered_segments=tuple(segments),
            ordered_mutations=tuple(all_mutations),
            resulting_entry_count=len(ordered),
            resulting_entry_accumulator=accumulator,
            result_fingerprint=context_fingerprint(
                "presentation-projection-apply-result:v1", payload
            ),
        )

    def restore_checkpoint_state(
        self,
        *,
        acceleration: PresentationHistorySpineAccelerationFact,
        confirmed_history_entries: tuple[PresentationHistoryEntryFact, ...] = (),
    ) -> None:
        """Restore bounded placement state without scanning the history tree."""

        if acceleration.runtime_session_id != self.runtime_session_id:
            raise ValueError("presentation checkpoint acceleration crosses sessions")
        if (
            acceleration.placement_key_contract_id
            != self.placement_contract.placement_key_contract_id
            or acceleration.placement_key_contract_version
            != self.placement_contract.placement_key_contract_version
            or acceleration.placement_key_contract_fingerprint
            != self.placement_contract.contract_fingerprint
        ):
            raise ValueError("presentation checkpoint placement contract drifted")
        with self._lock:
            self._through_sequence = acceleration.through_authority_sequence
            self._projection_revision = acceleration.projection_revision
            self._entries_by_id = {
                item.history_entry_id: item for item in confirmed_history_entries
            }
            self._known_entry_identities = {}
            self._entry_id_by_anchor_slot = {}
            self._anchors_by_slot = {}
            self._anchor_slot_by_leaf_fingerprint = {}
            self._transcript_entry_fingerprint_by_slot = {}
            self._tail_segments = []
            self._ordered_entry_set_complete = False
            supplied = {
                item.history_entry_id: item for item in confirmed_history_entries
            }
            for item in acceleration.ordered_entries:
                anchor = _anchor_reference_from_acceleration(item, acceleration)
                state = _AnchorState(
                    reference=anchor,
                    first_ordering_boundary_sequence=(
                        item.first_ordering_boundary_sequence
                    ),
                    last_ordering_boundary_sequence=item.last_ordering_boundary_sequence,
                    current_leaf_fact_fingerprint=(
                        item.transcript_entry_fact_fingerprint
                    ),
                    tombstone_fingerprint=item.tombstone_fingerprint,
                )
                slot = anchor.stable_anchor_slot_key
                self._anchors_by_slot[slot] = state
                if item.transcript_entry_fact_fingerprint is not None:
                    self._anchor_slot_by_leaf_fingerprint[
                        item.transcript_entry_fact_fingerprint
                    ] = slot
                    self._transcript_entry_fingerprint_by_slot[slot] = (
                        item.transcript_entry_fact_fingerprint
                    )
                if item.history_entry_id is not None:
                    assert item.history_entry_placement_key is not None
                    assert item.history_entry_fingerprint is not None
                    self._entry_id_by_anchor_slot[slot] = item.history_entry_id
                    existing = supplied.get(item.history_entry_id)
                    if existing is not None:
                        if (
                            existing.placement_key != item.history_entry_placement_key
                            or existing.entry_fingerprint
                            != item.history_entry_fingerprint
                        ):
                            raise ValueError(
                                "presentation checkpoint supplied entry identity drifted"
                            )
                    else:
                        self._known_entry_identities[item.history_entry_id] = (
                            _KnownEntryIdentity(
                                history_entry_id=item.history_entry_id,
                                placement_key=item.history_entry_placement_key,
                                entry_fingerprint=item.history_entry_fingerprint,
                                transcript_anchor_slot_key=slot,
                            )
                        )
            if _spine_fingerprint(self._anchors_by_slot.values()) != (
                acceleration.canonical_spine_fingerprint
            ):
                raise ValueError("presentation checkpoint spine acceleration drifted")

    def _apply_leaf_change(
        self,
        change: CanonicalTranscriptLeafChangeFact,
        *,
        transition_pool: list,
        source_sequence: int,
        fold_delta_fingerprint: str,
    ) -> tuple[PresentationHistoryTailMutationFact, ...]:
        if change.change_kind == "append":
            assert change.resulting_entry is not None
            anchor = _claim_resulting_anchor(change.resulting_entry, transition_pool)
            self._install_anchor(
                anchor,
                leaf=change.resulting_entry,
                source_sequence=source_sequence,
            )
            entry = self._entry_from_leaf(
                change.resulting_entry,
                anchor=anchor,
                fold_delta_fingerprint=fold_delta_fingerprint,
                leaf_change_ordinal=0,
            )
            if entry is None:
                return ()
            return (self._upsert_entry(entry, source_sequence=source_sequence),)
        if change.change_kind == "replace":
            assert (
                change.previous_entry is not None and change.resulting_entry is not None
            )
            previous_slot = self._anchor_slot_by_leaf_fingerprint.pop(
                change.previous_entry.fact_fingerprint
            )
            anchor = _claim_resulting_anchor(change.resulting_entry, transition_pool)
            if anchor.stable_anchor_slot_key != previous_slot:
                raise ValueError("transcript replacement changed stable anchor slot")
            state = self._anchors_by_slot[previous_slot]
            state.reference = anchor
            state.current_leaf_fact_fingerprint = (
                change.resulting_entry.fact_fingerprint
            )
            state.tombstone_fingerprint = None
            self._transcript_entry_fingerprint_by_slot[previous_slot] = (
                change.resulting_entry.fact_fingerprint
            )
            self._anchor_slot_by_leaf_fingerprint[
                change.resulting_entry.fact_fingerprint
            ] = previous_slot
            entry = self._entry_from_leaf(
                change.resulting_entry,
                anchor=anchor,
                fold_delta_fingerprint=fold_delta_fingerprint,
                leaf_change_ordinal=0,
            )
            existing_id = self._entry_id_by_anchor_slot.get(previous_slot)
            if entry is None:
                if existing_id is None:
                    return ()
                return (
                    self._remove_entry(
                        existing_id,
                        source_sequence=source_sequence,
                        tombstone=None,
                    ),
                )
            return (self._upsert_entry(entry, source_sequence=source_sequence),)
        assert change.previous_entry is not None
        slot = self._anchor_slot_by_leaf_fingerprint.pop(
            change.previous_entry.fact_fingerprint
        )
        state = self._anchors_by_slot[slot]
        state.current_leaf_fact_fingerprint = None
        self._transcript_entry_fingerprint_by_slot.pop(slot, None)
        tombstone = _claim_tombstone(state.reference, transition_pool)
        if tombstone is None:
            raise ValueError("transcript retirement lacks its placement tombstone")
        state.tombstone_fingerprint = tombstone.tombstone_fingerprint
        existing_id = self._entry_id_by_anchor_slot.get(slot)
        if existing_id is None:
            return ()
        return (
            self._remove_entry(
                existing_id,
                source_sequence=source_sequence,
                tombstone=tombstone,
            ),
        )

    def _install_anchor(
        self,
        anchor: CanonicalTranscriptPlacementAnchorReferenceFact,
        *,
        leaf: TranscriptProjectionLeafEntryFact,
        source_sequence: int,
    ) -> None:
        slot = anchor.stable_anchor_slot_key
        existing = self._anchors_by_slot.get(slot)
        if existing is not None:
            if existing.reference != anchor:
                raise ValueError("presentation anchor slot identity conflict")
            return
        self._anchors_by_slot[slot] = _AnchorState(
            reference=anchor,
            first_ordering_boundary_sequence=source_sequence,
            last_ordering_boundary_sequence=source_sequence,
            current_leaf_fact_fingerprint=leaf.fact_fingerprint,
        )
        self._anchor_slot_by_leaf_fingerprint[leaf.fact_fingerprint] = slot
        self._transcript_entry_fingerprint_by_slot[slot] = leaf.fact_fingerprint

    def _entry_from_leaf(
        self,
        leaf: TranscriptProjectionLeafEntryFact,
        *,
        anchor: CanonicalTranscriptPlacementAnchorReferenceFact,
        fold_delta_fingerprint: str,
        leaf_change_ordinal: int,
    ) -> PresentationHistoryEntryFact | None:
        cell = _cell_from_leaf(
            runtime_session_id=self.runtime_session_id,
            leaf=leaf,
            stable_cell_id=f"presentation:transcript:{anchor.stable_anchor_slot_key}",
            documents=self.transcript_documents,
            archive=self.archive,
        )
        if cell is None:
            return None
        history_entry_id = _canonical_history_entry_id(
            self.runtime_session_id, anchor.stable_anchor_slot_key
        )
        leaf_reference = _leaf_reference(self.runtime_session_id, leaf)
        source = build_frozen_fact(
            CanonicalTranscriptHistorySourceFact,
            schema_version="canonical_transcript_history_source.v1",
            source_kind="canonical_transcript",
            transcript_leaf_reference=leaf_reference,
            transcript_placement_anchor=anchor,
            transcript_reducer_id=TRANSCRIPT_PROJECTION_REDUCER_ID,
            transcript_reducer_version=TRANSCRIPT_PROJECTION_REDUCER_VERSION,
            transcript_reducer_contract_fingerprint=(
                TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
            ),
            source_fold_delta_fingerprint=fold_delta_fingerprint,
            source_leaf_change_ordinal=leaf_change_ordinal,
        )
        key = build_presentation_history_placement_key(
            contract=self.placement_contract,
            canonical_spine_left_coordinate=anchor.stable_first_spine_coordinate,
            canonical_spine_right_coordinate=anchor.stable_last_spine_coordinate,
            relative_position_kind="canonical_leaf",
            source_ledger_sequence_or_zero=0,
            relative_local_ordinal=0,
            stable_source_id=anchor.stable_anchor_slot_key,
        )
        return build_frozen_fact(
            PresentationHistoryEntryFact,
            schema_version="presentation_history_entry.v1",
            runtime_session_id=self.runtime_session_id,
            history_entry_id=history_entry_id,
            placement_key=key,
            source=source,
            cell=cell,
        )

    def _upsert_audit(
        self,
        audit: ExtractedDurableAuditCell,
        *,
        source_sequence: int,
        same_sequence_leaf_changes: tuple[CanonicalTranscriptLeafChangeFact, ...],
    ) -> UpsertPresentationHistoryEntryMutationFact:
        request = audit.placement_request
        target = None
        if request.request_kind in {"before_leaf", "after_leaf"}:
            if request.target_transcript_message_id is None:
                raise ValueError("audit leaf placement lacks its typed target")
            candidate_slots: list[str] = []
            for change in same_sequence_leaf_changes:
                leaf = change.resulting_entry
                if (
                    isinstance(leaf, TranscriptMessageLeafEntryFact)
                    and leaf.attribution.message_id
                    == request.target_transcript_message_id
                ):
                    try:
                        candidate_slots.append(
                            self._anchor_slot_by_leaf_fingerprint[leaf.fact_fingerprint]
                        )
                    except KeyError as exc:
                        raise ValueError(
                            "audit target transcript anchor was not installed"
                        ) from exc
            candidates = tuple(self._anchors_by_slot[slot] for slot in candidate_slots)
            if len(candidates) != 1:
                raise ValueError(
                    "audit leaf placement lacks one exact transcript anchor"
                )
            target = candidates[0]
        return self._upsert_audit_cell(
            cell=audit.cell,
            source_sequence=source_sequence,
            local_ordinal=request.audit_local_ordinal,
            request_kind=request.request_kind,
            target=target,
            extractor_id=audit.extractor_id,
            extractor_version=audit.extractor_version,
            extractor_contract_fingerprint=audit.extractor_contract_fingerprint,
            extractor_output_ordinal=audit.extractor_output_ordinal,
        )

    def _upsert_transcript_disposition(
        self,
        disposition: TranscriptAuditDispositionFact,
        *,
        raw,
    ) -> UpsertPresentationHistoryEntryMutationFact:
        if raw.event_id != disposition.source_event_id:
            raise ValueError("transcript audit disposition source mismatch")
        ref = ContextEventReferenceFact(
            runtime_session_id=self.runtime_session_id,
            event_id=raw.event_id,
            sequence=raw.sequence,
            event_type=raw.event_type,
            payload_fingerprint=raw.payload_fingerprint,
        )
        text = {
            "suppressed_model_output": "Model output was suppressed by runtime control",
            "recovered_transcript": "Transcript state was recovered",
            "rejected_transcript_candidate": "A transcript candidate was rejected",
        }[disposition.disposition_kind]
        block = _text_block(text, role="diagnostic")
        cell = build_frozen_fact(
            AuditCell,
            schema_version="presentation_audit_cell.v1",
            cell_kind="audit",
            stable_cell_id=f"presentation:transcript-audit:{raw.event_id}",
            semantic_revision=1,
            ordered_source_event_references=(ref,),
            source_accumulator=_source_accumulator((ref,)),
            visibility_policy="normal",
            content_blocks=(block,),
            semantic_group_id=f"run:{raw.run_id}",
            audit_kind=(
                "suppressed_model_output"
                if disposition.disposition_kind == "suppressed_model_output"
                else "recovery_lifecycle"
            ),
            severity="warning",
        )
        return self._upsert_audit_cell(
            cell=cell,
            source_sequence=raw.sequence,
            local_ordinal=0,
            request_kind="ledger_sequence",
            target=None,
            extractor_id=_TRANSCRIPT_DISPOSITION_EXTRACTOR_ID,
            extractor_version=_TRANSCRIPT_DISPOSITION_EXTRACTOR_VERSION,
            extractor_contract_fingerprint=(
                _TRANSCRIPT_DISPOSITION_EXTRACTOR_FINGERPRINT
            ),
            extractor_output_ordinal=0,
        )

    def _upsert_audit_cell(
        self,
        *,
        cell: DurableHistoryCell,
        source_sequence: int,
        local_ordinal: int,
        request_kind: Literal["before_leaf", "after_leaf", "ledger_sequence"],
        target: _AnchorState | None,
        extractor_id: str,
        extractor_version: str,
        extractor_contract_fingerprint: str,
        extractor_output_ordinal: int,
    ) -> UpsertPresentationHistoryEntryMutationFact:
        history_entry_id = context_fingerprint(
            "presentation-durable-audit-history-entry-id:v1",
            (
                self.runtime_session_id,
                cell.stable_cell_id,
                extractor_output_ordinal,
            ),
        )
        if request_kind == "before_leaf":
            assert target is not None
            ordered = self._ordered_anchor_states()
            index = ordered.index(target)
            left = (
                ordered[index - 1].reference.stable_last_spine_coordinate
                if index > 0
                else None
            )
            right = target.reference.stable_first_spine_coordinate
            kind = "before_leaf"
            anchor = build_frozen_fact(
                BeforeTranscriptLeafAuditAnchorFact,
                schema_version="presentation_before_leaf_audit_anchor.v1",
                anchor_kind="before_leaf",
                target_transcript_anchor=target.reference,
                audit_local_ordinal=local_ordinal,
            )
        elif request_kind == "after_leaf":
            assert target is not None
            ordered = self._ordered_anchor_states()
            index = ordered.index(target)
            left = target.reference.stable_last_spine_coordinate
            right = (
                ordered[index + 1].reference.stable_first_spine_coordinate
                if index + 1 < len(ordered)
                else None
            )
            kind = "after_leaf"
            anchor = build_frozen_fact(
                AfterTranscriptLeafAuditAnchorFact,
                schema_version="presentation_after_leaf_audit_anchor.v1",
                anchor_kind="after_leaf",
                target_transcript_anchor=target.reference,
                audit_local_ordinal=local_ordinal,
            )
        else:
            left_state, right_state = self._gap_for_sequence(source_sequence)
            left = (
                left_state.reference.stable_last_spine_coordinate
                if left_state is not None
                else None
            )
            right = (
                right_state.reference.stable_first_spine_coordinate
                if right_state is not None
                else None
            )
            kind = (
                "ledger_gap"
                if left is not None and right is not None
                else ("after_last" if left is not None else "before_first")
            )
            source_ref = cell.ordered_source_event_references[0]
            anchor = build_frozen_fact(
                LedgerSequenceAuditAnchorFact,
                schema_version="presentation_ledger_sequence_audit_anchor.v1",
                anchor_kind="ledger_sequence",
                source_event_reference=source_ref,
                resolved_left_transcript_anchor=(
                    left_state.reference if left_state is not None else None
                ),
                resolved_right_transcript_anchor=(
                    right_state.reference if right_state is not None else None
                ),
                transcript_gap_proof_fingerprint=context_fingerprint(
                    "presentation-transcript-gap-proof:v1",
                    (
                        source_sequence,
                        left_state.reference.anchor_reference_fingerprint
                        if left_state is not None
                        else None,
                        right_state.reference.anchor_reference_fingerprint
                        if right_state is not None
                        else None,
                    ),
                ),
                audit_local_ordinal=local_ordinal,
            )
        placement = build_presentation_history_placement_key(
            contract=self.placement_contract,
            canonical_spine_left_coordinate=left,
            canonical_spine_right_coordinate=right,
            relative_position_kind=kind,
            source_ledger_sequence_or_zero=source_sequence,
            relative_local_ordinal=local_ordinal,
            stable_source_id=history_entry_id,
        )
        source = build_frozen_fact(
            DurableAuditHistorySourceFact,
            schema_version="durable_audit_history_source.v1",
            source_kind="durable_audit",
            audit_cell_id=cell.stable_cell_id,
            audit_cell_semantic_revision=cell.semantic_revision,
            audit_cell_fingerprint=cell.cell_fingerprint,
            ordered_source_event_references=cell.ordered_source_event_references,
            presentation_policy_fingerprint=(
                self.purpose_policy.contract.registry_fingerprint
            ),
            extractor_id=extractor_id,
            extractor_version=extractor_version,
            extractor_contract_fingerprint=extractor_contract_fingerprint,
            extractor_output_ordinal=extractor_output_ordinal,
            audit_placement_anchor=anchor,
        )
        history_entry = build_frozen_fact(
            PresentationHistoryEntryFact,
            schema_version="presentation_history_entry.v1",
            runtime_session_id=self.runtime_session_id,
            history_entry_id=history_entry_id,
            placement_key=placement,
            source=source,
            cell=cell,
        )
        return self._upsert_entry(history_entry, source_sequence=source_sequence)

    def _upsert_entry(
        self,
        entry: PresentationHistoryEntryFact,
        *,
        source_sequence: int,
    ) -> UpsertPresentationHistoryEntryMutationFact:
        previous = self._entries_by_id.get(entry.history_entry_id)
        known = self._known_entry_identities.pop(entry.history_entry_id, None)
        previous_placement = (
            previous.placement_key
            if previous is not None
            else (known.placement_key if known is not None else None)
        )
        previous_fingerprint = (
            previous.entry_fingerprint
            if previous is not None
            else (known.entry_fingerprint if known is not None else None)
        )
        if previous_placement is not None and (
            previous_placement.canonical_comparable_key_bytes
            != entry.placement_key.canonical_comparable_key_bytes
        ):
            raise ValueError("history entry replacement changed stable placement")
        self._entries_by_id[entry.history_entry_id] = entry
        if isinstance(entry.source, CanonicalTranscriptHistorySourceFact):
            slot = entry.source.transcript_placement_anchor.stable_anchor_slot_key
            self._entry_id_by_anchor_slot[slot] = entry.history_entry_id
        payload = {
            "mutation_id": context_fingerprint(
                "presentation-history-mutation-id:v1",
                (source_sequence, entry.history_entry_id, "upsert"),
            ),
            "source_from_sequence_exclusive": source_sequence - 1,
            "source_through_sequence": source_sequence,
            "history_entry_id": entry.history_entry_id,
            "placement_key_fingerprint": entry.placement_key.placement_key_fingerprint,
            "expected_previous_entry_fingerprint": (previous_fingerprint),
            "resulting_entry_fingerprint": entry.entry_fingerprint,
        }
        return build_frozen_fact(
            UpsertPresentationHistoryEntryMutationFact,
            schema_version="presentation_history_upsert_mutation.v1",
            mutation_kind="upsert",
            mutation_id=payload["mutation_id"],
            source_from_sequence_exclusive=source_sequence - 1,
            source_through_sequence=source_sequence,
            history_entry_id=entry.history_entry_id,
            placement_key=entry.placement_key,
            expected_previous_entry_fingerprint=(previous_fingerprint),
            resulting_entry=entry,
        )

    def _remove_entry(
        self,
        history_entry_id: str,
        *,
        source_sequence: int,
        tombstone: CanonicalTranscriptPlacementAnchorTombstoneFact | None,
    ) -> RemovePresentationHistoryEntryMutationFact:
        previous = self._entries_by_id.pop(history_entry_id, None)
        known = self._known_entry_identities.pop(history_entry_id, None)
        if previous is None and known is None:
            raise ValueError("presentation removal target is unknown")
        if previous is not None and isinstance(
            previous.source, CanonicalTranscriptHistorySourceFact
        ):
            self._entry_id_by_anchor_slot.pop(
                previous.source.transcript_placement_anchor.stable_anchor_slot_key,
                None,
            )
        elif known is not None and known.transcript_anchor_slot_key is not None:
            self._entry_id_by_anchor_slot.pop(known.transcript_anchor_slot_key, None)
        placement_key = (
            previous.placement_key if previous is not None else known.placement_key
        )
        previous_fingerprint = (
            previous.entry_fingerprint
            if previous is not None
            else known.entry_fingerprint
        )
        return build_frozen_fact(
            RemovePresentationHistoryEntryMutationFact,
            schema_version="presentation_history_remove_mutation.v1",
            mutation_kind="remove",
            mutation_id=context_fingerprint(
                "presentation-history-mutation-id:v1",
                (source_sequence, history_entry_id, "remove"),
            ),
            source_from_sequence_exclusive=source_sequence - 1,
            source_through_sequence=source_sequence,
            history_entry_id=history_entry_id,
            placement_key=placement_key,
            expected_previous_entry_fingerprint=previous_fingerprint,
            resulting_anchor_tombstone_reference=tombstone,
        )

    def _gap_for_sequence(
        self, source_sequence: int
    ) -> tuple[_AnchorState | None, _AnchorState | None]:
        ordered = self._ordered_anchor_states()
        left = None
        right = None
        for state in ordered:
            if state.first_ordering_boundary_sequence <= source_sequence:
                left = state
            elif right is None:
                right = state
                break
        return left, right

    def _ordered_anchor_states(self) -> list[_AnchorState]:
        return sorted(
            self._anchors_by_slot.values(),
            key=lambda item: (
                item.reference.stable_first_spine_coordinate,
                item.reference.stable_last_spine_coordinate,
                item.reference.stable_anchor_slot_key,
            ),
        )

    def _ordered_entries_unlocked(self) -> tuple[PresentationHistoryEntryFact, ...]:
        return tuple(
            sorted(
                self._entries_by_id.values(),
                key=lambda item: item.placement_key.canonical_comparable_key_bytes,
            )
        )

    def _spine_acceleration_unlocked(
        self, canonical_spine_fingerprint: str
    ) -> PresentationHistorySpineAccelerationFact:
        entries: list[PresentationHistorySpineEntryAccelerationFact] = []
        for state in self._ordered_anchor_states():
            slot = state.reference.stable_anchor_slot_key
            history_entry_id = self._entry_id_by_anchor_slot.get(slot)
            full = (
                self._entries_by_id.get(history_entry_id)
                if history_entry_id is not None
                else None
            )
            known = (
                self._known_entry_identities.get(history_entry_id)
                if history_entry_id is not None
                else None
            )
            placement_key = (
                full.placement_key
                if full is not None
                else (known.placement_key if known is not None else None)
            )
            entry_fingerprint = (
                full.entry_fingerprint
                if full is not None
                else (known.entry_fingerprint if known is not None else None)
            )
            entries.append(
                build_frozen_storage_fact(
                    PresentationHistorySpineEntryAccelerationFact,
                    schema_version=("presentation_history_spine_entry_acceleration.v1"),
                    anchor_state_kind=(
                        "current"
                        if state.current_leaf_fact_fingerprint is not None
                        else "tombstone"
                    ),
                    transcript_entry_fact_fingerprint=(
                        state.current_leaf_fact_fingerprint
                    ),
                    transcript_anchor_id=state.reference.transcript_anchor_id,
                    stable_anchor_slot_key=slot,
                    stable_first_spine_coordinate=(
                        state.reference.stable_first_spine_coordinate
                    ),
                    stable_last_spine_coordinate=(
                        state.reference.stable_last_spine_coordinate
                    ),
                    anchor_fingerprint=state.reference.anchor_fingerprint,
                    anchor_reference_fingerprint=(
                        state.reference.anchor_reference_fingerprint
                    ),
                    first_ordering_boundary_sequence=(
                        state.first_ordering_boundary_sequence
                    ),
                    last_ordering_boundary_sequence=(
                        state.last_ordering_boundary_sequence
                    ),
                    tombstone_fingerprint=state.tombstone_fingerprint,
                    history_entry_id=history_entry_id,
                    history_entry_placement_key=placement_key,
                    history_entry_fingerprint=entry_fingerprint,
                )
            )
        return build_frozen_storage_fact(
            PresentationHistorySpineAccelerationFact,
            schema_version="presentation_history_spine_acceleration.v1",
            runtime_session_id=self.runtime_session_id,
            placement_key_contract_id=(
                self.placement_contract.placement_key_contract_id
            ),
            placement_key_contract_version=(
                self.placement_contract.placement_key_contract_version
            ),
            placement_key_contract_fingerprint=self.placement_contract.contract_fingerprint,
            through_authority_sequence=self._through_sequence,
            projection_revision=self._projection_revision,
            canonical_spine_fingerprint=canonical_spine_fingerprint,
            ordered_entries=tuple(entries),
        )

    def _capture_state(self):
        return (
            self._through_sequence,
            self._projection_revision,
            dict(self._entries_by_id),
            dict(self._known_entry_identities),
            dict(self._entry_id_by_anchor_slot),
            {
                key: _AnchorState(
                    reference=value.reference,
                    first_ordering_boundary_sequence=(
                        value.first_ordering_boundary_sequence
                    ),
                    last_ordering_boundary_sequence=(
                        value.last_ordering_boundary_sequence
                    ),
                    current_leaf_fact_fingerprint=(value.current_leaf_fact_fingerprint),
                    tombstone_fingerprint=value.tombstone_fingerprint,
                )
                for key, value in self._anchors_by_slot.items()
            },
            dict(self._anchor_slot_by_leaf_fingerprint),
            dict(self._transcript_entry_fingerprint_by_slot),
            list(self._tail_segments),
            self._ordered_entry_set_complete,
        )

    def _restore_state(self, state) -> None:
        (
            self._through_sequence,
            self._projection_revision,
            self._entries_by_id,
            self._known_entry_identities,
            self._entry_id_by_anchor_slot,
            self._anchors_by_slot,
            self._anchor_slot_by_leaf_fingerprint,
            self._transcript_entry_fingerprint_by_slot,
            self._tail_segments,
            self._ordered_entry_set_complete,
        ) = state


def _group_leaf_changes_by_sequence(
    changes: tuple[CanonicalTranscriptLeafChangeFact, ...],
    *,
    fallback_sequence: int,
) -> dict[int, tuple[CanonicalTranscriptLeafChangeFact, ...]]:
    replacement_sequences = tuple(
        max(ref.sequence for ref in item.resulting_entry.source_event_refs)
        for item in changes
        if item.resulting_entry is not None
    )
    replacement_fallback = max(replacement_sequences, default=fallback_sequence)
    grouped: dict[int, list[CanonicalTranscriptLeafChangeFact]] = {}
    for change in changes:
        if change.resulting_entry is not None:
            sequence = max(
                ref.sequence for ref in change.resulting_entry.source_event_refs
            )
        else:
            sequence = replacement_fallback
        grouped.setdefault(sequence, []).append(change)
    return {key: tuple(value) for key, value in grouped.items()}


def _claim_resulting_anchor(leaf, transitions):
    matches = []
    for item in transitions:
        candidate = item.resulting_anchor_reference
        if candidate is None:
            continue
        expected = context_fingerprint(
            "canonical-transcript-placement-anchor:v1",
            {
                "transcript_anchor_id": candidate.transcript_anchor_id,
                "stable_anchor_slot_key": candidate.stable_anchor_slot_key,
                "stable_first_spine_coordinate": (
                    candidate.stable_first_spine_coordinate
                ),
                "stable_last_spine_coordinate": (
                    candidate.stable_last_spine_coordinate
                ),
                "entry_fact_fingerprint": leaf.fact_fingerprint,
            },
        )
        if expected == candidate.anchor_fingerprint:
            matches.append(candidate)
    matches = tuple(matches)
    if len(matches) != 1:
        raise ValueError("canonical leaf lacks one placement transition anchor")
    result = matches[0]
    for index, item in enumerate(transitions):
        if item.resulting_anchor_reference == result:
            transitions.pop(index)
            break
    return result


def _claim_tombstone(anchor, transitions):
    for item in transitions:
        for tombstone in item.resulting_anchor_tombstones:
            if tombstone.retired_anchor_reference == anchor:
                return tombstone
    return None


def _leaf_reference(runtime_session_id, leaf):
    return build_frozen_fact(
        TranscriptProjectionLeafEntryReferenceFact,
        schema_version="transcript_projection_leaf_entry_reference.v2",
        runtime_session_id=runtime_session_id,
        entry_kind=leaf.entry_kind,
        ordinal=leaf.ordinal.value,
        entry_semantic_fingerprint=leaf.semantic_identity.semantic_fingerprint,
        entry_fact_fingerprint=leaf.fact_fingerprint,
        source_event_references=leaf.source_event_refs,
    )


def _canonical_history_entry_id(runtime_session_id: str, slot: str) -> str:
    return context_fingerprint(
        "presentation-canonical-transcript-history-entry-id:v1",
        (runtime_session_id, "canonical_transcript", slot),
    )


def _cell_from_leaf(
    *,
    runtime_session_id: str,
    leaf: TranscriptProjectionLeafEntryFact,
    stable_cell_id: str,
    documents: TranscriptProjectionDocumentRegistry,
    archive: ArtifactStore,
) -> DurableHistoryCell | None:
    if isinstance(leaf, TranscriptToolPairLeafEntryFact):
        return None
    refs = leaf.source_event_refs
    source_accumulator = _source_accumulator(refs)
    if isinstance(leaf, TranscriptMessageLeafEntryFact):
        blocks = _message_content_blocks(
            runtime_session_id=runtime_session_id,
            leaf=leaf,
            documents=documents,
            archive=archive,
        )
        role = leaf.semantic_identity.message_provider_semantic_identity.role
        if role in {"user", "runtime_request"}:
            return build_frozen_fact(
                UserPromptCell,
                schema_version="presentation_user_prompt_cell.v1",
                cell_kind="user_prompt",
                stable_cell_id=stable_cell_id,
                semantic_revision=1,
                ordered_source_event_references=refs,
                source_accumulator=source_accumulator,
                visibility_policy="normal",
                content_blocks=blocks,
                semantic_group_id=(
                    f"turn:{leaf.attribution.turn_id}"
                    if leaf.attribution.turn_id is not None
                    else None
                ),
            )
        return build_frozen_fact(
            AssistantMessageCell,
            schema_version="presentation_assistant_message_cell.v1",
            cell_kind="assistant_message",
            stable_cell_id=stable_cell_id,
            semantic_revision=1,
            ordered_source_event_references=refs,
            source_accumulator=source_accumulator,
            visibility_policy="normal",
            content_blocks=blocks,
            semantic_group_id=(
                f"reply:{leaf.attribution.reply_id}"
                if leaf.attribution.reply_id is not None
                else None
            ),
        )
    assert isinstance(leaf, TranscriptToolResultLeafEntryFact)
    document = documents.resolve(leaf.projection_reference)
    payload = document.payload
    if not isinstance(payload, ToolTerminalProjectionPayloadFact):
        raise ValueError("tool transcript leaf points at a model projection")
    canonical = payload.canonical_result_block
    blocks = []
    for item in canonical.content_blocks:
        semantic = item.semantic_identity
        if isinstance(semantic, CanonicalToolResultTextBlockSemanticFact):
            blocks.append(
                _text_block(
                    _terminal_content_text(
                        item.content,
                        archive=archive,
                        runtime_session_id=runtime_session_id,
                    ),
                    role="tool_result",
                )
            )
        elif isinstance(semantic, CanonicalToolResultDataBlockSemanticFact):
            blocks.append(
                _data_block(
                    media_type=semantic.media_type,
                    public_text=f"[{semantic.source_kind} data]",
                )
            )
    semantic = canonical.semantic_identity
    return build_frozen_fact(
        ToolTerminalCell,
        schema_version="presentation_tool_terminal_cell.v1",
        cell_kind="tool_terminal",
        stable_cell_id=stable_cell_id,
        semantic_revision=1,
        ordered_source_event_references=refs,
        source_accumulator=source_accumulator,
        visibility_policy=(
            "always" if semantic.result_state.value != "success" else "normal"
        ),
        content_blocks=tuple(blocks),
        semantic_group_id=f"tool-call:{semantic.tool_call_id}",
        tool_call_id=semantic.tool_call_id,
        tool_name=semantic.model_tool_name,
        result_state=semantic.result_state.value,
    )


def _message_content_blocks(
    *,
    runtime_session_id: str,
    leaf: TranscriptMessageLeafEntryFact,
    documents: TranscriptProjectionDocumentRegistry,
    archive: ArtifactStore,
):
    content = leaf.content
    if isinstance(content, InlineNormalizedMessageContentFact):
        return _inline_provider_blocks(content.blocks)
    if isinstance(content, NormalizedMessageContentArtifactReferenceFact):
        text = archive.get_text(
            content.document_artifact_id,
            session_id=runtime_session_id,
        )
        document = NormalizedMessageContentArtifactFact.model_validate_json(text)
        if document.fact_fingerprint != content.document_fact_fingerprint:
            raise ValueError("normalized message artifact fingerprint mismatch")
        return _inline_provider_blocks(document.blocks)
    assert isinstance(content, TerminalProjectionMessageContentRefFact)
    document = documents.resolve(content.projection_reference)
    selected = frozenset(content.selected_projection_orders)
    blocks = []
    for item in document.payload.items:
        semantic = item.semantic_identity
        if semantic.projection_order not in selected:
            continue
        if isinstance(semantic, ModelTextBlockSemanticFact):
            blocks.append(
                _text_block(
                    _terminal_content_text(
                        item.content,
                        archive=archive,
                        runtime_session_id=runtime_session_id,
                    ),
                    role="primary",
                )
            )
        elif isinstance(semantic, ModelThinkingBlockSemanticFact):
            continue
        elif isinstance(semantic, ModelDataBlockSemanticFact):
            blocks.append(
                _data_block(
                    media_type=semantic.media_type,
                    public_text="[model data]",
                )
            )
        elif isinstance(semantic, ModelToolCallBlockSemanticFact):
            blocks.append(
                _text_block(
                    f"{semantic.tool_name}({semantic.raw_arguments_json})",
                    role="tool_arguments",
                )
            )
        elif isinstance(semantic, ModelProviderErrorSemanticFact):
            raise ValueError("provider error entered canonical assistant history")
    return tuple(blocks)


def _inline_provider_blocks(blocks):
    projected = []
    for item in blocks:
        semantic = item.provider_semantic_identity
        if isinstance(semantic, TranscriptProviderTextBlockSemanticFact):
            projected.append(_text_block(semantic.text, role="primary"))
        elif isinstance(semantic, TranscriptProviderThinkingBlockSemanticFact):
            continue
        elif isinstance(semantic, TranscriptProviderDataPlaceholderSemanticFact):
            projected.append(
                _data_block(
                    media_type=semantic.media_type,
                    public_text=f"[{semantic.source_kind} data]",
                )
            )
        elif isinstance(semantic, TranscriptProviderToolCallBlockSemanticFact):
            projected.append(
                _text_block(
                    f"{semantic.model_tool_name}({semantic.raw_arguments_json})",
                    role="tool_arguments",
                )
            )
        elif isinstance(semantic, TranscriptProviderToolResultRefSemanticFact):
            continue
    return tuple(projected)


def _terminal_content_text(content, *, archive, runtime_session_id):
    if content is None:
        return ""
    if isinstance(content, TerminalInlineContentFact):
        return content.text
    assert isinstance(content, TerminalArtifactContentReferenceFact)
    text = archive.get_text(content.artifact_id, session_id=runtime_session_id)
    encoded = text.encode("utf-8")
    if (
        len(encoded) != content.artifact_bytes
        or f"sha256:{sha256(encoded).hexdigest()}" != content.artifact_sha256
    ):
        raise ValueError("terminal presentation artifact content mismatch")
    return text


def _text_block(text: str, *, role):
    bounded = text[:32_000]
    return build_frozen_fact(
        PresentationTextContentBlockFact,
        schema_version="presentation_text_content_block.v1",
        block_kind="text",
        text=bounded,
        text_utf8_bytes=len(bounded.encode("utf-8")),
        semantic_role=role,
    )


def _data_block(*, media_type: str, public_text: str):
    return build_frozen_fact(
        PresentationDataContentBlockFact,
        schema_version="presentation_data_content_block.v1",
        block_kind="data",
        media_type=media_type,
        public_canonical_text=public_text,
        public_utf8_bytes=len(public_text.encode("utf-8")),
    )


def _source_accumulator(refs: Iterable[ContextEventReferenceFact]) -> str:
    return context_fingerprint(
        "presentation-history-cell-sources:v1",
        tuple(
            (item.sequence, item.event_id, item.payload_fingerprint) for item in refs
        ),
    )


def _build_segment(
    *,
    runtime_session_id,
    raw,
    mutations,
    transcript_fold_delta_fingerprint,
    policy_fingerprint,
):
    source_range = context_fingerprint(
        "presentation-history-segment-source-range:v1",
        {
            "sequence": raw.sequence,
            "event_id": raw.event_id,
            "envelope_fingerprint": raw.envelope_fingerprint,
            "transcript_fold_delta_fingerprint": transcript_fold_delta_fingerprint,
            "presentation_policy_fingerprint": policy_fingerprint,
        },
    )
    mutation_accumulator = context_fingerprint(
        "presentation-history-segment-mutations:v1",
        tuple(item.mutation_fingerprint for item in mutations),
    )
    return build_frozen_fact(
        PresentationHistoryTailFoldSegmentFact,
        schema_version="presentation_history_tail_fold_segment.v1",
        runtime_session_id=runtime_session_id,
        from_sequence_exclusive=raw.sequence - 1,
        through_sequence=raw.sequence,
        source_range_fingerprint=source_range,
        source_range_accumulator=context_fingerprint(
            "presentation-history-segment-source-accumulator:v1",
            (raw.sequence, raw.envelope_fingerprint, source_range),
        ),
        ordered_mutations=mutations,
        mutation_count=len(mutations),
        mutation_accumulator=mutation_accumulator,
    )


def _entry_accumulator(entries):
    return context_fingerprint(
        "presentation-history-ordered-entries:v1",
        tuple(
            (
                item.history_entry_id,
                item.entry_fingerprint,
                item.placement_key.placement_key_fingerprint,
            )
            for item in entries
        ),
    )


def _spine_fingerprint(states):
    return context_fingerprint(
        "presentation-history-canonical-spine:v1",
        tuple(
            sorted(
                (
                    item.reference.stable_first_spine_coordinate,
                    item.reference.stable_last_spine_coordinate,
                    item.reference.stable_anchor_slot_key,
                    item.reference.anchor_reference_fingerprint,
                    item.tombstone_fingerprint,
                )
                for item in states
            )
        ),
    )


def _anchor_reference_from_acceleration(
    item: PresentationHistorySpineEntryAccelerationFact,
    acceleration: PresentationHistorySpineAccelerationFact,
) -> CanonicalTranscriptPlacementAnchorReferenceFact:
    return CanonicalTranscriptPlacementAnchorReferenceFact(
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


__all__ = [
    "PresentationHistoryProjectionOwner",
    "PresentationProjectionApplyResult",
    "PresentationProjectionSnapshot",
]
