"""Storage-only acceleration state for bounded presentation checkpoint restore."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from pulsara_agent.primitives.presentation_history import (
    HistoryCapacityReconciliationRequiredFact,
    HistoryTreeCapacityExhaustedFact,
    PresentationHistoryGrowthReservationFact,
    PresentationHistoryPlacementKeyFact,
)
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.storage_frozen import (
    FrozenStorageFactBase,
    register_durable_storage_fact,
)


class PresentationHistorySpineEntryAccelerationFact(FrozenStorageFactBase):
    schema_version: Literal["presentation_history_spine_entry_acceleration.v1"]
    anchor_state_kind: Literal["current", "tombstone"]
    transcript_entry_fact_fingerprint: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    transcript_anchor_id: str
    stable_anchor_slot_key: str
    stable_first_spine_coordinate: int = Field(ge=1, le=2**64 - 2)
    stable_last_spine_coordinate: int = Field(ge=1, le=2**64 - 2)
    anchor_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    anchor_reference_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    first_ordering_boundary_sequence: int = Field(ge=1)
    last_ordering_boundary_sequence: int = Field(ge=1)
    tombstone_fingerprint: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    history_entry_id: str | None
    history_entry_placement_key: PresentationHistoryPlacementKeyFact | None
    history_entry_fingerprint: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    entry_acceleration_fingerprint: str

    @model_validator(mode="after")
    def _matrix(self) -> "PresentationHistorySpineEntryAccelerationFact":
        if self.stable_first_spine_coordinate > self.stable_last_spine_coordinate:
            raise ValueError(
                "presentation spine acceleration coordinate range is invalid"
            )
        if self.first_ordering_boundary_sequence > self.last_ordering_boundary_sequence:
            raise ValueError(
                "presentation spine acceleration boundary range is invalid"
            )
        if self.anchor_state_kind == "current":
            if self.transcript_entry_fact_fingerprint is None:
                raise ValueError("current presentation anchor lacks transcript entry")
            if self.tombstone_fingerprint is not None:
                raise ValueError("current presentation anchor cannot carry tombstone")
        elif (
            self.transcript_entry_fact_fingerprint is not None
            or self.tombstone_fingerprint is None
        ):
            raise ValueError("presentation tombstone acceleration matrix mismatch")
        values = (
            self.history_entry_id,
            self.history_entry_placement_key,
            self.history_entry_fingerprint,
        )
        if any(item is None for item in values) and not all(
            item is None for item in values
        ):
            raise ValueError("presentation spine history-entry identity is partial")
        return self


class PresentationHistorySpineAccelerationFact(FrozenStorageFactBase):
    schema_version: Literal["presentation_history_spine_acceleration.v1"]
    runtime_session_id: str
    placement_key_contract_id: str
    placement_key_contract_version: str
    placement_key_contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    through_authority_sequence: int = Field(ge=0)
    projection_revision: int = Field(ge=0)
    canonical_spine_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ordered_entries: tuple[PresentationHistorySpineEntryAccelerationFact, ...]
    spine_acceleration_fingerprint: str

    @model_validator(mode="after")
    def _ordered(self) -> "PresentationHistorySpineAccelerationFact":
        coordinates = tuple(
            (item.stable_first_spine_coordinate, item.stable_last_spine_coordinate)
            for item in self.ordered_entries
        )
        if coordinates != tuple(sorted(coordinates)):
            raise ValueError("presentation spine acceleration is not ordered")
        if any(
            previous[1] >= current[0]
            for previous, current in zip(coordinates, coordinates[1:])
        ):
            raise ValueError("presentation spine acceleration ranges overlap")
        return self


class PresentationHistoryCapacityReservationCheckpointEntryFact(FrozenStorageFactBase):
    """Exact durable-source binding for one nonterminal capacity reservation."""

    schema_version: Literal[
        "presentation_history_capacity_reservation_checkpoint_entry.v1"
    ]
    reservation: PresentationHistoryGrowthReservationFact
    source_run_start_event_reference: ContextEventReferenceFact
    entry_fingerprint: str

    @model_validator(mode="after")
    def _active_run_source(
        self,
    ) -> "PresentationHistoryCapacityReservationCheckpointEntryFact":
        if self.reservation.reservation_state not in {
            "reserved",
            "reconciliation_required",
        }:
            raise ValueError("capacity checkpoint cannot retain terminal reservations")
        if self.reservation.quote.runtime_session_id != (
            self.source_run_start_event_reference.runtime_session_id
        ):
            raise ValueError("capacity reservation source crosses runtime sessions")
        if self.source_run_start_event_reference.event_type != "RUN_START":
            raise ValueError("capacity reservation source is not RunStart")
        if self.reservation.owner_kind != "host_run":
            raise ValueError("V1 capacity checkpoint only owns Host runs")
        return self


PresentationHistoryCapacityCheckpointFault = (
    HistoryCapacityReconciliationRequiredFact | HistoryTreeCapacityExhaustedFact
)


class PresentationHistoryCapacityCheckpointFact(FrozenStorageFactBase):
    """Storage-only acceleration for exact reservation recovery at one ledger cut."""

    schema_version: Literal["presentation_history_capacity_checkpoint.v1"]
    runtime_session_id: str
    through_authority_sequence: int = Field(ge=0)
    quote_policy_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ordered_active_reservations: tuple[
        PresentationHistoryCapacityReservationCheckpointEntryFact, ...
    ]
    fault: PresentationHistoryCapacityCheckpointFault | None = None
    capacity_checkpoint_fingerprint: str

    @model_validator(mode="after")
    def _canonical(self) -> "PresentationHistoryCapacityCheckpointFact":
        reservations = self.ordered_active_reservations
        keys = tuple(
            (
                item.source_run_start_event_reference.sequence,
                item.reservation.growth_reservation_id,
            )
            for item in reservations
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("capacity checkpoint reservations are not canonical")
        owner_ids = tuple(item.reservation.owner_id for item in reservations)
        source_fingerprints = tuple(
            item.reservation.quote.source_authority_fingerprint for item in reservations
        )
        if len(owner_ids) != len(set(owner_ids)) or len(source_fingerprints) != len(
            set(source_fingerprints)
        ):
            raise ValueError("capacity checkpoint has duplicate reservation authority")
        if any(
            item.reservation.quote.runtime_session_id != self.runtime_session_id
            or item.source_run_start_event_reference.runtime_session_id
            != self.runtime_session_id
            or item.source_run_start_event_reference.sequence
            > self.through_authority_sequence
            or item.reservation.quote.quote_policy_fingerprint
            != self.quote_policy_fingerprint
            for item in reservations
        ):
            raise ValueError("capacity checkpoint authority join mismatch")
        return self


register_durable_storage_fact(
    schema_version="presentation_history_spine_entry_acceleration.v1",
    own_fingerprint_field="entry_acceleration_fingerprint",
    domain_separator="presentation-history-spine-entry-acceleration:v1",
)
register_durable_storage_fact(
    schema_version="presentation_history_capacity_reservation_checkpoint_entry.v1",
    own_fingerprint_field="entry_fingerprint",
    domain_separator="presentation-history-capacity-reservation-checkpoint-entry:v1",
)
register_durable_storage_fact(
    schema_version="presentation_history_capacity_checkpoint.v1",
    own_fingerprint_field="capacity_checkpoint_fingerprint",
    domain_separator="presentation-history-capacity-checkpoint:v1",
)
register_durable_storage_fact(
    schema_version="presentation_history_spine_acceleration.v1",
    own_fingerprint_field="spine_acceleration_fingerprint",
    domain_separator="presentation-history-spine-acceleration:v1",
)


__all__ = [
    "PresentationHistorySpineAccelerationFact",
    "PresentationHistorySpineEntryAccelerationFact",
    "PresentationHistoryCapacityCheckpointFact",
    "PresentationHistoryCapacityReservationCheckpointEntryFact",
]
