"""Append-aware, provider-neutral ContextSource contracts.

The facts in this module deliberately stop at source-owned semantics.  They do
not contain provider messages, provider JSON, generation heads, or mutable
runtime state.  Provider-input planning lives in ``primitives.provider_input``.
"""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_utc_timestamp,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    register_durable_fact,
)


Fingerprint = str


def _fact(
    schema_version: str,
    own_fingerprint_field: str,
    domain_separator: str,
):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_fingerprint_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


def raw_content_sha256(value: str | bytes) -> str:
    content = value.encode("utf-8") if isinstance(value, str) else value
    return f"sha256:{sha256(content).hexdigest()}"


class ContextSourceId(StrEnum):
    SYSTEM = "system"
    RUNTIME_ENVIRONMENT = "runtime_environment"
    RUNTIME_CLOCK = "runtime_clock"
    MEMORY_INSTRUCTION = "memory_instruction"
    MEMORY_PROJECTION = "memory_projection"
    CAPABILITY_CATALOG = "capability_catalog"
    ACTIVE_SKILL = "active_skill"
    PLAN = "plan"
    RECOVERY = "recovery"
    ROLLOUT_STATUS = "rollout_status"
    SUBAGENT_HANDOFF = "subagent_handoff"
    SUBAGENT_RESULT = "subagent_result"
    MCP_DIAGNOSTIC = "mcp_diagnostic"
    WORKSPACE_SKILL = "workspace_skill"


@_fact(
    "ledger_authority_horizon.v1",
    "horizon_fingerprint",
    "ledger-authority-horizon:v1",
)
class LedgerAuthorityHorizonFact(FrozenFactBase):
    schema_version: Literal["ledger_authority_horizon.v1"] = (
        "ledger_authority_horizon.v1"
    )
    runtime_session_id: str = Field(min_length=1)
    through_sequence: int = Field(ge=0)
    ledger_event_count_through: int = Field(ge=0)
    ledger_continuity_accumulator_through: Fingerprint
    horizon_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _count(self) -> "LedgerAuthorityHorizonFact":
        if self.ledger_event_count_through > self.through_sequence:
            raise ValueError("authority horizon event count exceeds high-water")
        return self


@_fact("ledger_sequence_range.v1", "range_fingerprint", "ledger-sequence-range:v1")
class LedgerSequenceRangeFact(FrozenFactBase):
    schema_version: Literal["ledger_sequence_range.v1"] = "ledger_sequence_range.v1"
    runtime_session_id: str = Field(min_length=1)
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    range_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "LedgerSequenceRangeFact":
        if self.first_sequence > self.last_sequence:
            raise ValueError("ledger sequence range is reversed")
        return self


@_fact(
    "ledger_authority_horizon_set_node_reference.v1",
    "reference_fingerprint",
    "ledger-authority-horizon-set-node-reference:v1",
)
class LedgerAuthorityHorizonSetNodeReferenceFact(FrozenFactBase):
    schema_version: Literal["ledger_authority_horizon_set_node_reference.v1"] = (
        "ledger_authority_horizon_set_node_reference.v1"
    )
    node_kind: Literal["leaf", "internal"]
    first_runtime_session_id: str = Field(min_length=1)
    last_runtime_session_id: str = Field(min_length=1)
    subtree_horizon_count: int = Field(gt=0)
    subtree_horizon_accumulator: Fingerprint
    artifact_reference: "ContextArtifactReferenceFact"
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "LedgerAuthorityHorizonSetNodeReferenceFact":
        if self.first_runtime_session_id > self.last_runtime_session_id:
            raise ValueError("horizon set node range is reversed")
        return self


@_fact(
    "ledger_authority_horizon_set_reference.v1",
    "reference_fingerprint",
    "ledger-authority-horizon-set-reference:v1",
)
class LedgerAuthorityHorizonSetReferenceFact(FrozenFactBase):
    schema_version: Literal["ledger_authority_horizon_set_reference.v1"] = (
        "ledger_authority_horizon_set_reference.v1"
    )
    horizon_count: int = Field(ge=0)
    ordered_horizon_accumulator: Fingerprint
    root_node_ref: LedgerAuthorityHorizonSetNodeReferenceFact | None
    set_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _empty(self) -> "LedgerAuthorityHorizonSetReferenceFact":
        if (self.horizon_count == 0) != (self.root_node_ref is None):
            raise ValueError("empty horizon set root matrix mismatch")
        if (
            self.root_node_ref is not None
            and self.root_node_ref.subtree_horizon_count != self.horizon_count
        ):
            raise ValueError("horizon set count differs from root")
        return self


