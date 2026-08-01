"""Renderer-neutral viewport, paging, and root-transition carriers."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    register_durable_fact,
)
from pulsara_agent.primitives.presentation_history import (
    PresentationHistoryActiveHeadFact,
    PresentationHistoryPageCursorFact,
    PresentationHistoryPlacementKeyFact,
    PresentationHistoryRankedEntryView,
    PresentationHistoryRootIdentityFact,
)


Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class PresentationHistoryLatestRootCursorPairFact(FrozenFactBase):
    schema_version: Literal["presentation_history_latest_root_cursor_pair.v1"]
    root_identity: PresentationHistoryRootIdentityFact
    before_cursor: PresentationHistoryPageCursorFact | None
    after_cursor: PresentationHistoryPageCursorFact | None
    cursor_pair_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root_join(self) -> "PresentationHistoryLatestRootCursorPairFact":
        for cursor in (self.before_cursor, self.after_cursor):
            if (
                cursor is not None
                and cursor.history_root_identity != self.root_identity
            ):
                raise ValueError("presentation cursor pair crosses roots")
        return self


class PresentationHistoryViewportSnapshotFact(FrozenFactBase):
    schema_version: Literal["presentation_history_viewport_snapshot.v1"]
    runtime_session_id: str
    projection_revision: NonNegativeInt
    active_head: PresentationHistoryActiveHeadFact
    ordered_resident_entries: tuple[PresentationHistoryRankedEntryView, ...]
    latest_root_cursor_pair: PresentationHistoryLatestRootCursorPairFact
    resident_cell_count: NonNegativeInt
    resident_canonical_bytes: NonNegativeInt
    oldest_history_entry_id: str | None
    oldest_placement_key: PresentationHistoryPlacementKeyFact | None
    newest_history_entry_id: str | None
    newest_placement_key: PresentationHistoryPlacementKeyFact | None
    resident_vector_fingerprint: Fingerprint
    viewport_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _shape(self) -> "PresentationHistoryViewportSnapshotFact":
        if self.runtime_session_id != self.active_head.runtime_session_id:
            raise ValueError("presentation viewport crosses runtime sessions")
        if self.resident_cell_count != len(self.ordered_resident_entries):
            raise ValueError("presentation viewport resident count mismatch")
        if self.latest_root_cursor_pair.root_identity != (
            self.active_head.confirmed_root_identity
        ):
            raise ValueError("presentation viewport cursor/head root mismatch")
        empty = self.resident_cell_count == 0
        endpoints = (
            self.oldest_history_entry_id,
            self.oldest_placement_key,
            self.newest_history_entry_id,
            self.newest_placement_key,
        )
        if empty != all(item is None for item in endpoints):
            raise ValueError("presentation viewport endpoint shape mismatch")
        return self


class PresentationHistoryRootCursorRelationFact(FrozenFactBase):
    schema_version: Literal["presentation_history_root_cursor_relation.v1"]
    previous_root_identity: PresentationHistoryRootIdentityFact
    resulting_root_identity: PresentationHistoryRootIdentityFact
    relation_kind: Literal["strict_prefix_extended", "rewritten_generation"]
    previous_cursor_disposition: Literal["retained_pinned"]
    shared_prefix_entry_count: NonNegativeInt
    shared_prefix_accumulator: Fingerprint
    relation_fingerprint: Fingerprint


class ResidentEntriesUnchangedFact(FrozenFactBase):
    schema_version: Literal["presentation_resident_entries_unchanged.v1"]
    transition_kind: Literal["unchanged"]
    before_resident_vector_fingerprint: Fingerprint
    after_resident_vector_fingerprint: Fingerprint
    exact_equivalence_proof_fingerprint: Fingerprint
    transition_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _equal(self) -> "ResidentEntriesUnchangedFact":
        if (
            self.before_resident_vector_fingerprint
            != self.after_resident_vector_fingerprint
        ):
            raise ValueError("unchanged resident transition changed its vector")
        return self


class PresentationHistoryResidentUpsertFact(FrozenFactBase):
    schema_version: Literal["presentation_history_resident_upsert.v1"]
    change_kind: Literal["upsert"]
    history_entry_id: str
    placement_key: PresentationHistoryPlacementKeyFact
    expected_previous_entry_fingerprint: Fingerprint | None
    resulting_ranked_entry: PresentationHistoryRankedEntryView
    change_fingerprint: Fingerprint


class PresentationHistoryResidentRemoveFact(FrozenFactBase):
    schema_version: Literal["presentation_history_resident_remove.v1"]
    change_kind: Literal["remove"]
    history_entry_id: str
    placement_key: PresentationHistoryPlacementKeyFact
    expected_previous_entry_fingerprint: Fingerprint
    change_fingerprint: Fingerprint


PresentationHistoryResidentChangeFact: TypeAlias = Annotated[
    PresentationHistoryResidentUpsertFact | PresentationHistoryResidentRemoveFact,
    Field(discriminator="change_kind"),
]


class BoundedOrderedResidentChangesFact(FrozenFactBase):
    schema_version: Literal["presentation_bounded_resident_changes.v1"]
    transition_kind: Literal["bounded_ordered_changes"]
    before_resident_vector_fingerprint: Fingerprint
    after_resident_vector_fingerprint: Fingerprint
    ordered_changes: tuple[PresentationHistoryResidentChangeFact, ...]
    change_count: NonNegativeInt
    encoded_change_bytes: NonNegativeInt
    transition_limits_policy_fingerprint: Fingerprint
    ordered_change_accumulator: Fingerprint
    transition_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _count(self) -> "BoundedOrderedResidentChangesFact":
        if self.change_count != len(self.ordered_changes):
            raise ValueError("presentation resident change count mismatch")
        return self


class ResidentHistoryRebaseRequiredFact(FrozenFactBase):
    schema_version: Literal["presentation_resident_history_rebase_required.v1"]
    transition_kind: Literal["rebase_required"]
    before_resident_vector_fingerprint: Fingerprint
    target_root_identity: PresentationHistoryRootIdentityFact
    target_active_head_fingerprint: Fingerprint
    stable_reason: Literal[
        "RESIDENT_CHANGE_COUNT_EXCEEDED",
        "RESIDENT_CHANGE_BYTES_EXCEEDED",
        "REWRITE_REQUIRES_SNAPSHOT",
        "PINNED_WINDOW_NOT_PROVABLE",
        "SESSION_HISTORY_ROTATION_REQUIRED",
        "HISTORY_TREE_CAPACITY_EXHAUSTED",
    ]
    bounded_rebase_or_snapshot_token: str
    token_generation: PositiveInt
    expires_at_utc: str
    transition_fingerprint: Fingerprint


PresentationHistoryRootResidentTransitionFact: TypeAlias = Annotated[
    ResidentEntriesUnchangedFact
    | BoundedOrderedResidentChangesFact
    | ResidentHistoryRebaseRequiredFact,
    Field(discriminator="transition_kind"),
]


class PresentationHistoryRootAdvancedFact(FrozenFactBase):
    schema_version: Literal["presentation_history_root_advanced.v1"]
    base_projection_revision: NonNegativeInt
    resulting_projection_revision: PositiveInt
    previous_active_head_fingerprint: Fingerprint
    resulting_active_head: PresentationHistoryActiveHeadFact
    latest_root_cursor_pair: PresentationHistoryLatestRootCursorPairFact
    previous_root_relation: PresentationHistoryRootCursorRelationFact
    resident_transition: PresentationHistoryRootResidentTransitionFact
    consumed_checkpoint_candidate_cut_fingerprint: Fingerprint
    consumed_tail_prefix_through_sequence: NonNegativeInt
    consumed_tail_prefix_source_range_accumulator: Fingerprint
    consumed_tail_prefix_segment_count: NonNegativeInt
    consumed_tail_prefix_segment_accumulator: Fingerprint
    consumed_tail_prefix_mutation_count: NonNegativeInt
    consumed_tail_prefix_mutation_accumulator: Fingerprint
    retained_tail_suffix_from_sequence_exclusive: NonNegativeInt
    retained_tail_suffix_through_sequence: NonNegativeInt
    retained_tail_suffix_source_range_accumulator: Fingerprint
    retained_tail_suffix_segment_count: NonNegativeInt
    retained_tail_suffix_segment_accumulator: Fingerprint
    retained_tail_suffix_mutation_count: NonNegativeInt
    retained_tail_suffix_mutation_accumulator: Fingerprint
    checkpoint_full_confirmation_fingerprint: Fingerprint
    root_advanced_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _revision(self) -> "PresentationHistoryRootAdvancedFact":
        if self.resulting_projection_revision != self.base_projection_revision + 1:
            raise ValueError("presentation root advance skipped a projection revision")
        if (
            self.latest_root_cursor_pair.root_identity
            != self.resulting_active_head.confirmed_root_identity
        ):
            raise ValueError("presentation root advance cursor/head mismatch")
        return self


_FACT_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "presentation_history_latest_root_cursor_pair.v1",
        "cursor_pair_fingerprint",
        "presentation-history-latest-root-cursor-pair:v1",
    ),
    (
        "presentation_history_viewport_snapshot.v1",
        "viewport_fingerprint",
        "presentation-history-viewport-snapshot:v1",
    ),
    (
        "presentation_history_root_cursor_relation.v1",
        "relation_fingerprint",
        "presentation-history-root-cursor-relation:v1",
    ),
    (
        "presentation_resident_entries_unchanged.v1",
        "transition_fingerprint",
        "presentation-resident-entries-unchanged:v1",
    ),
    (
        "presentation_history_resident_upsert.v1",
        "change_fingerprint",
        "presentation-history-resident-upsert:v1",
    ),
    (
        "presentation_history_resident_remove.v1",
        "change_fingerprint",
        "presentation-history-resident-remove:v1",
    ),
    (
        "presentation_bounded_resident_changes.v1",
        "transition_fingerprint",
        "presentation-bounded-resident-changes:v1",
    ),
    (
        "presentation_resident_history_rebase_required.v1",
        "transition_fingerprint",
        "presentation-resident-history-rebase-required:v1",
    ),
    (
        "presentation_history_root_advanced.v1",
        "root_advanced_fingerprint",
        "presentation-history-root-advanced:v1",
    ),
)

for _schema, _field, _domain in _FACT_SPECS:
    register_durable_fact(
        schema_version=_schema,
        own_fingerprint_field=_field,
        domain_separator=_domain,
    )


__all__ = [
    "BoundedOrderedResidentChangesFact",
    "PresentationHistoryLatestRootCursorPairFact",
    "PresentationHistoryResidentChangeFact",
    "PresentationHistoryResidentRemoveFact",
    "PresentationHistoryResidentUpsertFact",
    "PresentationHistoryRootAdvancedFact",
    "PresentationHistoryRootCursorRelationFact",
    "PresentationHistoryRootResidentTransitionFact",
    "PresentationHistoryViewportSnapshotFact",
    "ResidentEntriesUnchangedFact",
    "ResidentHistoryRebaseRequiredFact",
]
