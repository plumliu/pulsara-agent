"""Immutable, event-safe input contracts for the context compiler.

This module is intentionally below runtime, message, MCP, provider, and event
schema layers.  It contains only serializable facts and pure validators.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import ConfigDict, Field, field_validator, model_validator

from pulsara_agent.primitives._context_base import (
    CapabilityDescriptorRenderAttributionFact,
    ContextEventRangeFact,
    ContextEventReferenceFact,
    FrozenContextFact,
    FrozenJsonArrayFact,
    FrozenJsonEntryFact,
    FrozenJsonObjectFact,
    FrozenJsonScalar,
    FrozenJsonValue,
    canonical_json_bytes,
    canonical_utc_timestamp,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.primitives.model_call import ResolvedModelCallFact
from pulsara_agent.primitives.long_horizon import (
    ContextWindowFact,
    ContextWindowProjectionState,
    LongHorizonContextBudgetDecisionFact,
    LongHorizonContextAllocationPolicyFact,
    LongHorizonProjectionPressureShadowFact,
    PreparedObservationRollupUnit,
    ProjectionTargetUnreachableAuditFact,
    RolloutBudgetStateFact,
    RolloutPhase,
    SubagentGraphAccelerationFact,
    SubagentGraphSemanticSourceFact,
    ToolObservationRepresentation,
)
from pulsara_agent.primitives.permission import (
    PermissionMode,
    preset_permission_payload,
)
from pulsara_agent.primitives.run_boundary import (
    InteractionResumeBoundaryFact,
    NewRunBoundaryFact,
    RunEntryFact,
)
from pulsara_agent.primitives.run_entry import (
    CurrentUserMessageFact,
    SubagentRunEntryFact,
)


_TRUSTED_MANIFEST_FINGERPRINT: ContextVar[str | None] = ContextVar(
    "trusted_context_input_manifest_fingerprint",
    default=None,
)


class ContextChannelFact(StrEnum):
    SYSTEM = "system"
    LEADING_USER = "leading_user"
    HANDOFF_HINT = "handoff_hint"
    TRAILING_STATUS = "trailing_status"


ContextCandidateSourceKind: TypeAlias = Literal[
    "system",
    "runtime_context",
    "memory_projection",
    "capability_catalog",
    "capability_active_skill",
    "plan",
    "recovery",
    "subagent_results",
    "rollout_status",
]
ContextCandidateLoweringKind: TypeAlias = Literal[
    "system_instruction",
    "leading_user_context",
    "handoff_hint",
    "trailing_status",
]


_CONTEXT_CANDIDATE_CHANNEL_MATRIX = {
    "system": (ContextChannelFact.SYSTEM, "system_instruction"),
    "runtime_context": (ContextChannelFact.LEADING_USER, "leading_user_context"),
    "memory_projection": (ContextChannelFact.LEADING_USER, "leading_user_context"),
    "capability_catalog": (ContextChannelFact.LEADING_USER, "leading_user_context"),
    "capability_active_skill": (ContextChannelFact.SYSTEM, "system_instruction"),
    "plan": (ContextChannelFact.LEADING_USER, "leading_user_context"),
    "recovery": (ContextChannelFact.HANDOFF_HINT, "handoff_hint"),
    "subagent_results": (ContextChannelFact.LEADING_USER, "leading_user_context"),
    "rollout_status": (ContextChannelFact.TRAILING_STATUS, "trailing_status"),
}


class ToolArgumentsParseErrorCode(StrEnum):
    INVALID_JSON_SYNTAX = "invalid_json_syntax"
    JSON_ROOT_NOT_OBJECT = "json_root_not_object"


class ContextRunEntryReferenceFact(FrozenContextFact):
    run_entry_kind: Literal["host", "subagent"]
    run_start: ContextEventReferenceFact
    stable_terminal_event_id: str = Field(min_length=1)
    run_entry: RunEntryFact

    @model_validator(mode="after")
    def _entry_kind(self) -> "ContextRunEntryReferenceFact":
        if self.run_entry_kind == "host" and not isinstance(
            self.run_entry, NewRunBoundaryFact
        ):
            raise ValueError("host context entry requires NewRunBoundaryFact")
        if self.run_entry_kind == "subagent" and not isinstance(
            self.run_entry, SubagentRunEntryFact
        ):
            raise ValueError("subagent context entry requires SubagentRunEntryFact")
        return self


class ContextContinuationReferenceFact(FrozenContextFact):
    resume_boundary: ContextEventReferenceFact
    boundary: InteractionResumeBoundaryFact
    suspended_run_id: str = Field(min_length=1)
    suspended_state_token_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _attribution(self) -> "ContextContinuationReferenceFact":
        if (
            self.suspended_state_token_fingerprint
            != self.boundary.suspended_state_token_fingerprint
        ):
            raise ValueError("continuation suspended token fingerprint mismatch")
        if self.resume_boundary.event_id != self.boundary.identity.boundary_id:
            raise ValueError("continuation boundary event identity mismatch")
        if self.suspended_run_id != self.boundary.identity.run_id:
            raise ValueError("continuation run mismatch")
        return self


class RunPermissionSnapshotFact(FrozenContextFact):
    snapshot_id: str = Field(min_length=1)
    runtime_session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    mode: PermissionMode
    expanded_policy: FrozenJsonObjectFact
    expanded_policy_fingerprint: str = Field(min_length=1)
    source: Literal["session_default", "plan_mode", "child_profile"]
    plan_restriction_active: bool
    fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_permission(self) -> "RunPermissionSnapshotFact":
        if thaw_json(self.expanded_policy) != preset_permission_payload(self.mode):
            raise ValueError("permission snapshot does not equal preset expansion")
        expected_policy = context_fingerprint(
            "run-permission-expanded-policy:v1", self.expanded_policy
        )
        if self.expanded_policy_fingerprint != expected_policy:
            raise ValueError("expanded permission policy fingerprint mismatch")
        if self.plan_restriction_active and not (
            self.mode is PermissionMode.READ_ONLY and self.source == "plan_mode"
        ):
            raise ValueError("plan restriction requires read-only plan snapshot")
        expected = context_fingerprint(
            "run-permission-snapshot:v1",
            self.model_dump(mode="json", exclude={"fingerprint"}),
        )
        if self.fingerprint != expected:
            raise ValueError("permission snapshot fingerprint mismatch")
        return self


class ToolResultEnvelopeRenderPolicyFact(FrozenContextFact):
    envelope_renderer_version: str = Field(min_length=1)
    truncation_marker_version: str = Field(min_length=1)
    artifact_envelope_version: str = Field(min_length=1)
    timing_header_version: str = Field(min_length=1)
    full_string_cap_chars: int = Field(ge=0)
    compact_string_cap_chars: int = Field(ge=0)
    minimal_string_cap_chars: int = Field(ge=0)
    ultra_minimal_string_cap_chars: int = Field(ge=0)
    max_process_summaries: int = Field(ge=0)
    compact_process_summaries: int = Field(ge=0)
    process_summary_string_cap_chars: int = Field(ge=0)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_policy(self) -> "ToolResultEnvelopeRenderPolicyFact":
        caps = (
            self.full_string_cap_chars,
            self.compact_string_cap_chars,
            self.minimal_string_cap_chars,
            self.ultra_minimal_string_cap_chars,
        )
        if tuple(sorted(caps, reverse=True)) != caps:
            raise ValueError("envelope string caps must be non-increasing")
        if self.compact_process_summaries > self.max_process_summaries:
            raise ValueError("compact process summary cap exceeds full cap")
        _validate_fingerprint(
            self, "tool-result-envelope-render-policy:v1", "policy_fingerprint"
        )
        return self


class ToolResultRenderPolicyBasisFact(FrozenContextFact):
    policy_version: Literal["tool-result-render-policy:v2"] = (
        "tool-result-render-policy:v2"
    )
    per_tool_cap_chars: int = Field(gt=0)
    per_message_cap_chars: int = Field(gt=0)
    per_envelope_cap_chars: int = Field(gt=0)
    minimum_essential_envelope_chars: int = Field(ge=1)
    max_artifact_refs_per_unit: int = Field(ge=0)
    max_data_placeholder_chars: int = Field(ge=0)
    envelope_render: ToolResultEnvelopeRenderPolicyFact
    basis_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_basis(self) -> "ToolResultRenderPolicyBasisFact":
        if self.minimum_essential_envelope_chars > self.per_envelope_cap_chars:
            raise ValueError("minimum essential envelope exceeds per-unit cap")
        _validate_fingerprint(
            self, "tool-result-render-policy-basis:v2", "basis_fingerprint"
        )
        return self


class ResolvedToolResultRenderPolicyFact(FrozenContextFact):
    basis: ToolResultRenderPolicyBasisFact
    ordered_unit_ids: tuple[str, ...]
    protected_unit_ids: tuple[str, ...]
    unit_order_fingerprint: str = Field(min_length=1)
    protection_fingerprint: str = Field(min_length=1)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_resolved(self) -> "ResolvedToolResultRenderPolicyFact":
        _ordered_unique(self.ordered_unit_ids, "tool result unit IDs")
        _ordered_unique(self.protected_unit_ids, "protected tool result unit IDs")
        if not set(self.protected_unit_ids).issubset(self.ordered_unit_ids):
            raise ValueError("protected units are not in ordered units")
        expected_order = context_fingerprint(
            "tool-result-unit-order:v2", self.ordered_unit_ids
        )
        if self.unit_order_fingerprint != expected_order:
            raise ValueError("tool result unit order fingerprint mismatch")
        expected_protection = context_fingerprint(
            "tool-result-unit-protection:v2", self.protected_unit_ids
        )
        if self.protection_fingerprint != expected_protection:
            raise ValueError("tool result protection fingerprint mismatch")
        _validate_fingerprint(
            self, "resolved-tool-result-render-policy:v2", "policy_fingerprint"
        )
        return self


class ContextCandidateCollectionPolicyFact(FrozenContextFact):
    policy_version: str = Field(min_length=1)
    projection_token_budget: int = Field(ge=0)
    max_subagent_results_per_parent_compile: int = Field(ge=0)
    max_inline_candidate_chars: int = Field(ge=0)
    max_aggregate_candidate_chars: int = Field(ge=0)
    max_candidate_source_refs: int = Field(ge=0)
    max_candidate_artifact_refs: int = Field(ge=0)
    max_input_manifest_chars: int = Field(ge=1)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ContextCandidateCollectionPolicyFact":
        _validate_fingerprint(
            self, "context-candidate-collection-policy:v1", "policy_fingerprint"
        )
        return self


class ContextAllocationPolicyFact(FrozenContextFact):
    section_policy_version: str = Field(min_length=1)
    required_section_ids: tuple[str, ...]
    optional_section_priority_order: tuple[str, ...]
    lifecycle_policy_version: str = Field(min_length=1)
    timing_header_policy_version: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ContextAllocationPolicyFact":
        _ordered_unique(self.required_section_ids, "required section IDs")
        _ordered_unique(self.optional_section_priority_order, "optional section IDs")
        _validate_fingerprint(self, "context-allocation-policy:v1", "fingerprint")
        return self


class ContextCompilePolicyFact(FrozenContextFact):
    compiler_contract_version: str = Field(min_length=1)
    tool_result_basis: ToolResultRenderPolicyBasisFact
    candidate_collection: ContextCandidateCollectionPolicyFact
    allocation: ContextAllocationPolicyFact
    fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ContextCompilePolicyFact":
        _validate_fingerprint(self, "context-compile-policy:v1", "fingerprint")
        return self


class TranscriptProjectionWindowFact(FrozenContextFact):
    window_kind: Literal[
        "uncompacted",
        "preflight_compaction",
        "mid_turn_compaction",
        "window_compaction",
    ]
    compaction_terminal_ref: ContextEventReferenceFact | None
    compaction_summary_artifact_id: str | None
    compacted_through_sequence: int | None = Field(default=None, ge=1)
    keep_after_sequence: int | None = Field(default=None, ge=0)
    window_compaction_started_ref: ContextEventReferenceFact | None = None
    window_compaction_source_document_artifact_id: str | None = None
    window_compaction_source_document_fingerprint: str | None = None
    summarized_message_ids: tuple[str, ...] = ()
    retained_message_ids: tuple[str, ...] = ()
    retained_history_from_sequence: int | None = Field(default=None, ge=1)
    retained_history_through_sequence: int | None = Field(default=None, ge=1)
    protected_run_start_sequence: int = Field(ge=1)
    protected_run_through_sequence: int = Field(ge=1)
    window_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_window(self) -> "TranscriptProjectionWindowFact":
        compacted = self.window_kind != "uncompacted"
        common_compaction_values = (
            self.compaction_terminal_ref,
            self.compaction_summary_artifact_id,
            self.compacted_through_sequence,
        )
        if compacted and any(value is None for value in common_compaction_values):
            raise ValueError("compacted window requires complete attribution")
        if not compacted and any(
            value is not None
            for value in (
                *common_compaction_values,
                self.keep_after_sequence,
                self.window_compaction_started_ref,
                self.window_compaction_source_document_artifact_id,
                self.window_compaction_source_document_fingerprint,
            )
        ):
            raise ValueError("uncompacted window cannot carry compaction attribution")
        is_window_compaction = self.window_kind == "window_compaction"
        window_compaction_values = (
            self.window_compaction_started_ref,
            self.window_compaction_source_document_artifact_id,
            self.window_compaction_source_document_fingerprint,
        )
        if is_window_compaction:
            if any(value is None for value in window_compaction_values):
                raise ValueError("window compaction attribution is incomplete")
            if self.keep_after_sequence is not None:
                raise ValueError("window compaction cannot use prefix keep-after")
            if not self.summarized_message_ids or not self.retained_message_ids:
                raise ValueError("window compaction requires exact message partitions")
            if set(self.summarized_message_ids) & set(self.retained_message_ids):
                raise ValueError("window compaction message partitions overlap")
        else:
            if any(value is not None for value in window_compaction_values):
                raise ValueError("prefix window cannot carry window-compaction facts")
            if self.summarized_message_ids or self.retained_message_ids:
                raise ValueError("prefix window cannot carry exact message partitions")
            if compacted and self.keep_after_sequence is None:
                raise ValueError("prefix compaction requires keep-after sequence")
        if self.protected_run_start_sequence > self.protected_run_through_sequence:
            raise ValueError("protected run range is reversed")
        retained = (
            self.retained_history_from_sequence,
            self.retained_history_through_sequence,
        )
        if (retained[0] is None) != (retained[1] is None):
            raise ValueError("retained history range must be all-or-none")
        if retained[0] is not None and int(retained[0]) > int(retained[1] or 0):
            raise ValueError("retained history range is reversed")
        if compacted and not is_window_compaction:
            if int(self.keep_after_sequence or 0) > int(
                self.compacted_through_sequence or 0
            ):
                raise ValueError("compaction keep-after exceeds compacted-through")
            terminal_sequence = int(self.compaction_terminal_ref.sequence)  # type: ignore[union-attr]
            if self.window_kind == "preflight_compaction" and not (
                terminal_sequence < self.protected_run_start_sequence
            ):
                raise ValueError("preflight compaction must precede current RunStart")
            if self.window_kind == "mid_turn_compaction" and not (
                terminal_sequence > self.protected_run_start_sequence
            ):
                raise ValueError("mid-turn compaction must follow current RunStart")
        if is_window_compaction:
            terminal_sequence = int(self.compaction_terminal_ref.sequence)  # type: ignore[union-attr]
            started_sequence = int(self.window_compaction_started_ref.sequence)  # type: ignore[union-attr]
            if not (
                self.protected_run_start_sequence
                < started_sequence
                < terminal_sequence
                <= self.protected_run_through_sequence
            ):
                raise ValueError("window compaction lifecycle ordering is invalid")
        _validate_fingerprint(
            self, "transcript-projection-window:v1", "window_fingerprint"
        )
        return self


class ContextAuthoritySlicePlan(FrozenContextFact):
    through_sequence: int = Field(ge=1)
    authority_from_sequence: int = Field(ge=1)
    required_local_event_refs: tuple[ContextEventReferenceFact, ...]
    transcript_window: TranscriptProjectionWindowFact
    plan_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_plan(self) -> "ContextAuthoritySlicePlan":
        if self.authority_from_sequence > self.through_sequence:
            raise ValueError("authority slice start exceeds high-water")
        sequences = tuple(ref.sequence for ref in self.required_local_event_refs)
        if tuple(sorted(sequences)) != sequences or len(sequences) != len(
            set(sequences)
        ):
            raise ValueError("authority refs must be sequence-sorted and unique")
        if not sequences:
            raise ValueError("authority slice requires local event references")
        if max(sequences) > self.through_sequence:
            raise ValueError("authority ref exceeds high-water")
        authority_candidates = [
            *sequences,
            self.transcript_window.protected_run_start_sequence,
        ]
        if self.transcript_window.retained_history_from_sequence is not None:
            authority_candidates.append(
                self.transcript_window.retained_history_from_sequence
            )
        if self.transcript_window.compaction_terminal_ref is not None:
            authority_candidates.append(
                self.transcript_window.compaction_terminal_ref.sequence
            )
        if self.authority_from_sequence != min(authority_candidates):
            raise ValueError("authority start does not cover every required source")
        if (
            self.transcript_window.protected_run_through_sequence
            != self.through_sequence
        ):
            raise ValueError("protected run must end at authority high-water")
        _validate_fingerprint(
            self, "context-authority-slice-plan:v1", "plan_fingerprint"
        )
        return self


class ContextInlineToolSchemaFact(FrozenContextFact):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    kind: Literal["inline"] = "inline"
    schema_value: FrozenJsonObjectFact = Field(
        alias="schema", serialization_alias="schema"
    )
    schema_chars: int = Field(ge=0)
    schema_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _schema(self) -> "ContextInlineToolSchemaFact":
        if self.schema_chars != len(
            canonical_json_bytes(self.schema_value).decode("utf-8")
        ):
            raise ValueError("inline tool schema char count mismatch")
        if self.schema_fingerprint != context_fingerprint(
            "tool-schema:v1", self.schema_value
        ):
            raise ValueError("inline tool schema fingerprint mismatch")
        return self


class ContextArtifactToolSchemaFact(FrozenContextFact):
    kind: Literal["artifact"] = "artifact"
    schema_artifact_id: str = Field(min_length=1)
    schema_chars: int = Field(ge=0)
    schema_fingerprint: str = Field(min_length=1)


ContextToolSchemaFact: TypeAlias = (
    ContextInlineToolSchemaFact | ContextArtifactToolSchemaFact
)


class ContextToolSpecFact(FrozenContextFact):
    model_tool_name: str = Field(min_length=1)
    descriptor_id: str = Field(min_length=1)
    descriptor_fingerprint: str = Field(min_length=1)
    descriptor_render_attribution: CapabilityDescriptorRenderAttributionFact
    result_render_contract_fingerprint: str = Field(min_length=1)
    input_schema: ContextToolSchemaFact
    description: str
    source_binding_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _attribution(self) -> "ContextToolSpecFact":
        attribution = self.descriptor_render_attribution
        if self.descriptor_id != attribution.descriptor_id:
            raise ValueError("tool spec descriptor ID mismatch")
        if self.descriptor_fingerprint != attribution.descriptor_fingerprint:
            raise ValueError("tool spec descriptor fingerprint mismatch")
        if (
            self.result_render_contract_fingerprint
            != attribution.result_render_contract_fingerprint
        ):
            raise ValueError("tool spec render contract fingerprint mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ContextMaterializedToolSpecInput:
    fact: ContextToolSpecFact
    materialized_schema: FrozenJsonObjectFact

    def __post_init__(self) -> None:
        if context_fingerprint("tool-schema:v1", self.materialized_schema) != (
            self.fact.input_schema.schema_fingerprint
        ):
            raise ValueError("materialized tool schema fingerprint mismatch")


class ContextProjectionReferenceFact(FrozenContextFact):
    projection_kind: Literal[
        "memory",
        "subagent_results",
        "recovery",
        "runtime_context",
        "capability_catalog",
        "capability_active_skill",
        "plan",
    ]
    owner_runtime_session_id: str = Field(min_length=1)
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_ids: tuple[str, ...]
    semantic_fingerprint: str = Field(min_length=1)


class ContextPlanSnapshotFact(FrozenContextFact):
    workflow_id: str | None
    active: bool
    revision: int = Field(ge=0)
    entered_event: ContextEventReferenceFact | None
    entry_run_id: str | None
    stored_default_permission_mode: PermissionMode
    stored_default_permission_fingerprint: str = Field(min_length=1)
    accepted_plan_artifact_id: str | None
    fact_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_plan(self) -> "ContextPlanSnapshotFact":
        required = (self.workflow_id, self.entered_event, self.entry_run_id)
        if self.active != all(item is not None for item in required):
            raise ValueError("plan snapshot active attribution mismatch")
        _validate_fingerprint(self, "context-plan-snapshot:v1", "fact_fingerprint")
        return self


class ContextStaticInstructionFact(FrozenContextFact):
    source_id: Literal[
        "base_system_instruction",
        "runtime_policy_instruction",
        "memory_scope_instruction",
    ]
    contract_version: str = Field(min_length=1)
    content_artifact_id: str = Field(min_length=1)
    content_fingerprint: str = Field(min_length=1)
    chars: int = Field(ge=0)
    fact_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ContextStaticInstructionFact":
        _validate_fingerprint(self, "context-static-instruction:v1", "fact_fingerprint")
        return self


class ContextCandidateSourceSelectionFact(FrozenContextFact):
    """Snapshot-owned source selection, independent from model-visible bytes."""

    source_instance_id: Literal["subagent:results"]
    eligible_source_count: int = Field(ge=0)
    selected_source_ids: tuple[str, ...]
    omitted_source_count: int = Field(ge=0)
    reason_code: Literal[
        "no_eligible_sources",
        "selected_all",
        "policy_limit",
    ]
    policy_fingerprint: str = Field(min_length=1)
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
    selection_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _selection(self) -> "ContextCandidateSourceSelectionFact":
        _ordered_unique(self.selected_source_ids, "selected candidate source IDs")
        if self.eligible_source_count != (
            len(self.selected_source_ids) + self.omitted_source_count
        ):
            raise ValueError("candidate source selection count mismatch")
        if self.reason_code == "no_eligible_sources":
            if self.eligible_source_count != 0:
                raise ValueError("empty selection reason requires no eligible sources")
        elif self.reason_code == "selected_all":
            if self.eligible_source_count == 0 or self.omitted_source_count != 0:
                raise ValueError("selected-all reason requires a non-empty full selection")
        elif self.omitted_source_count == 0:
            raise ValueError("policy-limit reason requires omitted sources")
        _validate_fingerprint(
            self,
            "context-candidate-source-selection:v1",
            "selection_fingerprint",
        )
        return self


class ContextCandidateAuthorityFact(FrozenContextFact):
    """Snapshot-owned authority for one model-visible candidate."""

    source_instance_id: str = Field(min_length=1)
    source_kind: ContextCandidateSourceKind
    source_fact_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_ids: tuple[str, ...]
    channel: ContextChannelFact
    priority: int
    required: bool
    stability: Literal["stable", "run", "step", "ephemeral"]
    lowering_kind: ContextCandidateLoweringKind
    lifecycle_dependency_fingerprint: str | None
    model_visible_text: str
    model_visible_content_fingerprint: str = Field(min_length=1)
    model_visible_chars: int = Field(ge=0)
    source_timing: "ContextSourceTimingFact"
    authority_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _authority(self) -> "ContextCandidateAuthorityFact":
        expected_channel, expected_lowering = _CONTEXT_CANDIDATE_CHANNEL_MATRIX[
            self.source_kind
        ]
        if (self.channel, self.lowering_kind) != (
            expected_channel,
            expected_lowering,
        ):
            raise ValueError("candidate authority channel/lowering matrix mismatch")
        if self.stability != "ephemeral" and not self.lifecycle_dependency_fingerprint:
            raise ValueError("cacheable candidate authority requires dependency")
        if self.model_visible_chars != len(self.model_visible_text):
            raise ValueError("candidate authority model-visible char count mismatch")
        if self.model_visible_content_fingerprint != context_fingerprint(
            "context-inline-text:v1", self.model_visible_text
        ):
            raise ValueError("candidate authority model-visible content mismatch")
        _ordered_unique(
            tuple(ref.event_id for ref in self.source_fact_refs),
            "candidate authority event refs",
        )
        _ordered_unique(self.source_artifact_ids, "candidate authority artifact IDs")
        _validate_fingerprint(
            self,
            "context-candidate-authority:v1",
            "authority_fingerprint",
        )
        return self


class ContextRuntimeEnvironmentFact(FrozenContextFact):
    workspace_identity_fingerprint: str = Field(min_length=1)
    workspace_kind: str = Field(min_length=1)
    model_visible_workspace_root: str
    terminal_current_cwd: str
    session_timezone: str | None
    observed_at_utc: str
    fact_fingerprint: str = Field(min_length=1)

    @field_validator("observed_at_utc")
    @classmethod
    def _utc(cls, value: str) -> str:
        return canonical_utc_timestamp(value)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ContextRuntimeEnvironmentFact":
        _validate_fingerprint(
            self, "context-runtime-environment:v1", "fact_fingerprint"
        )
        return self


class ContextCompileTimingFact(FrozenContextFact):
    compiled_at_utc: str
    session_timezone: str | None
    compiled_local_date: str | None
    current_user_observed_at_utc: str
    clock_source: Literal["host_clock"] = "host_clock"

    @field_validator("compiled_at_utc", "current_user_observed_at_utc")
    @classmethod
    def _utc(cls, value: str) -> str:
        return canonical_utc_timestamp(value)

    @model_validator(mode="after")
    def _order(self) -> "ContextCompileTimingFact":
        if self.compiled_at_utc < self.current_user_observed_at_utc:
            raise ValueError("compile time precedes current user observation")
        return self


class ContextInputIdentityFact(FrozenContextFact):
    snapshot_id: str = Field(min_length=1)
    schema_version: Literal["context-input:v2"] = "context-input:v2"
    compiler_contract_version: str = Field(min_length=1)
    runtime_session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    reply_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    model_call_index: int = Field(ge=1)
    compile_attempt_index: int = Field(ge=1)
    context_retry_index: int = Field(ge=0)
    source_through_sequence: int = Field(ge=1)


class LongHorizonContextAttributionFact(FrozenContextFact):
    schema_version: Literal["long-horizon-context-attribution:v1"] = (
        "long-horizon-context-attribution:v1"
    )
    run_contract_fingerprint: str = Field(min_length=1)
    window_id: str = Field(min_length=1)
    window_generation: int = Field(ge=1)
    window_semantic_fingerprint: str = Field(min_length=1)
    projection_generation: int = Field(ge=0)
    projection_state_fingerprint: str = Field(min_length=1)
    projection_rewrite_event_refs: tuple[ContextEventReferenceFact, ...]
    rollout_account_id: str = Field(min_length=1)
    rollout_account_owner_runtime_session_id: str = Field(min_length=1)
    rollout_state_through_sequence: int = Field(ge=0)
    rollout_phase: RolloutPhase
    rollout_state_fingerprint: str = Field(min_length=1)
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
    budget_decision: LongHorizonContextBudgetDecisionFact
    summary_artifact_id: str | None
    summary_content_sha256: str | None
    attribution_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _attribution(self) -> "LongHorizonContextAttributionFact":
        refs = self.projection_rewrite_event_refs
        sequences = tuple(item.sequence for item in refs)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("projection rewrite refs must be ordered and unique")
        if self.window_generation == 1:
            if self.summary_artifact_id is not None or self.summary_content_sha256 is not None:
                raise ValueError("initial context window cannot carry summary attribution")
        elif self.summary_artifact_id is None or self.summary_content_sha256 is None:
            raise ValueError("compacted context window requires summary attribution")
        if self.budget_decision.window_id != self.window_id:
            raise ValueError("context budget decision window mismatch")
        _validate_fingerprint(
            self,
            "long-horizon-context-attribution:v1",
            "attribution_fingerprint",
        )
        return self


class ProjectedToolResultCompileRefFact(FrozenContextFact):
    transcript_message_id: str = Field(min_length=1)
    block_index: int = Field(ge=0)
    tool_call_id: str = Field(min_length=1)
    tool_result_unit_id: str = Field(min_length=1)
    window_id: str = Field(min_length=1)
    projection_generation: int = Field(ge=0)
    projected_fragment_fingerprint: str = Field(min_length=1)
    representation: ToolObservationRepresentation
    rollup_id: str | None

    @model_validator(mode="after")
    def _projection(self) -> "ProjectedToolResultCompileRefFact":
        if (
            self.representation is ToolObservationRepresentation.ROLLUP_MEMBER
        ) != (self.rollup_id is not None):
            raise ValueError("projected compile ref rollup identity mismatch")
        return self


class ContextFactSnapshotFact(FrozenContextFact):
    identity: ContextInputIdentityFact
    run_entry: ContextRunEntryReferenceFact
    continuation: ContextContinuationReferenceFact | None
    continuation_refs: tuple[ContextEventReferenceFact, ...]
    continuation_count: int = Field(ge=0)
    current_user_message: CurrentUserMessageFact
    permission_snapshot: RunPermissionSnapshotFact
    resolved_model_call: ResolvedModelCallFact
    capability_snapshot: CapabilityExposureSnapshotFact
    plan_snapshot: ContextPlanSnapshotFact
    mcp_installation_id: str = Field(min_length=1)
    mcp_installation_owner_runtime_session_id: str = Field(min_length=1)
    static_instructions: tuple[ContextStaticInstructionFact, ...]
    runtime_environment: ContextRuntimeEnvironmentFact
    compile_policy: ContextCompilePolicyFact
    tool_specs: tuple[ContextToolSpecFact, ...]
    projections: tuple[ContextProjectionReferenceFact, ...]
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
    subagent_graph_acceleration: SubagentGraphAccelerationFact
    long_horizon_attribution: LongHorizonContextAttributionFact
    candidate_source_selections: tuple[ContextCandidateSourceSelectionFact, ...]
    candidate_authorities: tuple[ContextCandidateAuthorityFact, ...]
    timing: ContextCompileTimingFact
    authority_slice_plan: ContextAuthoritySlicePlan
    primary_event_range: ContextEventRangeFact
    named_event_ranges: tuple[ContextEventRangeFact, ...]
    snapshot_semantic_fingerprint: str = Field(min_length=1)
    snapshot_fact_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "ContextFactSnapshotFact":
        identity = self.identity
        start = self.run_entry.run_start
        if (identity.run_id, identity.runtime_session_id) != (
            self.permission_snapshot.run_id,
            self.permission_snapshot.runtime_session_id,
        ):
            raise ValueError("snapshot permission identity mismatch")
        if start.runtime_session_id != identity.runtime_session_id:
            raise ValueError("snapshot RunStart owner mismatch")
        if self.resolved_model_call.target.target_fingerprint != (
            self.run_entry.run_entry.model_target_fingerprint
        ):
            raise ValueError("snapshot model target mismatch")
        if (
            self.run_entry.run_entry.permission_snapshot_id
            != self.permission_snapshot.snapshot_id
        ):
            raise ValueError("snapshot run-entry permission mismatch")
        if (
            self.continuation is None
            and self.run_entry.run_entry.mcp_installation_id != self.mcp_installation_id
        ):
            raise ValueError("snapshot initial MCP installation mismatch")
        entry_mcp_owner = getattr(
            self.run_entry.run_entry,
            "mcp_installation_owner_runtime_session_id",
            None,
        )
        if entry_mcp_owner is None:
            entry_mcp_owner = self.run_entry.run_entry.identity.runtime_session_id  # type: ignore[union-attr]
        if entry_mcp_owner != self.mcp_installation_owner_runtime_session_id:
            raise ValueError("snapshot MCP installation owner mismatch")
        if (
            self.current_user_message.observed_at_utc
            != self.timing.current_user_observed_at_utc
        ):
            raise ValueError("snapshot current user timing mismatch")
        if self.timing.compiled_at_utc < self.runtime_environment.observed_at_utc:
            raise ValueError("snapshot compile time precedes runtime environment")
        if self.timing.session_timezone != self.runtime_environment.session_timezone:
            raise ValueError("snapshot runtime timezone facts differ")
        if (
            self.capability_snapshot.owner.runtime_session_id
            != identity.runtime_session_id
        ):
            raise ValueError("snapshot capability runtime owner mismatch")
        if self.capability_snapshot.owner.run_id != identity.run_id:
            raise ValueError("snapshot capability run owner mismatch")
        basis = self.capability_snapshot.resolve_basis
        if basis.permission_snapshot_id != self.permission_snapshot.snapshot_id:
            raise ValueError("snapshot capability permission basis mismatch")
        if basis.mcp_installation_id != self.mcp_installation_id:
            raise ValueError("snapshot capability MCP basis mismatch")
        if (
            basis.execution_surface_identity.mcp_installation_id
            != self.mcp_installation_id
        ):
            raise ValueError("snapshot execution surface MCP identity mismatch")
        if (
            basis.workspace_identity_fingerprint
            != self.runtime_environment.workspace_identity_fingerprint
        ):
            raise ValueError("snapshot workspace identity mismatch")
        if self.continuation_count != len(self.continuation_refs):
            raise ValueError("continuation count mismatch")
        if self.continuation_count == 0 and self.continuation is not None:
            raise ValueError(
                "empty continuation history cannot have latest continuation"
            )
        if self.continuation_count > 0:
            if (
                self.continuation is None
                or self.continuation_refs[-1] != self.continuation.resume_boundary
            ):
                raise ValueError("latest continuation does not match ordered refs")
            boundary = self.continuation.boundary
            if (
                boundary.identity.run_id != identity.run_id
                or boundary.original_run_start_event_id != start.event_id
                or boundary.original_run_start_sequence != start.sequence
            ):
                raise ValueError("snapshot continuation run attribution mismatch")
            if boundary.permission_snapshot_id != self.permission_snapshot.snapshot_id:
                raise ValueError("snapshot continuation permission mismatch")
            if (
                boundary.model_target_fingerprint
                != self.resolved_model_call.target.target_fingerprint
            ):
                raise ValueError("snapshot continuation model target mismatch")
            if boundary.mcp_installation_id != self.mcp_installation_id:
                raise ValueError("snapshot continuation MCP identity mismatch")
        sequences = tuple(ref.sequence for ref in self.continuation_refs)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("continuation refs must be ordered and unique")
        if self.primary_event_range.runtime_session_id != identity.runtime_session_id:
            raise ValueError("primary event range owner mismatch")
        if (
            self.primary_event_range.through_sequence
            != self.authority_slice_plan.through_sequence
            or identity.source_through_sequence
            != self.authority_slice_plan.through_sequence
        ):
            raise ValueError("snapshot authority range mismatch")
        local_ranges = tuple(
            item
            for item in (self.primary_event_range, *self.named_event_ranges)
            if item.runtime_session_id == identity.runtime_session_id
        )
        if min(item.first_sequence for item in local_ranges) != (
            self.authority_slice_plan.authority_from_sequence
        ):
            raise ValueError("snapshot authority range start mismatch")
        for ref in self.authority_slice_plan.required_local_event_refs:
            if not _event_ref_is_within_ranges(ref, local_ranges):
                raise ValueError("required authority ref is outside local ranges")
        _ordered_unique(
            tuple(item.source_id for item in self.static_instructions),
            "static instruction IDs",
        )
        _ordered_unique(
            tuple(item.model_tool_name for item in self.tool_specs), "model tool names"
        )
        _ordered_unique(
            tuple(item.source_instance_id for item in self.candidate_authorities),
            "candidate authority source IDs",
        )
        _ordered_unique(
            tuple(item.source_instance_id for item in self.candidate_source_selections),
            "candidate source selection IDs",
        )
        max_subagent_results = (
            self.compile_policy.candidate_collection.max_subagent_results_per_parent_compile
        )
        selections = {
            item.source_instance_id: item
            for item in self.candidate_source_selections
        }
        subagent_selection = selections.get("subagent:results")
        if subagent_selection is None:
            raise ValueError("snapshot requires subagent result selection fact")
        if (
            subagent_selection.policy_fingerprint
            != self.compile_policy.candidate_collection.policy_fingerprint
            or subagent_selection.subagent_graph_semantic_source
            != self.subagent_graph_semantic_source
        ):
            raise ValueError("snapshot subagent selection basis mismatch")
        if (
            self.subagent_graph_semantic_source.runtime_session_id
            != identity.runtime_session_id
            or self.subagent_graph_acceleration.ledger_through_sequence
            != identity.source_through_sequence
        ):
            raise ValueError("snapshot subagent graph attribution mismatch")
        if (
            self.long_horizon_attribution.subagent_graph_semantic_source
            != self.subagent_graph_semantic_source
        ):
            raise ValueError("snapshot long-horizon graph semantic source mismatch")
        if len(subagent_selection.selected_source_ids) > max_subagent_results:
            raise ValueError(
                "snapshot subagent result selection exceeds compile policy"
            )
        if (
            subagent_selection.omitted_source_count > 0
            and len(subagent_selection.selected_source_ids) != max_subagent_results
        ):
            raise ValueError(
                "snapshot policy-limited subagent selection must fill its cap"
            )
        subagent_projection = next(
            (
                item
                for item in self.projections
                if item.projection_kind == "subagent_results"
            ),
            None,
        )
        subagent_authority = next(
            (
                item
                for item in self.candidate_authorities
                if item.source_instance_id == "subagent:results"
            ),
            None,
        )
        if subagent_selection.selected_source_ids:
            if subagent_projection is None or subagent_authority is None:
                raise ValueError(
                    "selected subagent results require projection and authority"
                )
        elif subagent_projection is not None or subagent_authority is not None:
            raise ValueError(
                "empty subagent selection cannot create projection or authority"
            )
        ranges = (self.primary_event_range, *self.named_event_ranges)
        for projection in self.projections:
            for ref in projection.source_event_refs:
                if (
                    projection.projection_kind != "subagent_results"
                    and not _event_ref_is_within_ranges(ref, ranges)
                ):
                    raise ValueError("projection event ref exceeds snapshot authority")
        for authority in self.candidate_authorities:
            for ref in authority.source_fact_refs:
                if (
                    authority.source_instance_id != "subagent:results"
                    and not _event_ref_is_within_ranges(ref, ranges)
                ):
                    raise ValueError("candidate event ref exceeds snapshot authority")
        semantic = context_fingerprint(
            "context-snapshot-semantic:v2", _snapshot_semantic_payload(self)
        )
        if self.snapshot_semantic_fingerprint != semantic:
            raise ValueError("snapshot semantic fingerprint mismatch")
        fact = context_fingerprint(
            "context-snapshot-fact:v2",
            self.model_dump(mode="json", exclude={"snapshot_fact_fingerprint"}),
        )
        if self.snapshot_fact_fingerprint != fact:
            raise ValueError("snapshot fact fingerprint mismatch")
        return self


class TranscriptTextBlockFact(FrozenContextFact):
    kind: Literal["text"] = "text"
    block_id: str = Field(min_length=1)
    text: str
    content_fingerprint: str = Field(min_length=1)
    source_events: tuple[ContextEventReferenceFact, ...]

    @model_validator(mode="after")
    def _fingerprint(self) -> "TranscriptTextBlockFact":
        if self.content_fingerprint != context_fingerprint(
            "transcript-text:v1", self.text
        ):
            raise ValueError("transcript text fingerprint mismatch")
        return self


class TranscriptThinkingBlockFact(FrozenContextFact):
    kind: Literal["thinking"] = "thinking"
    block_id: str = Field(min_length=1)
    thinking: str
    lowering_policy: Literal["provider_neutral_structured"] = (
        "provider_neutral_structured"
    )
    content_fingerprint: str = Field(min_length=1)
    source_events: tuple[ContextEventReferenceFact, ...]

    @model_validator(mode="after")
    def _fingerprint(self) -> "TranscriptThinkingBlockFact":
        if self.content_fingerprint != context_fingerprint(
            "transcript-thinking:v1", self.thinking
        ):
            raise ValueError("transcript thinking fingerprint mismatch")
        return self


class TranscriptDataPlaceholderFact(FrozenContextFact):
    kind: Literal["data_placeholder"] = "data_placeholder"
    block_id: str = Field(min_length=1)
    name: str | None
    media_type: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    artifact_ids: tuple[str, ...]
    source_events: tuple[ContextEventReferenceFact, ...]


class TranscriptToolCallFact(FrozenContextFact):
    kind: Literal["tool_call"] = "tool_call"
    tool_call_id: str = Field(min_length=1)
    model_tool_name: str = Field(min_length=1)
    raw_arguments_json: str
    arguments_status: Literal["valid_object", "invalid_json", "non_object_json"]
    parsed_arguments: FrozenJsonObjectFact | None
    parse_error_code: ToolArgumentsParseErrorCode | None
    state: str = Field(min_length=1)
    source_events: tuple[ContextEventReferenceFact, ...]

    @model_validator(mode="after")
    def _arguments(self) -> "TranscriptToolCallFact":
        if self.arguments_status == "valid_object":
            if self.parsed_arguments is None or self.parse_error_code is not None:
                raise ValueError("valid tool arguments require parsed object only")
        elif self.parsed_arguments is not None:
            raise ValueError("malformed tool arguments cannot carry parsed object")
        elif (
            self.arguments_status == "invalid_json"
            and self.parse_error_code
            is not ToolArgumentsParseErrorCode.INVALID_JSON_SYNTAX
        ):
            raise ValueError("invalid JSON requires invalid_json_syntax code")
        elif (
            self.arguments_status == "non_object_json"
            and self.parse_error_code
            is not ToolArgumentsParseErrorCode.JSON_ROOT_NOT_OBJECT
        ):
            raise ValueError("non-object JSON requires json_root_not_object code")
        return self


class TranscriptToolResultRefFact(FrozenContextFact):
    kind: Literal["tool_result_ref"] = "tool_result_ref"
    tool_call_id: str = Field(min_length=1)
    tool_result_unit_id: str = Field(min_length=1)
    source_events: tuple[ContextEventReferenceFact, ...]


TranscriptBlockFact: TypeAlias = (
    TranscriptTextBlockFact
    | TranscriptThinkingBlockFact
    | TranscriptDataPlaceholderFact
    | TranscriptToolCallFact
    | TranscriptToolResultRefFact
)


class TranscriptMessageFact(FrozenContextFact):
    message_id: str = Field(min_length=1)
    role: Literal["system", "user", "assistant"]
    name: str | None
    run_id: str | None
    turn_id: str | None
    reply_id: str | None
    created_at_utc: str | None
    finished_at_utc: str | None
    segment: Literal[
        "compaction_summary",
        "prior_history",
        "current_user",
        "current_run_tail",
        "recovery_note",
        "terminal_lifecycle_note",
    ]
    blocks: tuple[TranscriptBlockFact, ...]
    source_sequence_start: int = Field(ge=1)
    source_sequence_end: int = Field(ge=1)
    message_fingerprint: str = Field(min_length=1)

    @field_validator("created_at_utc", "finished_at_utc")
    @classmethod
    def _utc(cls, value: str | None) -> str | None:
        return canonical_utc_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_message(self) -> "TranscriptMessageFact":
        if self.source_sequence_start > self.source_sequence_end:
            raise ValueError("transcript message source range is reversed")
        block_ids = tuple(
            getattr(block, "block_id", None) or getattr(block, "tool_call_id", "")
            for block in self.blocks
        )
        _ordered_unique(block_ids, "transcript block IDs")
        _validate_fingerprint(self, "transcript-message:v1", "message_fingerprint")
        return self


class ToolInteractionPairFact(FrozenContextFact):
    tool_call_id: str = Field(min_length=1)
    model_tool_name: str = Field(min_length=1)
    call_message_id: str = Field(min_length=1)
    call_block_index: int = Field(ge=0)
    result_message_id: str = Field(min_length=1)
    result_block_index: int = Field(ge=0)
    call_sequence: int = Field(ge=1)
    result_sequence: int = Field(ge=1)
    pairing_status: Literal["completed", "external_completed"]
    pair_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _pair(self) -> "ToolInteractionPairFact":
        if self.call_sequence > self.result_sequence:
            raise ValueError("tool result precedes tool call")
        _validate_fingerprint(self, "tool-interaction-pair:v1", "pair_fingerprint")
        return self


class CompactedWindowReferenceFact(FrozenContextFact):
    compaction_kind: Literal["prefix", "window"]
    compaction_id: str = Field(min_length=1)
    summary_artifact_id: str = Field(min_length=1)
    compacted_through_sequence: int = Field(ge=1)
    keep_after_sequence: int | None = Field(default=None, ge=0)
    summary_message_id: str = Field(min_length=1)
    source_event: ContextEventReferenceFact
    source_started_event: ContextEventReferenceFact | None = None

    @model_validator(mode="after")
    def _compaction_kind(self) -> "CompactedWindowReferenceFact":
        if self.compaction_kind == "prefix":
            if self.keep_after_sequence is None or self.source_started_event is not None:
                raise ValueError("prefix compaction reference attribution mismatch")
        elif self.keep_after_sequence is not None or self.source_started_event is None:
            raise ValueError("window compaction reference attribution mismatch")
        return self


class TranscriptCompileInput(FrozenContextFact):
    schema_version: Literal["transcript-input:v1"] = "transcript-input:v1"
    runtime_session_id: str = Field(min_length=1)
    through_sequence: int = Field(ge=1)
    current_user_anchor: str = Field(min_length=1)
    projection_window: TranscriptProjectionWindowFact
    messages: tuple[TranscriptMessageFact, ...]
    tool_pairs: tuple[ToolInteractionPairFact, ...]
    compacted_windows: tuple[CompactedWindowReferenceFact, ...]
    stripped_unfinished_call_ids: tuple[str, ...]
    omitted_non_model_block_ids: tuple[str, ...]
    transcript_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_transcript(self) -> "TranscriptCompileInput":
        if (
            self.projection_window.protected_run_through_sequence
            != self.through_sequence
        ):
            raise ValueError("transcript high-water/window mismatch")
        ids = tuple(message.message_id for message in self.messages)
        _ordered_unique(ids, "transcript message IDs")
        current = tuple(
            message for message in self.messages if message.segment == "current_user"
        )
        if len(current) != 1 or current[0].message_id != self.current_user_anchor:
            raise ValueError("transcript requires exactly one anchored current user")
        pair_ids = tuple(pair.tool_call_id for pair in self.tool_pairs)
        _ordered_unique(pair_ids, "tool interaction IDs")
        messages_by_id = {message.message_id: message for message in self.messages}
        result_ref_ids: list[str] = []
        for pair in self.tool_pairs:
            call_message = messages_by_id.get(pair.call_message_id)
            result_message = messages_by_id.get(pair.result_message_id)
            if call_message is None or result_message is None:
                raise ValueError("tool pair references an unknown transcript message")
            if pair.call_block_index >= len(
                call_message.blocks
            ) or pair.result_block_index >= len(result_message.blocks):
                raise ValueError("tool pair block position exceeds transcript message")
            call_block = call_message.blocks[pair.call_block_index]
            result_block = result_message.blocks[pair.result_block_index]
            if not isinstance(call_block, TranscriptToolCallFact) or not isinstance(
                result_block, TranscriptToolResultRefFact
            ):
                raise ValueError("tool pair does not reference call/result blocks")
            if (
                call_block.tool_call_id,
                result_block.tool_call_id,
                call_block.model_tool_name,
            ) != (
                pair.tool_call_id,
                pair.tool_call_id,
                pair.model_tool_name,
            ):
                raise ValueError("tool pair block identity mismatch")
            if not (
                call_message.source_sequence_start
                <= pair.call_sequence
                <= call_message.source_sequence_end
                and result_message.source_sequence_start
                <= pair.result_sequence
                <= result_message.source_sequence_end
            ):
                raise ValueError("tool pair sequence attribution mismatch")
            result_ref_ids.append(result_block.tool_call_id)
        if tuple(result_ref_ids) != pair_ids:
            raise ValueError("tool result refs differ from ordered tool pairs")
        _validate_fingerprint(
            self, "transcript-compile-input:v1", "transcript_fingerprint"
        )
        return self


class ContextSourceTimingFact(FrozenContextFact):
    observed_at_utc: str | None
    source_started_at_utc: str | None
    source_ended_at_utc: str | None
    source_sequence_start: int | None = Field(default=None, ge=1)
    source_sequence_end: int | None = Field(default=None, ge=1)
    freshness: Literal[
        "current_turn",
        "current_run_tail",
        "historical_replay",
        "compacted_history",
        "memory_projection",
        "current_tool_observation",
        "cached_snapshot",
        "background_process_observation",
        "subagent_result",
        "unknown",
    ]
    clock_source: Literal[
        "event_created_at",
        "message_created_at",
        "tool_observation_fact",
        "host_clock",
        "mixed",
    ]
    timing_fingerprint: str = Field(min_length=1)

    @field_validator("observed_at_utc", "source_started_at_utc", "source_ended_at_utc")
    @classmethod
    def _utc(cls, value: str | None) -> str | None:
        return canonical_utc_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def _timing(self) -> "ContextSourceTimingFact":
        if (
            self.source_started_at_utc is not None
            and self.source_ended_at_utc is not None
            and self.source_started_at_utc > self.source_ended_at_utc
        ):
            raise ValueError("candidate source end precedes start")
        if (self.source_sequence_start is None) != (self.source_sequence_end is None):
            raise ValueError("candidate source sequence range must be all-or-none")
        if self.source_sequence_start is not None and self.source_sequence_start > int(
            self.source_sequence_end or 0
        ):
            raise ValueError("candidate source sequence range is reversed")
        _validate_fingerprint(self, "context-source-timing:v1", "timing_fingerprint")
        return self


class WindowCompactionSourceEntryFact(FrozenContextFact):
    source_entry_id: str = Field(min_length=1)
    source_kind: Literal[
        "user_message",
        "assistant_text",
        "tool_call",
        "tool_result_projection",
        "observation_rollup",
        "subagent_result",
    ]
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[str, ...]
    source_message_id: str | None
    source_block_index: int | None = Field(default=None, ge=0)
    model_visible_text: str
    timing: ContextSourceTimingFact | None
    semantic_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _entry(self) -> "WindowCompactionSourceEntryFact":
        transcript_owned = self.source_kind != "observation_rollup"
        if transcript_owned != (
            self.source_message_id is not None and self.source_block_index is not None
        ):
            raise ValueError("window compaction source message attribution mismatch")
        if not self.source_event_refs:
            raise ValueError("window compaction source entry requires event facts")
        if tuple(sorted(set(self.source_artifact_refs))) != self.source_artifact_refs:
            raise ValueError("window compaction artifact refs must be sorted and unique")
        _validate_fingerprint(
            self,
            "window-compaction-source-entry:v1",
            "semantic_fingerprint",
        )
        return self


class WindowCompactionPairGroupFact(FrozenContextFact):
    group_id: str = Field(min_length=1)
    assistant_message_id: str = Field(min_length=1)
    tool_call_ids: tuple[str, ...]
    result_unit_ids: tuple[str, ...]
    source_sequence_from: int = Field(ge=1)
    source_sequence_through: int = Field(ge=1)
    protection_classes: tuple[str, ...]
    source_entry_ids: tuple[str, ...]
    group_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _group(self) -> "WindowCompactionPairGroupFact":
        if self.source_sequence_from > self.source_sequence_through:
            raise ValueError("window compaction pair-group range is reversed")
        if not self.tool_call_ids or len(self.tool_call_ids) != len(
            self.result_unit_ids
        ):
            raise ValueError("window compaction pair group is not pairing-complete")
        for values, label in (
            (self.tool_call_ids, "tool calls"),
            (self.result_unit_ids, "result units"),
            (self.source_entry_ids, "source entries"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"window compaction pair group duplicates {label}")
        if tuple(sorted(set(self.protection_classes))) != self.protection_classes:
            raise ValueError("window compaction protection classes are not canonical")
        _validate_fingerprint(
            self,
            "window-compaction-pair-group:v1",
            "group_fingerprint",
        )
        return self


class WindowCompactionSourceDocumentFact(FrozenContextFact):
    schema_version: Literal["window-compaction-source-document.v1"] = (
        "window-compaction-source-document.v1"
    )
    compaction_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_window_id: str = Field(min_length=1)
    source_projection_generation: int = Field(ge=0)
    source_through_sequence: int = Field(ge=1)
    entries: tuple[WindowCompactionSourceEntryFact, ...]
    pair_groups: tuple[WindowCompactionPairGroupFact, ...]
    summarized_entry_ids: tuple[str, ...]
    retained_entry_ids: tuple[str, ...]
    summarized_message_ids: tuple[str, ...]
    retained_message_ids: tuple[str, ...]
    summarized_pair_group_ids: tuple[str, ...]
    retained_pair_group_ids: tuple[str, ...]
    retained_transcript_baseline: FrozenJsonObjectFact
    document_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _document(self) -> "WindowCompactionSourceDocumentFact":
        entry_ids = tuple(entry.source_entry_id for entry in self.entries)
        group_ids = tuple(group.group_id for group in self.pair_groups)
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("window compaction source entries are duplicated")
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("window compaction pair groups are duplicated")
        summarized = set(self.summarized_entry_ids)
        retained = set(self.retained_entry_ids)
        if summarized & retained or summarized | retained != set(entry_ids):
            raise ValueError("window compaction source-entry partition is incomplete")
        entry_partition = {
            entry_id: "summarized" if entry_id in summarized else "retained"
            for entry_id in entry_ids
        }
        message_ids = {
            entry.source_message_id
            for entry in self.entries
            if entry.source_message_id is not None
        }
        summarized_messages = set(self.summarized_message_ids)
        retained_messages = set(self.retained_message_ids)
        if (
            summarized_messages & retained_messages
            or summarized_messages | retained_messages != message_ids
        ):
            raise ValueError("window compaction message partition is incomplete")
        for values, label in (
            (self.summarized_message_ids, "summarized messages"),
            (self.retained_message_ids, "retained messages"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"window compaction duplicates {label}")
        for entry in self.entries:
            if entry.source_message_id is None:
                continue
            expected = (
                "summarized"
                if entry.source_message_id in summarized_messages
                else "retained"
            )
            if entry_partition[entry.source_entry_id] != expected:
                raise ValueError("window compaction splits a transcript message")
        summarized_groups = set(self.summarized_pair_group_ids)
        retained_groups = set(self.retained_pair_group_ids)
        if (
            summarized_groups & retained_groups
            or summarized_groups | retained_groups != set(group_ids)
        ):
            raise ValueError("window compaction pair-group partition is incomplete")
        for group in self.pair_groups:
            expected = (
                "summarized" if group.group_id in summarized_groups else "retained"
            )
            if any(entry_partition[item] != expected for item in group.source_entry_ids):
                raise ValueError("window compaction splits a provider pairing group")
        baseline = thaw_json(self.retained_transcript_baseline)
        if not isinstance(baseline, dict):  # pragma: no cover - typed field guard
            raise ValueError("window compaction transcript baseline is not an object")
        if (
            baseline.get("schema_version")
            != "window-compaction-transcript-baseline.v1"
            or baseline.get("compaction_id") != self.compaction_id
            or baseline.get("run_id") != self.run_id
            or baseline.get("source_window_id") != self.source_window_id
            or baseline.get("source_through_sequence")
            != self.source_through_sequence
            or tuple(
                item.get("message_id")
                for item in baseline.get("retained_messages", ())
                if isinstance(item, dict)
            )
            != self.retained_message_ids
        ):
            raise ValueError(
                "window compaction transcript baseline attribution mismatch"
            )
        _validate_fingerprint(
            self,
            "window-compaction-source-document:v1",
            "document_fingerprint",
        )
        return self


class WindowCompactionSummaryFact(FrozenContextFact):
    schema_version: Literal["window-compaction-summary.v1"] = (
        "window-compaction-summary.v1"
    )
    observed_facts: tuple[str, ...]
    model_inferences: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    critical_constraints: tuple[str, ...]
    artifact_locators: tuple[str, ...]
    cited_source_entry_ids: tuple[str, ...]
    summary_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _summary(self) -> "WindowCompactionSummaryFact":
        if not any(
            (
                self.observed_facts,
                self.model_inferences,
                self.unresolved_questions,
                self.critical_constraints,
            )
        ):
            raise ValueError("window compaction summary is empty")
        if len(set(self.cited_source_entry_ids)) != len(
            self.cited_source_entry_ids
        ):
            raise ValueError("window compaction summary citations are duplicated")
        if len(set(self.artifact_locators)) != len(self.artifact_locators):
            raise ValueError("window compaction summary artifacts are duplicated")
        _validate_fingerprint(
            self,
            "window-compaction-summary:v1",
            "summary_fingerprint",
        )
        return self


class ContextInlineTextFact(FrozenContextFact):
    text: str
    chars: int = Field(ge=0)
    content_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _content(self) -> "ContextInlineTextFact":
        if self.chars != len(self.text):
            raise ValueError("inline candidate char count mismatch")
        if self.content_fingerprint != context_fingerprint(
            "context-inline-text:v1", self.text
        ):
            raise ValueError("inline candidate content fingerprint mismatch")
        return self


ContextCandidateAuthorityFact.model_rebuild()


class ContextArtifactTextFact(FrozenContextFact):
    artifact_id: str = Field(min_length=1)
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    content_fingerprint: str = Field(min_length=1)
    expected_chars: int = Field(ge=0)


ContextSectionPayloadFact: TypeAlias = ContextInlineTextFact | ContextArtifactTextFact


class ContextSectionCandidate(FrozenContextFact):
    schema_version: Literal["context-candidate:v1"] = "context-candidate:v1"
    candidate_id: str = Field(min_length=1)
    source_kind: ContextCandidateSourceKind
    source_instance_id: str = Field(min_length=1)
    source_fact_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_ids: tuple[str, ...]
    channel: ContextChannelFact
    priority: int
    required: bool
    stability: Literal["stable", "run", "step", "ephemeral"]
    lifecycle_dependency_fingerprint: str | None
    lowering_kind: ContextCandidateLoweringKind
    payload: ContextSectionPayloadFact
    source_timing: ContextSourceTimingFact
    semantic_fingerprint: str = Field(min_length=1)
    candidate_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _candidate(self) -> "ContextSectionCandidate":
        expected_channel, expected_lowering = _CONTEXT_CANDIDATE_CHANNEL_MATRIX[
            self.source_kind
        ]
        if (self.channel, self.lowering_kind) != (
            expected_channel,
            expected_lowering,
        ):
            raise ValueError("candidate channel/lowering matrix mismatch")
        if self.stability != "ephemeral" and not self.lifecycle_dependency_fingerprint:
            raise ValueError("cacheable candidate requires dependency fingerprint")
        if (
            not self.source_fact_refs
            and not self.source_artifact_ids
            and self.source_kind != "runtime_context"
        ):
            raise ValueError("candidate requires durable source attribution")
        expected_semantic = context_fingerprint(
            "context-section-candidate-semantic:v1",
            self.model_dump(
                mode="json",
                exclude={
                    "candidate_id",
                    "candidate_fingerprint",
                    "semantic_fingerprint",
                },
            ),
        )
        if self.semantic_fingerprint != expected_semantic:
            raise ValueError("candidate semantic fingerprint mismatch")
        _validate_fingerprint(
            self, "context-section-candidate-fact:v1", "candidate_fingerprint"
        )
        return self


class ContextCandidateLifecycleKeyFact(FrozenContextFact):
    source_instance_id: str
    candidate_id: str
    stability: str
    scope_id: str
    dependency_fingerprint: str
    policy_version: str


class ContextCandidateLifecycleDecisionFact(FrozenContextFact):
    candidate_id: str
    status: Literal["freshly_collected", "reused", "not_cacheable"]
    reason_code: str
    cache_key: ContextCandidateLifecycleKeyFact | None
    replaced_candidate_fingerprint: str | None
    decision_fingerprint: str

    @model_validator(mode="after")
    def _decision(self) -> "ContextCandidateLifecycleDecisionFact":
        if self.status == "not_cacheable":
            if self.cache_key is not None:
                raise ValueError(
                    "not-cacheable lifecycle decision cannot carry cache key"
                )
        elif self.cache_key is None:
            raise ValueError("cacheable lifecycle decision requires cache key")
        if self.status == "reused" and self.replaced_candidate_fingerprint is not None:
            raise ValueError("reused lifecycle decision cannot replace a candidate")
        _validate_fingerprint(
            self,
            "context-candidate-lifecycle-decision:v1",
            "decision_fingerprint",
        )
        return self


class ContextCandidateInvalidationFact(FrozenContextFact):
    candidate_id: str
    old_candidate_fingerprint: str
    new_candidate_fingerprint: str
    reason_code: str
    invalidation_fingerprint: str

    @model_validator(mode="after")
    def _invalidation(self) -> "ContextCandidateInvalidationFact":
        if self.old_candidate_fingerprint == self.new_candidate_fingerprint:
            raise ValueError("candidate invalidation requires a changed fingerprint")
        _validate_fingerprint(
            self,
            "context-candidate-invalidation:v1",
            "invalidation_fingerprint",
        )
        return self


class ContextCandidateCollectionDecisionFact(FrozenContextFact):
    source_kind: str
    selected_source_ids: tuple[str, ...]
    omitted_source_count: int = Field(ge=0)
    reason_code: str
    policy_fingerprint: str
    decision_fingerprint: str

    @model_validator(mode="after")
    def _decision(self) -> "ContextCandidateCollectionDecisionFact":
        _ordered_unique(self.selected_source_ids, "selected candidate source IDs")
        _validate_fingerprint(
            self,
            "context-candidate-collection-decision:v1",
            "decision_fingerprint",
        )
        return self


class PreparedContextCandidateEntryFact(FrozenContextFact):
    candidate: ContextSectionCandidate
    lifecycle: ContextCandidateLifecycleDecisionFact

    @model_validator(mode="after")
    def _entry(self) -> "PreparedContextCandidateEntryFact":
        if self.lifecycle.candidate_id != self.candidate.candidate_id:
            raise ValueError("candidate lifecycle owner mismatch")
        return self


class PreparedContextCandidateSet(FrozenContextFact):
    policy: ContextCandidateCollectionPolicyFact
    entries: tuple[PreparedContextCandidateEntryFact, ...]
    collection_decisions: tuple[ContextCandidateCollectionDecisionFact, ...]
    invalidations: tuple[ContextCandidateInvalidationFact, ...]
    candidate_set_fingerprint: str

    @model_validator(mode="after")
    def _candidate_set(self) -> "PreparedContextCandidateSet":
        ids = tuple(entry.candidate.candidate_id for entry in self.entries)
        _ordered_unique(ids, "prepared candidate IDs")
        selected = tuple(
            source_id
            for decision in self.collection_decisions
            for source_id in decision.selected_source_ids
        )
        if len(selected) != len(set(selected)):
            raise ValueError("collection decisions select a source more than once")
        _validate_fingerprint(
            self, "prepared-context-candidate-set:v1", "candidate_set_fingerprint"
        )
        return self


class ContextInputFailureReasonCode(StrEnum):
    LEDGER_UNTRUSTED = "ledger_untrusted"
    EVENT_SLICE_INVALID = "event_slice_invalid"
    SNAPSHOT_JOIN_MISMATCH = "snapshot_join_mismatch"
    TRANSCRIPT_INVALID = "transcript_invalid"
    TOOL_RESULT_INVALID = "tool_result_invalid"
    CANDIDATE_INVALID = "candidate_invalid"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    LONG_HORIZON_PREPARATION_CYCLE_EXCEEDED = (
        "long_horizon_preparation_cycle_exceeded"
    )
    CONTEXT_COMPILE_ATTEMPTS_EXHAUSTED = "context_compile_attempts_exhausted"
    LONG_HORIZON_FOLD_FAILED = "long_horizon_fold_failed"
    TOOL_OBSERVATION_PROJECTION_FAILED = "tool_observation_projection_failed"
    OBSERVATION_ROLLUP_FAILED = "observation_rollup_failed"
    WINDOW_COMPACTION_PLANNING_FAILED = "window_compaction_planning_failed"
    PAYLOAD_CONSISTENCY_FAILED = "payload_consistency_failed"
    MANIFEST_CONFIRMED_ABSENT = "manifest_confirmed_absent"
    MANIFEST_CONFLICT = "manifest_conflict"
    MANIFEST_OUTCOME_UNKNOWN = "manifest_outcome_unknown"
    MANIFEST_DEADLINE_EXCEEDED = "manifest_deadline_exceeded"


class ContextCompileFailureStage(StrEnum):
    EVENT_SLICE = "event_slice"
    SNAPSHOT_BUILD = "snapshot_build"
    TRANSCRIPT_NORMALIZATION = "transcript_normalization"
    TOOL_RESULT_NORMALIZATION = "tool_result_normalization"
    CANDIDATE_COLLECTION = "candidate_collection"
    CANDIDATE_MATERIALIZATION = "candidate_materialization"
    TOOL_RESULT_POLICY_RESOLUTION = "tool_result_policy_resolution"
    RENDER_CACHE_PREPARE = "render_cache_prepare"
    CANDIDATE_LIFECYCLE_PREPARE = "candidate_lifecycle_prepare"
    INPUT_MANIFEST_WRITE = "input_manifest_write"
    TOOL_RESULT_RENDER = "tool_result_render"
    CONTEXT_COMPILE = "context_compile"
    CONTEXT_BUDGET = "context_budget"
    LONG_HORIZON_PREPARATION = "long_horizon_preparation"
    LONG_HORIZON_FOLD = "long_horizon_fold"
    TOOL_OBSERVATION_PROJECTION = "tool_observation_projection"
    OBSERVATION_ROLLUP = "observation_rollup"
    WINDOW_COMPACTION_PLANNING = "window_compaction_planning"
    PAYLOAD_CONSISTENCY = "payload_consistency"


class ContextCompileInputAuditFact(FrozenContextFact):
    snapshot_id: str
    snapshot_semantic_fingerprint: str
    snapshot_fact_fingerprint: str
    snapshot_schema_version: str
    compiler_contract_version: str
    source_runtime_session_id: str
    authority_from_sequence: int = Field(ge=1)
    source_through_sequence: int = Field(ge=1)
    authority_slice_plan_fingerprint: str
    transcript_projection_window_fingerprint: str
    run_start_event_id: str
    run_start_sequence: int = Field(ge=1)
    continuation_event_id: str | None
    continuation_sequence: int | None = Field(default=None, ge=1)
    continuation_count: int = Field(ge=0)
    resolved_model_call_id: str
    model_call_index: int = Field(ge=1)
    compile_attempt_index: int = Field(ge=1)
    context_retry_index: int = Field(ge=0)
    transcript_fingerprint: str
    transcript_message_count: int = Field(ge=0)
    transcript_pair_count: int = Field(ge=0)
    tool_result_units_fingerprint: str
    tool_result_unit_count: int = Field(ge=0)
    tool_result_render_policy_fingerprint: str
    tool_result_render_input_fingerprint: str
    prepared_candidate_set_fingerprint: str
    section_candidate_count: int = Field(ge=0)
    input_aggregate_fingerprint: str
    input_manifest_artifact_id: str
    input_manifest_fingerprint: str
    long_horizon_attribution_fingerprint: str = Field(min_length=1)
    input_manifest_schema_version: Literal["context-input-manifest:v2"] = (
        "context-input-manifest:v2"
    )
    input_manifest_write_outcome: Literal["stored", "confirmed_existing"]

    @model_validator(mode="after")
    def _audit(self) -> "ContextCompileInputAuditFact":
        continuation_pair = (
            self.continuation_event_id,
            self.continuation_sequence,
        )
        if (continuation_pair[0] is None) != (continuation_pair[1] is None):
            raise ValueError("continuation audit attribution must be all-or-none")
        if self.continuation_count == 0 and continuation_pair[0] is not None:
            raise ValueError("zero continuation count cannot carry latest continuation")
        if self.continuation_count > 0 and continuation_pair[0] is None:
            raise ValueError("continuation audit requires latest continuation")
        if self.authority_from_sequence > self.source_through_sequence:
            raise ValueError("input audit authority range is reversed")
        return self


class ContextCompileInputFailureFact(FrozenContextFact):
    failure_stage: ContextCompileFailureStage
    context_id: str
    resolved_model_call_id: str
    model_call_index: int = Field(ge=1)
    compile_attempt_index: int = Field(ge=1)
    context_retry_index: int = Field(ge=0)
    snapshot_id: str | None
    source_through_sequence: int | None = Field(default=None, ge=1)
    available_component_fingerprints: tuple[tuple[str, str], ...]
    input_aggregate_fingerprint: str | None
    manifest_candidate_artifact_id: str | None
    manifest_candidate_content_fingerprint: str | None
    manifest_candidate_metadata_fingerprint: str | None
    manifest_write_outcome: Literal[
        "not_attempted",
        "confirmed_absent",
        "conflict",
        "outcome_unknown",
        "deadline_exceeded",
    ]
    reason_code: ContextInputFailureReasonCode

    @model_validator(mode="after")
    def _failure(self) -> "ContextCompileInputFailureFact":
        keys = tuple(item[0] for item in self.available_component_fingerprints)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("available component fingerprints must be key-sorted")
        candidates = (
            self.manifest_candidate_artifact_id,
            self.manifest_candidate_content_fingerprint,
            self.manifest_candidate_metadata_fingerprint,
        )
        manifest_stage = self.failure_stage == "input_manifest_write"
        if manifest_stage:
            if any(item is None for item in candidates):
                raise ValueError("manifest write failure requires stable candidate")
            if self.manifest_write_outcome == "not_attempted":
                raise ValueError("manifest write failure requires write outcome")
        elif (
            any(item is not None for item in candidates)
            or self.manifest_write_outcome != "not_attempted"
        ):
            raise ValueError("pre-manifest failure cannot carry manifest outcome")
        return self


class ContextCompileInputManifestFact(FrozenContextFact):
    schema_version: Literal["context-input-manifest:v2"] = "context-input-manifest:v2"
    input_aggregate_fingerprint: str
    snapshot: ContextFactSnapshotFact
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
    subagent_graph_acceleration: SubagentGraphAccelerationFact
    prepared_candidate_set: PreparedContextCandidateSet
    transcript_fingerprint: str
    tool_result_units_fingerprint: str
    tool_result_render_policy: ResolvedToolResultRenderPolicyFact
    tool_result_render_input_fingerprint: str
    active_window: ContextWindowFact
    window_policy: LongHorizonContextAllocationPolicyFact
    projection_state: ContextWindowProjectionState
    projected_tool_result_refs: tuple[ProjectedToolResultCompileRefFact, ...]
    prepared_rollup_units: tuple[PreparedObservationRollupUnit, ...]
    rollout_state: RolloutBudgetStateFact
    context_budget_decision: LongHorizonContextBudgetDecisionFact
    projection_pressure_shadow: LongHorizonProjectionPressureShadowFact
    projection_target_unreachable: ProjectionTargetUnreachableAuditFact | None
    safe_point_revision: int = Field(ge=0)
    compiler_contract_version: str
    manifest_fingerprint: str

    @classmethod
    def from_trusted_factory_payload(
        cls,
        payload: dict[str, object],
    ) -> "ContextCompileInputManifestFact":
        """Validate a factory-owned payload without hashing it a second time."""

        fingerprint = context_fingerprint(
            "context-compile-input-manifest:v2", payload
        )
        token = _TRUSTED_MANIFEST_FINGERPRINT.set(fingerprint)
        try:
            return cls(**payload, manifest_fingerprint=fingerprint)
        finally:
            _TRUSTED_MANIFEST_FINGERPRINT.reset(token)

    @model_validator(mode="after")
    def _manifest(self) -> "ContextCompileInputManifestFact":
        if (
            self.subagent_graph_semantic_source
            != self.snapshot.subagent_graph_semantic_source
            or self.subagent_graph_acceleration
            != self.snapshot.subagent_graph_acceleration
        ):
            raise ValueError("manifest subagent graph attribution mismatch")
        snapshot_policy = self.snapshot.compile_policy
        if self.tool_result_render_policy.basis != snapshot_policy.tool_result_basis:
            raise ValueError("manifest tool-result policy basis mismatch")
        if self.prepared_candidate_set.policy != snapshot_policy.candidate_collection:
            raise ValueError("manifest candidate policy mismatch")
        if (
            self.compiler_contract_version
            != self.snapshot.identity.compiler_contract_version
        ):
            raise ValueError("manifest compiler contract mismatch")
        attribution = self.snapshot.long_horizon_attribution
        if (
            self.active_window.window_id != attribution.window_id
            or self.active_window.generation != attribution.window_generation
            or self.active_window.window_semantic_fingerprint
            != attribution.window_semantic_fingerprint
            or self.projection_state.window_id != attribution.window_id
            or self.projection_state.projection_generation
            != attribution.projection_generation
            or self.projection_state.state_semantic_fingerprint
            != attribution.projection_state_fingerprint
            or self.rollout_state.account_id != attribution.rollout_account_id
            or self.rollout_state.through_sequence
            != attribution.rollout_state_through_sequence
            or self.rollout_state.phase != attribution.rollout_phase
            or self.rollout_state.state_fingerprint
            != attribution.rollout_state_fingerprint
            or self.context_budget_decision != attribution.budget_decision
            or self.window_policy.policy_fingerprint
            != self.active_window.window_policy_fingerprint
        ):
            raise ValueError("manifest long-horizon attribution mismatch")
        projection_ids = tuple(
            item.unit_id for item in self.projection_state.unit_projections
        )
        ref_ids = tuple(item.tool_result_unit_id for item in self.projected_tool_result_refs)
        if (
            len(ref_ids) != len(set(ref_ids))
            or len(projection_ids) != len(set(projection_ids))
            or set(ref_ids) != set(projection_ids)
        ):
            raise ValueError("manifest projected tool-result refs are incomplete")
        rollup_ids = tuple(item.rollup.rollup_id for item in self.prepared_rollup_units)
        if rollup_ids != tuple(item.rollup_id for item in self.projection_state.rollups):
            raise ValueError("manifest prepared rollups differ from projection state")
        if (
            self.projection_pressure_shadow.window_id != attribution.window_id
            or self.projection_pressure_shadow.source_through_sequence
            != self.context_budget_decision.source_through_sequence
        ):
            raise ValueError("manifest projection pressure shadow mismatch")
        unreachable = self.projection_target_unreachable
        if unreachable is not None and (
            unreachable.source_projection_generation
            > self.projection_state.projection_generation
            or unreachable.target_projected_tokens
            != self.context_budget_decision.post_rewrite_target_tokens
            or unreachable.minimum_projected_tokens
            != self.projection_state.total_projected_tokens
        ):
            raise ValueError("manifest projection-unreachable audit mismatch")
        expected_aggregate = context_fingerprint(
            "context-compile-input-aggregate:v2",
            [
                self.snapshot.snapshot_semantic_fingerprint,
                self.transcript_fingerprint,
                self.tool_result_render_input_fingerprint,
                self.prepared_candidate_set.candidate_set_fingerprint,
                self.active_window.window_semantic_fingerprint,
                self.window_policy.policy_fingerprint,
                self.projection_state.state_semantic_fingerprint,
                tuple(
                    item.prepared_fingerprint for item in self.prepared_rollup_units
                ),
                self.rollout_state.state_fingerprint,
                self.context_budget_decision.decision_fingerprint,
                (
                    self.projection_target_unreachable.audit_fingerprint
                    if self.projection_target_unreachable is not None
                    else None
                ),
                self.compiler_contract_version,
            ],
        )
        if self.input_aggregate_fingerprint != expected_aggregate:
            raise ValueError("manifest aggregate input fingerprint mismatch")
        if _TRUSTED_MANIFEST_FINGERPRINT.get() != self.manifest_fingerprint:
            _validate_fingerprint(
                self, "context-compile-input-manifest:v2", "manifest_fingerprint"
            )
        return self


def _snapshot_semantic_payload(snapshot: ContextFactSnapshotFact) -> dict[str, object]:
    payload = snapshot.model_dump(
        mode="json",
        exclude={"snapshot_semantic_fingerprint", "snapshot_fact_fingerprint"},
    )
    identity = dict(payload["identity"])
    identity.pop("snapshot_id", None)
    identity.pop("context_id", None)
    payload["identity"] = identity
    payload.pop("primary_event_range", None)
    payload.pop("named_event_ranges", None)
    payload.pop("subagent_graph_acceleration", None)
    return payload


def _ordered_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _event_ref_is_within_ranges(
    ref: ContextEventReferenceFact,
    ranges: tuple[ContextEventRangeFact, ...],
) -> bool:
    return any(
        item.runtime_session_id == ref.runtime_session_id
        and item.first_sequence <= ref.sequence <= item.through_sequence
        for item in ranges
    )


def _validate_fingerprint(
    model: FrozenContextFact, namespace: str, field_name: str
) -> None:
    expected = context_fingerprint(
        namespace, model.model_dump(mode="json", exclude={field_name})
    )
    if getattr(model, field_name) != expected:
        raise ValueError(f"{field_name} mismatch")


__all__ = [
    "CapabilityDescriptorRenderAttributionFact",
    "CompactedWindowReferenceFact",
    "ContextAllocationPolicyFact",
    "ContextArtifactTextFact",
    "ContextArtifactToolSchemaFact",
    "ContextAuthoritySlicePlan",
    "ContextCandidateCollectionDecisionFact",
    "ContextCandidateCollectionPolicyFact",
    "ContextCandidateAuthorityFact",
    "ContextCandidateSourceSelectionFact",
    "ContextCandidateLoweringKind",
    "ContextCandidateSourceKind",
    "ContextCandidateInvalidationFact",
    "ContextCandidateLifecycleDecisionFact",
    "ContextCandidateLifecycleKeyFact",
    "ContextChannelFact",
    "ContextCompilePolicyFact",
    "ContextCompileInputAuditFact",
    "ContextCompileInputFailureFact",
    "ContextCompileInputManifestFact",
    "ContextCompileTimingFact",
    "ContextContinuationReferenceFact",
    "ContextEventRangeFact",
    "ContextEventReferenceFact",
    "ContextFactSnapshotFact",
    "ContextInlineTextFact",
    "ContextInlineToolSchemaFact",
    "ContextInputIdentityFact",
    "LongHorizonContextAttributionFact",
    "ContextInputFailureReasonCode",
    "ContextCompileFailureStage",
    "ContextPlanSnapshotFact",
    "ContextMaterializedToolSpecInput",
    "ContextProjectionReferenceFact",
    "ContextRunEntryReferenceFact",
    "ContextRuntimeEnvironmentFact",
    "ContextSectionCandidate",
    "ContextSectionPayloadFact",
    "ContextSourceTimingFact",
    "WindowCompactionPairGroupFact",
    "WindowCompactionSourceDocumentFact",
    "WindowCompactionSourceEntryFact",
    "WindowCompactionSummaryFact",
    "ContextStaticInstructionFact",
    "ContextToolSchemaFact",
    "ContextToolSpecFact",
    "FrozenContextFact",
    "FrozenJsonArrayFact",
    "FrozenJsonEntryFact",
    "FrozenJsonObjectFact",
    "FrozenJsonScalar",
    "FrozenJsonValue",
    "PreparedContextCandidateEntryFact",
    "PreparedContextCandidateSet",
    "ProjectedToolResultCompileRefFact",
    "ResolvedToolResultRenderPolicyFact",
    "RunPermissionSnapshotFact",
    "ToolArgumentsParseErrorCode",
    "ToolInteractionPairFact",
    "ToolResultEnvelopeRenderPolicyFact",
    "ToolResultRenderPolicyBasisFact",
    "TranscriptBlockFact",
    "TranscriptCompileInput",
    "TranscriptDataPlaceholderFact",
    "TranscriptMessageFact",
    "TranscriptProjectionWindowFact",
    "TranscriptTextBlockFact",
    "TranscriptThinkingBlockFact",
    "TranscriptToolCallFact",
    "TranscriptToolResultRefFact",
    "canonical_json_bytes",
    "canonical_utc_timestamp",
    "context_fingerprint",
    "freeze_json",
    "thaw_json",
]