@_fact(
    "context_artifact_reference.v1",
    "reference_fingerprint",
    "context-artifact-reference:v1",
)
class ContextArtifactReferenceFact(FrozenFactBase):
    schema_version: Literal["context_artifact_reference.v1"] = (
        "context_artifact_reference.v1"
    )
    artifact_id: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=128)
    content_sha256: Fingerprint
    content_bytes: int = Field(ge=0)
    artifact_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


@_fact(
    "context_source_input_authority.v1",
    "authority_fingerprint",
    "context-source-input-authority:v1",
)
class ContextSourceInputAuthorityFact(FrozenFactBase):
    schema_version: Literal["context_source_input_authority.v1"] = (
        "context_source_input_authority.v1"
    )
    source_id: ContextSourceId
    source_contract_id: str = Field(min_length=1)
    source_contract_version: str = Field(min_length=1)
    source_contract_fingerprint: Fingerprint
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    physical_input_policy_fingerprint: Fingerprint
    input_dependency_fingerprint: Fingerprint
    authority_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _horizons(self) -> "ContextSourceInputAuthorityFact":
        owners = tuple(item.runtime_session_id for item in self.authority_horizons)
        if owners != tuple(sorted(set(owners))):
            raise ValueError("source input horizons are not ordered/unique")
        return self


@_fact(
    "runtime_clock_source_contract.v1",
    "contract_fingerprint",
    "runtime-clock-source-contract:v1",
)
class RuntimeClockSourceContractFact(FrozenFactBase):
    schema_version: Literal["runtime_clock_source_contract.v1"] = (
        "runtime_clock_source_contract.v1"
    )
    contract_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    timezone_resolution_contract_fingerprint: Fingerprint
    proposal_reason_matrix_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


