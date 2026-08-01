"""Renderer-neutral terminal presentation facts and projection identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.presentation_placement_contract import (
    PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT,
    PRESENTATION_PLACEMENT_KEY_CONTRACT_ID,
    PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION,
)
from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope
from pulsara_agent.primitives.transcript_projection import (
    TranscriptProjectionLeafEntryFact,
)


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptPlacementAnchorReferenceFact:
    schema_version: str
    placement_key_contract_id: str
    placement_key_contract_version: str
    placement_key_contract_fingerprint: str
    transcript_anchor_id: str
    stable_anchor_slot_key: str
    stable_first_spine_coordinate: int
    stable_last_spine_coordinate: int
    anchor_fingerprint: str
    anchor_reference_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != "canonical_transcript_placement_anchor_reference.v1":
            raise ValueError("unsupported transcript placement anchor reference")
        if (
            self.placement_key_contract_id != PRESENTATION_PLACEMENT_KEY_CONTRACT_ID
            or self.placement_key_contract_version
            != PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION
            or self.placement_key_contract_fingerprint
            != PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT
        ):
            raise ValueError("transcript anchor placement-key contract mismatch")
        if not (
            1
            <= self.stable_first_spine_coordinate
            <= self.stable_last_spine_coordinate
            <= 2**64 - 2
        ):
            raise ValueError("transcript anchor spine coordinates are invalid")
        expected = context_fingerprint(
            "canonical-transcript-placement-anchor-reference:v1",
            {
                "placement_key_contract_id": self.placement_key_contract_id,
                "placement_key_contract_version": self.placement_key_contract_version,
                "placement_key_contract_fingerprint": (
                    self.placement_key_contract_fingerprint
                ),
                "transcript_anchor_id": self.transcript_anchor_id,
                "stable_anchor_slot_key": self.stable_anchor_slot_key,
                "stable_first_spine_coordinate": self.stable_first_spine_coordinate,
                "stable_last_spine_coordinate": self.stable_last_spine_coordinate,
                "anchor_fingerprint": self.anchor_fingerprint,
            },
        )
        if expected != self.anchor_reference_fingerprint:
            raise ValueError("transcript anchor reference fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptPlacementAnchorTombstoneFact:
    schema_version: str
    placement_key_contract_id: str
    placement_key_contract_version: str
    placement_key_contract_fingerprint: str
    retired_anchor_reference: CanonicalTranscriptPlacementAnchorReferenceFact
    stable_anchor_slot_key: str
    stable_first_spine_coordinate: int
    stable_last_spine_coordinate: int
    retired_by_source_reference_fingerprint: str
    replacement_anchor_reference: CanonicalTranscriptPlacementAnchorReferenceFact | None
    tombstone_fingerprint: str

    def __post_init__(self) -> None:
        retired = self.retired_anchor_reference
        if self.schema_version != "canonical_transcript_placement_anchor_tombstone.v1":
            raise ValueError("unsupported transcript anchor tombstone")
        if (
            self.placement_key_contract_id != retired.placement_key_contract_id
            or self.placement_key_contract_version
            != retired.placement_key_contract_version
            or self.placement_key_contract_fingerprint
            != retired.placement_key_contract_fingerprint
            or self.stable_anchor_slot_key != retired.stable_anchor_slot_key
            or self.stable_first_spine_coordinate
            != retired.stable_first_spine_coordinate
            or self.stable_last_spine_coordinate != retired.stable_last_spine_coordinate
        ):
            raise ValueError("transcript anchor tombstone changed stable placement")
        expected = context_fingerprint(
            "canonical-transcript-placement-anchor-tombstone:v1",
            {
                "placement_key_contract_id": self.placement_key_contract_id,
                "placement_key_contract_version": self.placement_key_contract_version,
                "placement_key_contract_fingerprint": (
                    self.placement_key_contract_fingerprint
                ),
                "retired_anchor_reference_fingerprint": (
                    retired.anchor_reference_fingerprint
                ),
                "stable_anchor_slot_key": self.stable_anchor_slot_key,
                "stable_first_spine_coordinate": self.stable_first_spine_coordinate,
                "stable_last_spine_coordinate": self.stable_last_spine_coordinate,
                "retired_by_source_reference_fingerprint": (
                    self.retired_by_source_reference_fingerprint
                ),
                "replacement_anchor_reference_fingerprint": (
                    self.replacement_anchor_reference.anchor_reference_fingerprint
                    if self.replacement_anchor_reference is not None
                    else None
                ),
            },
        )
        if expected != self.tombstone_fingerprint:
            raise ValueError("transcript anchor tombstone fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptLeafChangeFact:
    schema_version: str
    change_kind: Literal["append", "replace", "retire"]
    previous_entry: TranscriptProjectionLeafEntryFact | None
    resulting_entry: TranscriptProjectionLeafEntryFact | None
    change_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != "canonical_transcript_leaf_change.v1":
            raise ValueError("unsupported canonical transcript leaf-change schema")
        if self.change_kind == "append" and (
            self.previous_entry is not None or self.resulting_entry is None
        ):
            raise ValueError("append leaf change has an invalid shape")
        if self.change_kind == "replace" and (
            self.previous_entry is None or self.resulting_entry is None
        ):
            raise ValueError("replace leaf change has an invalid shape")
        if self.change_kind == "retire" and (
            self.previous_entry is None or self.resulting_entry is not None
        ):
            raise ValueError("retire leaf change has an invalid shape")
        expected = context_fingerprint(
            "canonical-transcript-leaf-change:v1",
            {
                "change_kind": self.change_kind,
                "previous_entry_fingerprint": (
                    self.previous_entry.fact_fingerprint
                    if self.previous_entry is not None
                    else None
                ),
                "resulting_entry_fingerprint": (
                    self.resulting_entry.fact_fingerprint
                    if self.resulting_entry is not None
                    else None
                ),
            },
        )
        if expected != self.change_fingerprint:
            raise ValueError("canonical transcript leaf-change fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptPlacementTransitionProofFact:
    schema_version: str
    placement_key_contract_id: str
    placement_key_contract_version: str
    placement_key_contract_fingerprint: str
    transition_kind: Literal[
        "append", "single_replace", "interval_replace", "retire_to_tombstone"
    ]
    before_canonical_spine_fingerprint: str
    after_canonical_spine_fingerprint: str
    ordered_predecessor_anchor_references: tuple[
        CanonicalTranscriptPlacementAnchorReferenceFact, ...
    ]
    resulting_anchor_reference: CanonicalTranscriptPlacementAnchorReferenceFact | None
    resulting_anchor_tombstones: tuple[
        CanonicalTranscriptPlacementAnchorTombstoneFact, ...
    ]
    reducer_id: str
    reducer_version: str
    reducer_contract_fingerprint: str
    transition_proof_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != "canonical_transcript_placement_transition.v2":
            raise ValueError("unsupported transcript placement transition schema")
        if (
            self.placement_key_contract_id != PRESENTATION_PLACEMENT_KEY_CONTRACT_ID
            or self.placement_key_contract_version
            != PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION
            or self.placement_key_contract_fingerprint
            != PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT
        ):
            raise ValueError("transcript transition placement contract mismatch")
        predecessor_count = len(self.ordered_predecessor_anchor_references)
        if self.transition_kind == "append" and (
            predecessor_count != 0
            or self.resulting_anchor_reference is None
            or self.resulting_anchor_tombstones
        ):
            raise ValueError("append transition has an invalid shape")
        if self.transition_kind == "single_replace" and (
            predecessor_count != 1
            or self.resulting_anchor_reference is None
            or self.resulting_anchor_tombstones
        ):
            raise ValueError("single replacement has an invalid shape")
        if self.transition_kind == "interval_replace" and (
            predecessor_count < 1
            or self.resulting_anchor_reference is None
            or self.resulting_anchor_tombstones
        ):
            raise ValueError("interval replacement has an invalid shape")
        if self.transition_kind == "retire_to_tombstone" and (
            predecessor_count < 1
            or self.resulting_anchor_reference is not None
            or len(self.resulting_anchor_tombstones) != predecessor_count
        ):
            raise ValueError("retirement transition has an invalid shape")
        expected = context_fingerprint(
            "canonical-transcript-placement-transition:v2",
            {
                "placement_key_contract_id": self.placement_key_contract_id,
                "placement_key_contract_version": self.placement_key_contract_version,
                "placement_key_contract_fingerprint": (
                    self.placement_key_contract_fingerprint
                ),
                "transition_kind": self.transition_kind,
                "before_canonical_spine_fingerprint": (
                    self.before_canonical_spine_fingerprint
                ),
                "after_canonical_spine_fingerprint": (
                    self.after_canonical_spine_fingerprint
                ),
                "ordered_predecessor_anchor_reference_fingerprints": tuple(
                    item.anchor_reference_fingerprint
                    for item in self.ordered_predecessor_anchor_references
                ),
                "resulting_anchor_reference_fingerprint": (
                    self.resulting_anchor_reference.anchor_reference_fingerprint
                    if self.resulting_anchor_reference is not None
                    else None
                ),
                "resulting_anchor_tombstone_fingerprints": tuple(
                    item.tombstone_fingerprint
                    for item in self.resulting_anchor_tombstones
                ),
                "reducer_id": self.reducer_id,
                "reducer_version": self.reducer_version,
                "reducer_contract_fingerprint": self.reducer_contract_fingerprint,
            },
        )
        if expected != self.transition_proof_fingerprint:
            raise ValueError("transcript placement transition fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class TranscriptAuditDispositionFact:
    schema_version: str
    disposition_kind: Literal[
        "suppressed_model_output",
        "recovered_transcript",
        "rejected_transcript_candidate",
    ]
    source_event_id: str
    source_sequence: int
    reason_code: str
    disposition_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != "transcript_audit_disposition.v1":
            raise ValueError("unsupported transcript audit disposition schema")
        expected = context_fingerprint(
            "transcript-audit-disposition:v1",
            {
                "disposition_kind": self.disposition_kind,
                "source_event_id": self.source_event_id,
                "source_sequence": self.source_sequence,
                "reason_code": self.reason_code,
            },
        )
        if expected != self.disposition_fingerprint:
            raise ValueError("transcript audit disposition fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptFoldDeltaFact:
    schema_version: str
    runtime_session_id: str
    from_sequence_exclusive: int
    through_sequence: int
    reducer_contract_fingerprint: str
    event_registry_contract_fingerprint: str
    before_live_assembly_fingerprint: str
    after_live_assembly_fingerprint: str
    before_stable_state_fingerprint: str
    after_stable_state_fingerprint: str
    before_canonical_spine_fingerprint: str
    after_canonical_spine_fingerprint: str
    ordered_leaf_changes: tuple[CanonicalTranscriptLeafChangeFact, ...]
    ordered_placement_transition_proofs: tuple[
        CanonicalTranscriptPlacementTransitionProofFact, ...
    ]
    ordered_audit_dispositions: tuple[TranscriptAuditDispositionFact, ...]
    resulting_canonical_state_fingerprint: str
    fold_delta_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != "canonical_transcript_fold_delta.v1":
            raise ValueError("unsupported canonical transcript fold-delta schema")
        if self.from_sequence_exclusive < 0 or (
            self.through_sequence <= self.from_sequence_exclusive
        ):
            raise ValueError("canonical transcript fold interval is invalid")
        resulting = context_fingerprint(
            "canonical-transcript-resulting-state:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "through_sequence": self.through_sequence,
                "live_assembly_fingerprint": self.after_live_assembly_fingerprint,
                "stable_state_fingerprint": self.after_stable_state_fingerprint,
                "canonical_spine_fingerprint": (self.after_canonical_spine_fingerprint),
            },
        )
        if resulting != self.resulting_canonical_state_fingerprint:
            raise ValueError("canonical transcript resulting state mismatch")
        payload = {
            "runtime_session_id": self.runtime_session_id,
            "from_sequence_exclusive": self.from_sequence_exclusive,
            "through_sequence": self.through_sequence,
            "reducer_contract_fingerprint": self.reducer_contract_fingerprint,
            "event_registry_contract_fingerprint": (
                self.event_registry_contract_fingerprint
            ),
            "before_live_assembly_fingerprint": (self.before_live_assembly_fingerprint),
            "after_live_assembly_fingerprint": self.after_live_assembly_fingerprint,
            "before_stable_state_fingerprint": self.before_stable_state_fingerprint,
            "after_stable_state_fingerprint": self.after_stable_state_fingerprint,
            "before_canonical_spine_fingerprint": (
                self.before_canonical_spine_fingerprint
            ),
            "after_canonical_spine_fingerprint": (
                self.after_canonical_spine_fingerprint
            ),
            "ordered_leaf_change_fingerprints": tuple(
                item.change_fingerprint for item in self.ordered_leaf_changes
            ),
            "ordered_placement_transition_fingerprints": tuple(
                item.transition_proof_fingerprint
                for item in self.ordered_placement_transition_proofs
            ),
            "ordered_audit_disposition_fingerprints": tuple(
                item.disposition_fingerprint for item in self.ordered_audit_dispositions
            ),
            "resulting_canonical_state_fingerprint": (
                self.resulting_canonical_state_fingerprint
            ),
        }
        if self.fold_delta_fingerprint != context_fingerprint(
            "canonical-transcript-fold-delta:v1", payload
        ):
            raise ValueError("canonical transcript fold-delta fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class LiveCommittedFoldResult:
    source_stored_batch_ordered_join_fingerprint: str
    source_first_sequence: int
    source_last_sequence: int
    source_envelope_accumulator: str
    fold_delta: CanonicalTranscriptFoldDeltaFact
    live_result_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.fold_delta.from_sequence_exclusive != self.source_first_sequence - 1
            or self.fold_delta.through_sequence != self.source_last_sequence
        ):
            raise ValueError("live fold source interval mismatch")
        expected = context_fingerprint(
            "live-committed-transcript-fold-result:v1",
            {
                "source_stored_batch_ordered_join_fingerprint": (
                    self.source_stored_batch_ordered_join_fingerprint
                ),
                "source_first_sequence": self.source_first_sequence,
                "source_last_sequence": self.source_last_sequence,
                "source_envelope_accumulator": self.source_envelope_accumulator,
                "fold_delta_fingerprint": self.fold_delta.fold_delta_fingerprint,
            },
        )
        if expected != self.live_result_fingerprint:
            raise ValueError("live transcript fold result fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RestoredRangeFoldResult:
    source_range_proof_fingerprint: str
    source_first_sequence: int
    source_last_sequence: int
    source_envelope_accumulator: str
    fold_delta: CanonicalTranscriptFoldDeltaFact
    restored_result_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.fold_delta.from_sequence_exclusive != self.source_first_sequence - 1
            or self.fold_delta.through_sequence != self.source_last_sequence
        ):
            raise ValueError("restored fold source interval mismatch")
        expected = context_fingerprint(
            "restored-range-transcript-fold-result:v1",
            {
                "source_range_proof_fingerprint": self.source_range_proof_fingerprint,
                "source_first_sequence": self.source_first_sequence,
                "source_last_sequence": self.source_last_sequence,
                "source_envelope_accumulator": self.source_envelope_accumulator,
                "fold_delta_fingerprint": self.fold_delta.fold_delta_fingerprint,
            },
        )
        if expected != self.restored_result_fingerprint:
            raise ValueError("restored transcript fold result fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class CommittedPresentationTapEntry:
    schema_version: str
    runtime_session_id: str
    source_first_sequence: int
    source_last_sequence: int
    raw_stored_envelopes: tuple[RawStoredEventEnvelope, ...]
    stored_batch_ordered_join_fingerprint: str
    canonical_fold_result: LiveCommittedFoldResult
    tap_entry_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != "committed_presentation_tap_entry.v1":
            raise ValueError("unsupported committed presentation tap-entry schema")
        sequences = tuple(item.sequence for item in self.raw_stored_envelopes)
        if not sequences or sequences != tuple(
            range(self.source_first_sequence, self.source_last_sequence + 1)
        ):
            raise ValueError("tap entry raw envelope interval mismatch")
        if any(
            item.runtime_session_id != self.runtime_session_id
            for item in self.raw_stored_envelopes
        ):
            raise ValueError("tap entry crosses runtime sessions")
        fold = self.canonical_fold_result
        if (
            fold.source_first_sequence != self.source_first_sequence
            or fold.source_last_sequence != self.source_last_sequence
            or fold.source_stored_batch_ordered_join_fingerprint
            != self.stored_batch_ordered_join_fingerprint
        ):
            raise ValueError("tap entry fold source mismatch")
        expected = context_fingerprint(
            "committed-presentation-tap-entry:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "source_first_sequence": self.source_first_sequence,
                "source_last_sequence": self.source_last_sequence,
                "stored_batch_ordered_join_fingerprint": (
                    self.stored_batch_ordered_join_fingerprint
                ),
                "source_envelope_accumulator": fold.source_envelope_accumulator,
                "fold_result_fingerprint": fold.live_result_fingerprint,
            },
        )
        if expected != self.tap_entry_fingerprint:
            raise ValueError("committed presentation tap-entry fingerprint mismatch")


__all__ = [
    "CanonicalTranscriptFoldDeltaFact",
    "CanonicalTranscriptLeafChangeFact",
    "CanonicalTranscriptPlacementAnchorReferenceFact",
    "CanonicalTranscriptPlacementAnchorTombstoneFact",
    "CanonicalTranscriptPlacementTransitionProofFact",
    "CommittedPresentationTapEntry",
    "LiveCommittedFoldResult",
    "RestoredRangeFoldResult",
    "TranscriptAuditDispositionFact",
    "PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT",
    "PRESENTATION_PLACEMENT_KEY_CONTRACT_ID",
    "PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION",
]
