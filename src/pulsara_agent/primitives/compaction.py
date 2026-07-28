"""Immutable contracts for post-compaction memory extraction.

The module owns only event-safe values.  Live drivers, artifact writers and
database capabilities deliberately live behind ports in higher layers.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    context_fingerprint,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    build_frozen_fact,
    register_durable_fact,
)
from pulsara_agent.primitives.governance_evidence import (
    GovernanceEvidenceArtifactReferenceFact,
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.primitives.long_horizon import (
    ModelCallReservationQuoteFact,
    default_rollout_budget_policy,
)
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact
from pulsara_agent.primitives.memory_candidate import (
    MemoryCandidateSemanticFact,
    build_memory_candidate_semantic,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    BoundedRuntimeFailureDiagnosticFact,
)
from pulsara_agent.primitives.transcript_projection import (
    TranscriptProjectionLeafEntryReferenceFact,
)


Fingerprint = Annotated[str, Field(min_length=1, max_length=256)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


COMPACTION_MEMORY_EVIDENCE_SANITIZER_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-evidence-sanitizer-contract:v1",
    {
        "rules": (
            "pem-private-key:v1",
            "bearer-token:v1",
            "openai-style-token:v1",
            "credential-assignment:v1",
            "dsn-password:v1",
            "cloud-access-key:v1",
            "opaque-high-entropy-run:v1",
        ),
        "replacement": "[REDACTED:<rule-id>]",
    },
)
COMPACTION_MEMORY_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-evidence-selection-contract:v1",
    {
        "order": "newest-first-selection/causal-output",
        "maximum_nodes": 256,
        "oversize": "permanent-whole-message-omission",
        "non_fitting": "skip-and-backfill-older",
    },
)
COMPACTION_MEMORY_INPUT_PROJECTION_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-evidence-input-projection-contract:v1",
    {"projection": "full-sanitized-message", "truncation": "forbidden"},
)


def _ordered_accumulator(domain: str, values: tuple[str, ...]) -> str:
    current = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        current = context_fingerprint(f"{domain}:step", (current, value))
    return current


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
    "content_addressed_artifact_reference.v1",
    "reference_fingerprint",
    "content-addressed-artifact-reference:v1",
)
class ContentAddressedArtifactReferenceFact(FrozenFactBase):
    schema_version: Literal["content_addressed_artifact_reference.v1"] = (
        "content_addressed_artifact_reference.v1"
    )
    artifact_id: str = Field(min_length=1, max_length=256)
    artifact_kind: str = Field(min_length=1, max_length=128)
    media_type: str = Field(min_length=1, max_length=128)
    content_sha256: Sha256Hex
    content_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    artifact_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


@_fact(
    "compaction_human_evidence_selection_window_attribution.v1",
    "window_attribution_fingerprint",
    "compaction-human-evidence-selection-window-attribution:v1",
)
class CompactionHumanEvidenceSelectionWindowAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_selection_window_attribution.v1"
    ] = "compaction_human_evidence_selection_window_attribution.v1"
    previous_keep_after_sequence: int = Field(ge=0)
    current_keep_after_sequence: int = Field(ge=1)
    current_through_sequence: int = Field(ge=1)
    predecessor_completed_event_id: str | None
    transcript_projection_base_semantic_fingerprint: Fingerprint
    transcript_semantic_source_fingerprint: Fingerprint
    transcript_stable_state_semantic_fingerprint: Fingerprint
    selection_contract_fingerprint: Fingerprint
    window_attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _window(self) -> "CompactionHumanEvidenceSelectionWindowAttributionFact":
        if not (
            self.previous_keep_after_sequence < self.current_keep_after_sequence
            <= self.current_through_sequence
        ):
            raise ValueError("compaction human evidence window is invalid")
        return self


@_fact(
    "compaction_human_evidence_leaf_semantic.v1",
    "semantic_fingerprint",
    "compaction-human-evidence-leaf-semantic:v1",
)
class CompactionHumanEvidenceLeafSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_human_evidence_leaf_semantic.v1"] = (
        "compaction_human_evidence_leaf_semantic.v1"
    )
    source_kind: Literal["direct_human_input"] = "direct_human_input"
    message_provider_semantic_fingerprint: Fingerprint
    text_semantic_fingerprint: Fingerprint
    text_utf8_sha256: Sha256Hex
    text_utf8_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    semantic_fingerprint: Fingerprint


@_fact(
    "compaction_human_evidence_leaf_attribution.v1",
    "attribution_fingerprint",
    "compaction-human-evidence-leaf-attribution:v1",
)
class CompactionHumanEvidenceLeafAttributionFact(FrozenFactBase):
    schema_version: Literal["compaction_human_evidence_leaf_attribution.v1"] = (
        "compaction_human_evidence_leaf_attribution.v1"
    )
    leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    exact_run_start_event_reference: ContextEventReferenceFact
    message_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    reply_id: str = Field(min_length=1)
    source_sequence: int = Field(ge=1)
    leaf_semantic_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _reference_join(self) -> "CompactionHumanEvidenceLeafAttributionFact":
        ref = self.exact_run_start_event_reference
        if ref.sequence != self.source_sequence or ref.event_type != "RUN_START":
            raise ValueError("human evidence attribution must exact-join RunStart")
        return self


@_fact(
    "compaction_human_evidence_inline_selection_projection.v1",
    "selection_projection_fingerprint",
    "compaction-human-evidence-inline-selection-projection:v1",
)
class CompactionHumanEvidenceInlineSelectionProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_inline_selection_projection.v1"
    ] = "compaction_human_evidence_inline_selection_projection.v1"
    projection_kind: Literal["inline_full"] = "inline_full"
    source_leaf_semantic_fingerprint: Fingerprint
    sanitizer_contract_fingerprint: Fingerprint
    sanitized_full_text: str
    sanitized_full_text_sha256: Sha256Hex
    sanitized_full_text_utf8_bytes: int = Field(ge=0, le=8 * 1024)
    hard_size_disposition: Literal["selectable"] = "selectable"
    selection_projection_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content(self) -> "CompactionHumanEvidenceInlineSelectionProjectionFact":
        encoded = self.sanitized_full_text.encode("utf-8")
        from hashlib import sha256

        if len(encoded) != self.sanitized_full_text_utf8_bytes:
            raise ValueError("inline evidence byte count mismatch")
        if sha256(encoded).hexdigest() != self.sanitized_full_text_sha256:
            raise ValueError("inline evidence digest mismatch")
        return self


@_fact(
    "compaction_human_evidence_artifact_selection_projection.v1",
    "selection_projection_fingerprint",
    "compaction-human-evidence-artifact-selection-projection:v1",
)
class CompactionHumanEvidenceArtifactSelectionProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_artifact_selection_projection.v1"
    ] = "compaction_human_evidence_artifact_selection_projection.v1"
    projection_kind: Literal["artifact_full"] = "artifact_full"
    source_leaf_semantic_fingerprint: Fingerprint
    sanitizer_contract_fingerprint: Fingerprint
    sanitized_full_text_reference: ContentAddressedArtifactReferenceFact
    sanitized_full_text_sha256: Sha256Hex
    sanitized_full_text_utf8_bytes: int = Field(gt=8 * 1024, le=16 * 1024 * 1024)
    hard_size_disposition: Literal["permanently_oversize"] = "permanently_oversize"
    selection_projection_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _artifact_join(self) -> "CompactionHumanEvidenceArtifactSelectionProjectionFact":
        ref = self.sanitized_full_text_reference
        if (
            ref.content_sha256 != self.sanitized_full_text_sha256
            or ref.content_bytes != self.sanitized_full_text_utf8_bytes
        ):
            raise ValueError("artifact evidence content reference mismatch")
        return self


CompactionHumanEvidenceSelectionProjectionFact: TypeAlias = Annotated[
    CompactionHumanEvidenceInlineSelectionProjectionFact
    | CompactionHumanEvidenceArtifactSelectionProjectionFact,
    Field(discriminator="projection_kind"),
]


@_fact(
    "compaction_human_evidence_manifest_page.v1",
    "page_fingerprint",
    "compaction-human-evidence-manifest-page:v1",
)
class CompactionHumanEvidenceManifestPageFact(FrozenFactBase):
    schema_version: Literal["compaction_human_evidence_manifest_page.v1"] = (
        "compaction_human_evidence_manifest_page.v1"
    )
    page_index: int = Field(ge=0)
    ordered_leaf_semantics: tuple[CompactionHumanEvidenceLeafSemanticFact, ...] = (
        Field(max_length=256)
    )
    ordered_leaf_attributions: tuple[
        CompactionHumanEvidenceLeafAttributionFact, ...
    ] = Field(max_length=256)
    ordered_selection_projections: tuple[
        CompactionHumanEvidenceSelectionProjectionFact, ...
    ] = Field(max_length=256)
    first_source_sequence: int = Field(ge=1)
    last_source_sequence: int = Field(ge=1)
    semantic_accumulator: Fingerprint
    attribution_accumulator: Fingerprint
    selection_projection_accumulator: Fingerprint
    page_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _page(self) -> "CompactionHumanEvidenceManifestPageFact":
        count = len(self.ordered_leaf_semantics)
        if count == 0 or count != len(self.ordered_leaf_attributions) or count != len(
            self.ordered_selection_projections
        ):
            raise ValueError("manifest page columns must be non-empty and aligned")
        sequences = tuple(item.source_sequence for item in self.ordered_leaf_attributions)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("manifest page source sequences must be ordered/unique")
        if (self.first_source_sequence, self.last_source_sequence) != (
            sequences[0], sequences[-1]
        ):
            raise ValueError("manifest page source bounds mismatch")
        for semantic, attribution, projection in zip(
            self.ordered_leaf_semantics,
            self.ordered_leaf_attributions,
            self.ordered_selection_projections,
            strict=True,
        ):
            if (
                semantic.semantic_fingerprint != attribution.leaf_semantic_fingerprint
                or semantic.semantic_fingerprint
                != projection.source_leaf_semantic_fingerprint
            ):
                raise ValueError("manifest page leaf columns do not join")
        expected_accumulators = (
            _ordered_accumulator(
                "compaction-human-evidence-page-semantic:v1",
                tuple(item.semantic_fingerprint for item in self.ordered_leaf_semantics),
            ),
            _ordered_accumulator(
                "compaction-human-evidence-page-attribution:v1",
                tuple(
                    item.attribution_fingerprint
                    for item in self.ordered_leaf_attributions
                ),
            ),
            _ordered_accumulator(
                "compaction-human-evidence-page-selection:v1",
                tuple(
                    item.selection_projection_fingerprint
                    for item in self.ordered_selection_projections
                ),
            ),
        )
        if expected_accumulators != (
            self.semantic_accumulator,
            self.attribution_accumulator,
            self.selection_projection_accumulator,
        ):
            raise ValueError("manifest page accumulator mismatch")
        return self


@_fact(
    "compaction_human_evidence_manifest_root.v1",
    "root_fingerprint",
    "compaction-human-evidence-manifest-root:v1",
)
class CompactionHumanEvidenceManifestRootFact(FrozenFactBase):
    schema_version: Literal["compaction_human_evidence_manifest_root.v1"] = (
        "compaction_human_evidence_manifest_root.v1"
    )
    ordered_page_references: tuple[ContentAddressedArtifactReferenceFact, ...]
    page_count: int = Field(ge=0)
    eligible_leaf_count: int = Field(ge=0)
    ordered_semantic_accumulator: Fingerprint
    transitive_leaf_coverage_fingerprint: Fingerprint
    source_selection_contract_fingerprint: Fingerprint
    ordered_attribution_accumulator: Fingerprint
    ordered_selection_projection_accumulator: Fingerprint
    first_source_sequence: int | None = Field(default=None, ge=1)
    last_source_sequence: int | None = Field(default=None, ge=1)
    transcript_cursor_fingerprint: Fingerprint
    runtime_session_id: str = Field(min_length=1)
    selection_window_attribution: CompactionHumanEvidenceSelectionWindowAttributionFact
    transcript_cursor_generation: int = Field(ge=0)
    verified_through_sequence: int = Field(ge=1)
    ledger_continuity_accumulator: Fingerprint
    domain_completeness_proof_fingerprint: Fingerprint
    selection_projection_contract_fingerprint: Fingerprint
    root_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root(self) -> "CompactionHumanEvidenceManifestRootFact":
        if self.page_count != len(self.ordered_page_references):
            raise ValueError("manifest root page count mismatch")
        if (
            self.source_selection_contract_fingerprint
            != self.selection_window_attribution.selection_contract_fingerprint
            or self.verified_through_sequence
            < self.selection_window_attribution.current_keep_after_sequence
        ):
            raise ValueError("manifest root semantic/attribution join mismatch")
        empty = self.eligible_leaf_count == 0
        if empty != (self.page_count == 0):
            raise ValueError("manifest root empty page matrix mismatch")
        if empty != (self.first_source_sequence is None):
            raise ValueError("manifest root first sequence matrix mismatch")
        if empty != (self.last_source_sequence is None):
            raise ValueError("manifest root last sequence matrix mismatch")
        return self


@_fact(
    "compaction_human_evidence_manifest_semantic.v1",
    "manifest_semantic_fingerprint",
    "compaction-human-evidence-manifest-semantic:v1",
)
class CompactionHumanEvidenceManifestSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_human_evidence_manifest_semantic.v1"] = (
        "compaction_human_evidence_manifest_semantic.v1"
    )
    eligible_leaf_count: int = Field(ge=0)
    ordered_semantic_accumulator: Fingerprint
    transitive_leaf_coverage_fingerprint: Fingerprint
    selection_contract_fingerprint: Fingerprint
    manifest_semantic_fingerprint: Fingerprint


@_fact(
    "compaction_human_evidence_manifest_attribution.v1",
    "attribution_fingerprint",
    "compaction-human-evidence-manifest-attribution:v1",
)
class CompactionHumanEvidenceManifestAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_manifest_attribution.v1"
    ] = "compaction_human_evidence_manifest_attribution.v1"
    manifest_semantic_fingerprint: Fingerprint
    runtime_session_id: str = Field(min_length=1)
    selection_window_attribution: CompactionHumanEvidenceSelectionWindowAttributionFact
    transcript_cursor_fingerprint: Fingerprint
    transcript_cursor_generation: int = Field(ge=0)
    verified_through_sequence: int = Field(ge=1)
    ledger_continuity_accumulator: Fingerprint
    domain_completeness_proof_fingerprint: Fingerprint
    ordered_leaf_attribution_accumulator: Fingerprint
    ordered_selection_projection_accumulator: Fingerprint
    selection_projection_contract_fingerprint: Fingerprint
    paged_manifest_root_reference: ContentAddressedArtifactReferenceFact
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _horizon(self) -> "CompactionHumanEvidenceManifestAttributionFact":
        if self.verified_through_sequence < self.selection_window_attribution.current_keep_after_sequence:
            raise ValueError("manifest verification horizon is behind selection window")
        return self


@_fact(
    "compaction_human_evidence_manifest_reference.v1",
    "reference_fingerprint",
    "compaction-human-evidence-manifest-reference:v1",
)
class CompactionHumanEvidenceManifestReferenceFact(FrozenFactBase):
    schema_version: Literal["compaction_human_evidence_manifest_reference.v1"] = (
        "compaction_human_evidence_manifest_reference.v1"
    )
    manifest_semantic_fingerprint: Fingerprint
    manifest_attribution_fingerprint: Fingerprint
    paged_manifest_root_reference: ContentAddressedArtifactReferenceFact
    reference_fingerprint: Fingerprint


@_fact(
    "compaction_post_completion_extension_contract.v1",
    "contract_fingerprint",
    "compaction-post-completion-extension-contract:v1",
)
class CompactionPostCompletionExtensionContractFact(FrozenFactBase):
    schema_version: Literal["compaction_post_completion_extension_contract.v1"] = (
        "compaction_post_completion_extension_contract.v1"
    )
    extension_id: str = Field(min_length=1)
    extension_version: str = Field(min_length=1)
    request_event_type: str = Field(min_length=1)
    request_event_schema_fingerprint: Fingerprint
    source_manifest_contract_fingerprint: Fingerprint
    admission_policy_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


@_fact(
    "compaction_post_completion_extension_link.v1",
    "extension_link_id",
    "compaction-post-completion-extension-link:v1",
)
class CompactionPostCompletionExtensionLinkFact(FrozenFactBase):
    schema_version: Literal["compaction_post_completion_extension_link.v1"] = (
        "compaction_post_completion_extension_link.v1"
    )
    compaction_id: str = Field(min_length=1)
    completed_event_id: str = Field(min_length=1)
    request_event_id: str = Field(min_length=1)
    extension_contract_fingerprint: Fingerprint
    extension_link_id: Fingerprint


@_fact(
    "compaction_post_completion_extension_requested.v1",
    "disposition_fingerprint",
    "compaction-post-completion-extension-requested:v1",
)
class CompactionPostCompletionExtensionRequestedFact(FrozenFactBase):
    schema_version: Literal["compaction_post_completion_extension_requested.v1"] = (
        "compaction_post_completion_extension_requested.v1"
    )
    disposition_kind: Literal["requested"] = "requested"
    extension_link: CompactionPostCompletionExtensionLinkFact
    disposition_fingerprint: Fingerprint


ExtensionAdmissionFailureStage: TypeAlias = Literal[
    "intent_factory",
    "target_resolution",
    "manifest_prepare",
    "manifest_not_ready_at_completion",
    "manifest_abandoned",
    "request_factory",
]


@_fact(
    "compaction_post_completion_extension_admission_failed.v1",
    "disposition_fingerprint",
    "compaction-post-completion-extension-admission-failed:v1",
)
class CompactionPostCompletionExtensionAdmissionFailedFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_post_completion_extension_admission_failed.v1"
    ] = "compaction_post_completion_extension_admission_failed.v1"
    disposition_kind: Literal["admission_failed"] = "admission_failed"
    extension_contract_fingerprint: Fingerprint
    failure_stage: ExtensionAdmissionFailureStage
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    disposition_fingerprint: Fingerprint


CompactionPostCompletionExtensionDispositionFact: TypeAlias = Annotated[
    CompactionPostCompletionExtensionRequestedFact
    | CompactionPostCompletionExtensionAdmissionFailedFact,
    Field(discriminator="disposition_kind"),
]


@_fact(
    "compaction_memory_extraction_contract.v1",
    "contract_fingerprint",
    "compaction-memory-extraction-contract:v1",
)
class CompactionMemoryExtractionContractFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_extraction_contract.v1"] = (
        "compaction_memory_extraction_contract.v1"
    )
    extractor_id: Literal["pulsara.compaction-memory-extraction"] = (
        "pulsara.compaction-memory-extraction"
    )
    extractor_version: Literal["1"] = "1"
    accepted_source_kind: Literal["direct_human_input_only"] = (
        "direct_human_input_only"
    )
    output_candidate_kinds: tuple[Literal["Preference"], ...]
    input_document_schema_fingerprint: Fingerprint
    output_document_schema_fingerprint: Fingerprint
    evidence_selection_contract_fingerprint: Fingerprint
    sanitizer_contract_fingerprint: Fingerprint
    parser_contract_fingerprint: Fingerprint
    normalization_contract_fingerprint: Fingerprint
    candidate_identity_contract_fingerprint: Fingerprint
    maximum_evidence_nodes: int = Field(ge=1, le=256)
    maximum_input_utf8_bytes: int = Field(ge=1, le=512 * 1024)
    maximum_output_utf8_bytes: int = Field(ge=1, le=64 * 1024)
    maximum_candidates: int = Field(ge=1, le=3)
    maximum_evidence_refs_per_candidate: int = Field(ge=1, le=8)
    maximum_statement_utf8_bytes: int = Field(ge=1, le=1000)
    contract_fingerprint: Fingerprint


@_fact(
    "compaction_memory_extraction_policy.v1",
    "policy_fingerprint",
    "compaction-memory-extraction-policy:v1",
)
class CompactionMemoryExtractionPolicyFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_extraction_policy.v1"] = (
        "compaction_memory_extraction_policy.v1"
    )
    enabled: bool
    allowed_triggers: tuple[Literal["manual", "auto"], ...]
    allowed_phases: tuple[
        Literal["pre_run", "mid_turn", "manual", "window_maintenance"], ...
    ]
    model_target: ResolvedModelTargetFact
    maximum_attempts: int = Field(ge=1, le=3)
    provider_timeout_seconds: int = Field(ge=1, le=120)
    lease_duration_seconds: int = Field(ge=61, le=3600)
    retry_policy_fingerprint: Fingerprint
    input_budget_policy_fingerprint: Fingerprint
    background_work_budget_policy_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _policy(self) -> "CompactionMemoryExtractionPolicyFact":
        if tuple(sorted(set(self.allowed_triggers))) != self.allowed_triggers:
            raise ValueError("extraction triggers must be sorted and unique")
        if tuple(sorted(set(self.allowed_phases))) != self.allowed_phases:
            raise ValueError("extraction phases must be sorted and unique")
        if self.lease_duration_seconds < self.provider_timeout_seconds + 60:
            raise ValueError("extraction lease lacks terminal settlement reserve")
        return self


@_fact(
    "background_derived_work_budget_policy.v1",
    "policy_fingerprint",
    "background-derived-work-budget-policy:v1",
)
class BackgroundDerivedWorkBudgetPolicyFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_policy.v1"] = (
        "background_derived_work_budget_policy.v1"
    )
    policy_id: str = Field(min_length=1)
    maximum_dispatched_calls_per_session: int = Field(ge=1)
    maximum_physical_input_tokens_per_session: int = Field(ge=1)
    maximum_output_tokens_per_session: int = Field(ge=1)
    maximum_milliunits_per_session: int = Field(ge=1)
    pricing_contract_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint


@_fact(
    "background_derived_work_budget_account.v1",
    "account_fingerprint",
    "background-derived-work-budget-account:v1",
)
class BackgroundDerivedWorkBudgetAccountFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_account.v1"] = (
        "background_derived_work_budget_account.v1"
    )
    runtime_session_id: str = Field(min_length=1)
    policy_fingerprint: Fingerprint
    account_revision: int = Field(ge=0)
    account_status: Literal["active", "reconciliation_required"]
    dispatched_call_count: int = Field(ge=0)
    settled_call_count: int = Field(ge=0)
    open_reservation_count: int = Field(ge=0)
    open_reserved_input_tokens: int = Field(ge=0)
    open_reserved_output_tokens: int = Field(ge=0)
    open_reserved_milliunits: int = Field(ge=0)
    settled_charged_input_tokens: int = Field(ge=0)
    settled_charged_output_tokens: int = Field(ge=0)
    settled_charged_milliunits: int = Field(ge=0)
    account_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _recurrence(self) -> "BackgroundDerivedWorkBudgetAccountFact":
        if self.dispatched_call_count != self.settled_call_count + self.open_reservation_count:
            raise ValueError("background budget account count recurrence mismatch")
        return self


@_fact(
    "background_derived_work_budget_reservation.v1",
    "reservation_fingerprint",
    "background-derived-work-budget-reservation:v1",
)
class BackgroundDerivedWorkBudgetReservationFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_reservation.v1"] = (
        "background_derived_work_budget_reservation.v1"
    )
    reservation_id: str = Field(min_length=1)
    runtime_session_id: str = Field(min_length=1)
    extraction_job_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    dispatch_attempt_ordinal: int = Field(ge=1)
    model_call_reservation_quote: ModelCallReservationQuoteFact
    source_account_revision: int = Field(ge=0)
    reservation_fingerprint: Fingerprint


@_fact(
    "background_derived_work_budget_settlement.v1",
    "settlement_fingerprint",
    "background-derived-work-budget-settlement:v1",
)
class BackgroundDerivedWorkBudgetSettlementFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_settlement.v1"] = (
        "background_derived_work_budget_settlement.v1"
    )
    reservation_fingerprint: Fingerprint
    model_call_end_event_id: str = Field(min_length=1)
    accounting_basis: Literal[
        "provider_reported_usage",
        "not_started_zero",
        "reserved_missing_usage",
        "cancelled_reserved",
    ]
    charged_input_tokens: int = Field(ge=0)
    charged_output_tokens: int = Field(ge=0)
    charged_milliunits: int = Field(ge=0)
    usage_charge_fingerprint: Fingerprint
    source_account_revision: int = Field(ge=0)
    resulting_account_revision: int = Field(ge=1)
    settlement_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _revision(self) -> "BackgroundDerivedWorkBudgetSettlementFact":
        if self.resulting_account_revision != self.source_account_revision + 1:
            raise ValueError("background budget settlement revision mismatch")
        if self.accounting_basis == "not_started_zero" and any(
            (self.charged_input_tokens, self.charged_output_tokens, self.charged_milliunits)
        ):
            raise ValueError("not-started settlement must charge zero")
        return self


@_fact(
    "background_derived_work_budget_account_ref.v1",
    "reference_fingerprint",
    "background-derived-work-budget-account-reference:v1",
)
class BackgroundDerivedWorkBudgetAccountReferenceFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_account_ref.v1"] = (
        "background_derived_work_budget_account_ref.v1"
    )
    runtime_session_id: str = Field(min_length=1)
    account_revision: int = Field(ge=0)
    account_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


@_fact(
    "background_derived_work_budget_admission_failure.v1",
    "failure_fingerprint",
    "background-derived-work-budget-admission-failure:v1",
)
class BackgroundDerivedWorkBudgetAdmissionFailureFact(FrozenFactBase):
    schema_version: Literal[
        "background_derived_work_budget_admission_failure.v1"
    ] = "background_derived_work_budget_admission_failure.v1"
    failure_kind: Literal[
        "call_cap_exhausted",
        "input_token_cap_exhausted",
        "output_token_cap_exhausted",
        "milliunit_cap_exhausted",
        "account_reconciliation_required",
    ]
    source_account_reference: BackgroundDerivedWorkBudgetAccountReferenceFact
    rejected_quote_fact_fingerprint: Fingerprint
    failure_fingerprint: Fingerprint


EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-input-budget-selection:v1",
    {
        "whole_message_only": True,
        "target_aware": True,
        "maximum_bytes": 512 * 1024,
    },
)


DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY_ID = (
    "pulsara.compaction-memory-extraction.background-budget.v1"
)
DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY = build_frozen_fact(
    BackgroundDerivedWorkBudgetPolicyFact,
    schema_version="background_derived_work_budget_policy.v1",
    policy_id=DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY_ID,
    maximum_dispatched_calls_per_session=16,
    maximum_physical_input_tokens_per_session=2_097_152,
    maximum_output_tokens_per_session=131_072,
    maximum_milliunits_per_session=4_000_000_000,
    pricing_contract_fingerprint=default_rollout_budget_policy().policy_fingerprint,
)


def build_background_budget_genesis(
    *,
    runtime_session_id: str,
    policy: BackgroundDerivedWorkBudgetPolicyFact = (
        DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY
    ),
) -> BackgroundDerivedWorkBudgetAccountFact:
    return build_frozen_fact(
        BackgroundDerivedWorkBudgetAccountFact,
        schema_version="background_derived_work_budget_account.v1",
        runtime_session_id=runtime_session_id,
        policy_fingerprint=policy.policy_fingerprint,
        account_revision=0,
        account_status="active",
        dispatched_call_count=0,
        settled_call_count=0,
        open_reservation_count=0,
        open_reserved_input_tokens=0,
        open_reserved_output_tokens=0,
        open_reserved_milliunits=0,
        settled_charged_input_tokens=0,
        settled_charged_output_tokens=0,
        settled_charged_milliunits=0,
    )


@_fact(
    "compaction_memory_evidence_redaction_audit.v1",
    "audit_fingerprint",
    "compaction-memory-evidence-redaction-audit:v1",
)
class CompactionMemoryEvidenceRedactionAuditFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_redaction_audit.v1"] = (
        "compaction_memory_evidence_redaction_audit.v1"
    )
    redaction_ordinal: int = Field(ge=0)
    sanitizer_rule_id: str = Field(min_length=1)
    sanitizer_rule_version: str = Field(min_length=1)
    replacement_text: str
    sanitized_start_char: int = Field(ge=0)
    sanitized_end_char: int = Field(ge=0)
    audit_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _span(self) -> "CompactionMemoryEvidenceRedactionAuditFact":
        if self.sanitized_end_char < self.sanitized_start_char:
            raise ValueError("sanitized redaction span is invalid")
        return self


@_fact(
    "compaction_memory_evidence_semantic.v1",
    "evidence_semantic_fingerprint",
    "compaction-memory-evidence-semantic:v1",
)
class CompactionMemoryEvidenceSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_semantic.v1"] = (
        "compaction_memory_evidence_semantic.v1"
    )
    source_kind: Literal["direct_human_input"] = "direct_human_input"
    sanitized_full_message_text: str
    sanitized_full_message_sha256: Sha256Hex
    sanitized_full_message_utf8_bytes: int = Field(ge=0, le=8 * 1024)
    sanitizer_contract_fingerprint: Fingerprint
    evidence_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _text(self) -> "CompactionMemoryEvidenceSemanticFact":
        from hashlib import sha256

        encoded = self.sanitized_full_message_text.encode("utf-8")
        if len(encoded) != self.sanitized_full_message_utf8_bytes:
            raise ValueError("sanitized evidence byte count mismatch")
        if sha256(encoded).hexdigest() != self.sanitized_full_message_sha256:
            raise ValueError("sanitized evidence digest mismatch")
        return self


@_fact(
    "compaction_memory_evidence_input_projection.v1",
    "projection_fingerprint",
    "compaction-memory-evidence-input-projection:v1",
)
class CompactionMemoryEvidenceInputProjectionFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_input_projection.v1"] = (
        "compaction_memory_evidence_input_projection.v1"
    )
    projection_kind: Literal["full"] = "full"
    evidence_semantic_fingerprint: Fingerprint
    projected_text: str
    projected_text_sha256: Sha256Hex
    projected_text_utf8_bytes: int = Field(ge=0, le=8 * 1024)
    projection_contract_fingerprint: Fingerprint
    projection_fingerprint: Fingerprint


@_fact(
    "compaction_memory_evidence_attribution.v1",
    "attribution_fingerprint",
    "compaction-memory-evidence-attribution:v1",
)
class CompactionMemoryEvidenceAttributionFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_attribution.v1"] = (
        "compaction_memory_evidence_attribution.v1"
    )
    evidence_semantic_fingerprint: Fingerprint
    source_event_reference: GovernanceStoredEventReferenceFact
    source_run_id: str = Field(min_length=1)
    source_turn_id: str = Field(min_length=1)
    source_reply_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    original_text_sha256: Sha256Hex
    original_text_utf8_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    source_wire_semantic_fingerprint: Fingerprint
    ordered_redaction_audits: tuple[CompactionMemoryEvidenceRedactionAuditFact, ...]
    attribution_fingerprint: Fingerprint


@_fact(
    "compaction_memory_evidence_node.v1",
    "node_fingerprint",
    "compaction-memory-evidence-node:v1",
)
class CompactionMemoryEvidenceNodeFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_node.v1"] = (
        "compaction_memory_evidence_node.v1"
    )
    evidence_node_id: str = Field(min_length=1)
    semantic: CompactionMemoryEvidenceSemanticFact
    input_projection: CompactionMemoryEvidenceInputProjectionFact
    attribution: CompactionMemoryEvidenceAttributionFact
    node_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "CompactionMemoryEvidenceNodeFact":
        semantic = self.semantic.evidence_semantic_fingerprint
        if (
            self.input_projection.evidence_semantic_fingerprint != semantic
            or self.attribution.evidence_semantic_fingerprint != semantic
        ):
            raise ValueError("evidence node semantic join mismatch")
        if (
            self.input_projection.projected_text != self.semantic.sanitized_full_message_text
            or self.input_projection.projected_text_sha256
            != self.semantic.sanitized_full_message_sha256
            or self.input_projection.projected_text_utf8_bytes
            != self.semantic.sanitized_full_message_utf8_bytes
        ):
            raise ValueError("V1 evidence input projection must be the full message")
        return self


@_fact(
    "compaction_memory_evidence_set_semantic.v1",
    "evidence_set_semantic_fingerprint",
    "compaction-memory-evidence-set-semantic:v1",
)
class CompactionMemoryEvidenceSetSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_set_semantic.v1"] = (
        "compaction_memory_evidence_set_semantic.v1"
    )
    ordered_evidence_semantics: tuple[CompactionMemoryEvidenceSemanticFact, ...]
    ordered_input_projection_fingerprints: tuple[Fingerprint, ...]
    evidence_count: int = Field(ge=0, le=256)
    ordered_evidence_semantic_accumulator: Fingerprint
    selection_contract_fingerprint: Fingerprint
    sanitizer_contract_fingerprint: Fingerprint
    input_projection_contract_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _count(self) -> "CompactionMemoryEvidenceSetSemanticFact":
        if self.evidence_count != len(self.ordered_evidence_semantics):
            raise ValueError("evidence set count mismatch")
        if len(self.ordered_input_projection_fingerprints) != self.evidence_count:
            raise ValueError("evidence projection count mismatch")
        accumulator = _ordered_accumulator(
            "compaction-memory-evidence-set-semantic:v1",
            tuple(
                semantic.evidence_semantic_fingerprint
                for semantic in self.ordered_evidence_semantics
            ),
        )
        if accumulator != self.ordered_evidence_semantic_accumulator:
            raise ValueError("evidence semantic accumulator mismatch")
        return self


CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET = build_frozen_fact(
    CompactionMemoryEvidenceSetSemanticFact,
    schema_version="compaction_memory_evidence_set_semantic.v1",
    ordered_evidence_semantics=(),
    ordered_input_projection_fingerprints=(),
    evidence_count=0,
    ordered_evidence_semantic_accumulator=_ordered_accumulator(
        "compaction-memory-evidence-set-semantic:v1",
        (),
    ),
    selection_contract_fingerprint=(
        COMPACTION_MEMORY_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT
    ),
    sanitizer_contract_fingerprint=(
        COMPACTION_MEMORY_EVIDENCE_SANITIZER_CONTRACT_FINGERPRINT
    ),
    input_projection_contract_fingerprint=(
        COMPACTION_MEMORY_INPUT_PROJECTION_CONTRACT_FINGERPRINT
    ),
)
CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT = (
    CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET.evidence_set_semantic_fingerprint
)


@_fact(
    "resolved_extraction_input_budget_attribution.v1",
    "attribution_fingerprint",
    "resolved-extraction-input-budget-attribution:v1",
)
class ResolvedExtractionInputBudgetAttributionFact(FrozenFactBase):
    schema_version: Literal["resolved_extraction_input_budget_attribution.v1"] = (
        "resolved_extraction_input_budget_attribution.v1"
    )
    resolved_model_target_fingerprint: Fingerprint
    target_input_limit_tokens: int = Field(ge=1)
    static_prompt_tokens: int = Field(ge=0)
    carrier_and_framing_reserve_tokens: int = Field(ge=0)
    output_reserve_tokens: int = Field(ge=1)
    safety_margin_tokens: int = Field(ge=0)
    usable_evidence_tokens: int = Field(ge=0)
    maximum_physical_input_utf8_bytes: int = Field(ge=1, le=512 * 1024)
    token_estimator_contract_fingerprint: Fingerprint
    budget_selection_contract_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _budget(self) -> "ResolvedExtractionInputBudgetAttributionFact":
        expected = max(0, self.target_input_limit_tokens - (
            self.static_prompt_tokens
            + self.carrier_and_framing_reserve_tokens
            + self.output_reserve_tokens
            + self.safety_margin_tokens
        ))
        if self.usable_evidence_tokens != expected:
            raise ValueError("resolved extraction input budget recurrence mismatch")
        return self


@_fact(
    "compaction_memory_input_budget_failure.v1",
    "failure_fingerprint",
    "compaction-memory-input-budget-failure:v1",
)
class CompactionMemoryInputBudgetFailureFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_input_budget_failure.v1"] = (
        "compaction_memory_input_budget_failure.v1"
    )
    failure_kind: Literal[
        "prompt_and_reserves_exceed_target", "no_complete_evidence_message_fits"
    ]
    resolved_budget_attribution_fingerprint: Fingerprint
    failure_fingerprint: Fingerprint


@_fact(
    "compaction_memory_extraction_input_semantic.v1",
    "input_semantic_fingerprint",
    "compaction-memory-extraction-input-semantic:v1",
)
class CompactionMemoryExtractionInputSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_extraction_input_semantic.v1"] = (
        "compaction_memory_extraction_input_semantic.v1"
    )
    evidence_set: CompactionMemoryEvidenceSetSemanticFact
    prompt_contract_fingerprint: Fingerprint
    input_codec_contract_fingerprint: Fingerprint
    extraction_contract_fingerprint: Fingerprint
    input_semantic_fingerprint: Fingerprint


@_fact(
    "compaction_memory_extraction_input_attribution.v1",
    "attribution_fingerprint",
    "compaction-memory-extraction-input-attribution:v1",
)
class CompactionMemoryExtractionInputAttributionFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_extraction_input_attribution.v1"] = (
        "compaction_memory_extraction_input_attribution.v1"
    )
    compaction_id: str = Field(min_length=1)
    extension_link: CompactionPostCompletionExtensionLinkFact
    request_event_reference: GovernanceStoredEventReferenceFact
    durable_job_id: str = Field(min_length=1)
    durable_job_source_reference_fingerprint: Fingerprint
    human_evidence_manifest_reference: CompactionHumanEvidenceManifestReferenceFact
    ordered_evidence_attributions: tuple[CompactionMemoryEvidenceAttributionFact, ...]
    resolved_input_budget_attribution: ResolvedExtractionInputBudgetAttributionFact
    permanent_omission_count: int = Field(ge=0)
    permanent_omission_semantic_accumulator: Fingerprint
    permanent_omission_attribution_accumulator: Fingerprint
    attribution_fingerprint: Fingerprint


@_fact(
    "compaction_memory_extraction_input_document.v1",
    "document_fingerprint",
    "compaction-memory-extraction-input-document:v1",
)
class CompactionMemoryExtractionInputDocumentFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_extraction_input_document.v1"] = (
        "compaction_memory_extraction_input_document.v1"
    )
    semantic: CompactionMemoryExtractionInputSemanticFact
    attribution: CompactionMemoryExtractionInputAttributionFact
    document_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _evidence_join(self) -> "CompactionMemoryExtractionInputDocumentFact":
        semantics = self.semantic.evidence_set.ordered_evidence_semantics
        attributions = self.attribution.ordered_evidence_attributions
        if len(semantics) != len(attributions):
            raise ValueError("extraction input evidence cardinality mismatch")
        for semantic, attribution in zip(semantics, attributions, strict=True):
            if (
                semantic.evidence_semantic_fingerprint
                != attribution.evidence_semantic_fingerprint
            ):
                raise ValueError("extraction input semantic/attribution join mismatch")
        sequences = tuple(item.source_event_reference.sequence for item in attributions)
        if sequences != tuple(sorted(sequences)) or len(set(sequences)) != len(
            sequences
        ):
            raise ValueError("extraction evidence source order is invalid")
        return self


@_fact(
    "compaction_memory_extraction_model_input_attribution.v1",
    "attribution_fingerprint",
    "compaction-memory-extraction-model-input-attribution:v1",
)
class CompactionMemoryExtractionModelInputAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_model_input_attribution.v1"
    ] = "compaction_memory_extraction_model_input_attribution.v1"
    extraction_job_id: str = Field(min_length=1)
    dispatch_attempt_ordinal: int = Field(ge=1)
    request_event_reference: GovernanceStoredEventReferenceFact
    input_artifact_reference: GovernanceEvidenceArtifactReferenceFact
    input_semantic_fingerprint: Fingerprint
    input_document_fingerprint: Fingerprint
    resolved_input_budget_attribution_fingerprint: Fingerprint
    background_budget_reservation: BackgroundDerivedWorkBudgetReservationFact
    extraction_contract_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


@_fact(
    "compaction_memory_preference_candidate_payload.v1",
    "payload_fingerprint",
    "compaction-memory-preference-candidate-payload:v1",
)
class CompactionMemoryPreferenceCandidatePayloadFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_preference_candidate_payload.v1"] = (
        "compaction_memory_preference_candidate_payload.v1"
    )
    kind: Literal["Preference"] = "Preference"
    candidate_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    source_authority: Literal["conversation_evidence"] = "conversation_evidence"
    verification_status: Literal["inferred"] = "inferred"
    candidate_semantic: MemoryCandidateSemanticFact
    payload_fingerprint: Fingerprint

    @property
    def candidate_semantic_fingerprint(self) -> str:
        return self.candidate_semantic.semantic_fingerprint

    @model_validator(mode="after")
    def _bounded(self) -> "CompactionMemoryPreferenceCandidatePayloadFact":
        if len(self.statement.encode("utf-8")) > 1000:
            raise ValueError("compaction memory preference statement is oversized")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("candidate evidence IDs must be unique")
        expected_semantic = build_memory_candidate_semantic(
            kind=self.kind,
            scope=self.scope,
            statement=self.statement,
        )
        if self.candidate_semantic != expected_semantic:
            raise ValueError("compaction memory preference semantic identity drifted")
        return self


@_fact(
    "compaction_memory_extraction_candidate_attribution.v1",
    "attribution_fingerprint",
    "compaction-memory-extraction-candidate-attribution:v1",
)
class CompactionMemoryExtractionCandidateAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_candidate_attribution.v1"
    ] = "compaction_memory_extraction_candidate_attribution.v1"
    candidate_entry_id: str = Field(min_length=1)
    candidate_ordinal: int = Field(ge=0, le=2)
    candidate_payload: CompactionMemoryPreferenceCandidatePayloadFact
    candidate_occurrence_fingerprint: Fingerprint
    candidate_created_at_utc: str = Field(min_length=1)
    ordered_evidence_node_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    ordered_evidence_semantic_fingerprints: tuple[Fingerprint, ...] = Field(
        min_length=1, max_length=8
    )
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _candidate(self) -> "CompactionMemoryExtractionCandidateAttributionFact":
        if self.candidate_payload.evidence_ids != self.ordered_evidence_node_ids:
            raise ValueError("candidate payload/evidence attribution mismatch")
        if len(self.ordered_evidence_node_ids) != len(
            self.ordered_evidence_semantic_fingerprints
        ):
            raise ValueError("candidate evidence semantic count mismatch")
        occurrence_suffix = self.candidate_occurrence_fingerprint.removeprefix(
            "sha256:"
        )
        if (
            self.candidate_payload.candidate_id
            != f"candidate:compaction-memory:{occurrence_suffix}"
            or self.candidate_entry_id
            != f"pool:compaction-memory:{occurrence_suffix}"
        ):
            raise ValueError("candidate occurrence identity drifted")
        return self


__all__ = [name for name in globals() if name.endswith(("Fact", "Stage"))]