@_fact(
    "capability_tool_catalog_root.v1",
    "root_fingerprint",
    "capability-tool-catalog-root:v1",
)
class CapabilityToolCatalogRootFact(FrozenFactBase):
    """The sole owner of provider tool definitions for one generation root."""

    schema_version: Literal["capability_tool_catalog_root.v1"] = (
        "capability_tool_catalog_root.v1"
    )
    capability_snapshot_semantic_fingerprint: Fingerprint
    ordered_descriptor_fingerprints: tuple[Fingerprint, ...]
    ordered_tool_spec_fingerprints: tuple[Fingerprint, ...]
    tool_catalog_contract_fingerprint: Fingerprint
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    root_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordered(self) -> "CapabilityToolCatalogRootFact":
        for values, label in (
            (self.ordered_descriptor_fingerprints, "descriptor"),
            (self.ordered_tool_spec_fingerprints, "tool spec"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"capability {label} fingerprints are duplicated")
        owners = tuple(item.runtime_session_id for item in self.authority_horizons)
        if owners != tuple(sorted(set(owners))):
            raise ValueError("capability tool root horizons are not ordered/unique")
        return self


@_fact(
    "context_candidate_lowering_intent.v1",
    "intent_fingerprint",
    "context-candidate-lowering-intent:v1",
)
class ContextCandidateLoweringIntentFact(FrozenFactBase):
    schema_version: Literal["context_candidate_lowering_intent.v1"] = (
        "context_candidate_lowering_intent.v1"
    )
    intent_kind: Literal[
        "system_instruction",
        "leading_context",
        "paired_observation",
        "trailing_observation",
        "status_observation",
    ]
    role_constraint: Literal["system", "user", "tool", "runtime"] | None
    pairing_constraint: Literal["none", "must_follow_open_tool_call"]
    intent_contract_fingerprint: Fingerprint
    intent_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _pairing(self) -> "ContextCandidateLoweringIntentFact":
        if (
            self.pairing_constraint == "must_follow_open_tool_call"
            and self.intent_kind != "paired_observation"
        ):
            raise ValueError("pairing constraint requires paired-observation intent")
        return self


@_fact(
    "resolved_context_source_physical_input_policy.v1",
    "policy_fingerprint",
    "resolved-context-source-physical-input-policy:v1",
)
class ResolvedContextSourcePhysicalInputPolicyFact(FrozenFactBase):
    schema_version: Literal["resolved_context_source_physical_input_policy.v1"] = (
        "resolved_context_source_physical_input_policy.v1"
    )
    resolved_model_input_token_limit: int = Field(gt=0)
    resolved_max_provider_input_units: int = Field(gt=0)
    tokenizer_or_estimator_contract_fingerprint: Fingerprint
    canonical_codec_contract_fingerprint: Fingerprint
    conservative_utf8_bytes_per_token_numerator: int = Field(gt=0)
    conservative_utf8_bytes_per_token_denominator: int = Field(gt=0)
    canonical_encoding_expansion_numerator: int = Field(gt=0)
    canonical_encoding_expansion_denominator: int = Field(gt=0)
    structural_overhead_bytes_per_unit: int = Field(ge=0)
    max_token_budget_admissible_utf8_bytes: int = Field(gt=0)
    max_canonical_materialization_bytes: int = Field(gt=0)
    max_inline_item_utf8_bytes: int = Field(gt=0)
    max_hydrated_working_set_bytes: int = Field(gt=0)
    max_source_entries: int = Field(gt=0)
    artifact_page_bytes: int = Field(gt=0)
    policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _derived_limits(self) -> "ResolvedContextSourcePhysicalInputPolicyFact":
        utf8_numerator = (
            self.resolved_model_input_token_limit
            * self.conservative_utf8_bytes_per_token_numerator
        )
        expected_utf8 = (
            utf8_numerator + self.conservative_utf8_bytes_per_token_denominator - 1
        ) // self.conservative_utf8_bytes_per_token_denominator
        canonical_numerator = (
            expected_utf8 * self.canonical_encoding_expansion_numerator
        )
        expected_canonical = (
            canonical_numerator + self.canonical_encoding_expansion_denominator - 1
        ) // self.canonical_encoding_expansion_denominator
        expected_canonical += (
            self.resolved_max_provider_input_units
            * self.structural_overhead_bytes_per_unit
        )
        if self.max_token_budget_admissible_utf8_bytes != expected_utf8:
            raise ValueError("context source UTF-8 quote is not derived")
        if self.max_canonical_materialization_bytes != expected_canonical:
            raise ValueError("context source canonical quote is not derived")
        if self.max_source_entries < self.resolved_max_provider_input_units:
            raise ValueError("context source entry cap is below provider unit cap")
        if self.max_inline_item_utf8_bytes > self.max_hydrated_working_set_bytes:
            raise ValueError("context source inline cap exceeds hydrated working set")
        return self


@_fact(
    "inline_context_source_content_semantic.v1",
    "semantic_fingerprint",
    "inline-context-source-content-semantic:v1",
)
class InlineContextSourceContentSemanticFact(FrozenFactBase):
    schema_version: Literal["inline_context_source_content_semantic.v1"] = (
        "inline_context_source_content_semantic.v1"
    )
    content_kind: Literal["inline_text"] = "inline_text"
    text: str
    chars: int = Field(ge=0)
    utf8_bytes: int = Field(ge=0)
    media_type: Literal["text/plain", "text/markdown"]
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content(self) -> "InlineContextSourceContentSemanticFact":
        if self.chars != len(self.text):
            raise ValueError("context source content char count mismatch")
        if self.utf8_bytes != len(self.text.encode("utf-8")):
            raise ValueError("context source content byte count mismatch")
        return self


@_fact(
    "artifact_context_source_content_semantic.v1",
    "semantic_fingerprint",
    "artifact-context-source-content-semantic:v1",
)
class ArtifactContextSourceContentSemanticFact(FrozenFactBase):
    schema_version: Literal["artifact_context_source_content_semantic.v1"] = (
        "artifact_context_source_content_semantic.v1"
    )
    content_kind: Literal["artifact_text"] = "artifact_text"
    content_sha256: Fingerprint
    expected_chars: int = Field(ge=0)
    expected_utf8_bytes: int = Field(ge=0)
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    codec_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


ContextSourceContentSemanticFact: TypeAlias = (
    InlineContextSourceContentSemanticFact | ArtifactContextSourceContentSemanticFact
)


@_fact(
    "context_source_absolute_timing.v1",
    "fact_fingerprint",
    "context-source-absolute-timing:v1",
)
class ContextSourceAbsoluteTimingFact(FrozenFactBase):
    schema_version: Literal["context_source_absolute_timing.v1"] = (
        "context_source_absolute_timing.v1"
    )
    observed_at_utc: str | None
    source_started_at_utc: str | None
    source_ended_at_utc: str | None
    source_sequence_ranges: tuple[LedgerSequenceRangeFact, ...]
    clock_source: Literal[
        "event_created_at",
        "terminal_observation",
        "artifact_metadata",
        "host_clock",
        "mixed",
    ]
    freshness_kind: Literal[
        "static",
        "current_turn",
        "current_run_tail",
        "historical_replay",
        "compacted_history",
    ]
    timing_contract_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _timing(self) -> "ContextSourceAbsoluteTimingFact":
        for field_name in (
            "observed_at_utc",
            "source_started_at_utc",
            "source_ended_at_utc",
        ):
            value = getattr(self, field_name)
            if value is not None and canonical_utc_timestamp(value) != value:
                raise ValueError(f"{field_name} must be canonical UTC")
        if (
            self.source_started_at_utc is not None
            and self.source_ended_at_utc is not None
            and self.source_started_at_utc > self.source_ended_at_utc
        ):
            raise ValueError("context source timing interval is reversed")
        keys = tuple(
            (item.runtime_session_id, item.first_sequence)
            for item in self.source_sequence_ranges
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("context source timing ranges are not ordered/unique")
        return self


@_fact(
    "context_source_timing_semantic.v1",
    "semantic_fingerprint",
    "context-source-timing-semantic:v1",
)
class ContextSourceTimingSemanticFact(FrozenFactBase):
    schema_version: Literal["context_source_timing_semantic.v1"] = (
        "context_source_timing_semantic.v1"
    )
    rendered_absolute_time: str
    timing_semantic_kind: Literal[
        "observed_at", "source_interval", "terminal_observation"
    ]
    rendering_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


@_fact(
    "runtime_clock_proposal_payload.v1",
    "semantic_fingerprint",
    "runtime-clock-proposal-payload:v1",
)
class RuntimeClockProposalPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_clock_proposal_payload.v1"] = (
        "runtime_clock_proposal_payload.v1"
    )
    observed_at_utc: str
    timezone_name: str
    local_date: str
    proposal_reason: Literal[
        "compile",
        "user_turn",
        "long_operation_completed",
        "local_date_changed",
        "explicit_temporal_requirement",
    ]
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _clock(self) -> "RuntimeClockProposalPayloadFact":
        if canonical_utc_timestamp(self.observed_at_utc) != self.observed_at_utc:
            raise ValueError("runtime clock proposal is not canonical UTC")
        return self


@_fact(
    "provider_runtime_clock_observation_semantic.v1",
    "semantic_fingerprint",
    "provider-runtime-clock-observation-semantic:v1",
)
class ProviderRuntimeClockObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_runtime_clock_observation_semantic.v1"] = (
        "provider_runtime_clock_observation_semantic.v1"
    )
    proposal: RuntimeClockProposalPayloadFact
    append_reason: Literal[
        "generation_start",
        "user_turn",
        "staleness_threshold",
        "long_operation_completed",
        "local_date_changed",
        "explicit_temporal_requirement",
    ]
    supersedes_clock_unit_semantic_fingerprint: Fingerprint | None
    semantic_fingerprint: Fingerprint


