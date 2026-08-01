"""Durable D3 authority for compaction-memory extraction results."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import context_fingerprint

from pulsara_agent.projection_jobs.contracts import (
    CompactionMemoryExtractionProjectionResultReceiptFact as _CompactionMemoryExtractionProjectionResultReceiptFact,
    DurableProjectionSourceEventReferenceFact,
    ProjectionJobResultOwnerFact,
)
from pulsara_agent.primitives.compaction import (
    CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT,
    BackgroundDerivedWorkBudgetAdmissionFailureFact,
    BackgroundDerivedWorkBudgetReservationFact,
    BackgroundDerivedWorkBudgetSettlementFact,
    CompactionHumanEvidenceManifestReferenceFact,
    CompactionMemoryInputBudgetFailureFact,
    CompactionPostCompletionExtensionLinkFact,
    ResolvedExtractionInputBudgetAttributionFact,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    FrozenRuntimeStateBase,
    register_durable_fact,
)
from pulsara_agent.primitives.governance_evidence import (
    GovernanceEvidenceArtifactReferenceFact,
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.primitives.long_horizon import ModelCallReservationQuoteFact
from pulsara_agent.primitives.terminal_projection import TerminalProjectionReferenceFact


Fingerprint = Annotated[str, Field(min_length=1, max_length=256)]
CompactionMemoryExtractionProjectionResultReceiptFact = (
    _CompactionMemoryExtractionProjectionResultReceiptFact
)


def _fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


@_fact(
    "compaction_memory_no_eligible_result_semantic.v1",
    "result_semantic_fingerprint",
    "compaction-memory-no-eligible-result-semantic:v1",
)
class CompactionMemoryNoEligibleEvidenceResultSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_no_eligible_result_semantic.v1"] = (
        "compaction_memory_no_eligible_result_semantic.v1"
    )
    outcome_kind: Literal["no_eligible_evidence"] = "no_eligible_evidence"
    evidence_set_semantic_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint
    result_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _canonical_empty(
        self,
    ) -> "CompactionMemoryNoEligibleEvidenceResultSemanticFact":
        if (
            self.evidence_set_semantic_fingerprint
            != CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT
        ):
            raise ValueError("no-eligible outcome requires canonical empty evidence")
        return self


@_fact(
    "compaction_memory_input_budget_unsatisfiable_result_semantic.v1",
    "result_semantic_fingerprint",
    "compaction-memory-input-budget-unsatisfiable-result-semantic:v1",
)
class CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_input_budget_unsatisfiable_result_semantic.v1"
    ] = "compaction_memory_input_budget_unsatisfiable_result_semantic.v1"
    outcome_kind: Literal["input_budget_unsatisfiable"] = "input_budget_unsatisfiable"
    failure_kind: Literal[
        "prompt_and_reserves_exceed_target", "no_complete_evidence_message_fits"
    ]
    evidence_set_semantic_fingerprint: Fingerprint
    budget_selection_contract_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint
    result_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _canonical_empty(
        self,
    ) -> "CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact":
        if (
            self.evidence_set_semantic_fingerprint
            != CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT
        ):
            raise ValueError(
                "input-budget-unsatisfiable outcome requires canonical empty evidence"
            )
        return self


@_fact(
    "compaction_memory_background_budget_exhausted_result_semantic.v1",
    "result_semantic_fingerprint",
    "compaction-memory-background-budget-exhausted-result-semantic:v1",
)
class CompactionMemoryBackgroundBudgetExhaustedResultSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_background_budget_exhausted_result_semantic.v1"
    ] = "compaction_memory_background_budget_exhausted_result_semantic.v1"
    outcome_kind: Literal["background_budget_exhausted"] = "background_budget_exhausted"
    input_semantic_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint
    exhaustion_kind: Literal[
        "call_cap_exhausted",
        "input_token_cap_exhausted",
        "output_token_cap_exhausted",
        "milliunit_cap_exhausted",
        "account_reconciliation_required",
    ]
    extraction_semantic_contract_fingerprint: Fingerprint
    result_semantic_fingerprint: Fingerprint


@_fact(
    "compaction_memory_valid_empty_result_semantic.v1",
    "result_semantic_fingerprint",
    "compaction-memory-valid-empty-result-semantic:v1",
)
class CompactionMemoryValidEmptyResultSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_valid_empty_result_semantic.v1"] = (
        "compaction_memory_valid_empty_result_semantic.v1"
    )
    outcome_kind: Literal["valid_empty"] = "valid_empty"
    input_semantic_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint
    terminal_projection_semantic_fingerprint: Fingerprint
    parser_contract_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint
    result_semantic_fingerprint: Fingerprint


@_fact(
    "compaction_memory_valid_candidates_result_semantic.v1",
    "result_semantic_fingerprint",
    "compaction-memory-valid-candidates-result-semantic:v1",
)
class CompactionMemoryValidCandidatesResultSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_valid_candidates_result_semantic.v1"] = (
        "compaction_memory_valid_candidates_result_semantic.v1"
    )
    outcome_kind: Literal["valid_candidates"] = "valid_candidates"
    input_semantic_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint
    terminal_projection_semantic_fingerprint: Fingerprint
    parser_contract_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint
    ordered_candidate_semantic_fingerprints: tuple[Fingerprint, ...] = Field(
        min_length=1, max_length=3
    )
    result_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordered(self) -> "CompactionMemoryValidCandidatesResultSemanticFact":
        values = self.ordered_candidate_semantic_fingerprints
        if values != tuple(sorted(set(values))):
            raise ValueError("candidate semantic fingerprints must be sorted/unique")
        return self


CompactionMemoryExtractionResultSemanticFact: TypeAlias = Annotated[
    CompactionMemoryNoEligibleEvidenceResultSemanticFact
    | CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact
    | CompactionMemoryBackgroundBudgetExhaustedResultSemanticFact
    | CompactionMemoryValidEmptyResultSemanticFact
    | CompactionMemoryValidCandidatesResultSemanticFact,
    Field(discriminator="outcome_kind"),
]


@_fact(
    "compaction_memory_no_eligible_attribution.v1",
    "attribution_fingerprint",
    "compaction-memory-no-eligible-attribution:v1",
)
class CompactionMemoryNoEligibleEvidenceAttributionFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_no_eligible_attribution.v1"] = (
        "compaction_memory_no_eligible_attribution.v1"
    )
    outcome_kind: Literal["no_eligible_evidence"] = "no_eligible_evidence"
    attribution_fingerprint: Fingerprint


@_fact(
    "compaction_memory_input_budget_unsatisfiable_attribution.v1",
    "attribution_fingerprint",
    "compaction-memory-input-budget-unsatisfiable-attribution:v1",
)
class CompactionMemoryInputBudgetUnsatisfiableAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_input_budget_unsatisfiable_attribution.v1"
    ] = "compaction_memory_input_budget_unsatisfiable_attribution.v1"
    outcome_kind: Literal["input_budget_unsatisfiable"] = "input_budget_unsatisfiable"
    resolved_input_budget: ResolvedExtractionInputBudgetAttributionFact
    budget_failure: CompactionMemoryInputBudgetFailureFact
    attribution_fingerprint: Fingerprint


@_fact(
    "compaction_memory_background_budget_exhausted_attribution.v1",
    "attribution_fingerprint",
    "compaction-memory-background-budget-exhausted-attribution:v1",
)
class CompactionMemoryBackgroundBudgetExhaustedAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_background_budget_exhausted_attribution.v1"
    ] = "compaction_memory_background_budget_exhausted_attribution.v1"
    outcome_kind: Literal["background_budget_exhausted"] = "background_budget_exhausted"
    input_artifact_reference: GovernanceEvidenceArtifactReferenceFact
    input_semantic_fingerprint: Fingerprint
    resolved_input_budget: ResolvedExtractionInputBudgetAttributionFact
    rejected_reservation_quote: ModelCallReservationQuoteFact
    budget_admission_failure: BackgroundDerivedWorkBudgetAdmissionFailureFact
    attribution_fingerprint: Fingerprint


@_fact(
    "compaction_memory_model_result_attribution.v1",
    "attribution_fingerprint",
    "compaction-memory-model-result-attribution:v1",
)
class CompactionMemoryModelResultAttributionFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_model_result_attribution.v1"] = (
        "compaction_memory_model_result_attribution.v1"
    )
    outcome_kind: Literal["valid_empty", "valid_candidates"]
    input_artifact_reference: GovernanceEvidenceArtifactReferenceFact
    input_semantic_fingerprint: Fingerprint
    resolved_input_budget: ResolvedExtractionInputBudgetAttributionFact
    model_call_start_event_reference: GovernanceStoredEventReferenceFact
    model_call_end_event_reference: GovernanceStoredEventReferenceFact
    model_terminal_projection_reference: TerminalProjectionReferenceFact
    parsed_output_semantic_fingerprint: Fingerprint
    dispatch_attempt_ordinal: int = Field(ge=1)
    background_budget_reservation: BackgroundDerivedWorkBudgetReservationFact
    background_budget_settlement: BackgroundDerivedWorkBudgetSettlementFact
    attribution_fingerprint: Fingerprint


CompactionMemoryExtractionOutcomeAttributionFact: TypeAlias = Annotated[
    CompactionMemoryNoEligibleEvidenceAttributionFact
    | CompactionMemoryInputBudgetUnsatisfiableAttributionFact
    | CompactionMemoryBackgroundBudgetExhaustedAttributionFact
    | CompactionMemoryModelResultAttributionFact,
    Field(discriminator="outcome_kind"),
]


@_fact(
    "compaction_memory_extraction_occurrence_attribution.v1",
    "occurrence_attribution_fingerprint",
    "compaction-memory-extraction-occurrence-attribution:v1",
)
class CompactionMemoryExtractionOccurrenceAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_occurrence_attribution.v1"
    ] = "compaction_memory_extraction_occurrence_attribution.v1"
    compaction_id: str
    extension_link: CompactionPostCompletionExtensionLinkFact
    request_event_reference: GovernanceStoredEventReferenceFact
    durable_job_id: str
    durable_job_source_reference: DurableProjectionSourceEventReferenceFact
    human_evidence_manifest_reference: CompactionHumanEvidenceManifestReferenceFact
    outcome_attribution: CompactionMemoryExtractionOutcomeAttributionFact
    occurrence_attribution_fingerprint: Fingerprint


@_fact(
    "durable_projection_event_write_candidate.v1",
    "candidate_fingerprint",
    "durable-projection-event-write-candidate:v1",
)
class DurableProjectionEventWriteCandidateFact(FrozenFactBase):
    schema_version: Literal["durable_projection_event_write_candidate.v1"] = (
        "durable_projection_event_write_candidate.v1"
    )
    event_id: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: Fingerprint
    event_domain_contract_fingerprint: Fingerprint
    canonical_unsequenced_payload_utf8: str
    canonical_payload_sha256: Fingerprint
    canonical_payload_utf8_bytes: int = Field(ge=1)
    candidate_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _payload(self) -> "DurableProjectionEventWriteCandidateFact":
        from hashlib import sha256

        encoded = self.canonical_unsequenced_payload_utf8.encode("utf-8")
        if len(encoded) != self.canonical_payload_utf8_bytes:
            raise ValueError("durable event candidate byte count mismatch")
        actual = f"sha256:{sha256(encoded).hexdigest()}"
        if self.canonical_payload_sha256 != actual:
            raise ValueError("durable event candidate digest mismatch")
        return self


@_fact(
    "candidate_outbox_plan_item.v1",
    "item_fingerprint",
    "candidate-outbox-plan-item:v1",
)
class CandidateOutboxPlanItemFact(FrozenFactBase):
    schema_version: Literal["candidate_outbox_plan_item.v1"] = (
        "candidate_outbox_plan_item.v1"
    )
    candidate_ordinal: int = Field(ge=0, le=2)
    candidate_entry_id: str
    candidate_attribution_fingerprint: Fingerprint
    expected_projection_item_fingerprint: Fingerprint
    expected_physical_row_fingerprint: Fingerprint
    item_fingerprint: Fingerprint


@_fact(
    "candidate_outbox_plan.v1",
    "plan_fingerprint",
    "candidate-outbox-plan:v1",
)
class CandidateOutboxPlanFact(FrozenFactBase):
    schema_version: Literal["candidate_outbox_plan.v1"] = "candidate_outbox_plan.v1"
    producer_event_id: str
    ordered_items: tuple[CandidateOutboxPlanItemFact, ...] = Field(max_length=3)
    item_count: int = Field(ge=0, le=3)
    ordered_item_accumulator: Fingerprint
    lowering_contract_fingerprint: Fingerprint
    plan_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _items(self) -> "CandidateOutboxPlanFact":
        if self.item_count != len(self.ordered_items):
            raise ValueError("candidate outbox plan count mismatch")
        if tuple(item.candidate_ordinal for item in self.ordered_items) != tuple(
            range(self.item_count)
        ):
            raise ValueError("candidate outbox plan ordinals are not contiguous")
        accumulator = context_fingerprint(
            "candidate-outbox-plan-item-accumulator:v1:empty", ()
        )
        for item in self.ordered_items:
            accumulator = context_fingerprint(
                "candidate-outbox-plan-item-accumulator:v1:step",
                (accumulator, item.item_fingerprint),
            )
        if accumulator != self.ordered_item_accumulator:
            raise ValueError("candidate outbox plan accumulator mismatch")
        return self


@_fact(
    "compaction_memory_extraction_result_candidate.v1",
    "result_candidate_fingerprint",
    "compaction-memory-extraction-result-candidate:v1",
)
class CompactionMemoryExtractionResultCandidateFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_extraction_result_candidate.v1"] = (
        "compaction_memory_extraction_result_candidate.v1"
    )
    result_candidate_id: str
    result_owner: ProjectionJobResultOwnerFact
    job_id: str
    target_key: str
    completed_event_id: str
    producer_event_candidate: DurableProjectionEventWriteCandidateFact
    result_semantic_fingerprint: Fingerprint
    receipt_id: str
    intended_target_head_revision: int = Field(ge=1)
    expected_target_head_fingerprint: Fingerprint | None
    candidate_outbox_plan: CandidateOutboxPlanFact
    permanent_automatic_omission_count: int = Field(ge=0)
    permanent_automatic_omission_semantic_accumulator: Fingerprint
    permanent_automatic_omission_attribution_accumulator: Fingerprint
    result_candidate_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "CompactionMemoryExtractionResultCandidateFact":
        if (
            self.result_owner.job_id != self.job_id
            or self.producer_event_candidate.event_id != self.completed_event_id
            or self.candidate_outbox_plan.producer_event_id != self.completed_event_id
        ):
            raise ValueError("compaction result candidate identity join failed")
        return self


class ResultCandidateInstallationGuard(FrozenRuntimeStateBase):
    """Process-local CAS proof for one RESULT_READY installation attempt."""

    result_candidate_id: str
    result_candidate_fingerprint: Fingerprint
    job_id: str
    source_job_state_revision: int = Field(ge=0)
    source_job_lease_generation: int = Field(ge=1)
    source_job_lease_fingerprint: Fingerprint
    target_lease_fingerprint: Fingerprint
    guard_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _guard(self) -> "ResultCandidateInstallationGuard":
        payload = self.model_dump(
            mode="json",
            exclude={"guard_fingerprint"},
        )
        expected = context_fingerprint(
            "compaction-memory-result-candidate-installation-guard:v1",
            payload,
        )
        if self.guard_fingerprint != expected:
            raise ValueError("result candidate installation guard fingerprint mismatch")
        return self


def result_candidate_installation_guard(
    *,
    result_candidate: CompactionMemoryExtractionResultCandidateFact,
    source_job_state_revision: int,
    source_job_lease_generation: int,
    source_job_lease_fingerprint: str,
    target_lease_fingerprint: str,
) -> ResultCandidateInstallationGuard:
    payload = {
        "result_candidate_id": result_candidate.result_candidate_id,
        "result_candidate_fingerprint": (result_candidate.result_candidate_fingerprint),
        "job_id": result_candidate.job_id,
        "source_job_state_revision": source_job_state_revision,
        "source_job_lease_generation": source_job_lease_generation,
        "source_job_lease_fingerprint": source_job_lease_fingerprint,
        "target_lease_fingerprint": target_lease_fingerprint,
    }
    return ResultCandidateInstallationGuard(
        **payload,
        guard_fingerprint=context_fingerprint(
            "compaction-memory-result-candidate-installation-guard:v1",
            payload,
        ),
    )


__all__ = [name for name in globals() if name.endswith("Fact")]
__all__.extend(
    [
        "ResultCandidateInstallationGuard",
        "result_candidate_installation_guard",
    ]
)
