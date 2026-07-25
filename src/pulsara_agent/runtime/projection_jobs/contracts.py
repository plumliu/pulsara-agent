"""Immutable contracts for durable projection jobs and mutation delivery."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Literal, Protocol, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    thaw_json,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    FrozenRuntimeStateBase,
    build_frozen_fact,
    register_durable_fact,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    BoundedRuntimeFailureDiagnosticFact,
)
from pulsara_agent.primitives.terminal_projection import (
    ToolResultTerminalProjectionEndReferenceFact,
)
from pulsara_agent.message.blocks import ToolResultState


class DurableProjectionKind(StrEnum):
    RUN_TIMELINE = "run_timeline.v1"
    TOOL_RESULT_EXECUTION_EVIDENCE = "tool_result_execution_evidence.v1"


class DurableProjectionTargetUpdatePolicy(StrEnum):
    FULL_REPLACEMENT = "full_replacement"
    SINGLE_ASSIGNMENT = "single_assignment"


class DurableProjectionJobStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    SUPERSEDED = "superseded"
    DEAD_LETTER = "dead_letter"


class DurableProjectionCommitConfirmation(StrEnum):
    FULL = "full"
    NONE = "none"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"


class DurableProjectionFailureKind(StrEnum):
    TRANSIENT_STORAGE_UNAVAILABLE = "transient_storage_unavailable"
    TRANSIENT_EXTERNAL_SURFACE_UNAVAILABLE = (
        "transient_external_surface_unavailable"
    )
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SOURCE_NOT_READY = "source_not_ready"
    SOURCE_AUTHORITY_CONFLICT = "source_authority_conflict"
    TARGET_AUTHORITY_CONFLICT = "target_authority_conflict"
    REPOSITORY_AUTHORITY_CONFLICT = "repository_authority_conflict"
    HISTORICAL_DECODER_UNAVAILABLE = "historical_decoder_unavailable"
    HANDLER_CONTRACT_MISMATCH = "handler_contract_mismatch"
    PROJECTION_INPUT_OVERSIZE = "projection_input_oversize"
    PROJECTION_OUTPUT_OVERSIZE = "projection_output_oversize"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    RESULT_IDENTITY_CONFLICT = "result_identity_conflict"
    EXTERNAL_SURFACE_CONTRACT_MISMATCH = (
        "external_surface_contract_mismatch"
    )


class RuntimeWriteAdmissionMode(StrEnum):
    NORMAL = "normal"
    MAINTENANCE = "maintenance"


class CanonicalMutationKind(StrEnum):
    GOVERNED_MEMORY = "governed_memory.v2"
    RUNTIME_SEMANTIC = "runtime_semantic.v2"
    GRAPH_RESET = "graph_reset.v2"
    GRAPH_DELETE = "graph_delete.v2"


class CanonicalMutationSurface(StrEnum):
    SEARCH_INDEX = "search_index.v1"
    VECTOR_INDEX = "vector_index.v1"
    OXIGRAPH = "oxigraph.v1"


class PostgresMigrationPreparationKind(StrEnum):
    LEGACY_SURFACE_BINDING_PLAN = "legacy_surface_binding_plan.v1"
    RUN_TIMELINE_PRE_ACTIVATION_COVERAGE = (
        "run_timeline_pre_activation_coverage.v1"
    )
    TOOL_RESULT_EVIDENCE_PRE_ACTIVATION_COVERAGE = (
        "tool_result_evidence_pre_activation_coverage.v1"
    )


class DurableProjectionRepairReason(StrEnum):
    TRANSIENT_DEPENDENCY_RESTORED = "transient_dependency_restored"
    SOURCE_AUTHORITY_REPAIRED = "source_authority_repaired"
    TARGET_AUTHORITY_REPAIRED = "target_authority_repaired"
    SURFACE_PROVIDER_REBOUND = "surface_provider_rebound"
    OPERATOR_SUPERSEDED = "operator_superseded"


class DurableProjectionSourceEventReferenceFact(FrozenFactBase):
    schema_version: Literal["durable_projection_source_event_reference.v1"] = (
        "durable_projection_source_event_reference.v1"
    )
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    event_id: str
    sequence: int
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    payload_fingerprint: str
    stored_envelope_fingerprint: str
    reference_fingerprint: str

    @model_validator(mode="after")
    def _validate_reference(self) -> "DurableProjectionSourceEventReferenceFact":
        if self.sequence < 1:
            raise ValueError("projection source sequence must be positive")
        if not all(
            (
                self.runtime_session_id,
                self.run_id,
                self.turn_id,
                self.reply_id,
                self.event_id,
                self.event_type,
            )
        ):
            raise ValueError("projection source identity is incomplete")
        return self


class DurableProjectionLedgerHorizonFact(FrozenFactBase):
    schema_version: Literal["durable_projection_ledger_horizon.v1"] = (
        "durable_projection_ledger_horizon.v1"
    )
    runtime_session_id: str
    through_sequence: int
    ledger_continuity_accumulator: str
    ledger_payload_prefix_bytes: int
    transcript_semantic_prefix_count: int
    transcript_semantic_prefix_accumulator: str
    horizon_fingerprint: str

    @model_validator(mode="after")
    def _validate_horizon(self) -> "DurableProjectionLedgerHorizonFact":
        if (
            self.through_sequence < 0
            or self.ledger_payload_prefix_bytes < 0
            or self.transcript_semantic_prefix_count < 0
        ):
            raise ValueError("projection horizon counters must be non-negative")
        return self


class DurableProjectionHandlerContractFact(FrozenFactBase):
    schema_version: Literal["durable_projection_handler_contract.v1"] = (
        "durable_projection_handler_contract.v1"
    )
    projection_kind: DurableProjectionKind
    handler_id: str
    handler_version: str
    accepted_source_event_types: tuple[str, ...]
    accepted_source_schema_bindings_fingerprint: str
    target_update_policy: DurableProjectionTargetUpdatePolicy
    result_schema_fingerprint: str
    idempotency_contract_fingerprint: str
    contract_fingerprint: str

    @model_validator(mode="after")
    def _validate_contract(self) -> "DurableProjectionHandlerContractFact":
        if (
            not self.accepted_source_event_types
            or len(self.accepted_source_event_types)
            != len(set(self.accepted_source_event_types))
        ):
            raise ValueError("handler source event types must be non-empty and unique")
        expected_policy = {
            DurableProjectionKind.RUN_TIMELINE: (
                DurableProjectionTargetUpdatePolicy.FULL_REPLACEMENT
            ),
            DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE: (
                DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT
            ),
        }[self.projection_kind]
        if self.target_update_policy is not expected_policy:
            raise ValueError("projection kind/target update policy mismatch")
        return self


class DurableProjectionPhysicalPolicyFact(FrozenFactBase):
    schema_version: Literal["durable_projection_physical_policy.v1"] = (
        "durable_projection_physical_policy.v1"
    )
    database_operation_timeout_seconds: int
    source_hydration_timeout_seconds: int
    handler_compute_timeout_seconds: int
    result_commit_timeout_seconds: int
    external_surface_attempt_timeout_seconds: int
    maximum_physical_attempt_seconds: int
    policy_fingerprint: str


class DurableProjectionRetryPolicyFact(FrozenFactBase):
    schema_version: Literal["durable_projection_retry_policy.v1"] = (
        "durable_projection_retry_policy.v1"
    )
    maximum_attempts: int
    base_delay_milliseconds: int
    maximum_delay_milliseconds: int
    lease_duration_seconds: int
    claim_batch_size: int
    policy_fingerprint: str

    @model_validator(mode="after")
    def _validate_retry(self) -> "DurableProjectionRetryPolicyFact":
        if min(
            self.maximum_attempts,
            self.base_delay_milliseconds,
            self.maximum_delay_milliseconds,
            self.lease_duration_seconds,
            self.claim_batch_size,
        ) < 1:
            raise ValueError("projection retry policy values must be positive")
        if self.base_delay_milliseconds > self.maximum_delay_milliseconds:
            raise ValueError("projection retry delay bounds are inverted")
        return self


class DurableProjectionDeliveryPolicyFact(FrozenFactBase):
    schema_version: Literal["durable_projection_delivery_policy.v1"] = (
        "durable_projection_delivery_policy.v1"
    )
    retry_policy: DurableProjectionRetryPolicyFact
    physical_policy: DurableProjectionPhysicalPolicyFact
    delivery_policy_fingerprint: str


class CanonicalMutationSurfaceHandlerContractFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_handler_contract.v1"
    ] = "canonical_mutation_surface_handler_contract.v1"
    surface: CanonicalMutationSurface
    handler_id: str
    handler_version: str
    accepted_mutation_kinds: tuple[CanonicalMutationKind, ...]
    payload_codec_fingerprint: str
    target_compatibility_fingerprint: str
    idempotency_contract_fingerprint: str
    contract_fingerprint: str


class CanonicalMutationPlannedSurfaceFact(FrozenFactBase):
    schema_version: Literal["canonical_mutation_planned_surface.v1"] = (
        "canonical_mutation_planned_surface.v1"
    )
    handler_contract: CanonicalMutationSurfaceHandlerContractFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    planned_surface_fingerprint: str

    @property
    def surface(self) -> CanonicalMutationSurface:
        return self.handler_contract.surface


class CanonicalMutationSurfacePlanFact(FrozenFactBase):
    schema_version: Literal["canonical_mutation_surface_plan.v1"] = (
        "canonical_mutation_surface_plan.v1"
    )
    ordered_surfaces: tuple[CanonicalMutationPlannedSurfaceFact, ...]
    composition_fingerprint: str
    plan_fingerprint: str

    @property
    def surface_plan_fingerprint(self) -> str:
        return self.plan_fingerprint

    @model_validator(mode="after")
    def _validate_plan(self) -> "CanonicalMutationSurfacePlanFact":
        names = tuple(item.surface for item in self.ordered_surfaces)
        if len(names) > 3 or len(names) != len(set(names)):
            raise ValueError("mutation surface plan must be bounded and unique")
        return self


class DurableProjectionJobSemanticFact(FrozenFactBase):
    schema_version: Literal["durable_projection_job_semantic.v1"] = (
        "durable_projection_job_semantic.v1"
    )
    job_id: str
    projection_kind: DurableProjectionKind
    target_key: str
    source_event_reference: DurableProjectionSourceEventReferenceFact
    trigger_horizon: DurableProjectionLedgerHorizonFact
    handler_contract: DurableProjectionHandlerContractFact
    job_semantic_fingerprint: str

    @model_validator(mode="after")
    def _validate_job(self) -> "DurableProjectionJobSemanticFact":
        if self.projection_kind is not self.handler_contract.projection_kind:
            raise ValueError("job/handler projection kind mismatch")
        if (
            self.source_event_reference.runtime_session_id
            != self.trigger_horizon.runtime_session_id
            or self.source_event_reference.sequence
            != self.trigger_horizon.through_sequence
        ):
            raise ValueError("job trigger horizon must be exact at its source event")
        return self


class DurableProjectionJobCandidateFact(FrozenFactBase):
    schema_version: Literal["durable_projection_job_candidate.v1"] = (
        "durable_projection_job_candidate.v1"
    )
    job_semantic: DurableProjectionJobSemanticFact
    activation_fingerprint: str
    seed_contract_fingerprint: str
    delivery_policy: DurableProjectionDeliveryPolicyFact
    canonical_mutation_surface_plan: CanonicalMutationSurfacePlanFact
    candidate_fingerprint: str


class DurableContentAddressedArtifactReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_content_addressed_artifact_reference.v1"
    ] = "durable_content_addressed_artifact_reference.v1"
    artifact_semantic_id: str
    content_sha256: str
    content_utf8_bytes: int
    artifact_store_contract_fingerprint: str
    artifact_semantic_fingerprint: str
    reference_fingerprint: str


class DurableProjectionArtifactResultDocumentReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_artifact_result_document_reference.v1"
    ] = "durable_projection_artifact_result_document_reference.v1"
    document_kind: Literal["artifact"]
    semantic_document_id: str
    document_semantic_fingerprint: str
    media_type: str
    content_codec_contract_fingerprint: str
    metadata_contract_fingerprint: str
    artifact_reference: DurableContentAddressedArtifactReferenceFact
    reference_fingerprint: str


class DurableProjectionGraphResultDocumentReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_graph_result_document_reference.v1"
    ] = "durable_projection_graph_result_document_reference.v1"
    document_kind: Literal["graph_document"]
    graph_id: str
    semantic_document_id: str
    graph_document_type: str
    document_semantic_fingerprint: str
    canonical_json_sha256: str
    canonical_json_utf8_bytes: int
    jsonld_codec_contract_fingerprint: str
    reference_fingerprint: str


class DurableProjectionGraphRelationReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_graph_relation_reference.v1"
    ] = "durable_projection_graph_relation_reference.v1"
    document_kind: Literal["graph_relation"]
    relation_id: str
    graph_id: str
    source_document_id: str
    predicate_iri: str
    target_document_id: str
    relation_semantic_fingerprint: str
    lowering_contract_fingerprint: str
    reference_fingerprint: str


DurableProjectionResultDocumentReferenceFact: TypeAlias = Annotated[
    DurableProjectionArtifactResultDocumentReferenceFact
    | DurableProjectionGraphResultDocumentReferenceFact
    | DurableProjectionGraphRelationReferenceFact,
    Field(discriminator="document_kind"),
]


class DurableProjectionCanonicalMutationReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_canonical_mutation_reference.v1"
    ] = "durable_projection_canonical_mutation_reference.v1"
    mutation_id: str
    mutation_semantic_fingerprint: str
    ordered_surface_delivery_identity_fingerprints: tuple[str, ...]
    reference_fingerprint: str


class DurableProjectionResultSemanticFact(FrozenFactBase):
    schema_version: Literal["durable_projection_result_semantic.v1"] = (
        "durable_projection_result_semantic.v1"
    )
    projection_kind: DurableProjectionKind
    source_projection_fingerprint: str
    ordered_document_semantic_fingerprints: tuple[str, ...]
    ordered_canonical_mutation_semantic_fingerprints: tuple[str, ...]
    result_semantic_fingerprint: str


class ProjectionJobResultOwnerFact(FrozenFactBase):
    schema_version: Literal["projection_job_result_owner.v1"] = (
        "projection_job_result_owner.v1"
    )
    owner_kind: Literal["durable_projection_job"]
    job_id: str
    job_semantic_fingerprint: str
    job_candidate_fingerprint: str
    source_event_reference_fingerprint: str
    owner_fingerprint: str


class PreActivationHookResultOwnerFact(FrozenFactBase):
    schema_version: Literal["pre_activation_hook_result_owner.v1"] = (
        "pre_activation_hook_result_owner.v1"
    )
    owner_kind: Literal["pre_activation_hook"]
    projection_kind: DurableProjectionKind
    source_event_reference: DurableProjectionSourceEventReferenceFact
    hook_contract_fingerprint: str
    owner_fingerprint: str


DurableProjectionResultOwner: TypeAlias = Annotated[
    ProjectionJobResultOwnerFact | PreActivationHookResultOwnerFact,
    Field(discriminator="owner_kind"),
]


class DurableProjectionResultReceiptReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_result_receipt_reference.v1"
    ] = "durable_projection_result_receipt_reference.v1"
    receipt_id: str
    receipt_fingerprint: str
    reference_fingerprint: str


class DurableProjectionAppliedResultReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_applied_result_receipt.v1"
    ] = "durable_projection_applied_result_receipt.v1"
    receipt_kind: Literal["applied"]
    receipt_id: str
    result_owner: DurableProjectionResultOwner
    result_semantic: DurableProjectionResultSemanticFact
    target_key: str
    source_event_reference_fingerprint: str
    source_sequence: int
    target_head_revision: int
    result_document_references: tuple[
        DurableProjectionResultDocumentReferenceFact, ...
    ]
    canonical_mutation_references: tuple[
        DurableProjectionCanonicalMutationReferenceFact, ...
    ]
    receipt_fingerprint: str


class DurableProjectionSupersededResultReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_superseded_result_receipt.v1"
    ] = "durable_projection_superseded_result_receipt.v1"
    receipt_kind: Literal["superseded"]
    receipt_id: str
    candidate_result_owner: DurableProjectionResultOwner
    projection_kind: DurableProjectionKind
    target_key: str
    candidate_source_event_reference_fingerprint: str
    candidate_source_sequence: int
    effective_applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact
    )
    target_head_revision: int
    receipt_fingerprint: str


DurableProjectionResultReceiptFact: TypeAlias = Annotated[
    DurableProjectionAppliedResultReceiptFact
    | DurableProjectionSupersededResultReceiptFact,
    Field(discriminator="receipt_kind"),
]


class DurableProjectionJobOperationalStateFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_job_operational_state.v1"
    ] = "durable_projection_job_operational_state.v1"
    status: DurableProjectionJobStatus
    state_revision: int
    repair_generation: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    last_failure: BoundedRuntimeFailureDiagnosticFact | None
    result_receipt_reference: DurableProjectionResultReceiptReferenceFact | None
    state_fingerprint: str

    @model_validator(mode="after")
    def _validate_state(self) -> "DurableProjectionJobOperationalStateFact":
        if min(
            self.state_revision,
            self.repair_generation,
            self.attempt_count,
            self.lease_generation,
        ) < 0:
            raise ValueError("projection state counters must be non-negative")
        leased = self.status is DurableProjectionJobStatus.LEASED
        if leased != bool(self.lease_owner_id and self.lease_expires_at):
            raise ValueError("projection lease state matrix mismatch")
        if self.status is DurableProjectionJobStatus.RETRY_WAIT and (
            self.next_attempt_at is None or self.last_failure is None
        ):
            raise ValueError("retry_wait requires next attempt and failure")
        if self.status in {
            DurableProjectionJobStatus.SUCCEEDED,
            DurableProjectionJobStatus.SUPERSEDED,
        } and self.result_receipt_reference is None:
            raise ValueError("successful projection state requires result receipt")
        if (
            self.status is DurableProjectionJobStatus.DEAD_LETTER
            and self.last_failure is None
        ):
            raise ValueError("dead-letter requires failure")
        return self


class LeasedDurableProjectionJob(FrozenFactBase):
    schema_version: Literal["leased_durable_projection_job.v1"] = (
        "leased_durable_projection_job.v1"
    )
    job: DurableProjectionJobSemanticFact
    job_candidate_fingerprint: str
    activation_fingerprint: str
    seed_contract_fingerprint: str
    delivery_policy: DurableProjectionDeliveryPolicyFact
    canonical_mutation_surface_plan: CanonicalMutationSurfacePlanFact
    expected_state_revision: int
    repair_generation: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_expires_at: datetime
    lease_fingerprint: str


class PreparedDurableProjectionArtifactDocumentFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_durable_projection_artifact_document.v1"
    ] = "prepared_durable_projection_artifact_document.v1"
    document_kind: Literal["artifact"]
    semantic_document_id: str
    document_semantic_fingerprint: str
    media_type: str
    content_codec_contract_fingerprint: str
    metadata_contract_fingerprint: str
    content_sha256: str
    content_utf8_bytes: int
    canonical_content_utf8: str
    artifact_reference: DurableContentAddressedArtifactReferenceFact
    document_fingerprint: str


class PreparedDurableProjectionGraphDocumentFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_durable_projection_graph_document.v1"
    ] = "prepared_durable_projection_graph_document.v1"
    document_kind: Literal["graph_document"]
    graph_id: str
    semantic_document_id: str
    graph_document_type: str
    document_semantic_fingerprint: str
    canonical_json_sha256: str
    canonical_json_utf8_bytes: int
    canonical_json_utf8: str
    jsonld_codec_contract_fingerprint: str
    document_fingerprint: str


class PreparedDurableProjectionGraphRelationFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_durable_projection_graph_relation.v1"
    ] = "prepared_durable_projection_graph_relation.v1"
    document_kind: Literal["graph_relation"]
    relation_reference: DurableProjectionGraphRelationReferenceFact
    source_authority_fingerprint: str
    relation_fingerprint: str


PreparedDurableProjectionDocumentFact: TypeAlias = Annotated[
    PreparedDurableProjectionArtifactDocumentFact
    | PreparedDurableProjectionGraphDocumentFact
    | PreparedDurableProjectionGraphRelationFact,
    Field(discriminator="document_kind"),
]


class CanonicalInlineJsonDocumentFact(FrozenFactBase):
    schema_version: Literal["canonical_inline_json_document.v1"] = (
        "canonical_inline_json_document.v1"
    )
    carrier_kind: Literal["inline_json"] = "inline_json"
    canonical_json_utf8: str
    canonical_utf8_bytes: int
    canonical_sha256: str
    document_semantic_fingerprint: str


class CanonicalArtifactDocumentReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_artifact_document_reference.v1"
    ] = "canonical_artifact_document_reference.v1"
    carrier_kind: Literal["artifact_reference"] = "artifact_reference"
    document_semantic_fingerprint: str
    artifact_reference: DurableContentAddressedArtifactReferenceFact
    reference_fingerprint: str


CanonicalMutationDocumentCarrier: TypeAlias = Annotated[
    CanonicalInlineJsonDocumentFact | CanonicalArtifactDocumentReferenceFact,
    Field(discriminator="carrier_kind"),
]


class CanonicalMutationOrderingFact(FrozenFactBase):
    schema_version: Literal["canonical_mutation_ordering.v1"] = (
        "canonical_mutation_ordering.v1"
    )
    sequence_key: str
    sequence_number: int
    predecessor_mutation_id: str | None
    predecessor_ordering_fingerprint: str | None
    ordering_contract_fingerprint: str
    ordering_fingerprint: str


class CanonicalMutationSemanticFact(FrozenFactBase):
    schema_version: Literal["canonical_mutation_semantic.v2"] = (
        "canonical_mutation_semantic.v2"
    )
    mutation_kind: CanonicalMutationKind
    graph_id: str
    graph_document_semantic_fingerprint: str
    mutation_payload: CanonicalMutationDocumentCarrier
    mutation_contract_fingerprint: str
    mutation_semantic_fingerprint: str


class CanonicalMemoryMutationOperationKind(StrEnum):
    CLAIM = "claim"
    PREFERENCE = "preference"
    ACTION_BOUNDARY = "action_boundary"
    OBSERVATION = "observation"
    DECISION = "decision"
    TURN_RELATION = "turn_relation"
    WORKING_CONTEXT = "working_context"
    RUNTIME_SEMANTIC_DOCUMENT = "runtime_semantic_document"


class ProjectionResultCanonicalMutationOwnerFact(FrozenFactBase):
    schema_version: Literal[
        "projection_result_canonical_mutation_owner.v1"
    ] = "projection_result_canonical_mutation_owner.v1"
    owner_kind: Literal["projection_result"]
    result_owner: DurableProjectionResultOwner
    projection_kind: DurableProjectionKind
    source_event_reference: DurableProjectionSourceEventReferenceFact
    projection_result_semantic_fingerprint: str
    owner_fingerprint: str


class GovernanceCanonicalMutationOwnerFact(FrozenFactBase):
    schema_version: Literal["governance_canonical_mutation_owner.v1"] = (
        "governance_canonical_mutation_owner.v1"
    )
    owner_kind: Literal["memory_governance"]
    governance_batch_id: str
    governance_batch_input_fingerprint: str
    decision_id: str
    decision_semantic_fingerprint: str
    ordered_source_event_reference_fingerprints: tuple[str, ...]
    owner_fingerprint: str


class CanonicalMemoryWriteMutationOwnerFact(FrozenFactBase):
    schema_version: Literal["canonical_memory_write_mutation_owner.v1"] = (
        "canonical_memory_write_mutation_owner.v1"
    )
    owner_kind: Literal["canonical_memory_write"]
    operation_id: str
    operation_kind: "CanonicalMemoryMutationOperationKind"
    ordered_authority_fingerprints: tuple[str, ...]
    owner_fingerprint: str


class GraphMaintenanceMutationOwnerFact(FrozenFactBase):
    schema_version: Literal["graph_maintenance_mutation_owner.v1"] = (
        "graph_maintenance_mutation_owner.v1"
    )
    owner_kind: Literal["graph_maintenance"]
    maintenance_operation_id: str
    maintenance_kind: Literal["graph_reset", "graph_delete"]
    graph_id: str
    ordered_authority_fingerprints: tuple[str, ...]
    owner_fingerprint: str


class LegacyCanonicalMutationOwnerFact(FrozenFactBase):
    schema_version: Literal["legacy_canonical_mutation_owner.v1"] = (
        "legacy_canonical_mutation_owner.v1"
    )
    owner_kind: Literal["legacy_migration"]
    legacy_outbox_id: str
    legacy_payload_sha256: str
    migration_version: int
    owner_fingerprint: str


CanonicalMutationOwner: TypeAlias = Annotated[
    ProjectionResultCanonicalMutationOwnerFact
    | GovernanceCanonicalMutationOwnerFact
    | CanonicalMemoryWriteMutationOwnerFact
    | GraphMaintenanceMutationOwnerFact
    | LegacyCanonicalMutationOwnerFact,
    Field(discriminator="owner_kind"),
]


class CanonicalMutationCandidateFact(FrozenFactBase):
    schema_version: Literal["canonical_mutation_candidate.v2"] = (
        "canonical_mutation_candidate.v2"
    )
    mutation_id: str
    mutation_ordinal: int
    mutation_semantic: CanonicalMutationSemanticFact
    source_owner_fingerprint: str
    source_authority_fingerprints: tuple[str, ...]
    requested_surfaces: tuple[CanonicalMutationSurface, ...]
    surface_plan_fingerprint: str
    candidate_fingerprint: str


class CanonicalMutationDocumentFact(FrozenFactBase):
    schema_version: Literal["canonical_mutation_document.v2"] = (
        "canonical_mutation_document.v2"
    )
    candidate: CanonicalMutationCandidateFact
    ordering: CanonicalMutationOrderingFact
    mutation_fact_fingerprint: str


class PreparedDurableProjectionResultFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_durable_projection_result.v1"
    ] = "prepared_durable_projection_result.v1"
    result_owner: DurableProjectionResultOwner
    result_semantic: DurableProjectionResultSemanticFact
    ordered_documents: tuple[PreparedDurableProjectionDocumentFact, ...]
    canonical_mutation_candidates: tuple[CanonicalMutationCandidateFact, ...]
    prepared_result_fingerprint: str


class DurableProjectionSettlementOutcome(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_settlement_outcome.v1"
    ] = "durable_projection_settlement_outcome.v1"
    confirmation: DurableProjectionCommitConfirmation
    job_id: str
    attempted_lease_fingerprint: str
    resulting_status: DurableProjectionJobStatus | None
    resulting_state_revision: int | None
    resulting_repair_generation: int | None
    result_receipt_reference: DurableProjectionResultReceiptReferenceFact | None
    failure: BoundedRuntimeFailureDiagnosticFact | None
    outcome_fingerprint: str


class DurableProjectionTriggerBindingFact(FrozenFactBase):
    schema_version: Literal["durable_projection_trigger_binding.v1"] = (
        "durable_projection_trigger_binding.v1"
    )
    projection_kind: DurableProjectionKind
    trigger_event_type: str
    accepted_event_schema_fingerprints: tuple[str, ...]
    target_resolver_id: str
    target_resolver_version: str
    target_resolver_contract_fingerprint: str
    binding_fingerprint: str


class DurableProjectionSeedContractFact(FrozenFactBase):
    schema_version: Literal["durable_projection_seed_contract.v1"] = (
        "durable_projection_seed_contract.v1"
    )
    projection_kind: DurableProjectionKind
    handler_contract: DurableProjectionHandlerContractFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    canonical_mutation_surface_plan: CanonicalMutationSurfacePlanFact
    ordered_trigger_bindings: tuple[DurableProjectionTriggerBindingFact, ...]
    source_query_contract_fingerprint: str
    candidate_factory_contract_fingerprint: str
    seed_contract_fingerprint: str


class DurableProjectionKindActivationSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_kind_activation_semantic.v1"
    ] = "durable_projection_kind_activation_semantic.v1"
    activation_id: str
    projection_kind: DurableProjectionKind
    seed_contract: DurableProjectionSeedContractFact
    activation_policy: Literal["post_cutover_events_only"]
    activation_semantic_fingerprint: str


class DurableProjectionKindActivationFact(FrozenFactBase):
    schema_version: Literal["durable_projection_kind_activation.v1"] = (
        "durable_projection_kind_activation.v1"
    )
    activation_semantic: DurableProjectionKindActivationSemanticFact
    activation_migration_version: int
    resulting_migration_registry_prefix_fingerprint: str
    activation_fingerprint: str


class DurableProjectionSessionCutoverFact(FrozenFactBase):
    schema_version: Literal["durable_projection_session_cutover.v1"] = (
        "durable_projection_session_cutover.v1"
    )
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    cutover_through_sequence: int
    cutover_ledger_continuity_accumulator: str
    cutover_ledger_payload_prefix_bytes: int
    cutover_transcript_semantic_prefix_count: int
    cutover_transcript_semantic_prefix_accumulator: str
    migration_version: int
    migration_registry_prefix_fingerprint: str
    activation_fingerprint: str
    seed_contract_fingerprint: str
    cutover_policy_id: Literal["post_cutover_events_only"]
    cutover_fingerprint: str


class RuntimeWriteAdmissionEpochFact(FrozenFactBase):
    schema_version: Literal["runtime_write_admission_epoch.v1"] = (
        "runtime_write_admission_epoch.v1"
    )
    database_target_fingerprint: str
    epoch_number: int
    mode: RuntimeWriteAdmissionMode
    authorized_runtime_role: str
    active_migration_registry_prefix_fingerprint: str
    protected_relation_registry_fingerprint: str
    maintenance_operation_id: str | None
    target_migration_version: int | None
    state_revision: int
    epoch_fingerprint: str

    @model_validator(mode="after")
    def _validate_epoch(self) -> "RuntimeWriteAdmissionEpochFact":
        if self.epoch_number < 1 or self.state_revision < 1:
            raise ValueError("runtime write epoch counters must be positive")
        maintenance = self.mode is RuntimeWriteAdmissionMode.MAINTENANCE
        if maintenance != bool(
            self.maintenance_operation_id
            and self.target_migration_version is not None
        ):
            raise ValueError("runtime write admission mode/authority mismatch")
        return self


class RuntimeWriteMaintenanceAuthorityFact(FrozenFactBase):
    schema_version: Literal["runtime_write_maintenance_authority.v1"] = (
        "runtime_write_maintenance_authority.v1"
    )
    maintenance_operation_id: str
    database_target_fingerprint: str
    expected_normal_epoch_fingerprint: str
    maintenance_epoch_fingerprint: str
    target_migration_version: int
    authority_fingerprint: str


class RuntimeWriteProtectedRelationFact(FrozenFactBase):
    schema_version: Literal["runtime_write_protected_relation.v1"] = (
        "runtime_write_protected_relation.v1"
    )
    schema_name: str
    relation_name: str
    allowed_normal_operations: tuple[Literal["insert", "update", "delete"], ...]
    allowed_maintenance_operations: tuple[
        Literal["insert", "update", "delete"], ...
    ]
    owning_write_domains: tuple[str, ...]
    guard_trigger_name: str
    guard_trigger_contract_fingerprint: str
    relation_fingerprint: str


class RuntimeWriteProtectedRelationRegistryFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_write_protected_relation_registry.v1"
    ] = "runtime_write_protected_relation_registry.v1"
    registry_version: str
    ordered_relations: tuple[RuntimeWriteProtectedRelationFact, ...]
    relation_count: int
    relation_identity_accumulator: str
    production_dml_inventory_fingerprint: str
    registry_fingerprint: str


class RuntimeWriteAdmissionGuard(Protocol):
    @property
    def admission_epoch(self) -> RuntimeWriteAdmissionEpochFact: ...

    @property
    def transaction_owner_id(self) -> str: ...

    @property
    def guard_lock_identity_fingerprint(self) -> str: ...

    @property
    def maintenance_authority_fingerprint(self) -> str | None: ...


class RuntimeSessionOwnerSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_session_owner_semantic.v1"] = (
        "runtime_session_owner_semantic.v1"
    )
    runtime_session_id: str
    workspace_root: str | None
    owner_semantic_fingerprint: str


class RuntimeSessionOwnerBootstrapCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_session_owner_bootstrap_candidate.v1"
    ] = "runtime_session_owner_bootstrap_candidate.v1"
    session_owner: RuntimeSessionOwnerSemanticFact
    expected_admission_epoch_fingerprint: str
    candidate_fingerprint: str


class RuntimeSessionBootstrapStateFact(FrozenFactBase):
    schema_version: Literal["runtime_session_bootstrap_state.v1"] = (
        "runtime_session_bootstrap_state.v1"
    )
    session_owner: RuntimeSessionOwnerSemanticFact
    ordered_active_cutover_fingerprints: tuple[str, ...]
    ordered_pre_activation_cutover_fingerprints: tuple[str, ...]
    cutover_set_accumulator: str
    admission_epoch_fingerprint: str
    state_fingerprint: str


class RuntimeSessionBootstrapCommitOutcomeFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_session_bootstrap_commit_outcome.v1"
    ] = "runtime_session_bootstrap_commit_outcome.v1"
    confirmation: DurableProjectionCommitConfirmation
    attempted_candidate_fingerprint: str
    resulting_state: RuntimeSessionBootstrapStateFact | None
    physical_disposition: Literal["inserted", "exact_confirmed"] | None
    failure: BoundedRuntimeFailureDiagnosticFact | None
    outcome_fingerprint: str


class RuntimeSessionOwnerBootstrapPort(Protocol):
    def bootstrap(
        self,
        *,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
        deadline_monotonic: float,
    ) -> RuntimeSessionBootstrapCommitOutcomeFact: ...


class DurableProjectionSeedStateFact(FrozenFactBase):
    schema_version: Literal["durable_projection_seed_state.v1"] = (
        "durable_projection_seed_state.v1"
    )
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    cutover_fingerprint: str
    through_sequence: int
    ledger_continuity_accumulator: str
    ledger_payload_prefix_bytes: int
    transcript_semantic_prefix_count: int
    transcript_semantic_prefix_accumulator: str
    admitted_job_candidate_count: int
    admitted_job_candidate_accumulator: str
    seed_contract_fingerprint: str
    state_fingerprint: str

    @model_validator(mode="after")
    def _validate_seed_state(self) -> "DurableProjectionSeedStateFact":
        if min(
            self.through_sequence,
            self.ledger_payload_prefix_bytes,
            self.transcript_semantic_prefix_count,
            self.admitted_job_candidate_count,
        ) < 0:
            raise ValueError("projection seed state counters must be non-negative")
        return self


class DurableProjectionSeedFailureFact(FrozenFactBase):
    schema_version: Literal["durable_projection_seed_failure.v1"] = (
        "durable_projection_seed_failure.v1"
    )
    failure_id: str
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    activation_fingerprint: str
    expected_seed_state_fingerprint: str | None
    blocked_from_sequence: int
    blocked_through_sequence: int
    observed_scan_horizon: DurableProjectionLedgerHorizonFact | None
    failure_kind: Literal[
        "active_cutover_missing",
        "ledger_account_missing",
        "ledger_account_prefix_conflict",
        "source_authority_conflict",
        "historical_decoder_unavailable",
        "trigger_contract_mismatch",
        "job_identity_conflict",
    ]
    conflicting_source_event_reference_fingerprint: str | None
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    seed_contract_fingerprint: str
    failure_fingerprint: str

    @model_validator(mode="after")
    def _validate_seed_failure(self) -> "DurableProjectionSeedFailureFact":
        if (
            self.blocked_from_sequence < 0
            or self.blocked_through_sequence < self.blocked_from_sequence
        ):
            raise ValueError("projection seed failure range is invalid")
        return self


class DurableProjectionSeedRepairActionFact(FrozenFactBase):
    schema_version: Literal["durable_projection_seed_repair_action.v1"] = (
        "durable_projection_seed_repair_action.v1"
    )
    repair_action_id: str
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    expected_seed_failure_fingerprint: str
    expected_seed_state_fingerprint: str | None
    action: Literal[
        "retry_after_authority_repair",
        "reverify_after_schema_repair",
    ]
    authority_references: tuple["DurableRepairAuthorityReferenceFact", ...]
    repair_generation: int
    predecessor_repair_action_fingerprint: str | None
    action_fingerprint: str

    @model_validator(mode="after")
    def _validate_repair_generation(
        self,
    ) -> "DurableProjectionSeedRepairActionFact":
        if self.repair_generation < 1:
            raise ValueError("seed repair generation must be positive")
        if (self.repair_generation == 1) != (
            self.predecessor_repair_action_fingerprint is None
        ):
            raise ValueError("seed repair predecessor/generation mismatch")
        if not self.authority_references:
            raise ValueError("seed repair requires typed authority")
        return self


class DurableProjectionSeedFailureResolutionFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_failure_resolution.v1"
    ] = "durable_projection_seed_failure_resolution.v1"
    seed_failure_fingerprint: str
    repair_action_fingerprint: str
    resulting_seed_state_fingerprint: str
    resolved_through_sequence: int
    resolution_fingerprint: str


class DurableProjectionSeedCommitCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_commit_candidate.v1"
    ] = "durable_projection_seed_commit_candidate.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    expected_seed_state: DurableProjectionSeedStateFact
    resulting_seed_state: DurableProjectionSeedStateFact
    scan_horizon: DurableProjectionLedgerHorizonFact
    repaired_seed_failure_fingerprint: str | None
    seed_repair_action_fingerprint: str | None
    ordered_job_candidates: tuple[DurableProjectionJobCandidateFact, ...]
    source_event_count: int
    source_payload_bytes: int
    candidate_fingerprint: str

    @model_validator(mode="after")
    def _validate_seed_candidate(
        self,
    ) -> "DurableProjectionSeedCommitCandidateFact":
        if self.source_event_count < 0 or self.source_payload_bytes < 0:
            raise ValueError("seed batch counters must be non-negative")
        if self.source_event_count > 512:
            raise ValueError("seed batch exceeds the event bound")
        if self.source_payload_bytes > 8 * 1024 * 1024:
            raise ValueError("seed batch exceeds the byte bound")
        if len(self.ordered_job_candidates) > 512:
            raise ValueError("seed batch exceeds the job bound")
        if (
            self.runtime_session_id
            != self.expected_seed_state.runtime_session_id
            or self.runtime_session_id
            != self.resulting_seed_state.runtime_session_id
            or self.runtime_session_id != self.scan_horizon.runtime_session_id
        ):
            raise ValueError("seed candidate runtime identity drifted")
        if (
            self.projection_kind
            is not self.expected_seed_state.projection_kind
            or self.projection_kind
            is not self.resulting_seed_state.projection_kind
        ):
            raise ValueError("seed candidate projection kind drifted")
        if (
            self.expected_seed_state.cutover_fingerprint
            != self.resulting_seed_state.cutover_fingerprint
            or self.expected_seed_state.seed_contract_fingerprint
            != self.resulting_seed_state.seed_contract_fingerprint
        ):
            raise ValueError("seed candidate authority changed")
        if self.resulting_seed_state.through_sequence != (
            self.scan_horizon.through_sequence
        ):
            raise ValueError("resulting seed state must reach scan horizon")
        if (
            self.repaired_seed_failure_fingerprint is None
        ) != (self.seed_repair_action_fingerprint is None):
            raise ValueError("seed repair fields must be jointly present")
        return self


class DurableProjectionSeedFailureCommitCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_failure_commit_candidate.v1"
    ] = "durable_projection_seed_failure_commit_candidate.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    activation_fingerprint: str
    expected_seed_state_fingerprint: str | None
    failure: DurableProjectionSeedFailureFact
    candidate_fingerprint: str

    @model_validator(mode="after")
    def _validate_failure_candidate(
        self,
    ) -> "DurableProjectionSeedFailureCommitCandidateFact":
        if (
            self.runtime_session_id != self.failure.runtime_session_id
            or self.projection_kind is not self.failure.projection_kind
            or self.activation_fingerprint != self.failure.activation_fingerprint
            or self.expected_seed_state_fingerprint
            != self.failure.expected_seed_state_fingerprint
        ):
            raise ValueError("seed failure candidate/failure authority mismatch")
        return self


DurableProjectionSeedWriteCandidate: TypeAlias = (
    DurableProjectionSeedCommitCandidateFact
    | DurableProjectionSeedFailureCommitCandidateFact
)


class DurableProjectionSeedCommitOutcome(FrozenFactBase):
    schema_version: Literal["durable_projection_seed_commit_outcome.v1"] = (
        "durable_projection_seed_commit_outcome.v1"
    )
    confirmation: DurableProjectionCommitConfirmation
    attempted_candidate_fingerprint: str
    committed_seed_state_fingerprint: str | None
    committed_seed_failure_fingerprint: str | None
    committed_seed_failure_resolution_fingerprint: str | None
    committed_job_ids: tuple[str, ...]
    failure: BoundedRuntimeFailureDiagnosticFact | None
    outcome_fingerprint: str


class DurableProjectionSeedCommitPort(Protocol):
    def commit(
        self,
        *,
        candidate: DurableProjectionSeedWriteCandidate,
        admission_guard: RuntimeWriteAdmissionGuard,
        deadline_monotonic: float,
    ) -> DurableProjectionSeedCommitOutcome: ...


class DurableProjectionTargetExecutionLeaseFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_target_execution_lease.v1"
    ] = "durable_projection_target_execution_lease.v1"
    projection_kind: DurableProjectionKind
    target_key: str
    owner_job_id: str
    owner_source_sequence: int
    lease_generation: int
    lease_owner_id: str
    lease_expires_at: datetime
    state_revision: int
    lease_fingerprint: str


class DurableProjectionTargetHeadFact(FrozenFactBase):
    schema_version: Literal["durable_projection_target_head.v1"] = (
        "durable_projection_target_head.v1"
    )
    projection_kind: DurableProjectionKind
    target_key: str
    applied_source_sequence: int
    applied_source_event_reference_fingerprint: str
    applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact
    )
    head_revision: int
    head_fingerprint: str


class DurableProjectionTargetAuthorityConflictFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_target_authority_conflict.v1"
    ] = "durable_projection_target_authority_conflict.v1"
    conflict_id: str
    projection_kind: DurableProjectionKind
    target_key: str
    target_update_policy: DurableProjectionTargetUpdatePolicy
    conflict_kind: Literal[
        "distinct_source_for_single_assignment",
        "same_source_different_result",
        "target_receipt_rebind_conflict",
    ]
    candidate_source_event_reference_fingerprint: str
    candidate_source_sequence: int
    candidate_result_semantic_fingerprint: str | None
    existing_head_fingerprint: str
    existing_applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact
    )
    handler_contract_fingerprint: str
    conflict_fingerprint: str

    @model_validator(mode="after")
    def _validate_conflict(
        self,
    ) -> "DurableProjectionTargetAuthorityConflictFact":
        if self.conflict_kind == "distinct_source_for_single_assignment":
            if (
                self.target_update_policy
                is not DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT
                or self.candidate_result_semantic_fingerprint is not None
            ):
                raise ValueError("single-assignment conflict matrix mismatch")
        elif self.candidate_result_semantic_fingerprint is None:
            raise ValueError("result/rebind conflict requires candidate result")
        return self


class PreActivationProjectionHookContractSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_hook_contract_semantic.v1"
    ] = "pre_activation_projection_hook_contract_semantic.v1"
    projection_kind: DurableProjectionKind
    hook_contract_id: str
    hook_contract_version: str
    handler_contract: DurableProjectionHandlerContractFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    canonical_mutation_surface_plan: CanonicalMutationSurfacePlanFact
    ordered_trigger_bindings: tuple[DurableProjectionTriggerBindingFact, ...]
    source_query_contract_fingerprint: str
    prepared_result_factory_contract_fingerprint: str
    contract_semantic_fingerprint: str


class PreActivationProjectionHookContractFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_hook_contract.v1"
    ] = "pre_activation_projection_hook_contract.v1"
    contract_semantic: PreActivationProjectionHookContractSemanticFact
    installation_migration_version: int
    resulting_migration_registry_prefix_fingerprint: str
    contract_fingerprint: str


class PreActivationProjectionSessionCutoverFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_session_cutover.v1"
    ] = "pre_activation_projection_session_cutover.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    pre_activation_contract_fingerprint: str
    cutover_through_sequence: int
    cutover_ledger_continuity_accumulator: str
    cutover_ledger_payload_prefix_bytes: int
    cutover_transcript_semantic_prefix_count: int
    cutover_transcript_semantic_prefix_accumulator: str
    migration_version: int
    migration_registry_prefix_fingerprint: str
    cutover_fingerprint: str


class PreActivationProjectionTargetCoverageItemFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_target_coverage_item.v1"
    ] = "pre_activation_projection_target_coverage_item.v1"
    projection_kind: DurableProjectionKind
    target_key: str
    latest_trigger_event_reference: DurableProjectionSourceEventReferenceFact
    applied_result_receipt_reference: DurableProjectionResultReceiptReferenceFact
    item_fingerprint: str


class PreActivationProjectionCoveragePageFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_coverage_page.v1"
    ] = "pre_activation_projection_coverage_page.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    page_index: int
    previous_page_fingerprint: str | None
    ordered_items: tuple[PreActivationProjectionTargetCoverageItemFact, ...]
    item_count: int
    item_accumulator: str
    canonical_utf8_bytes: int
    page_fingerprint: str


class PreActivationProjectionCoverageSetReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_coverage_set_reference.v1"
    ] = "pre_activation_projection_coverage_set_reference.v1"
    page_count: int
    target_count: int
    ordered_page_fingerprint_accumulator: str
    ordered_target_item_accumulator: str
    last_page_fingerprint: str | None
    reference_fingerprint: str


class PreActivationProjectionCoverageReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_coverage_receipt.v1"
    ] = "pre_activation_projection_coverage_receipt.v1"
    coverage_receipt_id: str
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    pre_activation_contract_fingerprint: str
    start_cutover_fingerprint: str
    frozen_horizon: DurableProjectionLedgerHorizonFact
    scanned_trigger_event_count: int
    scanned_trigger_event_accumulator: str
    target_coverage_set: PreActivationProjectionCoverageSetReferenceFact
    maintenance_operation_id: str
    maintenance_authority_fingerprint: str
    receipt_fingerprint: str


class PreActivationProjectionCommitOutcomeFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_commit_outcome.v1"
    ] = "pre_activation_projection_commit_outcome.v1"
    confirmation: DurableProjectionCommitConfirmation
    attempted_result_owner_fingerprint: str
    result_receipt_reference: DurableProjectionResultReceiptReferenceFact | None
    resulting_target_head_fingerprint: str | None
    failure: BoundedRuntimeFailureDiagnosticFact | None
    outcome_fingerprint: str


class PreActivationProjectionCommitPort(Protocol):
    def commit(
        self,
        *,
        prepared_result: PreparedDurableProjectionResultFact,
        admission_guard: RuntimeWriteAdmissionGuard,
        deadline_monotonic: float,
    ) -> PreActivationProjectionCommitOutcomeFact: ...


class PostgresMigrationPreparationRequirementFact(FrozenFactBase):
    schema_version: Literal[
        "postgres_migration_preparation_requirement.v1"
    ] = "postgres_migration_preparation_requirement.v1"
    current_head_version: int
    next_migration_version: int
    preparation_kind: PostgresMigrationPreparationKind
    expected_registry_prefix_fingerprint: str
    expected_database_target_fingerprint: str
    required_maintenance_operation_kind: str
    preparation_contract_fingerprint: str
    requirement_fingerprint: str


class PostgresMigrationProgressOutcomeFact(FrozenFactBase):
    schema_version: Literal["postgres_migration_progress_outcome.v1"] = (
        "postgres_migration_progress_outcome.v1"
    )
    status: Literal["advanced", "up_to_date", "preparation_required"]
    initial_head_version: int | None
    resulting_head_version: int | None
    applied_versions: tuple[int, ...]
    preparation_requirement: PostgresMigrationPreparationRequirementFact | None
    resulting_registry_prefix_fingerprint: str
    outcome_fingerprint: str


class LegacySurfaceHistoricalBindingProofFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_historical_binding_proof.v1"
    ] = "legacy_surface_historical_binding_proof.v1"
    binding_kind: Literal["historical_confirmed"]
    surface: CanonicalMutationSurface
    historical_handler_contract: CanonicalMutationSurfaceHandlerContractFact
    observed_target_semantic_identity: str
    observed_target_contract_fingerprint: str
    ordered_target_authority_fingerprints: tuple[str, ...]
    proof_fingerprint: str


class LegacySurfaceMigrationRebindAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_rebind_authority.v1"
    ] = "legacy_surface_migration_rebind_authority.v1"
    binding_kind: Literal["migration_rebound"]
    authority_id: str
    database_target_fingerprint: str
    maintenance_authority_fingerprint: str
    legacy_outbox_id: str
    surface: CanonicalMutationSurface
    expected_legacy_status: Literal["pending", "failed"]
    no_full_side_effect_proof_fingerprint: str
    resulting_planned_surface: CanonicalMutationPlannedSurfaceFact
    authority_fingerprint: str


class LegacySurfaceDecommissionAndRebuildAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_decommission_and_rebuild_authority.v1"
    ] = "legacy_surface_decommission_and_rebuild_authority.v1"
    binding_kind: Literal["decommission_and_rebuild"]
    authority_id: str
    database_target_fingerprint: str
    maintenance_authority_fingerprint: str
    surface: CanonicalMutationSurface
    expected_legacy_surface_head_fingerprint: str
    rebuild_receipt_fingerprint: str
    resulting_handler_contract_fingerprint: str
    resulting_target_compatibility_fingerprint: str
    authority_fingerprint: str


LegacySurfaceMigrationBindingAuthority: TypeAlias = Annotated[
    LegacySurfaceHistoricalBindingProofFact
    | LegacySurfaceMigrationRebindAuthorityFact
    | LegacySurfaceDecommissionAndRebuildAuthorityFact,
    Field(discriminator="binding_kind"),
]


class LegacySurfaceMigrationBindingEntryFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_binding_entry.v1"
    ] = "legacy_surface_migration_binding_entry.v1"
    legacy_outbox_id: str
    legacy_payload_sha256: str
    surface: CanonicalMutationSurface
    legacy_surface_status: str
    binding_authority: LegacySurfaceMigrationBindingAuthority
    entry_fingerprint: str

    @model_validator(mode="after")
    def _validate_binding(self) -> "LegacySurfaceMigrationBindingEntryFact":
        if self.binding_authority.surface is not self.surface:
            raise ValueError("legacy surface binding authority drifted")
        if isinstance(
            self.binding_authority, LegacySurfaceMigrationRebindAuthorityFact
        ):
            if self.binding_authority.legacy_outbox_id != self.legacy_outbox_id:
                raise ValueError("legacy rebind authority/outbox mismatch")
            if self.legacy_surface_status not in {"pending", "failed"}:
                raise ValueError("legacy rebind only accepts pending or failed state")
        return self


class LegacySurfaceMigrationBindingPageFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_binding_page.v1"
    ] = "legacy_surface_migration_binding_page.v1"
    page_index: int
    previous_page_fingerprint: str | None
    ordered_entries: tuple[LegacySurfaceMigrationBindingEntryFact, ...]
    entry_count: int
    entry_accumulator: str
    canonical_utf8_bytes: int
    page_fingerprint: str


class LegacySurfaceMigrationBindingPlanFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_binding_plan.v1"
    ] = "legacy_surface_migration_binding_plan.v1"
    plan_id: str
    database_target_fingerprint: str
    expected_v5_registry_prefix_fingerprint: str
    maintenance_authority_fingerprint: str
    legacy_row_count: int
    legacy_row_accumulator: str
    binding_page_count: int
    ordered_binding_page_fingerprint_accumulator: str
    binding_entry_count: int
    binding_entry_accumulator: str
    ordered_privileged_authority_fingerprints: tuple[str, ...]
    plan_fingerprint: str


class LegacySurfaceMigrationRebaseFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_rebase.v1"
    ] = "legacy_surface_migration_rebase.v1"
    surface: CanonicalMutationSurface
    sequence_key: str
    covered_through_legacy_outbox_id: str
    covered_through_surface_sequence_number: int
    rebuild_receipt_fingerprint: str
    resulting_handler_contract_fingerprint: str
    resulting_target_compatibility_fingerprint: str
    maintenance_authority_fingerprint: str
    rebase_fingerprint: str


class LegacySurfaceMigrationBindingAppliedReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_binding_applied_receipt.v1"
    ] = "legacy_surface_migration_binding_applied_receipt.v1"
    plan_fingerprint: str
    resulting_v6_registry_prefix_fingerprint: str
    historical_confirmed_count: int
    migration_rebound_count: int
    decommissioned_and_rebuilt_count: int
    ordered_surface_rebase_fingerprints: tuple[str, ...]
    resulting_mutation_accumulator: str
    resulting_surface_delivery_accumulator: str
    receipt_fingerprint: str


class PostgresMigrationDataTransformContractFact(FrozenFactBase):
    schema_version: Literal[
        "postgres_migration_data_transform_contract.v1"
    ] = "postgres_migration_data_transform_contract.v1"
    transform_id: Literal[
        "bind_canonical_mutation_v1_to_v2",
        "bind_legacy_surface_contracts_v1_to_v2",
        "activate_run_timeline_projection_v1",
        "activate_tool_result_execution_evidence_projection_v1",
    ]
    transform_version: str
    input_schema_fingerprint: str
    output_schema_fingerprint: str
    canonical_codec_fingerprint: str
    maximum_rows_per_fetch: int
    maximum_payload_bytes_per_fetch: int
    transform_contract_fingerprint: str


class DurableRepairAuthorityReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_repair_authority_reference.v1"
    ] = "durable_repair_authority_reference.v1"
    authority_kind: Literal[
        "operator_command",
        "deployment_configuration",
        "projection_rebuild",
        "source_authority_repair",
    ]
    authority_id: str
    authority_semantic_fingerprint: str
    reference_fingerprint: str


class DurableProjectionRepairActionFact(FrozenFactBase):
    schema_version: Literal["durable_projection_repair_action.v1"] = (
        "durable_projection_repair_action.v1"
    )
    repair_action_id: str
    job_id: str
    expected_state_revision: int
    expected_job_semantic_fingerprint: str
    expected_repair_generation: int
    action: Literal["retry_same_contract", "supersede_after_manual_repair"]
    operator_reason_code: DurableProjectionRepairReason
    authority_references: tuple[DurableRepairAuthorityReferenceFact, ...]
    requested_at: datetime
    resulting_repair_generation: int
    action_fingerprint: str


class CanonicalMutationSurfaceRepairActionFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_repair_action.v1"
    ] = "canonical_mutation_surface_repair_action.v1"
    repair_action_id: str
    delivery_identity_fingerprint: str
    expected_state_revision: int
    expected_surface_head_fingerprint: str | None
    expected_repair_generation: int
    action: Literal[
        "retry_same_contract",
        "decommission_with_authority",
        "decommission_after_rebuild",
    ]
    authority_references: tuple[DurableRepairAuthorityReferenceFact, ...]
    rebuild_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact | None
    )
    resulting_repair_generation: int
    requested_at: datetime
    action_fingerprint: str

    @model_validator(mode="after")
    def _validate_rebuild_receipt(
        self,
    ) -> "CanonicalMutationSurfaceRepairActionFact":
        if (self.action == "decommission_after_rebuild") != (
            self.rebuild_result_receipt_reference is not None
        ):
            raise ValueError("surface repair rebuild receipt matrix mismatch")
        rebuild_authorities = tuple(
            item
            for item in self.authority_references
            if item.authority_kind == "projection_rebuild"
        )
        if self.action == "decommission_after_rebuild":
            if (
                len(rebuild_authorities) != 1
                or rebuild_authorities[0].authority_id
                != self.rebuild_result_receipt_reference.receipt_id
                or rebuild_authorities[0].authority_semantic_fingerprint
                != self.rebuild_result_receipt_reference.receipt_fingerprint
            ):
                raise ValueError("surface repair rebuild authority drifted")
        elif rebuild_authorities:
            raise ValueError("non-rebuild surface repair has rebuild authority")
        return self


class CanonicalMutationSequenceHeadFact(FrozenFactBase):
    schema_version: Literal["canonical_mutation_sequence_head.v1"] = (
        "canonical_mutation_sequence_head.v1"
    )
    sequence_key: str
    last_mutation_sequence_number: int
    last_mutation_id: str
    last_ordering_fingerprint: str
    head_revision: int
    head_fingerprint: str


class CanonicalMutationSurfaceDeliveryIdentityFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_delivery_identity.v1"
    ] = "canonical_mutation_surface_delivery_identity.v1"
    mutation_id: str
    surface: CanonicalMutationSurface
    mutation_semantic_fingerprint: str
    mutation_fact_fingerprint: str
    mutation_ordering_fingerprint: str
    surface_sequence_number: int
    predecessor_surface_delivery_identity_fingerprint: str | None
    predecessor_surface_sequence_number: int | None
    handler_contract: CanonicalMutationSurfaceHandlerContractFact
    delivery_identity_fingerprint: str


class CanonicalMutationSurfaceSequenceHeadFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_sequence_head.v1"
    ] = "canonical_mutation_surface_sequence_head.v1"
    surface: CanonicalMutationSurface
    sequence_key: str
    last_surface_sequence_number: int
    last_mutation_sequence_number: int
    last_mutation_id: str
    last_delivery_identity_fingerprint: str
    head_revision: int
    head_fingerprint: str


class ConfirmedCanonicalMutationSurfaceAppliedReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "confirmed_canonical_mutation_surface_applied_receipt.v1"
    ] = "confirmed_canonical_mutation_surface_applied_receipt.v1"
    receipt_kind: Literal["confirmed_applied"] = "confirmed_applied"
    mutation_id: str
    surface: CanonicalMutationSurface
    mutation_semantic_fingerprint: str
    delivery_identity_fingerprint: str
    target_semantic_identity: str
    applied_document_semantic_fingerprint: str
    surface_handler_contract_fingerprint: str
    receipt_fingerprint: str


class LegacyRecordedSurfaceAppliedReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_recorded_surface_applied_receipt.v1"
    ] = "legacy_recorded_surface_applied_receipt.v1"
    receipt_kind: Literal["legacy_applied"] = "legacy_applied"
    mutation_id: str
    surface: CanonicalMutationSurface
    legacy_outbox_id: str
    legacy_payload_sha256: str
    legacy_recorded_status: Literal["applied"]
    migration_version: int
    receipt_fingerprint: str


class CanonicalMutationSurfaceDecommissionedReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_decommissioned_receipt.v1"
    ] = "canonical_mutation_surface_decommissioned_receipt.v1"
    receipt_kind: Literal["decommissioned"] = "decommissioned"
    mutation_id: str
    surface: CanonicalMutationSurface
    delivery_identity_fingerprint: str
    decommission_reason: Literal[
        "operator_decommission",
        "superseded_by_rebuild",
    ]
    repair_action_fingerprint: str
    replacement_surface_identity_fingerprint: str | None
    receipt_fingerprint: str


CanonicalMutationSurfaceTerminalReceipt: TypeAlias = Annotated[
    ConfirmedCanonicalMutationSurfaceAppliedReceiptFact
    | LegacyRecordedSurfaceAppliedReceiptFact
    | CanonicalMutationSurfaceDecommissionedReceiptFact,
    Field(discriminator="receipt_kind"),
]


class CanonicalMutationSurfaceDeliveryStateFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_delivery_state.v1"
    ] = "canonical_mutation_surface_delivery_state.v1"
    delivery_identity: CanonicalMutationSurfaceDeliveryIdentityFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    status: Literal[
        "pending",
        "leased",
        "retry_wait",
        "applied",
        "decommissioned",
        "dead_letter",
    ]
    state_revision: int
    repair_generation: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    terminal_receipt: CanonicalMutationSurfaceTerminalReceipt | None
    last_failure: BoundedRuntimeFailureDiagnosticFact | None
    state_fingerprint: str


class LeasedCanonicalMutationSurfaceDeliveryFact(FrozenFactBase):
    schema_version: Literal[
        "leased_canonical_mutation_surface_delivery.v1"
    ] = "leased_canonical_mutation_surface_delivery.v1"
    delivery_identity: CanonicalMutationSurfaceDeliveryIdentityFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    expected_state_revision: int
    repair_generation: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_expires_at: datetime
    lease_fingerprint: str


class CanonicalMutationSurfaceTargetHeadFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_target_head.v1"
    ] = "canonical_mutation_surface_target_head.v1"
    surface: CanonicalMutationSurface
    sequence_key: str
    terminal_surface_sequence_number: int
    terminal_mutation_sequence_number: int
    terminal_mutation_id: str
    terminal_mutation_semantic_fingerprint: str
    terminal_disposition: Literal["applied", "decommissioned"]
    terminal_receipt_fingerprint: str
    head_revision: int
    head_fingerprint: str


class CanonicalMutationAppendReceipt(FrozenFactBase):
    schema_version: Literal["canonical_mutation_append_receipt.v1"] = (
        "canonical_mutation_append_receipt.v1"
    )
    mutation_id: str
    mutation_semantic_fingerprint: str
    append_disposition: Literal["inserted", "exact_confirmed"]
    mutation_fact_fingerprint: str
    ordering_fingerprint: str
    ordered_surface_delivery_identity_fingerprints: tuple[str, ...]
    receipt_fingerprint: str


class PreparedCanonicalMutationBundleFact(FrozenFactBase):
    schema_version: Literal["prepared_canonical_mutation_bundle.v1"] = (
        "prepared_canonical_mutation_bundle.v1"
    )
    source_owner: CanonicalMutationOwner
    surface_plan: CanonicalMutationSurfacePlanFact
    ordered_mutation_candidates: tuple[CanonicalMutationCandidateFact, ...]
    bundle_fingerprint: str


class CanonicalMutationBundleAppendReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_bundle_append_receipt.v1"
    ] = "canonical_mutation_bundle_append_receipt.v1"
    attempted_bundle_fingerprint: str
    ordered_mutation_receipts: tuple[CanonicalMutationAppendReceipt, ...]
    receipt_fingerprint: str


class VerifiedPostgresTransactionHandle(Protocol):
    @property
    def schema_binding_fingerprint(self) -> str: ...

    @property
    def transaction_owner_id(self) -> str: ...

    @property
    def transaction_generation(self) -> int: ...

    @property
    def connection_provider_borrower_id(self) -> str: ...


class CanonicalMutationCommitPort(Protocol):
    def append_bundle_in_transaction(
        self,
        *,
        connection: VerifiedPostgresTransactionHandle,
        admission_guard: RuntimeWriteAdmissionGuard,
        bundle: PreparedCanonicalMutationBundleFact,
    ) -> CanonicalMutationBundleAppendReceiptFact: ...


class DurableProjectionStoredEventFact(FrozenFactBase):
    schema_version: Literal["durable_projection_stored_event.v1"] = (
        "durable_projection_stored_event.v1"
    )
    event_reference: DurableProjectionSourceEventReferenceFact
    canonical_payload_json_utf8: str
    canonical_payload_utf8_bytes: int
    canonical_payload_sha256: str
    stored_event_fingerprint: str


class RunTimelineReducerBaseFact(FrozenFactBase):
    schema_version: Literal["run_timeline_reducer_base.v1"] = (
        "run_timeline_reducer_base.v1"
    )
    base_kind: Literal["genesis", "applied_result"]
    runtime_session_id: str
    run_id: str
    base_through_sequence: int
    base_applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact | None
    )
    base_state_semantic_fingerprint: str
    base_fingerprint: str


class RawRunProjectionSourcePage(FrozenFactBase):
    schema_version: Literal["raw_run_projection_source_page.v1"] = (
        "raw_run_projection_source_page.v1"
    )
    runtime_session_id: str
    run_id: str
    after_sequence_exclusive: int
    through_sequence_inclusive: int
    page_index: int
    previous_page_fingerprint: str | None
    ordered_stored_events: tuple[DurableProjectionStoredEventFact, ...]
    selected_event_count: int
    selected_payload_bytes: int
    selected_event_accumulator: str
    has_more: bool
    next_after_sequence: int | None
    page_fingerprint: str


class RunTimelinePersistentStateSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "run_timeline_persistent_state_semantic.v1"
    ] = "run_timeline_persistent_state_semantic.v1"
    runtime_session_id: str
    run_id: str
    through_sequence: int
    status: str
    start_sequence: int
    end_sequence: int | None
    item_count: int
    ordered_item_semantic_accumulator: str
    persistent_item_vector_root_semantic_fingerprint: str
    open_item_state_semantic_fingerprint: str
    state_semantic_fingerprint: str


class PreparedRunTimelineProjectionFact(FrozenFactBase):
    schema_version: Literal["prepared_run_timeline_projection.v1"] = (
        "prepared_run_timeline_projection.v1"
    )
    reducer_base: RunTimelineReducerBaseFact
    trigger_event_reference: DurableProjectionSourceEventReferenceFact
    trigger_horizon: DurableProjectionLedgerHorizonFact
    resulting_state: RunTimelinePersistentStateSemanticFact
    ordered_source_page_fingerprint_accumulator: str
    ordered_new_vector_node_semantic_fingerprints: tuple[str, ...]
    manifest_document_semantic_fingerprint: str
    graph_head_document_semantic_fingerprint: str
    preparation_fingerprint: str


class RunProjectionSourceReader(Protocol):
    def read_run_projection_source_page(
        self,
        *,
        runtime_session_id: str,
        run_id: str,
        after_sequence_exclusive: int,
        through_sequence_inclusive: int,
        page_index: int,
        previous_page_fingerprint: str | None,
        deadline_monotonic: float,
    ) -> RawRunProjectionSourcePage: ...


class RunTimelineProjectionCommitPort(Protocol):
    def commit(
        self,
        *,
        leased_job: LeasedDurableProjectionJob,
        timeline_preparation: PreparedRunTimelineProjectionFact,
        prepared_result: PreparedDurableProjectionResultFact,
        admission_guard: RuntimeWriteAdmissionGuard,
        deadline_monotonic: float,
    ) -> DurableProjectionSettlementOutcome: ...


class ToolCallArgumentsEvidenceProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "tool_call_arguments_evidence_projection.v1"
    ] = "tool_call_arguments_evidence_projection.v1"
    tool_call_start_reference: DurableProjectionSourceEventReferenceFact
    tool_call_end_reference: DurableProjectionSourceEventReferenceFact
    arguments_segment_count: int
    arguments_segment_reference_accumulator: str
    raw_arguments_json: str
    raw_arguments_json_sha256: str
    raw_arguments_json_utf8_bytes: int
    parse_disposition: Literal[
        "valid_object",
        "invalid_json",
        "non_object_json",
    ]
    parsed_arguments_object: FrozenJsonObjectFact | None
    parse_error_code: Literal[
        "json_decode_error",
        "duplicate_object_key",
        "non_finite_number",
        "top_level_non_object",
    ] | None
    canonical_arguments_json_sha256: str | None
    canonical_arguments_json_utf8_bytes: int | None
    bounded_input_summary: str
    bounded_input_summary_sha256: str
    summary_contract_fingerprint: str
    projection_fingerprint: str

    @model_validator(mode="after")
    def _validate_argument_projection(
        self,
    ) -> "ToolCallArgumentsEvidenceProjectionFact":
        encoded = self.raw_arguments_json.encode("utf-8")
        if (
            self.raw_arguments_json_utf8_bytes != len(encoded)
            or self.raw_arguments_json_sha256
            != f"sha256:{sha256(encoded).hexdigest()}"
        ):
            raise ValueError("tool argument raw JSON identity drifted")
        valid = self.parse_disposition == "valid_object"
        if valid != (self.parsed_arguments_object is not None):
            raise ValueError("tool argument parsed object matrix mismatch")
        if valid != (
            self.canonical_arguments_json_sha256 is not None
            and self.canonical_arguments_json_utf8_bytes is not None
        ):
            raise ValueError("tool argument canonical identity matrix mismatch")
        valid_error = (
            self.parse_error_code is None
            if self.parse_disposition == "valid_object"
            else self.parse_error_code
            in {
                "json_decode_error",
                "duplicate_object_key",
                "non_finite_number",
            }
            if self.parse_disposition == "invalid_json"
            else self.parse_error_code == "top_level_non_object"
        )
        if not valid_error:
            raise ValueError("tool argument parse error matrix mismatch")
        if self.parsed_arguments_object is not None:
            canonical = canonical_json_bytes(
                thaw_json(self.parsed_arguments_object)
            )
            if (
                self.canonical_arguments_json_sha256
                != f"sha256:{sha256(canonical).hexdigest()}"
                or self.canonical_arguments_json_utf8_bytes != len(canonical)
            ):
                raise ValueError("tool argument canonical JSON drifted")
        return self


class ToolResultEvidenceOutputProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "tool_result_evidence_output_projection.v1"
    ] = "tool_result_evidence_output_projection.v1"
    result_state: ToolResultState
    result_semantic_fingerprint: str
    bounded_output_summary: str
    bounded_output_summary_sha256: str
    output_was_truncated: bool
    ordered_artifact_reference_fingerprints: tuple[str, ...]
    projection_contract_fingerprint: str
    projection_fingerprint: str


class ToolResultExecutionEvidenceSourceFact(FrozenFactBase):
    schema_version: Literal[
        "tool_result_execution_evidence_source.v1"
    ] = "tool_result_execution_evidence_source.v1"
    tool_result_start_reference: DurableProjectionSourceEventReferenceFact
    tool_result_end_reference: DurableProjectionSourceEventReferenceFact
    terminal_projection: ToolResultTerminalProjectionEndReferenceFact
    tool_call_arguments: ToolCallArgumentsEvidenceProjectionFact
    output_projection: ToolResultEvidenceOutputProjectionFact
    tool_call_id: str
    tool_name: str
    evidence_scope: str
    source_fingerprint: str


class ToolResultExecutionEvidenceSourceReader(Protocol):
    def read_source(
        self,
        *,
        job: DurableProjectionJobSemanticFact,
        maximum_exact_event_reads: int,
        maximum_artifact_references: int,
        deadline_monotonic: float,
    ) -> ToolResultExecutionEvidenceSourceFact: ...


class TurnProducedToolResultRelationFact(FrozenFactBase):
    schema_version: Literal["turn_produced_tool_result_relation.v1"] = (
        "turn_produced_tool_result_relation.v1"
    )
    relation_document_id: str
    graph_id: str
    turn_id: str
    predicate_iri: Literal["https://pulsara.dev/runtime#produced"]
    tool_result_document_id: str
    source_tool_result_end_reference_fingerprint: str
    relation_semantic_fingerprint: str


class ToolResultArtifactRelationFact(FrozenFactBase):
    schema_version: Literal["tool_result_artifact_relation.v1"] = (
        "tool_result_artifact_relation.v1"
    )
    relation_document_id: str
    graph_id: str
    tool_result_document_id: str
    predicate_iri: Literal["https://pulsara.dev/runtime#provides"]
    artifact_document_id: str
    artifact_semantic_reference_fingerprint: str
    artifact_role: str
    artifact_ordinal: int
    relation_semantic_fingerprint: str


class CanonicalGraphRelationLoweringContractFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_graph_relation_lowering_contract.v1"
    ] = "canonical_graph_relation_lowering_contract.v1"
    contract_id: Literal["canonical-graph-relation-lowering.v1"]
    accepted_relation_schema_fingerprints: tuple[str, ...]
    postgres_relation_schema_fingerprint: str
    rdf_named_graph_codec_fingerprint: str
    jsonld_read_merge_contract_fingerprint: str
    owned_predicate_iris: tuple[str, ...]
    contract_fingerprint: str


class CanonicalGraphRelationRowFact(FrozenFactBase):
    schema_version: Literal["canonical_graph_relation_row.v1"] = (
        "canonical_graph_relation_row.v1"
    )
    relation_id: str
    graph_id: str
    relation_kind: Literal[
        "turn_produced_tool_result",
        "tool_result_provides_artifact",
    ]
    source_document_id: str
    predicate_iri: str
    target_document_id: str
    relation_semantic_fingerprint: str
    source_authority_fingerprint: str
    lowering_contract_fingerprint: str
    row_fingerprint: str


class CanonicalGraphRelationReadPageFact(FrozenFactBase):
    schema_version: Literal["canonical_graph_relation_read_page.v1"] = (
        "canonical_graph_relation_read_page.v1"
    )
    graph_id: str
    source_document_id: str
    predicate_iri: str | None
    after_relation_id: str | None
    ordered_relations: tuple[CanonicalGraphRelationRowFact, ...]
    relation_count: int
    relation_accumulator: str
    has_more: bool
    next_after_relation_id: str | None
    page_fingerprint: str


class CanonicalGraphNodeReadViewFact(FrozenFactBase):
    schema_version: Literal["canonical_graph_node_read_view.v1"] = (
        "canonical_graph_node_read_view.v1"
    )
    graph_id: str
    node_id: str
    base_document_semantic_fingerprint: str
    ordered_relation_semantic_accumulator: str
    merged_relation_count: int
    merged_canonical_json_utf8: str
    merged_canonical_json_sha256: str
    jsonld_read_merge_contract_fingerprint: str
    view_fingerprint: str


class ToolResultExecutionEvidenceProjectionCommitPort(Protocol):
    def commit(
        self,
        *,
        leased_job: LeasedDurableProjectionJob,
        source: ToolResultExecutionEvidenceSourceFact,
        prepared_result: PreparedDurableProjectionResultFact,
        admission_guard: RuntimeWriteAdmissionGuard,
        deadline_monotonic: float,
    ) -> DurableProjectionSettlementOutcome: ...


class RuntimeWriteAdmissionGuardHandle(FrozenRuntimeStateBase):
    admission_epoch: RuntimeWriteAdmissionEpochFact
    transaction_owner_id: str
    guard_lock_identity_fingerprint: str
    maintenance_authority_fingerprint: str | None = None


_FACT_FINGERPRINTS: tuple[tuple[type[FrozenFactBase], str, str], ...] = (
    (DurableProjectionSourceEventReferenceFact, "reference_fingerprint", "durable-projection-source-event-reference:v1"),
    (DurableProjectionLedgerHorizonFact, "horizon_fingerprint", "durable-projection-ledger-horizon:v1"),
    (DurableProjectionHandlerContractFact, "contract_fingerprint", "durable-projection-handler-contract:v1"),
    (DurableProjectionPhysicalPolicyFact, "policy_fingerprint", "durable-projection-physical-policy:v1"),
    (DurableProjectionRetryPolicyFact, "policy_fingerprint", "durable-projection-retry-policy:v1"),
    (DurableProjectionDeliveryPolicyFact, "delivery_policy_fingerprint", "durable-projection-delivery-policy:v1"),
    (CanonicalMutationSurfaceHandlerContractFact, "contract_fingerprint", "canonical-mutation-surface-handler-contract:v1"),
    (CanonicalMutationPlannedSurfaceFact, "planned_surface_fingerprint", "canonical-mutation-planned-surface:v1"),
    (CanonicalMutationSurfacePlanFact, "plan_fingerprint", "canonical-mutation-surface-plan:v1"),
    (DurableProjectionJobSemanticFact, "job_semantic_fingerprint", "durable-projection-job-semantic:v1"),
    (DurableProjectionJobCandidateFact, "candidate_fingerprint", "durable-projection-job-candidate:v1"),
    (DurableContentAddressedArtifactReferenceFact, "reference_fingerprint", "durable-content-addressed-artifact-reference:v1"),
    (DurableProjectionArtifactResultDocumentReferenceFact, "reference_fingerprint", "durable-projection-artifact-result-document-reference:v1"),
    (DurableProjectionGraphResultDocumentReferenceFact, "reference_fingerprint", "durable-projection-graph-result-document-reference:v1"),
    (DurableProjectionGraphRelationReferenceFact, "reference_fingerprint", "durable-projection-graph-relation-reference:v1"),
    (DurableProjectionCanonicalMutationReferenceFact, "reference_fingerprint", "durable-projection-canonical-mutation-reference:v1"),
    (DurableProjectionResultSemanticFact, "result_semantic_fingerprint", "durable-projection-result-semantic:v1"),
    (ProjectionJobResultOwnerFact, "owner_fingerprint", "projection-job-result-owner:v1"),
    (PreActivationHookResultOwnerFact, "owner_fingerprint", "pre-activation-hook-result-owner:v1"),
    (DurableProjectionResultReceiptReferenceFact, "reference_fingerprint", "durable-projection-result-receipt-reference:v1"),
    (DurableProjectionAppliedResultReceiptFact, "receipt_fingerprint", "durable-projection-applied-result-receipt:v1"),
    (DurableProjectionSupersededResultReceiptFact, "receipt_fingerprint", "durable-projection-superseded-result-receipt:v1"),
    (DurableProjectionJobOperationalStateFact, "state_fingerprint", "durable-projection-job-operational-state:v1"),
    (LeasedDurableProjectionJob, "lease_fingerprint", "leased-durable-projection-job:v1"),
    (PreparedDurableProjectionArtifactDocumentFact, "document_fingerprint", "prepared-durable-projection-artifact-document:v1"),
    (PreparedDurableProjectionGraphDocumentFact, "document_fingerprint", "prepared-durable-projection-graph-document:v1"),
    (PreparedDurableProjectionGraphRelationFact, "relation_fingerprint", "prepared-durable-projection-graph-relation:v1"),
    (CanonicalInlineJsonDocumentFact, "document_semantic_fingerprint", "canonical-inline-json-document:v1"),
    (CanonicalArtifactDocumentReferenceFact, "reference_fingerprint", "canonical-artifact-document-reference:v1"),
    (CanonicalMutationDocumentFact, "mutation_fact_fingerprint", "canonical-mutation-document:v2"),
    (CanonicalMutationOrderingFact, "ordering_fingerprint", "canonical-mutation-ordering:v1"),
    (CanonicalMutationSemanticFact, "mutation_semantic_fingerprint", "canonical-mutation-semantic:v2"),
    (ProjectionResultCanonicalMutationOwnerFact, "owner_fingerprint", "projection-result-canonical-mutation-owner:v1"),
    (GovernanceCanonicalMutationOwnerFact, "owner_fingerprint", "governance-canonical-mutation-owner:v1"),
    (CanonicalMemoryWriteMutationOwnerFact, "owner_fingerprint", "canonical-memory-write-mutation-owner:v1"),
    (GraphMaintenanceMutationOwnerFact, "owner_fingerprint", "graph-maintenance-mutation-owner:v1"),
    (LegacyCanonicalMutationOwnerFact, "owner_fingerprint", "legacy-canonical-mutation-owner:v1"),
    (CanonicalMutationCandidateFact, "candidate_fingerprint", "canonical-mutation-candidate:v2"),
    (PreparedDurableProjectionResultFact, "prepared_result_fingerprint", "prepared-durable-projection-result:v1"),
    (DurableProjectionSettlementOutcome, "outcome_fingerprint", "durable-projection-settlement-outcome:v1"),
    (DurableProjectionTriggerBindingFact, "binding_fingerprint", "durable-projection-trigger-binding:v1"),
    (DurableProjectionSeedContractFact, "seed_contract_fingerprint", "durable-projection-seed-contract:v1"),
    (DurableProjectionKindActivationSemanticFact, "activation_semantic_fingerprint", "durable-projection-kind-activation-semantic:v1"),
    (DurableProjectionKindActivationFact, "activation_fingerprint", "durable-projection-kind-activation:v1"),
    (DurableProjectionSessionCutoverFact, "cutover_fingerprint", "durable-projection-session-cutover:v1"),
    (RuntimeWriteAdmissionEpochFact, "epoch_fingerprint", "runtime-write-admission-epoch:v1"),
    (RuntimeWriteMaintenanceAuthorityFact, "authority_fingerprint", "runtime-write-maintenance-authority:v1"),
    (RuntimeWriteProtectedRelationFact, "relation_fingerprint", "runtime-write-protected-relation:v1"),
    (RuntimeWriteProtectedRelationRegistryFact, "registry_fingerprint", "runtime-write-protected-relation-registry:v1"),
    (RuntimeSessionOwnerSemanticFact, "owner_semantic_fingerprint", "runtime-session-owner-semantic:v1"),
    (RuntimeSessionOwnerBootstrapCandidateFact, "candidate_fingerprint", "runtime-session-owner-bootstrap-candidate:v1"),
    (RuntimeSessionBootstrapStateFact, "state_fingerprint", "runtime-session-bootstrap-state:v1"),
    (RuntimeSessionBootstrapCommitOutcomeFact, "outcome_fingerprint", "runtime-session-bootstrap-commit-outcome:v1"),
    (DurableProjectionSeedStateFact, "state_fingerprint", "durable-projection-seed-state:v1"),
    (DurableProjectionSeedFailureFact, "failure_fingerprint", "durable-projection-seed-failure:v1"),
    (DurableProjectionSeedRepairActionFact, "action_fingerprint", "durable-projection-seed-repair-action:v1"),
    (DurableProjectionSeedFailureResolutionFact, "resolution_fingerprint", "durable-projection-seed-failure-resolution:v1"),
    (DurableProjectionSeedCommitCandidateFact, "candidate_fingerprint", "durable-projection-seed-commit-candidate:v1"),
    (DurableProjectionSeedFailureCommitCandidateFact, "candidate_fingerprint", "durable-projection-seed-failure-commit-candidate:v1"),
    (DurableProjectionSeedCommitOutcome, "outcome_fingerprint", "durable-projection-seed-commit-outcome:v1"),
    (DurableProjectionTargetExecutionLeaseFact, "lease_fingerprint", "durable-projection-target-execution-lease:v1"),
    (DurableProjectionTargetHeadFact, "head_fingerprint", "durable-projection-target-head:v1"),
    (DurableProjectionTargetAuthorityConflictFact, "conflict_fingerprint", "durable-projection-target-authority-conflict:v1"),
    (PreActivationProjectionHookContractSemanticFact, "contract_semantic_fingerprint", "pre-activation-projection-hook-contract-semantic:v1"),
    (PreActivationProjectionHookContractFact, "contract_fingerprint", "pre-activation-projection-hook-contract:v1"),
    (PreActivationProjectionSessionCutoverFact, "cutover_fingerprint", "pre-activation-projection-session-cutover:v1"),
    (PreActivationProjectionTargetCoverageItemFact, "item_fingerprint", "pre-activation-projection-target-coverage-item:v1"),
    (PreActivationProjectionCoveragePageFact, "page_fingerprint", "pre-activation-projection-coverage-page:v1"),
    (PreActivationProjectionCoverageSetReferenceFact, "reference_fingerprint", "pre-activation-projection-coverage-set-reference:v1"),
    (PreActivationProjectionCoverageReceiptFact, "receipt_fingerprint", "pre-activation-projection-coverage-receipt:v1"),
    (PreActivationProjectionCommitOutcomeFact, "outcome_fingerprint", "pre-activation-projection-commit-outcome:v1"),
    (PostgresMigrationPreparationRequirementFact, "requirement_fingerprint", "postgres-migration-preparation-requirement:v1"),
    (PostgresMigrationProgressOutcomeFact, "outcome_fingerprint", "postgres-migration-progress-outcome:v1"),
    (LegacySurfaceHistoricalBindingProofFact, "proof_fingerprint", "legacy-surface-historical-binding-proof:v1"),
    (LegacySurfaceMigrationRebindAuthorityFact, "authority_fingerprint", "legacy-surface-migration-rebind-authority:v1"),
    (LegacySurfaceDecommissionAndRebuildAuthorityFact, "authority_fingerprint", "legacy-surface-decommission-and-rebuild-authority:v1"),
    (LegacySurfaceMigrationBindingEntryFact, "entry_fingerprint", "legacy-surface-migration-binding-entry:v1"),
    (LegacySurfaceMigrationBindingPageFact, "page_fingerprint", "legacy-surface-migration-binding-page:v1"),
    (LegacySurfaceMigrationBindingPlanFact, "plan_fingerprint", "legacy-surface-migration-binding-plan:v1"),
    (LegacySurfaceMigrationRebaseFact, "rebase_fingerprint", "legacy-surface-migration-rebase:v1"),
    (LegacySurfaceMigrationBindingAppliedReceiptFact, "receipt_fingerprint", "legacy-surface-migration-binding-applied-receipt:v1"),
    (PostgresMigrationDataTransformContractFact, "transform_contract_fingerprint", "postgres-migration-data-transform-contract:v1"),
    (DurableRepairAuthorityReferenceFact, "reference_fingerprint", "durable-repair-authority-reference:v1"),
    (DurableProjectionRepairActionFact, "action_fingerprint", "durable-projection-repair-action:v1"),
    (CanonicalMutationSurfaceRepairActionFact, "action_fingerprint", "canonical-mutation-surface-repair-action:v1"),
    (CanonicalMutationSequenceHeadFact, "head_fingerprint", "canonical-mutation-sequence-head:v1"),
    (CanonicalMutationSurfaceDeliveryIdentityFact, "delivery_identity_fingerprint", "canonical-mutation-surface-delivery-identity:v1"),
    (CanonicalMutationSurfaceSequenceHeadFact, "head_fingerprint", "canonical-mutation-surface-sequence-head:v1"),
    (ConfirmedCanonicalMutationSurfaceAppliedReceiptFact, "receipt_fingerprint", "confirmed-canonical-mutation-surface-applied-receipt:v1"),
    (LegacyRecordedSurfaceAppliedReceiptFact, "receipt_fingerprint", "legacy-recorded-surface-applied-receipt:v1"),
    (CanonicalMutationSurfaceDecommissionedReceiptFact, "receipt_fingerprint", "canonical-mutation-surface-decommissioned-receipt:v1"),
    (CanonicalMutationSurfaceDeliveryStateFact, "state_fingerprint", "canonical-mutation-surface-delivery-state:v1"),
    (LeasedCanonicalMutationSurfaceDeliveryFact, "lease_fingerprint", "leased-canonical-mutation-surface-delivery:v1"),
    (CanonicalMutationSurfaceTargetHeadFact, "head_fingerprint", "canonical-mutation-surface-target-head:v1"),
    (CanonicalMutationAppendReceipt, "receipt_fingerprint", "canonical-mutation-append-receipt:v1"),
    (PreparedCanonicalMutationBundleFact, "bundle_fingerprint", "prepared-canonical-mutation-bundle:v1"),
    (CanonicalMutationBundleAppendReceiptFact, "receipt_fingerprint", "canonical-mutation-bundle-append-receipt:v1"),
    (DurableProjectionStoredEventFact, "stored_event_fingerprint", "durable-projection-stored-event:v1"),
    (RunTimelineReducerBaseFact, "base_fingerprint", "run-timeline-reducer-base:v1"),
    (RawRunProjectionSourcePage, "page_fingerprint", "raw-run-projection-source-page:v1"),
    (RunTimelinePersistentStateSemanticFact, "state_semantic_fingerprint", "run-timeline-persistent-state-semantic:v1"),
    (PreparedRunTimelineProjectionFact, "preparation_fingerprint", "prepared-run-timeline-projection:v1"),
    (ToolCallArgumentsEvidenceProjectionFact, "projection_fingerprint", "tool-call-arguments-evidence-projection:v1"),
    (ToolResultEvidenceOutputProjectionFact, "projection_fingerprint", "tool-result-evidence-output-projection:v1"),
    (ToolResultExecutionEvidenceSourceFact, "source_fingerprint", "tool-result-execution-evidence-source:v1"),
    (TurnProducedToolResultRelationFact, "relation_semantic_fingerprint", "turn-produced-tool-result-relation:v1"),
    (ToolResultArtifactRelationFact, "relation_semantic_fingerprint", "tool-result-artifact-relation:v1"),
    (CanonicalGraphRelationLoweringContractFact, "contract_fingerprint", "canonical-graph-relation-lowering-contract:v1"),
    (CanonicalGraphRelationRowFact, "row_fingerprint", "canonical-graph-relation-row:v1"),
    (CanonicalGraphRelationReadPageFact, "page_fingerprint", "canonical-graph-relation-read-page:v1"),
    (CanonicalGraphNodeReadViewFact, "view_fingerprint", "canonical-graph-node-read-view:v1"),
)

for _fact_type, _fingerprint_field, _domain in _FACT_FINGERPRINTS:
    if _fingerprint_field not in _fact_type.model_fields:
        raise RuntimeError(
            f"{_fact_type.__name__} does not declare "
            f"{_fingerprint_field}"
        )
    register_durable_fact(
        schema_version=str(_fact_type.model_fields["schema_version"].default),
        own_fingerprint_field=_fingerprint_field,
        domain_separator=_domain,
    )


def build_projection_fact(
    fact_type: type[FrozenFactBase],
    /,
    **payload: Any,
) -> FrozenFactBase:
    """Build one registered projection fact and own its fingerprint."""

    return build_frozen_fact(fact_type, **payload)


def projection_target_key(
    *,
    projection_kind: DurableProjectionKind,
    runtime_session_id: str,
    run_id: str,
    tool_call_id: str | None = None,
) -> str:
    """Derive the closed target identity for one projection kind."""

    from pulsara_agent.primitives._context_base import context_fingerprint

    if projection_kind is DurableProjectionKind.RUN_TIMELINE:
        return "run:" + context_fingerprint(
            "durable-projection-run-target:v1",
            {"runtime_session_id": runtime_session_id, "run_id": run_id},
        )
    if not tool_call_id:
        raise ValueError("tool-result evidence target requires tool_call_id")
    return "tool-result:" + context_fingerprint(
        "durable-projection-tool-result-target:v1",
        {
            "runtime_session_id": runtime_session_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
        },
    )


def durable_projection_job_id(
    *,
    projection_kind: DurableProjectionKind,
    source_event_reference: DurableProjectionSourceEventReferenceFact,
    target_key: str,
    handler_contract_fingerprint: str,
) -> str:
    from pulsara_agent.primitives._context_base import context_fingerprint

    return "projection-job:" + context_fingerprint(
        "pulsara:durable-projection-job-id:v1",
        {
            "projection_kind": projection_kind.value,
            "runtime_session_id": source_event_reference.runtime_session_id,
            "event_id": source_event_reference.event_id,
            "target_key": target_key,
            "handler_contract_fingerprint": handler_contract_fingerprint,
        },
    )


def durable_result_receipt_reference(
    receipt: DurableProjectionResultReceiptFact,
) -> DurableProjectionResultReceiptReferenceFact:
    return build_frozen_fact(
        DurableProjectionResultReceiptReferenceFact,
        schema_version="durable_projection_result_receipt_reference.v1",
        receipt_id=receipt.receipt_id,
        receipt_fingerprint=receipt.receipt_fingerprint,
    )


__all__ = [name for name in globals() if not name.startswith("_")]