@_fact(
    "generation_root_lifecycle.v1",
    "lifecycle_fingerprint",
    "generation-root-lifecycle:v1",
)
class GenerationRootLifecycleFact(FrozenFactBase):
    schema_version: Literal["generation_root_lifecycle.v1"] = (
        "generation_root_lifecycle.v1"
    )
    lifecycle_kind: Literal["generation_root"] = "generation_root"
    on_semantic_change: Literal["rollover"] = "rollover"
    lifecycle_fingerprint: Fingerprint


@_fact(
    "append_once_lifecycle.v1",
    "lifecycle_fingerprint",
    "append-once-lifecycle:v1",
)
class AppendOnceLifecycleFact(FrozenFactBase):
    schema_version: Literal["append_once_lifecycle.v1"] = "append_once_lifecycle.v1"
    lifecycle_kind: Literal["append_once"] = "append_once"
    duplicate_semantic_identity: Literal["no_op"] = "no_op"
    conflicting_same_key: Literal["contract_mismatch"] = "contract_mismatch"
    lifecycle_fingerprint: Fingerprint


@_fact(
    "append_revision_lifecycle.v1",
    "lifecycle_fingerprint",
    "append-revision-lifecycle:v1",
)
class AppendRevisionLifecycleFact(FrozenFactBase):
    schema_version: Literal["append_revision_lifecycle.v1"] = (
        "append_revision_lifecycle.v1"
    )
    lifecycle_kind: Literal["append_revision"] = "append_revision"
    supersession_semantics: Literal["latest_revision_wins"] = "latest_revision_wins"
    continuity_kind: Literal["complete_snapshot", "strict_delta"]
    source_revision_contract_fingerprint: Fingerprint
    lifecycle_fingerprint: Fingerprint


