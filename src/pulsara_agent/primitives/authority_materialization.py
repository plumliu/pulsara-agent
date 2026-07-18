"""Event-safe contracts for bounded authority materialization.

The contracts in this module deliberately contain no runtime services.  They
form the AP0 vocabulary shared by storage, admission, replay, and Inspector.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    FrozenRuntimeStateBase,
    StableEventIdentityFact,
    register_durable_fact,
)


Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


# V1 physical product circuit breakers.  These bound provider-originated
# source material, not the model context window and not the complete durable
# lifecycle batch.  Structural and terminal events use the separate tail
# allowances below.
MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL = 16_384
MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL = 16 * 1024 * 1024
MAX_MODEL_STREAM_STRUCTURAL_TAIL_EVENTS = 32
MAX_MODEL_STREAM_STRUCTURAL_TAIL_PAYLOAD_BYTES = 512 * 1024


def _require_sorted_unique(values: tuple[object, ...], *, context: str) -> None:
    if tuple(values) != tuple(sorted(values, key=str)) or len(values) != len(
        set(values)
    ):
        raise ValueError(f"{context} must be sorted and unique")


def _validate_business_window(
    run_id: str | None,
    window_id: str | None,
    generation: int | None,
) -> None:
    present = (run_id is not None, window_id is not None, generation is not None)
    if any(present) and not all(present):
        raise ValueError("business window attribution must be all-null or all-present")
    if generation is not None and generation < 0:
        raise ValueError("business window generation must be non-negative")


class PhysicalOperationKind(StrEnum):
    LEDGER_GENESIS = "ledger_genesis"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    EXTERNAL_EXECUTION = "external_execution"
    MCP_RESUME = "mcp_resume"
    CHILD_PARENT_GRAPH_WRITE = "child_parent_graph_write"
    HOST_RUN_BOUNDARY = "host_run_boundary"
    CHECKPOINT_COMMIT = "checkpoint_commit"
    RUNTIME_INTERNAL_WRITE = "runtime_internal_write"


ReservablePhysicalOperationKind: TypeAlias = Literal[
    PhysicalOperationKind.MODEL_CALL,
    PhysicalOperationKind.TOOL_CALL,
    PhysicalOperationKind.EXTERNAL_EXECUTION,
    PhysicalOperationKind.MCP_RESUME,
    PhysicalOperationKind.CHILD_PARENT_GRAPH_WRITE,
    PhysicalOperationKind.HOST_RUN_BOUNDARY,
    PhysicalOperationKind.CHECKPOINT_COMMIT,
    PhysicalOperationKind.RUNTIME_INTERNAL_WRITE,
]


class FixedBatchEventContractFact(FrozenFactBase):
    schema_version: Literal["fixed_batch_event_contract.v1"]
    event_type: str = Field(min_length=1, max_length=128)
    event_schema_version: str = Field(min_length=1, max_length=128)
    event_schema_fingerprint: Fingerprint
    minimum_occurrences: NonNegativeInt
    maximum_occurrences: NonNegativeInt
    max_candidate_payload_bytes_per_occurrence: PositiveInt
    event_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _occurrences(self) -> "FixedBatchEventContractFact":
        if self.minimum_occurrences > self.maximum_occurrences:
            raise ValueError("fixed batch minimum exceeds maximum occurrences")
        return self


class PhysicalBurstContractBase(FrozenFactBase):
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    operation_kind: PhysicalOperationKind
    max_commit_batches: PositiveInt
    max_structural_tail_events: NonNegativeInt
    max_structural_tail_payload_bytes: NonNegativeInt
    max_terminal_recovery_events: NonNegativeInt
    max_terminal_recovery_payload_bytes: NonNegativeInt
    terminal_tail_reserved_events: NonNegativeInt
    terminal_tail_reserved_payload_bytes: NonNegativeInt
    max_total_reserved_events: PositiveInt
    max_total_reserved_payload_bytes: PositiveInt
    event_domain_registry_contract_fingerprint: Fingerprint
    canonical_event_serialization_contract_fingerprint: Fingerprint
    physical_charge_contract_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _terminal_tail_is_covered(self) -> "PhysicalBurstContractBase":
        if self.terminal_tail_reserved_events > (
            self.max_structural_tail_events + self.max_terminal_recovery_events
        ):
            raise ValueError("terminal event tail exceeds the bounded operation tail")
        if self.terminal_tail_reserved_payload_bytes > (
            self.max_structural_tail_payload_bytes
            + self.max_terminal_recovery_payload_bytes
        ):
            raise ValueError("terminal byte tail exceeds the bounded operation tail")
        return self


class TransportSegmentedBurstContractFact(PhysicalBurstContractBase):
    schema_version: Literal["transport_segmented_burst_contract.v1"]
    burst_shape: Literal["transport_segmented"]
    operation_kind: Literal[PhysicalOperationKind.MODEL_CALL]
    segmentation_mode: Literal["contiguous_model_delta_segment_v1"]
    max_source_items: PositiveInt
    max_source_payload_bytes: PositiveInt
    max_single_source_item_canonical_bytes: PositiveInt
    max_segment_source_items: PositiveInt
    max_segment_content_utf8_bytes: PositiveInt
    max_segment_canonical_event_bytes: PositiveInt
    max_durable_event_wrapper_overhead_bytes: PositiveInt
    max_unconfirmed_age_millis: PositiveInt
    max_durable_events_per_source_item: PositiveInt
    max_synthetic_semantic_tail_events: PositiveInt
    max_synthetic_semantic_tail_payload_bytes: PositiveInt
    max_start_commit_batches: PositiveInt
    max_terminal_commit_batches: PositiveInt
    max_recovery_commit_batches: PositiveInt
    max_bookkeeping_events_per_commit: PositiveInt
    max_bookkeeping_base_payload_bytes_per_commit: PositiveInt
    max_bookkeeping_payload_bytes_per_business_event: PositiveInt
    segment_policy_contract_fingerprint: Fingerprint
    sanitization_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _covers_segmented_burst(self) -> "TransportSegmentedBurstContractFact":
        if self.max_durable_events_per_source_item != 1:
            raise ValueError("V1 model segmentation requires one event/source worst case")
        if self.max_synthetic_semantic_tail_events != 1:
            raise ValueError("V1 model segmentation permits one synthetic semantic tail")
        if self.max_start_commit_batches != 1 or self.max_terminal_commit_batches != 1:
            raise ValueError("V1 model lifecycle has one start and terminal batch")
        max_durable_events = (
            self.max_source_items * self.max_durable_events_per_source_item
            + self.max_synthetic_semantic_tail_events
        )
        required_commit_batches = (
            self.max_start_commit_batches
            + max_durable_events
            + self.max_terminal_commit_batches
            + self.max_recovery_commit_batches
        )
        required_events = (
            max_durable_events
            + required_commit_batches * self.max_bookkeeping_events_per_commit
            + self.max_structural_tail_events
            + self.max_terminal_recovery_events
        )
        required_bytes = (
            self.max_source_payload_bytes
            + self.max_synthetic_semantic_tail_payload_bytes
            + max_durable_events
            * self.max_durable_event_wrapper_overhead_bytes
            + required_commit_batches
            * self.max_bookkeeping_base_payload_bytes_per_commit
            + max_durable_events
            * self.max_bookkeeping_payload_bytes_per_business_event
            + self.max_structural_tail_payload_bytes
            + self.max_terminal_recovery_payload_bytes
        )
        if self.max_commit_batches < required_commit_batches:
            raise ValueError("segmented transport commit batch bound is underestimated")
        if self.max_total_reserved_events < required_events:
            raise ValueError("transport burst event reservation is underestimated")
        if self.max_total_reserved_payload_bytes < required_bytes:
            raise ValueError("transport burst byte reservation is underestimated")
        return self


class ToolDeltaBurstContractFact(PhysicalBurstContractBase):
    schema_version: Literal["tool_delta_burst_contract.v1"]
    burst_shape: Literal["tool_delta"]
    operation_kind: Literal[
        PhysicalOperationKind.TOOL_CALL,
        PhysicalOperationKind.EXTERNAL_EXECUTION,
    ]
    max_result_delta_items: PositiveInt
    max_result_delta_payload_bytes: PositiveInt
    max_durable_events_per_delta_item: PositiveInt
    max_canonical_wrapper_payload_bytes_per_delta_item: PositiveInt
    result_capture_contract_fingerprint: Fingerprint
    artifact_fallback_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _covers_delta_burst(self) -> "ToolDeltaBurstContractFact":
        required_events = (
            self.max_result_delta_items * self.max_durable_events_per_delta_item
            + self.max_structural_tail_events
            + self.max_terminal_recovery_events
        )
        required_bytes = (
            self.max_result_delta_payload_bytes
            + self.max_result_delta_items
            * self.max_canonical_wrapper_payload_bytes_per_delta_item
            + self.max_structural_tail_payload_bytes
            + self.max_terminal_recovery_payload_bytes
        )
        if self.max_total_reserved_events < required_events:
            raise ValueError("tool delta burst event reservation is underestimated")
        if self.max_total_reserved_payload_bytes < required_bytes:
            raise ValueError("tool delta burst byte reservation is underestimated")
        return self


class FixedBatchBurstContractFact(PhysicalBurstContractBase):
    schema_version: Literal["fixed_batch_burst_contract.v1"]
    burst_shape: Literal["fixed_batch"]
    operation_kind: Literal[
        PhysicalOperationKind.LEDGER_GENESIS,
        PhysicalOperationKind.EXTERNAL_EXECUTION,
        PhysicalOperationKind.MCP_RESUME,
        PhysicalOperationKind.CHILD_PARENT_GRAPH_WRITE,
        PhysicalOperationKind.HOST_RUN_BOUNDARY,
        PhysicalOperationKind.CHECKPOINT_COMMIT,
        PhysicalOperationKind.RUNTIME_INTERNAL_WRITE,
    ]
    max_business_events: PositiveInt
    max_business_candidate_payload_bytes: PositiveInt
    batch_event_contracts: tuple[FixedBatchEventContractFact, ...]
    batch_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _covers_fixed_batch(self) -> "FixedBatchBurstContractFact":
        keys = tuple(
            (item.event_type, item.event_schema_version)
            for item in self.batch_event_contracts
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("fixed batch event contracts must be sorted and unique")
        required_events = (
            self.max_business_events
            + self.max_structural_tail_events
            + self.max_terminal_recovery_events
        )
        required_bytes = (
            self.max_business_candidate_payload_bytes
            + self.max_structural_tail_payload_bytes
            + self.max_terminal_recovery_payload_bytes
        )
        if self.max_total_reserved_events < required_events:
            raise ValueError("fixed batch event reservation is underestimated")
        if self.max_total_reserved_payload_bytes < required_bytes:
            raise ValueError("fixed batch byte reservation is underestimated")
        if sum(item.maximum_occurrences for item in self.batch_event_contracts) > (
            self.max_business_events
        ):
            raise ValueError("fixed batch occurrence matrix exceeds event bound")
        return self


PhysicalBurstContractFact: TypeAlias = Annotated[
    TransportSegmentedBurstContractFact
    | ToolDeltaBurstContractFact
    | FixedBatchBurstContractFact,
    Field(discriminator="burst_shape"),
]


class StoredEnvelopeIdentityBoundsFact(FrozenFactBase):
    schema_version: Literal["stored_envelope_identity_bounds.v1"]
    maximum_ledger_sequence: PositiveInt
    sequence_encoding: Literal["unsigned_decimal"]
    max_sequence_encoded_bytes: PositiveInt
    max_event_id_utf8_bytes: PositiveInt
    max_runtime_session_id_utf8_bytes: PositiveInt
    max_run_id_utf8_bytes: PositiveInt
    max_turn_id_utf8_bytes: PositiveInt
    max_context_id_utf8_bytes: PositiveInt
    max_event_type_utf8_bytes: PositiveInt
    max_event_schema_version_utf8_bytes: PositiveInt
    max_created_at_utc_utf8_bytes: PositiveInt
    max_wrapper_metadata_canonical_bytes: NonNegativeInt
    bounds_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _sequence_width(self) -> "StoredEnvelopeIdentityBoundsFact":
        if len(str(self.maximum_ledger_sequence).encode("ascii")) > (
            self.max_sequence_encoded_bytes
        ):
            raise ValueError("maximum ledger sequence exceeds encoded byte bound")
        return self


class PhysicalBookkeepingEventBoundFact(FrozenFactBase):
    schema_version: Literal["physical_bookkeeping_event_bound.v1"]
    event_type: str = Field(min_length=1, max_length=128)
    event_schema_version: str = Field(min_length=1, max_length=128)
    event_schema_fingerprint: Fingerprint
    max_payload_canonical_bytes: PositiveInt
    max_stored_envelope_bytes: PositiveInt
    bound_fingerprint: Fingerprint


class PhysicalChargeContractFact(FrozenFactBase):
    schema_version: Literal["physical_charge_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    candidate_payload_canonicalization_fingerprint: Fingerprint
    stored_envelope_identity_bounds: StoredEnvelopeIdentityBoundsFact
    bookkeeping_event_bounds: tuple[PhysicalBookkeepingEventBoundFact, ...]
    stored_envelope_bounds_contract_fingerprint: Fingerprint
    fixed_sequence_wrapper_charge_bytes_per_event: PositiveInt
    fixed_schema_wrapper_charge_bytes_per_event: PositiveInt
    reservation_bookkeeping_charge_events: PositiveInt
    reservation_bookkeeping_charge_bytes: PositiveInt
    charge_applied_bookkeeping_charge_events: PositiveInt
    charge_applied_bookkeeping_base_charge_bytes: PositiveInt
    charge_applied_bookkeeping_per_business_event_charge_bytes: PositiveInt
    suspension_bookkeeping_charge_events: PositiveInt
    suspension_bookkeeping_charge_bytes: PositiveInt
    settlement_bookkeeping_charge_events: PositiveInt
    settlement_bookkeeping_charge_bytes: PositiveInt
    operational_observation_excluded_from_settlement: Literal[True]
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _canonical_bounds(self) -> "PhysicalChargeContractFact":
        keys = tuple(
            (item.event_type, item.event_schema_version)
            for item in self.bookkeeping_event_bounds
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("bookkeeping event bounds must be sorted and unique")
        if (
            self.stored_envelope_bounds_contract_fingerprint
            != self.stored_envelope_identity_bounds.bounds_contract_fingerprint
        ):
            raise ValueError("stored envelope bounds fingerprint join mismatch")
        return self


class TranscriptSemanticEventContractFact(FrozenFactBase):
    schema_version: Literal["transcript_semantic_event_contract.v1"]
    event_type: str = Field(min_length=1, max_length=128)
    event_schema_version: str = Field(min_length=1, max_length=128)
    event_schema_fingerprint: Fingerprint
    event_domain: Literal["transcript_semantic"]
    event_domain_contract_fingerprint: Fingerprint
    semantic_projection_contract_fingerprint: Fingerprint
    supported_event_fingerprint: Fingerprint


class TranscriptAccelerationEventContractFact(FrozenFactBase):
    schema_version: Literal["transcript_acceleration_event_contract.v1"]
    event_type: str = Field(min_length=1, max_length=128)
    event_schema_version: str = Field(min_length=1, max_length=128)
    event_schema_fingerprint: Fingerprint
    event_domain: Literal["transcript_acceleration"]
    event_domain_contract_fingerprint: Fingerprint
    deterministic_noop_contract_fingerprint: Fingerprint
    supported_event_fingerprint: Fingerprint


class NonTranscriptEventContractFact(FrozenFactBase):
    schema_version: Literal["non_transcript_event_contract.v1"]
    event_type: str = Field(min_length=1, max_length=128)
    event_schema_version: str = Field(min_length=1, max_length=128)
    event_schema_fingerprint: Fingerprint
    event_domain: Literal["non_transcript"]
    event_domain_contract_fingerprint: Fingerprint
    exclusion_contract_fingerprint: Fingerprint
    supported_event_fingerprint: Fingerprint


SupportedTranscriptEventContractFact: TypeAlias = Annotated[
    TranscriptSemanticEventContractFact
    | TranscriptAccelerationEventContractFact
    | NonTranscriptEventContractFact,
    Field(discriminator="event_domain"),
]


class TranscriptEventDomainRegistryContractFact(FrozenFactBase):
    schema_version: Literal["transcript_event_domain_registry.v1"]
    registry_id: str = Field(min_length=1, max_length=128)
    registry_version: str = Field(min_length=1, max_length=64)
    supported_events: tuple[SupportedTranscriptEventContractFact, ...]
    event_classification_contract_fingerprint: Fingerprint
    transcript_semantic_domain_contract_fingerprint: Fingerprint
    transcript_prefix_accumulator_contract_fingerprint: Fingerprint
    ledger_continuity_accumulator_contract_fingerprint: Fingerprint
    registry_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _canonical_entries(self) -> "TranscriptEventDomainRegistryContractFact":
        keys = tuple(
            (item.event_type, item.event_schema_version) for item in self.supported_events
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("event domain registry entries must be sorted and unique")
        return self


class TranscriptProjectionStableSemanticStateFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_stable_semantic_state.v1"]
    semantic_source_event_count: NonNegativeInt
    semantic_source_accumulator: Fingerprint
    normalized_transcript_fingerprint: Fingerprint
    state_semantic_fingerprint: Fingerprint


class TranscriptProjectionLiveAssemblyState(FrozenRuntimeStateBase):
    schema_version: Literal["transcript_projection_live_assembly.v1"]
    stable_semantic_state: TranscriptProjectionStableSemanticStateFact
    pending_model_projection_ids: tuple[str, ...]
    pending_model_disposition_call_ids: tuple[str, ...]
    pending_assistant_tool_call_ids: tuple[str, ...]
    pending_tool_result_projection_ids: tuple[str, ...]
    pending_tool_pair_ids: tuple[str, ...]
    suspended_tool_call_ids: tuple[str, ...]
    pending_external_requirement_ids: tuple[str, ...]
    ledger_through_sequence: NonNegativeInt
    ledger_continuity_accumulator: Fingerprint
    transcript_semantic_event_count: NonNegativeInt
    transcript_semantic_accumulator: Fingerprint
    checkpointable: bool
    assembly_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _pending_state(self) -> "TranscriptProjectionLiveAssemblyState":
        groups = (
            self.pending_model_projection_ids,
            self.pending_model_disposition_call_ids,
            self.pending_assistant_tool_call_ids,
            self.pending_tool_result_projection_ids,
            self.pending_tool_pair_ids,
            self.suspended_tool_call_ids,
            self.pending_external_requirement_ids,
        )
        for group in groups:
            _require_sorted_unique(group, context="live assembly identifiers")
        if self.checkpointable and any(groups):
            raise ValueError("checkpointable live assembly cannot contain pending state")
        return self


class TranscriptDomainPrefixFact(FrozenFactBase):
    schema_version: Literal["transcript_domain_prefix.v1"]
    runtime_session_id: str = Field(min_length=1, max_length=256)
    ledger_through_sequence: NonNegativeInt
    ledger_event_count: NonNegativeInt
    ledger_continuity_accumulator: Fingerprint
    transcript_semantic_event_count: NonNegativeInt
    transcript_semantic_accumulator: Fingerprint
    event_domain_registry_contract_fingerprint: Fingerprint
    prefix_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _append_only_prefix(self) -> "TranscriptDomainPrefixFact":
        if self.ledger_event_count != self.ledger_through_sequence:
            raise ValueError("ledger prefix count must equal contiguous high-water")
        if self.transcript_semantic_event_count > self.ledger_event_count:
            raise ValueError("transcript semantic count exceeds ledger prefix")
        return self


class TranscriptDomainSparseReadProofFact(FrozenFactBase):
    schema_version: Literal["transcript_domain_sparse_read_proof.v1"]
    range_kind: Literal["empty", "non_empty"]
    from_sequence: PositiveInt
    through_sequence: NonNegativeInt
    prefix_before: TranscriptDomainPrefixFact
    prefix_through: TranscriptDomainPrefixFact
    selected_transcript_semantic_event_count: NonNegativeInt
    selected_transcript_semantic_accumulator: Fingerprint
    selected_event_ids_fingerprint: Fingerprint
    completeness_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _complete_range(self) -> "TranscriptDomainSparseReadProofFact":
        if (
            self.prefix_before.runtime_session_id
            != self.prefix_through.runtime_session_id
            or self.prefix_before.event_domain_registry_contract_fingerprint
            != self.prefix_through.event_domain_registry_contract_fingerprint
        ):
            raise ValueError("transcript sparse proof prefix identity mismatch")
        if self.from_sequence != self.prefix_before.ledger_through_sequence + 1:
            raise ValueError("transcript sparse proof start does not follow prefix")
        if self.through_sequence != self.prefix_through.ledger_through_sequence:
            raise ValueError("transcript sparse proof end does not match prefix")
        selected_count = (
            self.prefix_through.transcript_semantic_event_count
            - self.prefix_before.transcript_semantic_event_count
        )
        if selected_count != self.selected_transcript_semantic_event_count:
            raise ValueError("transcript sparse proof selected count mismatch")
        if self.range_kind == "empty":
            if (
                self.from_sequence != self.through_sequence + 1
                or self.selected_transcript_semantic_event_count != 0
                or self.prefix_before != self.prefix_through
                or self.selected_transcript_semantic_accumulator
                != self.prefix_before.transcript_semantic_accumulator
            ):
                raise ValueError("empty transcript sparse proof is inconsistent")
        elif self.from_sequence > self.through_sequence:
            raise ValueError("non-empty transcript sparse proof range is reversed")
        if (
            self.selected_transcript_semantic_accumulator
            != self.prefix_through.transcript_semantic_accumulator
        ):
            raise ValueError("transcript sparse proof accumulator is not closed")
        return self


class AuthorityMaterializationLimits(FrozenFactBase):
    schema_version: Literal["authority_materialization_limits.v2"]
    max_unreclaimable_ledger_events: PositiveInt
    max_unreclaimable_charged_payload_bytes: PositiveInt
    max_active_materialization_consumers: PositiveInt
    max_active_physical_reservations: PositiveInt
    soft_reclaim_pressure_events: PositiveInt
    soft_reclaim_pressure_payload_bytes: PositiveInt
    maintenance_reserved_events: PositiveInt
    maintenance_reserved_payload_bytes: PositiveInt
    max_active_projection_entries: PositiveInt
    max_checkpoint_root_bytes: PositiveInt
    max_checkpoint_node_bytes: PositiveInt
    max_checkpoint_changed_leaves_per_operation: PositiveInt
    max_checkpoint_changed_nodes_per_operation: PositiveInt
    max_checkpoint_nodes_per_artifact_batch: PositiveInt
    max_normalized_message_content_artifact_bytes: PositiveInt
    max_changed_message_content_artifacts_per_operation: PositiveInt
    max_checkpoint_total_artifact_bytes_per_operation: PositiveInt
    max_checkpoint_artifact_batches_per_operation: PositiveInt
    checkpoint_operation_timeout_seconds: PositiveFloat
    max_named_authority_events: PositiveInt
    max_named_authority_payload_bytes: PositiveInt
    max_model_stream_source_items_per_call: PositiveInt
    max_model_stream_recovery_events: PositiveInt
    max_model_stream_recovery_payload_bytes: PositiveInt
    operation_timeout_seconds: PositiveFloat
    reservation_wait_timeout_seconds: PositiveFloat
    limits_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _watermarks(self) -> "AuthorityMaterializationLimits":
        if self.soft_reclaim_pressure_events >= self.max_unreclaimable_ledger_events:
            raise ValueError("event soft pressure must be below the hard limit")
        if (
            self.soft_reclaim_pressure_payload_bytes
            >= self.max_unreclaimable_charged_payload_bytes
        ):
            raise ValueError("byte soft pressure must be below the hard limit")
        if self.maintenance_reserved_events >= self.max_unreclaimable_ledger_events:
            raise ValueError("event maintenance reserve exhausts the hard limit")
        if (
            self.maintenance_reserved_payload_bytes
            >= self.max_unreclaimable_charged_payload_bytes
        ):
            raise ValueError("byte maintenance reserve exhausts the hard limit")
        if (
            self.max_checkpoint_changed_leaves_per_operation
            > self.max_checkpoint_changed_nodes_per_operation
        ):
            raise ValueError("changed leaf bound exceeds changed node bound")
        return self


class LedgerWriteAdmissionClass(StrEnum):
    PRODUCER = "producer"
    OPERATION_CONTINUATION = "operation_continuation"
    CHECKPOINT_BARRIER_CONTROL = "checkpoint_barrier_control"
    RECONCILIATION_CONTROL = "reconciliation_control"


class CheckpointDispatchBarrierFact(FrozenFactBase):
    schema_version: Literal["checkpoint_dispatch_barrier.v2"]
    barrier_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: str = Field(min_length=1, max_length=128)
    materialization_consumer_id: str = Field(min_length=1, max_length=256)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_candidate_fingerprint: Fingerprint
    checkpoint_intent_event_identity: StableEventIdentityFact
    source_ledger_materialization_generation: NonNegativeInt
    source_consumer_horizon_revision: NonNegativeInt
    frozen_ledger_through_sequence: NonNegativeInt
    frozen_ledger_continuity_accumulator: Fingerprint
    maintenance_reservation_id: str = Field(min_length=1, max_length=128)
    admitted_producer_generation: NonNegativeInt
    allowed_control_write_contract_fingerprint: Fingerprint
    barrier_fingerprint: Fingerprint


class LedgerMaterializationConsumerKind(StrEnum):
    TRANSCRIPT_WINDOW = "transcript_window"
    SUBAGENT_GRAPH = "subagent_graph"
    OTHER_REGISTERED_REDUCER = "other_registered_reducer"


class LedgerMaterializationConsumerHorizonFact(FrozenFactBase):
    schema_version: Literal["ledger_materialization_consumer_horizon.v1"]
    runtime_session_id: str = Field(min_length=1, max_length=128)
    consumer_kind: LedgerMaterializationConsumerKind
    consumer_id: str = Field(min_length=1, max_length=256)
    business_run_id: str | None = Field(default=None, max_length=128)
    business_window_id: str | None = Field(default=None, max_length=128)
    business_window_generation: int | None = Field(default=None, ge=0)
    through_sequence: NonNegativeInt
    ledger_event_count_through: NonNegativeInt
    ledger_charged_payload_bytes_through: NonNegativeInt
    ledger_continuity_accumulator: Fingerprint
    consumer_contract_fingerprint: Fingerprint
    horizon_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _attribution(self) -> "LedgerMaterializationConsumerHorizonFact":
        _validate_business_window(
            self.business_run_id,
            self.business_window_id,
            self.business_window_generation,
        )
        if self.consumer_kind == LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW:
            if self.business_run_id is None:
                raise ValueError("transcript consumer requires business window")
        elif self.consumer_kind == LedgerMaterializationConsumerKind.SUBAGENT_GRAPH:
            if self.business_run_id is not None:
                raise ValueError("subagent graph consumer cannot carry business window")
        return self


class LedgerMaterializationGenerationFact(FrozenFactBase):
    schema_version: Literal["ledger_materialization_generation.v1"]
    runtime_session_id: str = Field(min_length=1, max_length=128)
    ledger_materialization_generation: NonNegativeInt
    consumer_horizon_revision: NonNegativeInt
    consumer_horizons: tuple[LedgerMaterializationConsumerHorizonFact, ...]
    active_consumer_set_fingerprint: Fingerprint
    reclaimable_through_sequence: NonNegativeInt
    reclaimable_event_count_through: NonNegativeInt
    reclaimable_charged_payload_bytes_through: NonNegativeInt
    ledger_continuity_accumulator_through_reclaimable: Fingerprint
    physical_charge_contract_fingerprint: Fingerprint
    generation_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _minimum_horizon(self) -> "LedgerMaterializationGenerationFact":
        keys = tuple(
            (item.consumer_kind.value, item.consumer_id)
            for item in self.consumer_horizons
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("consumer horizons must be sorted and unique")
        if self.consumer_horizons:
            minimum = min(item.through_sequence for item in self.consumer_horizons)
            if self.reclaimable_through_sequence != minimum:
                raise ValueError("reclaimable sequence is not the minimum horizon")
        return self


class LedgerGenesisEventContractFact(FrozenFactBase):
    schema_version: Literal["ledger_genesis_event_contract.v1"]
    event_role: Literal["genesis", "consumer_registration", "initial_business_fact"]
    event_type: str = Field(min_length=1, max_length=128)
    event_schema_version: str = Field(min_length=1, max_length=128)
    event_schema_fingerprint: Fingerprint
    minimum_occurrences: NonNegativeInt
    maximum_occurrences: NonNegativeInt
    event_bound_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _occurrences(self) -> "LedgerGenesisEventContractFact":
        if self.minimum_occurrences > self.maximum_occurrences:
            raise ValueError("genesis minimum occurrences exceeds maximum")
        return self


class LedgerGenesisBatchContractFact(FrozenFactBase):
    schema_version: Literal["ledger_genesis_batch_contract.v1"]
    genesis_profile: Literal["host_first_run", "subagent_first_run"]
    event_contracts: tuple[LedgerGenesisEventContractFact, ...]
    required_consumer_kinds: tuple[LedgerMaterializationConsumerKind, ...]
    burst_contract_fingerprint: Fingerprint
    physical_charge_contract_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _canonical_matrix(self) -> "LedgerGenesisBatchContractFact":
        keys = tuple(
            (item.event_type, item.event_schema_version) for item in self.event_contracts
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("genesis event contracts must be sorted and unique")
        values = tuple(item.value for item in self.required_consumer_kinds)
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("genesis consumer kinds must be sorted and unique")
        return self


class LedgerMaterializationAccountGenesisFact(FrozenFactBase):
    schema_version: Literal["ledger_materialization_account_genesis.v1"]
    genesis_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: str = Field(min_length=1, max_length=128)
    empty_account: LedgerMaterializationGenerationFact
    genesis_burst_contract_fingerprint: Fingerprint
    genesis_batch_contract_fingerprint: Fingerprint
    physical_charge_contract_fingerprint: Fingerprint
    required_initial_consumer_kinds: tuple[LedgerMaterializationConsumerKind, ...]
    genesis_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _empty_generation(self) -> "LedgerMaterializationAccountGenesisFact":
        generation = self.empty_account
        if generation.runtime_session_id != self.runtime_session_id:
            raise ValueError("genesis runtime session mismatch")
        if any(
            (
                generation.ledger_materialization_generation,
                generation.consumer_horizon_revision,
                generation.reclaimable_through_sequence,
                generation.reclaimable_event_count_through,
                generation.reclaimable_charged_payload_bytes_through,
            )
        ) or generation.consumer_horizons:
            raise ValueError("genesis requires the canonical empty generation")
        values = tuple(item.value for item in self.required_initial_consumer_kinds)
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("genesis consumer kinds must be sorted and unique")
        return self


class ActivePhysicalReservationStateFact(FrozenFactBase):
    schema_version: Literal["active_physical_reservation_state.v1"]
    reservation_id: str = Field(min_length=1, max_length=128)
    owner_kind: ReservablePhysicalOperationKind
    owner_id: str = Field(min_length=1, max_length=128)
    lifecycle_status: Literal[
        "active", "suspended_tail", "reconciliation_required"
    ]
    reservation_fingerprint: Fingerprint
    suspension_fingerprint: Fingerprint | None
    reserved_events_total: PositiveInt
    reserved_payload_bytes_total: PositiveInt
    charged_candidate_events_lifetime: NonNegativeInt
    charged_candidate_payload_bytes_lifetime: NonNegativeInt
    charged_wrapper_bytes_lifetime: NonNegativeInt
    charged_bookkeeping_events_lifetime: NonNegativeInt
    charged_bookkeeping_bytes_lifetime: NonNegativeInt
    charged_events_lifetime: NonNegativeInt
    charged_payload_bytes_lifetime: NonNegativeInt
    remaining_events: NonNegativeInt
    remaining_payload_bytes: NonNegativeInt
    latest_reservation_event_id: str = Field(min_length=1, max_length=128)
    latest_lifecycle_event_id: str = Field(min_length=1, max_length=128)
    latest_charge_applied_event_id: str | None = Field(default=None, max_length=128)
    state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _suspension_identity(self) -> "ActivePhysicalReservationStateFact":
        if self.lifecycle_status == "suspended_tail":
            if self.suspension_fingerprint is None:
                raise ValueError("suspended reservation requires suspension fingerprint")
        elif self.suspension_fingerprint is not None:
            raise ValueError("non-suspended reservation cannot carry suspension fingerprint")
        if self.charged_events_lifetime != (
            self.charged_candidate_events_lifetime
            + self.charged_bookkeeping_events_lifetime
        ):
            raise ValueError("active reservation charged event split mismatch")
        if self.charged_payload_bytes_lifetime != (
            self.charged_candidate_payload_bytes_lifetime
            + self.charged_wrapper_bytes_lifetime
            + self.charged_bookkeeping_bytes_lifetime
        ):
            raise ValueError("active reservation charged byte split mismatch")
        if self.charged_events_lifetime + self.remaining_events > self.reserved_events_total:
            raise ValueError("active reservation event balance exceeds total")
        if (
            self.charged_payload_bytes_lifetime + self.remaining_payload_bytes
            > self.reserved_payload_bytes_total
        ):
            raise ValueError("active reservation byte balance exceeds total")
        return self


class LedgerMaterializationTransitionCauseIdentityFact(FrozenFactBase):
    schema_version: Literal["ledger_materialization_transition_cause_identity.v1"]
    cause_role: Literal[
        "run_start",
        "business_dispatch",
        "business_charge",
        "business_terminal",
        "checkpoint_intent",
        "checkpoint_committed",
        "checkpoint_terminal",
        "successor_terminal",
        "external_authority",
    ]
    event_identity: StableEventIdentityFact
    cause_fingerprint: Fingerprint


class LedgerMaterializationAccountTransitionFact(FrozenFactBase):
    schema_version: Literal["ledger_materialization_account_transition.v2"]
    runtime_session_id: str = Field(min_length=1, max_length=128)
    source_generation: NonNegativeInt
    source_consumer_horizon_revision: NonNegativeInt
    result_generation: NonNegativeInt
    result_consumer_horizon_revision: NonNegativeInt
    before_account_state_fingerprint: Fingerprint
    after_account_state_fingerprint: Fingerprint
    cause_event_identities: tuple[LedgerMaterializationTransitionCauseIdentityFact, ...]
    transition_contract_fingerprint: Fingerprint
    transition_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _cause_dag(self) -> "LedgerMaterializationAccountTransitionFact":
        if not self.cause_event_identities:
            raise ValueError("account transition requires a non-transition cause")
        keys = tuple(
            (
                item.event_identity.runtime_session_id,
                item.event_identity.event_id,
                item.cause_role,
            )
            for item in self.cause_event_identities
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("account transition causes must be sorted and unique")
        forbidden_prefixes = (
            "LEDGER_MATERIALIZATION_",
            "PHYSICAL_OPERATION_",
            "CHECKPOINT_DISPATCH_BARRIER_",
        )
        if any(
            item.event_identity.event_type.startswith(forbidden_prefixes)
            for item in self.cause_event_identities
        ):
            raise ValueError("account transition cause cannot be bookkeeping event")
        return self


class LedgerMaterializationAccountStateFact(FrozenFactBase):
    schema_version: Literal["ledger_materialization_account_state.v1"]
    runtime_session_id: str = Field(min_length=1, max_length=128)
    generation: LedgerMaterializationGenerationFact
    ledger_through_sequence: NonNegativeInt
    ledger_event_count_through: NonNegativeInt
    ledger_charged_payload_bytes_through: NonNegativeInt
    used_since_reclaimable_events: NonNegativeInt
    used_since_reclaimable_payload_bytes: NonNegativeInt
    active_reservations: Annotated[
        tuple[ActivePhysicalReservationStateFact, ...],
        Field(max_length=64),
    ]
    active_checkpoint_barrier: CheckpointDispatchBarrierFact | None
    latest_transition_event_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=128)], ...],
        Field(max_length=128),
    ]
    reconciliation_required: bool
    reconciliation_reason_code: str | None = Field(default=None, max_length=128)
    account_state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _canonical_account(self) -> "LedgerMaterializationAccountStateFact":
        reservation_ids = tuple(item.reservation_id for item in self.active_reservations)
        _require_sorted_unique(reservation_ids, context="active reservations")
        _require_sorted_unique(
            self.latest_transition_event_ids,
            context="latest transition event IDs",
        )
        if self.reconciliation_required != (
            self.reconciliation_reason_code is not None
        ):
            raise ValueError("reconciliation reason nullability mismatch")
        expected_events = (
            self.ledger_event_count_through
            - self.generation.reclaimable_event_count_through
        )
        expected_bytes = (
            self.ledger_charged_payload_bytes_through
            - self.generation.reclaimable_charged_payload_bytes_through
        )
        if self.used_since_reclaimable_events != expected_events:
            raise ValueError("used event count does not match reclaimable prefix")
        if self.used_since_reclaimable_payload_bytes != expected_bytes:
            raise ValueError("used byte count does not match reclaimable prefix")
        return self


class PhysicalOperationReservationFact(FrozenFactBase):
    schema_version: Literal["physical_operation_reservation.v2"]
    reservation_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: str = Field(min_length=1, max_length=128)
    business_run_id: str | None = Field(default=None, max_length=128)
    business_window_id: str | None = Field(default=None, max_length=128)
    business_window_generation: int | None = Field(default=None, ge=0)
    owner_kind: ReservablePhysicalOperationKind
    owner_id: str = Field(min_length=1, max_length=128)
    ledger_materialization_generation: NonNegativeInt
    consumer_horizon_revision: NonNegativeInt
    source_ledger_through_sequence: NonNegativeInt
    burst_contract_id: str = Field(min_length=1, max_length=128)
    burst_contract_version: str = Field(min_length=1, max_length=64)
    burst_contract_fingerprint: Fingerprint
    physical_charge_contract_fingerprint: Fingerprint
    reserved_events: PositiveInt
    reserved_payload_bytes: PositiveInt
    terminal_tail_reserved_events: NonNegativeInt
    terminal_tail_reserved_payload_bytes: NonNegativeInt
    reservation_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _reservation_bounds(self) -> "PhysicalOperationReservationFact":
        _validate_business_window(
            self.business_run_id,
            self.business_window_id,
            self.business_window_generation,
        )
        if self.terminal_tail_reserved_events > self.reserved_events:
            raise ValueError("terminal event tail exceeds reservation")
        if self.terminal_tail_reserved_payload_bytes > self.reserved_payload_bytes:
            raise ValueError("terminal byte tail exceeds reservation")
        return self


class PhysicalOperationSuspensionTailFact(FrozenFactBase):
    schema_version: Literal["physical_operation_suspension_tail.v2"]
    reservation_id: str = Field(min_length=1, max_length=128)
    suspension_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: str = Field(min_length=1, max_length=128)
    business_run_id: str | None = Field(default=None, max_length=128)
    business_window_id: str | None = Field(default=None, max_length=128)
    business_window_generation: int | None = Field(default=None, ge=0)
    ledger_materialization_generation: NonNegativeInt
    consumer_horizon_revision: NonNegativeInt
    owner_kind: ReservablePhysicalOperationKind
    owner_id: str = Field(min_length=1, max_length=128)
    reservation_fingerprint: Fingerprint
    burst_contract_fingerprint: Fingerprint
    physical_charge_contract_fingerprint: Fingerprint
    predecessor_lifecycle_event_id: str = Field(min_length=1, max_length=128)
    predecessor_reservation_state_fingerprint: Fingerprint
    binding_identity_fingerprint: Fingerprint
    remaining_before_suspension_events: NonNegativeInt
    remaining_before_suspension_payload_bytes: NonNegativeInt
    suspension_event_charge_events: NonNegativeInt
    suspension_event_charge_payload_bytes: NonNegativeInt
    released_on_suspension_events: NonNegativeInt
    released_on_suspension_payload_bytes: NonNegativeInt
    retained_tail_after_suspension_events: NonNegativeInt
    retained_tail_after_suspension_payload_bytes: NonNegativeInt
    resulting_reservation_state_fingerprint: Fingerprint
    suspension_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _balance(self) -> "PhysicalOperationSuspensionTailFact":
        _validate_business_window(
            self.business_run_id,
            self.business_window_id,
            self.business_window_generation,
        )
        if self.retained_tail_after_suspension_events != (
            self.remaining_before_suspension_events
            - self.suspension_event_charge_events
            - self.released_on_suspension_events
        ):
            raise ValueError("suspension event balance mismatch")
        if self.retained_tail_after_suspension_payload_bytes != (
            self.remaining_before_suspension_payload_bytes
            - self.suspension_event_charge_payload_bytes
            - self.released_on_suspension_payload_bytes
        ):
            raise ValueError("suspension byte balance mismatch")
        return self


class PhysicalOperationSettlementFact(FrozenFactBase):
    schema_version: Literal["physical_operation_settlement.v2"]
    reservation_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: str = Field(min_length=1, max_length=128)
    business_run_id: str | None = Field(default=None, max_length=128)
    business_window_id: str | None = Field(default=None, max_length=128)
    business_window_generation: int | None = Field(default=None, ge=0)
    ledger_materialization_generation: NonNegativeInt
    consumer_horizon_revision: NonNegativeInt
    owner_kind: ReservablePhysicalOperationKind
    owner_id: str = Field(min_length=1, max_length=128)
    reservation_fingerprint: Fingerprint
    predecessor_status: Literal["active", "suspended_tail"]
    predecessor_lifecycle_event_id: str = Field(min_length=1, max_length=128)
    predecessor_reservation_state_fingerprint: Fingerprint
    burst_contract_fingerprint: Fingerprint
    physical_charge_contract_fingerprint: Fingerprint
    predecessor_remaining_events: NonNegativeInt
    predecessor_remaining_payload_bytes: NonNegativeInt
    terminal_batch_charge_before_settlement_events: NonNegativeInt
    terminal_batch_charge_before_settlement_payload_bytes: NonNegativeInt
    settlement_event_charge_events: NonNegativeInt
    settlement_event_charge_payload_bytes: NonNegativeInt
    charged_candidate_events: NonNegativeInt
    charged_candidate_payload_bytes: NonNegativeInt
    charged_wrapper_bytes: NonNegativeInt
    charged_bookkeeping_events: NonNegativeInt
    charged_bookkeeping_bytes: NonNegativeInt
    total_charged_events: NonNegativeInt
    total_charged_payload_bytes: NonNegativeInt
    terminal_outcome: Literal[
        "completed",
        "denied",
        "cancelled",
        "provider_error",
        "runtime_error",
        "host_teardown",
        "recovered_interrupted",
    ]
    model_stream_measurement_fingerprint: Fingerprint | None = None
    released_on_suspension_events_lifetime: NonNegativeInt
    released_on_suspension_payload_bytes_lifetime: NonNegativeInt
    released_on_settlement_events: NonNegativeInt
    released_on_settlement_payload_bytes: NonNegativeInt
    resulting_reservation_state_fingerprint: Fingerprint
    settlement_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _balance(self) -> "PhysicalOperationSettlementFact":
        _validate_business_window(
            self.business_run_id,
            self.business_window_id,
            self.business_window_generation,
        )
        if self.released_on_settlement_events != (
            self.predecessor_remaining_events
            - self.terminal_batch_charge_before_settlement_events
            - self.settlement_event_charge_events
        ):
            raise ValueError("settlement event balance mismatch")
        if self.released_on_settlement_payload_bytes != (
            self.predecessor_remaining_payload_bytes
            - self.terminal_batch_charge_before_settlement_payload_bytes
            - self.settlement_event_charge_payload_bytes
        ):
            raise ValueError("settlement byte balance mismatch")
        if self.total_charged_events != (
            self.charged_candidate_events + self.charged_bookkeeping_events
        ):
            raise ValueError("settlement total charged events mismatch")
        if self.total_charged_payload_bytes != (
            self.charged_candidate_payload_bytes
            + self.charged_wrapper_bytes
            + self.charged_bookkeeping_bytes
        ):
            raise ValueError("settlement total charged bytes mismatch")
        if self.predecessor_status == "active" and any(
            (
                self.released_on_suspension_events_lifetime,
                self.released_on_suspension_payload_bytes_lifetime,
            )
        ):
            raise ValueError("active settlement cannot report suspension release")
        if (self.owner_kind == PhysicalOperationKind.MODEL_CALL) != (
            self.model_stream_measurement_fingerprint is not None
        ):
            raise ValueError(
                "only model-call settlement requires model stream measurement"
            )
        return self


class PhysicalOperationChargeAppliedFact(FrozenFactBase):
    schema_version: Literal["physical_operation_charge_applied.v1"]
    reservation_id: str = Field(min_length=1, max_length=128)
    reservation_fingerprint: Fingerprint
    runtime_session_id: str = Field(min_length=1, max_length=128)
    owner_kind: ReservablePhysicalOperationKind
    owner_id: str = Field(min_length=1, max_length=128)
    ledger_materialization_generation: NonNegativeInt
    consumer_horizon_revision: NonNegativeInt
    predecessor_reservation_state_fingerprint: Fingerprint
    charged_business_event_identities: tuple[StableEventIdentityFact, ...]
    business_candidate_charge_events: PositiveInt
    business_candidate_charge_payload_bytes: NonNegativeInt
    business_wrapper_charge_payload_bytes: NonNegativeInt
    charge_applied_event_charge_events: PositiveInt
    charge_applied_event_charge_payload_bytes: PositiveInt
    remaining_before_events: NonNegativeInt
    remaining_before_payload_bytes: NonNegativeInt
    remaining_after_events: NonNegativeInt
    remaining_after_payload_bytes: NonNegativeInt
    resulting_reservation_state_fingerprint: Fingerprint
    charge_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _balance(self) -> "PhysicalOperationChargeAppliedFact":
        if not self.charged_business_event_identities:
            raise ValueError("charge transition requires business events")
        keys = tuple(
            (item.runtime_session_id, item.event_id)
            for item in self.charged_business_event_identities
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("charged event identities must be sorted and unique")
        if self.remaining_after_events != (
            self.remaining_before_events
            - self.business_candidate_charge_events
            - self.charge_applied_event_charge_events
        ):
            raise ValueError("charge-applied event balance mismatch")
        if self.remaining_after_payload_bytes != (
            self.remaining_before_payload_bytes
            - self.business_candidate_charge_payload_bytes
            - self.business_wrapper_charge_payload_bytes
            - self.charge_applied_event_charge_payload_bytes
        ):
            raise ValueError("charge-applied byte balance mismatch")
        return self


class RunSeedConsumerCauseFact(FrozenFactBase):
    schema_version: Literal["run_seed_consumer_cause.v1"]
    cause_kind: Literal["run_seed"]
    run_start_event_identity: StableEventIdentityFact
    seed_semantic_fingerprint: Fingerprint
    seed_reference_fingerprint: Fingerprint
    cause_fingerprint: Fingerprint


class LedgerGenesisConsumerCauseFact(FrozenFactBase):
    schema_version: Literal["ledger_genesis_consumer_cause.v1"]
    cause_kind: Literal["ledger_genesis"]
    genesis_event_identity: StableEventIdentityFact
    genesis_contract_fingerprint: Fingerprint
    cause_fingerprint: Fingerprint


class ReducerRegistrationCauseFact(FrozenFactBase):
    schema_version: Literal["reducer_registration_cause.v1"]
    cause_kind: Literal["reducer_registration"]
    reducer_id: str = Field(min_length=1, max_length=128)
    reducer_version: str = Field(min_length=1, max_length=64)
    reducer_contract_fingerprint: Fingerprint
    registration_authority_event_identity: StableEventIdentityFact
    cause_fingerprint: Fingerprint


class CheckpointConsumerCauseFact(FrozenFactBase):
    schema_version: Literal["checkpoint_consumer_cause.v1"]
    cause_kind: Literal["checkpoint"]
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_committed_event_identity: StableEventIdentityFact
    checkpoint_candidate_fingerprint: Fingerprint
    cause_fingerprint: Fingerprint


class ConsumerRetirementCauseFact(FrozenFactBase):
    schema_version: Literal["consumer_retirement_cause.v1"]
    cause_kind: Literal["retirement"]
    successor_consumer_id: str | None = Field(default=None, max_length=256)
    terminal_or_successor_event_identity: StableEventIdentityFact
    cause_fingerprint: Fingerprint


LedgerMaterializationConsumerRegistrationCauseFact: TypeAlias = Annotated[
    LedgerGenesisConsumerCauseFact
    | RunSeedConsumerCauseFact
    | ReducerRegistrationCauseFact,
    Field(discriminator="cause_kind"),
]


class PhysicalStoredEnvelopeObservation(FrozenRuntimeStateBase):
    schema_version: Literal["physical_stored_envelope_observation.v1"]
    reservation_id: str
    committed_event_ids: tuple[str, ...]
    deterministic_charged_payload_bytes: NonNegativeInt
    observed_stored_envelope_bytes: NonNegativeInt
    storage_serialization_contract_fingerprint: Fingerprint
    observed_at_utc: str
    operational_observation_excluded_from_settlement: Literal[True] = True


_OWN_FINGERPRINTS: tuple[tuple[str, str, str], ...] = (
    ("fixed_batch_event_contract.v1", "event_contract_fingerprint", "fixed-batch-event-contract:v1"),
    ("transport_segmented_burst_contract.v1", "contract_fingerprint", "transport-segmented-burst-contract:v1"),
    ("tool_delta_burst_contract.v1", "contract_fingerprint", "tool-delta-burst-contract:v1"),
    ("fixed_batch_burst_contract.v1", "contract_fingerprint", "fixed-batch-burst-contract:v1"),
    ("stored_envelope_identity_bounds.v1", "bounds_contract_fingerprint", "stored-envelope-identity-bounds:v1"),
    ("physical_bookkeeping_event_bound.v1", "bound_fingerprint", "physical-bookkeeping-event-bound:v1"),
    ("physical_charge_contract.v1", "contract_fingerprint", "physical-charge-contract:v1"),
    ("transcript_semantic_event_contract.v1", "supported_event_fingerprint", "transcript-semantic-event-contract:v1"),
    ("transcript_acceleration_event_contract.v1", "supported_event_fingerprint", "transcript-acceleration-event-contract:v1"),
    ("non_transcript_event_contract.v1", "supported_event_fingerprint", "non-transcript-event-contract:v1"),
    ("transcript_event_domain_registry.v1", "registry_contract_fingerprint", "transcript-event-domain-registry:v1"),
    ("transcript_projection_stable_semantic_state.v1", "state_semantic_fingerprint", "transcript-projection-stable-state:v1"),
    ("transcript_domain_prefix.v1", "prefix_fingerprint", "transcript-domain-prefix:v1"),
    ("transcript_domain_sparse_read_proof.v1", "completeness_fingerprint", "transcript-domain-sparse-read-proof:v1"),
    ("authority_materialization_limits.v2", "limits_contract_fingerprint", "authority-materialization-limits:v2"),
    ("checkpoint_dispatch_barrier.v2", "barrier_fingerprint", "checkpoint-dispatch-barrier:v2"),
    ("ledger_materialization_consumer_horizon.v1", "horizon_fingerprint", "ledger-materialization-consumer-horizon:v1"),
    ("ledger_materialization_generation.v1", "generation_fingerprint", "ledger-materialization-generation:v1"),
    ("ledger_genesis_event_contract.v1", "event_bound_fingerprint", "ledger-genesis-event-contract:v1"),
    ("ledger_genesis_batch_contract.v1", "contract_fingerprint", "ledger-genesis-batch-contract:v1"),
    ("ledger_materialization_account_genesis.v1", "genesis_fingerprint", "ledger-materialization-account-genesis:v1"),
    ("active_physical_reservation_state.v1", "state_fingerprint", "active-physical-reservation-state:v1"),
    ("ledger_materialization_transition_cause_identity.v1", "cause_fingerprint", "ledger-materialization-transition-cause-identity:v1"),
    ("ledger_materialization_account_transition.v2", "transition_fingerprint", "ledger-materialization-account-transition:v2"),
    ("ledger_materialization_account_state.v1", "account_state_fingerprint", "ledger-materialization-account-state:v1"),
    ("physical_operation_reservation.v2", "reservation_fingerprint", "physical-operation-reservation:v2"),
    ("physical_operation_suspension_tail.v2", "suspension_fingerprint", "physical-operation-suspension-tail:v2"),
    ("physical_operation_settlement.v2", "settlement_fingerprint", "physical-operation-settlement:v2"),
    ("physical_operation_charge_applied.v1", "charge_fingerprint", "physical-operation-charge-applied:v1"),
    ("run_seed_consumer_cause.v1", "cause_fingerprint", "run-seed-consumer-cause:v1"),
    ("ledger_genesis_consumer_cause.v1", "cause_fingerprint", "ledger-genesis-consumer-cause:v1"),
    ("reducer_registration_cause.v1", "cause_fingerprint", "reducer-registration-cause:v1"),
    ("checkpoint_consumer_cause.v1", "cause_fingerprint", "checkpoint-consumer-cause:v1"),
    ("consumer_retirement_cause.v1", "cause_fingerprint", "consumer-retirement-cause:v1"),
)

for _schema_version, _fingerprint_field, _domain in _OWN_FINGERPRINTS:
    register_durable_fact(
        schema_version=_schema_version,
        own_fingerprint_field=_fingerprint_field,
        domain_separator=_domain,
    )


__all__ = [
    "ActivePhysicalReservationStateFact",
    "AuthorityMaterializationLimits",
    "CheckpointDispatchBarrierFact",
    "FixedBatchBurstContractFact",
    "FixedBatchEventContractFact",
    "LedgerMaterializationAccountStateFact",
    "LedgerMaterializationAccountGenesisFact",
    "LedgerMaterializationAccountTransitionFact",
    "LedgerMaterializationConsumerHorizonFact",
    "LedgerMaterializationConsumerKind",
    "LedgerMaterializationGenerationFact",
    "LedgerGenesisBatchContractFact",
    "LedgerGenesisEventContractFact",
    "LedgerWriteAdmissionClass",
    "MAX_MODEL_STREAM_STRUCTURAL_TAIL_EVENTS",
    "MAX_MODEL_STREAM_STRUCTURAL_TAIL_PAYLOAD_BYTES",
    "MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL",
    "MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL",
    "NonTranscriptEventContractFact",
    "PhysicalBookkeepingEventBoundFact",
    "PhysicalBurstContractFact",
    "PhysicalChargeContractFact",
    "PhysicalOperationKind",
    "PhysicalOperationReservationFact",
    "PhysicalOperationChargeAppliedFact",
    "PhysicalOperationSettlementFact",
    "PhysicalOperationSuspensionTailFact",
    "PhysicalStoredEnvelopeObservation",
    "ReservablePhysicalOperationKind",
    "StoredEnvelopeIdentityBoundsFact",
    "CheckpointConsumerCauseFact",
    "ConsumerRetirementCauseFact",
    "LedgerGenesisConsumerCauseFact",
    "LedgerMaterializationConsumerRegistrationCauseFact",
    "ReducerRegistrationCauseFact",
    "RunSeedConsumerCauseFact",
    "SupportedTranscriptEventContractFact",
    "ToolDeltaBurstContractFact",
    "TranscriptAccelerationEventContractFact",
    "TranscriptEventDomainRegistryContractFact",
    "TranscriptProjectionLiveAssemblyState",
    "TranscriptProjectionStableSemanticStateFact",
    "TranscriptDomainPrefixFact",
    "TranscriptDomainSparseReadProofFact",
    "TranscriptSemanticEventContractFact",
    "TransportSegmentedBurstContractFact",
]