@_fact(
    "audit_only_lifecycle.v1",
    "lifecycle_fingerprint",
    "audit-only-lifecycle:v1",
)
class AuditOnlyLifecycleFact(FrozenFactBase):
    schema_version: Literal["audit_only_lifecycle.v1"] = "audit_only_lifecycle.v1"
    lifecycle_kind: Literal["audit_only"] = "audit_only"
    model_visible: Literal[False] = False
    lifecycle_fingerprint: Fingerprint


ContextSourceLifecycleFact: TypeAlias = (
    GenerationRootLifecycleFact
    | AppendOnceLifecycleFact
    | AppendRevisionLifecycleFact
    | AuditOnlyLifecycleFact
)


@_fact(
    "immutable_source_revision.v1",
    "revision_fingerprint",
    "immutable-source-revision:v1",
)
class ImmutableSourceRevisionFact(FrozenFactBase):
    schema_version: Literal["immutable_source_revision.v1"] = (
        "immutable_source_revision.v1"
    )
    revision_kind: Literal["immutable"] = "immutable"
    source_revision_id: str = Field(min_length=1)
    source_state_semantic_fingerprint: Fingerprint
    revision_fingerprint: Fingerprint


@_fact(
    "event_source_revision.v1",
    "revision_fingerprint",
    "event-source-revision:v1",
)
class EventSourceRevisionFact(FrozenFactBase):
    schema_version: Literal["event_source_revision.v1"] = "event_source_revision.v1"
    revision_kind: Literal["event"] = "event"
    source_revision_id: str = Field(min_length=1)
    producer_event_semantic_fingerprint: Fingerprint
    revision_fingerprint: Fingerprint


@_fact(
    "snapshot_source_revision.v1",
    "revision_fingerprint",
    "snapshot-source-revision:v1",
)
class SnapshotSourceRevisionFact(FrozenFactBase):
    schema_version: Literal["snapshot_source_revision.v1"] = (
        "snapshot_source_revision.v1"
    )
    revision_kind: Literal["complete_snapshot"] = "complete_snapshot"
    source_revision_id: str = Field(min_length=1)
    source_revision_ordinal: int = Field(ge=0)
    predecessor_source_revision_id: str | None
    predecessor_source_revision_fingerprint: Fingerprint | None
    source_state_semantic_fingerprint: Fingerprint
    revision_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _predecessor(self) -> "SnapshotSourceRevisionFact":
        if (self.predecessor_source_revision_id is None) != (
            self.predecessor_source_revision_fingerprint is None
        ):
            raise ValueError("source revision predecessor is all-or-none")
        return self


@_fact(
    "delta_source_revision.v1",
    "revision_fingerprint",
    "delta-source-revision:v1",
)
class DeltaSourceRevisionFact(FrozenFactBase):
    schema_version: Literal["delta_source_revision.v1"] = "delta_source_revision.v1"
    revision_kind: Literal["strict_delta"] = "strict_delta"
    source_revision_id: str = Field(min_length=1)
    source_revision_ordinal: int = Field(ge=1)
    predecessor_source_revision_id: str = Field(min_length=1)
    predecessor_source_revision_fingerprint: Fingerprint
    delta_semantic_fingerprint: Fingerprint
    resulting_source_state_semantic_fingerprint: Fingerprint
    revision_fingerprint: Fingerprint


CanonicalContextSourceRevisionFact: TypeAlias = (
    ImmutableSourceRevisionFact
    | EventSourceRevisionFact
    | SnapshotSourceRevisionFact
    | DeltaSourceRevisionFact
)


# Source-owned payloads.  Their structured metadata prevents consumers from
# reverse-engineering domain identity from rendered prose.


@_fact(
    "system_instruction_payload.v1",
    "semantic_fingerprint",
    "system-instruction-payload:v1",
)
class SystemInstructionPayloadFact(FrozenFactBase):
    schema_version: Literal["system_instruction_payload.v1"] = (
        "system_instruction_payload.v1"
    )
    instruction_source_id: str
    instruction_contract_version: str
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact(
    "runtime_environment_payload.v1",
    "semantic_fingerprint",
    "runtime-environment-payload:v1",
)
class RuntimeEnvironmentPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_environment_payload.v1"] = (
        "runtime_environment_payload.v1"
    )
    workspace_kind: str
    model_visible_workspace_root: str
    terminal_current_cwd: str
    session_timezone: str | None
    rendering_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


@_fact(
    "memory_instruction_payload.v1",
    "semantic_fingerprint",
    "memory-instruction-payload:v1",
)
class MemoryInstructionPayloadFact(FrozenFactBase):
    schema_version: Literal["memory_instruction_payload.v1"] = (
        "memory_instruction_payload.v1"
    )
    instruction_contract_version: str
    memory_scope_policy_fingerprint: Fingerprint
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact(
    "memory_projection_payload.v1",
    "semantic_fingerprint",
    "memory-projection-payload:v1",
)
class MemoryProjectionPayloadFact(FrozenFactBase):
    schema_version: Literal["memory_projection_payload.v1"] = (
        "memory_projection_payload.v1"
    )
    projection_semantic_fingerprint: Fingerprint
    ordered_memory_semantic_fingerprints: tuple[Fingerprint, ...]
    selection_contract_fingerprint: Fingerprint
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact(
    "capability_catalog_payload.v1",
    "semantic_fingerprint",
    "capability-catalog-payload:v1",
)
class CapabilityCatalogPayloadFact(FrozenFactBase):
    schema_version: Literal["capability_catalog_payload.v1"] = (
        "capability_catalog_payload.v1"
    )
    prose_projection_semantic_fingerprint: Fingerprint
    ordered_projection_entry_semantic_fingerprints: tuple[Fingerprint, ...]
    projection_contract_fingerprint: Fingerprint
    prose_content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact(
    "active_skill_payload.v1",
    "semantic_fingerprint",
    "active-skill-payload:v1",
)
class ActiveSkillPayloadFact(FrozenFactBase):
    schema_version: Literal["active_skill_payload.v1"] = "active_skill_payload.v1"
    skill_projection_semantic_fingerprint: Fingerprint
    ordered_active_skill_semantic_fingerprints: tuple[Fingerprint, ...]
    projection_contract_fingerprint: Fingerprint
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact(
    "plan_revision_payload.v1",
    "semantic_fingerprint",
    "plan-revision-payload:v1",
)
class PlanRevisionPayloadFact(FrozenFactBase):
    schema_version: Literal["plan_revision_payload.v1"] = "plan_revision_payload.v1"
    workflow_id: str | None
    active: bool
    canonical_plan_revision: int = Field(ge=0)
    plan_decision: Literal["enter", "continue", "revise", "exit", "inactive"]
    plan_semantic_fingerprint: Fingerprint
    content: ContextSourceContentSemanticFact | None
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _active(self) -> "PlanRevisionPayloadFact":
        if self.active != (self.workflow_id is not None and self.content is not None):
            raise ValueError("plan payload active fields are inconsistent")
        if not self.active and self.plan_decision != "inactive":
            raise ValueError("inactive plan payload requires inactive decision")
        return self


@_fact(
    "recovery_observation_payload.v1",
    "semantic_fingerprint",
    "recovery-observation-payload:v1",
)
class RecoveryObservationPayloadFact(FrozenFactBase):
    schema_version: Literal["recovery_observation_payload.v1"] = (
        "recovery_observation_payload.v1"
    )
    recovery_kind: Literal[
        "run_resume", "model_stream_recovered", "tool_resume", "window_recovered"
    ]
    stable_status_code: str
    recovery_semantic_fingerprint: Fingerprint
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact(
    "rollout_status_payload.v1",
    "semantic_fingerprint",
    "rollout-status-payload:v1",
)
class RolloutStatusPayloadFact(FrozenFactBase):
    schema_version: Literal["rollout_status_payload.v1"] = "rollout_status_payload.v1"
    rollout_account_semantic_fingerprint: Fingerprint
    phase: Literal["exploration", "finalization", "exhausted"]
    completed_model_calls: int = Field(ge=0)
    completed_tool_invocations: int = Field(ge=0)
    status_policy_fingerprint: Fingerprint
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact(
    "subagent_handoff_payload.v1",
    "semantic_fingerprint",
    "subagent-handoff-payload:v1",
)
class SubagentHandoffPayloadFact(FrozenFactBase):
    schema_version: Literal["subagent_handoff_payload.v1"] = (
        "subagent_handoff_payload.v1"
    )
    child_runtime_session_id: str
    spawn_semantic_fingerprint: Fingerprint
    handoff_semantic_fingerprint: Fingerprint
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact(
    "subagent_result_payload.v1",
    "semantic_fingerprint",
    "subagent-result-payload:v1",
)
class SubagentResultPayloadFact(FrozenFactBase):
    schema_version: Literal["subagent_result_payload.v1"] = "subagent_result_payload.v1"
    child_runtime_session_id: str
    completion_semantic_fingerprint: Fingerprint
    delivery_semantic_fingerprint: Fingerprint
    result_state: Literal["success", "error", "interrupted"]
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


@_fact("mcp_diagnostic_entry.v1", "entry_fingerprint", "mcp-diagnostic-entry:v1")
class McpDiagnosticEntryFact(FrozenFactBase):
    schema_version: Literal["mcp_diagnostic_entry.v1"] = "mcp_diagnostic_entry.v1"
    server_id: str
    status: Literal["starting", "ready", "degraded", "failed", "disabled"]
    stable_diagnostic_code: str | None
    entry_fingerprint: Fingerprint


@_fact(
    "mcp_diagnostic_payload.v1",
    "semantic_fingerprint",
    "mcp-diagnostic-payload:v1",
)
class McpDiagnosticPayloadFact(FrozenFactBase):
    schema_version: Literal["mcp_diagnostic_payload.v1"] = "mcp_diagnostic_payload.v1"
    installed_snapshot_semantic_fingerprint: Fingerprint
    ordered_entries: tuple[McpDiagnosticEntryFact, ...]
    rendering_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


@_fact(
    "workspace_skill_payload.v1",
    "semantic_fingerprint",
    "workspace-skill-payload:v1",
)
class WorkspaceSkillPayloadFact(FrozenFactBase):
    schema_version: Literal["workspace_skill_payload.v1"] = "workspace_skill_payload.v1"
    ordered_skill_semantic_fingerprints: tuple[Fingerprint, ...]
    projection_contract_fingerprint: Fingerprint
    content: ContextSourceContentSemanticFact
    semantic_fingerprint: Fingerprint


ContextSourcePayloadSemanticFact: TypeAlias = (
    SystemInstructionPayloadFact
    | RuntimeEnvironmentPayloadFact
    | RuntimeClockProposalPayloadFact
    | MemoryInstructionPayloadFact
    | MemoryProjectionPayloadFact
    | CapabilityCatalogPayloadFact
    | ActiveSkillPayloadFact
    | PlanRevisionPayloadFact
    | RecoveryObservationPayloadFact
    | RolloutStatusPayloadFact
    | SubagentHandoffPayloadFact
    | SubagentResultPayloadFact
    | McpDiagnosticPayloadFact
    | WorkspaceSkillPayloadFact
)


@_fact(
    "context_source_contract.v1",
    "contract_fingerprint",
    "context-source-contract:v1",
)
class ContextSourceContractFact(FrozenFactBase):
    schema_version: Literal["context_source_contract.v1"] = "context_source_contract.v1"
    source_id: ContextSourceId
    source_version: str
    binding_policy_fingerprint: Fingerprint
    lifecycle_contract_fingerprint: Fingerprint
    selection_contract_fingerprint: Fingerprint
    lowering_intent_contract_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


@_fact(
    "context_source_candidate_semantic.v1",
    "semantic_fingerprint",
    "context-source-candidate-semantic:v1",
)
class ContextSourceCandidateSemanticFact(FrozenFactBase):
    schema_version: Literal["context_source_candidate_semantic.v1"] = (
        "context_source_candidate_semantic.v1"
    )
    source_id: ContextSourceId
    source_instance_id: str = Field(min_length=1)
    candidate_key: str = Field(min_length=1)
    source_revision: CanonicalContextSourceRevisionFact
    payload: ContextSourcePayloadSemanticFact
    lifecycle: ContextSourceLifecycleFact
    priority: int
    required: bool
    lowering_intent: ContextCandidateLoweringIntentFact
    model_visible_timing_semantic: ContextSourceTimingSemanticFact | None
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _lifecycle(self) -> "ContextSourceCandidateSemanticFact":
        if isinstance(self.payload, RuntimeClockProposalPayloadFact) and (
            self.source_id is not ContextSourceId.RUNTIME_CLOCK
        ):
            raise ValueError("runtime clock payload has the wrong source owner")
        if isinstance(self.lifecycle, AuditOnlyLifecycleFact) and self.required:
            raise ValueError("audit-only source cannot be model-visible required")
        return self


@_fact(
    "context_source_candidate_attribution.v1",
    "fact_fingerprint",
    "context-source-candidate-attribution:v1",
)
class ContextSourceCandidateAttributionFact(FrozenFactBase):
    schema_version: Literal["context_source_candidate_attribution.v1"] = (
        "context_source_candidate_attribution.v1"
    )
    semantic: ContextSourceCandidateSemanticFact
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...]
    source_absolute_timing: ContextSourceAbsoluteTimingFact | None
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    source_input_authority_fingerprint: Fingerprint
    physical_input_policy_fingerprint: Fingerprint
    source_contract_id: str
    source_contract_version: str
    source_contract_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _authority(self) -> "ContextSourceCandidateAttributionFact":
        horizon_ids = tuple(item.runtime_session_id for item in self.authority_horizons)
        if horizon_ids != tuple(sorted(set(horizon_ids))):
            raise ValueError("candidate authority horizons are not ordered/unique")
        by_owner = {item.runtime_session_id: item for item in self.authority_horizons}
        event_keys = tuple(
            (item.runtime_session_id, item.sequence, item.event_id)
            for item in self.source_event_refs
        )
        if event_keys != tuple(sorted(set(event_keys))):
            raise ValueError("candidate source event refs are not ordered/unique")
        for ref in self.source_event_refs:
            horizon = by_owner.get(ref.runtime_session_id)
            if horizon is None or ref.sequence > horizon.through_sequence:
                raise ValueError("candidate event ref is outside authority horizon")
        artifact_ids = tuple(item.artifact_id for item in self.source_artifact_refs)
        if artifact_ids != tuple(sorted(set(artifact_ids))):
            raise ValueError("candidate source artifacts are not ordered/unique")
        return self


def context_source_payload_content(
    payload: ContextSourcePayloadSemanticFact,
) -> ContextSourceContentSemanticFact | None:
    if isinstance(
        payload, RuntimeEnvironmentPayloadFact | RuntimeClockProposalPayloadFact
    ):
        return None
    if isinstance(payload, CapabilityCatalogPayloadFact):
        return payload.prose_content
    return getattr(payload, "content", None)


LedgerAuthorityHorizonSetNodeReferenceFact.model_rebuild()


__all__ = [
    "ActiveSkillPayloadFact",
    "AppendOnceLifecycleFact",
    "AppendRevisionLifecycleFact",
    "ArtifactContextSourceContentSemanticFact",
    "AuditOnlyLifecycleFact",
    "CanonicalContextSourceRevisionFact",
    "CapabilityCatalogPayloadFact",
    "CapabilityToolCatalogRootFact",
    "ContextArtifactReferenceFact",
    "ContextCandidateLoweringIntentFact",
    "ContextSourceAbsoluteTimingFact",
    "ContextSourceCandidateAttributionFact",
    "ContextSourceCandidateSemanticFact",
    "ContextSourceContentSemanticFact",
    "ContextSourceContractFact",
    "ContextSourceId",
    "ContextSourceInputAuthorityFact",
    "ContextSourceLifecycleFact",
    "ContextSourcePayloadSemanticFact",
    "ContextSourceTimingSemanticFact",
    "DeltaSourceRevisionFact",
    "EventSourceRevisionFact",
    "GenerationRootLifecycleFact",
    "ImmutableSourceRevisionFact",
    "InlineContextSourceContentSemanticFact",
    "LedgerAuthorityHorizonFact",
    "LedgerAuthorityHorizonSetNodeReferenceFact",
    "LedgerAuthorityHorizonSetReferenceFact",
    "LedgerSequenceRangeFact",
    "McpDiagnosticEntryFact",
    "McpDiagnosticPayloadFact",
    "MemoryInstructionPayloadFact",
    "MemoryProjectionPayloadFact",
    "PlanRevisionPayloadFact",
    "ProviderRuntimeClockObservationSemanticFact",
    "RecoveryObservationPayloadFact",
    "ResolvedContextSourcePhysicalInputPolicyFact",
    "RolloutStatusPayloadFact",
    "RuntimeClockProposalPayloadFact",
    "RuntimeClockSourceContractFact",
    "RuntimeEnvironmentPayloadFact",
    "SnapshotSourceRevisionFact",
    "SubagentHandoffPayloadFact",
    "SubagentResultPayloadFact",
    "SystemInstructionPayloadFact",
    "WorkspaceSkillPayloadFact",
    "context_source_payload_content",
    "raw_content_sha256",
]
