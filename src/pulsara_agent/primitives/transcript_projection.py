"""Durable lossless transcript projection and run-seed contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    FrozenJsonObjectFact,
    ToolArgumentsParseErrorCode,
    canonical_utc_timestamp,
)
from pulsara_agent.primitives.authority_materialization import Fingerprint
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    PreparedRuntimeValueBase,
    register_durable_fact,
)
from pulsara_agent.primitives.terminal_projection import (
    TerminalProjectionReferenceFact,
    ToolTerminalProjectionSemanticFact,
)


NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


def _ordered_unique(values: tuple[object, ...], *, context: str) -> None:
    if tuple(values) != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError(f"{context} must be sorted and unique")


class TranscriptProjectionScopeFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_scope.v1"]
    runtime_session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    window_id: str = Field(min_length=1, max_length=128)
    window_generation: NonNegativeInt


class TranscriptProjectionSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_semantic_source.v1"]
    reducer_id: str = Field(min_length=1, max_length=128)
    reducer_version: str = Field(min_length=1, max_length=64)
    reducer_contract_fingerprint: Fingerprint
    transcript_semantic_domain_contract_fingerprint: Fingerprint
    semantic_source_event_count: NonNegativeInt
    semantic_source_accumulator: Fingerprint
    resulting_state_fingerprint: Fingerprint
    semantic_source_fingerprint: Fingerprint


class TranscriptProjectionOrdinalFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_ordinal.v1"]
    encoding: Literal["u64_be_hex16"]
    value_hex: str = Field(pattern=r"^[0-9a-f]{16}$")

    @property
    def value(self) -> int:
        return int(self.value_hex, 16)


class TranscriptProjectionTreeContractFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_tree_contract.v1"]
    tree_contract_id: str = Field(min_length=1, max_length=128)
    tree_contract_version: str = Field(min_length=1, max_length=64)
    max_internal_fanout: Annotated[int, Field(ge=2)]
    max_leaf_entries: PositiveInt
    max_inline_entry_bytes: PositiveInt
    max_node_bytes: PositiveInt
    max_tree_height: PositiveInt
    maximum_representable_entries: PositiveInt
    ordinal_contract_fingerprint: Fingerprint
    node_canonicalization_contract_fingerprint: Fingerprint
    ordering_contract_fingerprint: Fingerprint
    tree_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _representable_entries(self) -> "TranscriptProjectionTreeContractFact":
        capacity = self.max_leaf_entries * self.max_internal_fanout ** max(
            self.max_tree_height - 1,
            0,
        )
        if self.maximum_representable_entries > capacity:
            raise ValueError("tree entry capacity exceeds height/fanout proof")
        return self


class TranscriptProjectionRootManifestContractFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_root_manifest_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    empty_root_schema_fingerprint: Fingerprint
    non_empty_root_schema_fingerprint: Fingerprint
    tree_contract_fingerprint: Fingerprint
    normalized_transcript_fingerprint_contract_fingerprint: Fingerprint
    root_canonicalization_contract_fingerprint: Fingerprint
    max_root_manifest_bytes: PositiveInt
    contract_fingerprint: Fingerprint


class TranscriptProviderTextBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_text_block_semantic.v1"]
    block_kind: Literal["text"]
    text: str
    semantic_fingerprint: Fingerprint


class TranscriptProviderThinkingBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_thinking_block_semantic.v1"]
    block_kind: Literal["thinking"]
    thinking: str
    lowering_policy: Literal["provider_neutral_structured"]
    semantic_fingerprint: Fingerprint


class TranscriptProviderDataPlaceholderSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_data_placeholder_semantic.v1"]
    block_kind: Literal["data_placeholder"]
    name: str | None = Field(default=None, max_length=256)
    media_type: str = Field(min_length=1, max_length=256)
    source_kind: str = Field(min_length=1, max_length=128)
    artifact_content_fingerprints: tuple[Fingerprint, ...]
    semantic_fingerprint: Fingerprint


class TranscriptProviderToolCallBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_tool_call_block_semantic.v1"]
    block_kind: Literal["tool_call"]
    tool_call_id: str = Field(min_length=1, max_length=128)
    model_tool_name: str = Field(min_length=1, max_length=256)
    raw_arguments_json: str
    arguments_status: Literal["valid_object", "invalid_json", "non_object_json"]
    parsed_arguments: FrozenJsonObjectFact | None
    parse_error_code: ToolArgumentsParseErrorCode | None
    state: Literal["finished"]
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _arguments(self) -> "TranscriptProviderToolCallBlockSemanticFact":
        if self.arguments_status == "valid_object":
            if self.parsed_arguments is None or self.parse_error_code is not None:
                raise ValueError("valid transcript tool call requires parsed object")
        elif self.parsed_arguments is not None or self.parse_error_code is None:
            raise ValueError("invalid transcript tool call requires parse error")
        return self


class TranscriptProviderToolResultRefSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_tool_result_ref_semantic.v1"]
    block_kind: Literal["tool_result_ref"]
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_result_unit_semantic_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


TranscriptProviderBlockSemanticFact: TypeAlias = Annotated[
    TranscriptProviderTextBlockSemanticFact
    | TranscriptProviderThinkingBlockSemanticFact
    | TranscriptProviderDataPlaceholderSemanticFact
    | TranscriptProviderToolCallBlockSemanticFact
    | TranscriptProviderToolResultRefSemanticFact,
    Field(discriminator="block_kind"),
]


class TranscriptInlineBlockAttributionFact(FrozenFactBase):
    schema_version: Literal["transcript_inline_block_attribution.v1"]
    block_id: str = Field(min_length=1, max_length=128)
    block_index: NonNegativeInt
    source_projection_order: int | None = Field(default=None, ge=0)
    attribution_fingerprint: Fingerprint


class TranscriptInlineBlockFact(FrozenFactBase):
    schema_version: Literal["transcript_inline_block.v1"]
    provider_semantic_identity: TranscriptProviderBlockSemanticFact
    attribution: TranscriptInlineBlockAttributionFact
    fact_fingerprint: Fingerprint


Segment = Literal[
    "compaction_summary",
    "prior_history",
    "current_user",
    "current_run_tail",
    "recovery_note",
    "terminal_lifecycle_note",
]
ProviderLane = Literal[
    "prior_history",
    "current_user",
    "current_run_tail",
    "runtime_system",
]
LoweringScope = Literal[
    "transcript_prior",
    "leading_user",
    "transcript_current_run",
    "system_runtime",
]
TimingOverlayKind = Literal[
    "historical_replay",
    "compacted_history",
    "current_user",
    "current_run_observation",
    "runtime_observation",
]


class TranscriptMessageProviderPlacementRuleFact(FrozenFactBase):
    schema_version: Literal["transcript_message_provider_placement_rule.v1"]
    source_segment: Segment
    message_role: Literal["system", "user", "assistant"]
    normalized_lane: ProviderLane
    lowering_scope: LoweringScope
    timing_overlay_kind: TimingOverlayKind
    rule_fingerprint: Fingerprint


class TranscriptMessageProviderPlacementContractFact(FrozenFactBase):
    schema_version: Literal["transcript_message_provider_placement_contract.v2"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    rules: tuple[TranscriptMessageProviderPlacementRuleFact, ...]
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _rules(self) -> "TranscriptMessageProviderPlacementContractFact":
        keys = tuple((item.source_segment, item.message_role) for item in self.rules)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("message placement rules must be sorted and unique")
        return self


class TranscriptMessageProviderPlacementSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_message_provider_placement_semantic.v2"]
    normalized_lane: ProviderLane
    lowering_scope: LoweringScope
    timing_overlay_kind: TimingOverlayKind
    timing_policy_semantic_fingerprint: Fingerprint
    placement_contract_id: str = Field(min_length=1, max_length=128)
    placement_contract_version: str = Field(min_length=1, max_length=64)
    placement_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class TranscriptMessageProviderSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_message_provider_semantic.v3"]
    role: Literal["system", "user", "assistant"]
    name: str | None = Field(default=None, max_length=256)
    placement_semantic: TranscriptMessageProviderPlacementSemanticFact
    ordered_block_semantic_fingerprints: tuple[Fingerprint, ...]
    semantic_fingerprint: Fingerprint


class TranscriptMessageAttributionFact(FrozenFactBase):
    schema_version: Literal["transcript_message_attribution.v2"]
    message_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    turn_id: str | None = Field(default=None, max_length=128)
    reply_id: str | None = Field(default=None, max_length=128)
    created_at_utc: str | None
    finished_at_utc: str | None
    segment: Segment
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _timestamps(self) -> "TranscriptMessageAttributionFact":
        for name in ("created_at_utc", "finished_at_utc"):
            value = getattr(self, name)
            if value is not None and canonical_utc_timestamp(value) != value:
                raise ValueError(f"{name} must be canonical UTC")
        if (
            self.created_at_utc is not None
            and self.finished_at_utc is not None
            and self.created_at_utc > self.finished_at_utc
        ):
            raise ValueError("message finish precedes creation")
        return self


class TranscriptProviderLoweringOrderRuleFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_lowering_order_rule.v1"]
    normalized_lane: ProviderLane
    lowering_scope: LoweringScope
    section_order: NonNegativeInt
    section_grouping: Literal["contiguous_same_lane_and_scope"]
    within_section_order: Literal["stable_transcript_traversal"]
    rule_fingerprint: Fingerprint


class TranscriptProviderLoweringOrderContractFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_lowering_order_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    rules: tuple[TranscriptProviderLoweringOrderRuleFact, ...]
    base_system_prompt_position: Literal["outside_transcript_projection"]
    contract_fingerprint: Fingerprint


class TranscriptTimingOverlayRuleFact(FrozenFactBase):
    schema_version: Literal["transcript_timing_overlay_rule.v1"]
    timing_overlay_kind: TimingOverlayKind
    header_policy: Literal["full", "minimal", "none"]
    source_range_aggregation: Literal["section_min_start_max_end"]
    age_basis: Literal["compiled_at_minus_source_observed_at", "not_applicable"]
    rule_fingerprint: Fingerprint


class TranscriptTimingOverlayContractFact(FrozenFactBase):
    schema_version: Literal["transcript_timing_overlay_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    rules: tuple[TranscriptTimingOverlayRuleFact, ...]
    compiled_at_source: Literal["context_compile_request_compiled_at_utc"]
    local_date_source: Literal["session_timezone_from_compile_request"]
    age_rounding: Literal["floor_non_negative_seconds"]
    header_format_version: str = Field(min_length=1, max_length=64)
    max_rendered_header_characters: PositiveInt
    max_rendered_header_utf8_bytes: PositiveInt
    contract_fingerprint: Fingerprint


class TranscriptProviderInvocationRenderingContractFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_invocation_rendering_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    lowering_order_contract: TranscriptProviderLoweringOrderContractFact
    timing_overlay_contract: TranscriptTimingOverlayContractFact
    contract_fingerprint: Fingerprint


class TranscriptProviderSectionTimingSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_section_timing_semantic.v1"]
    timing_overlay_kind: TimingOverlayKind
    compiled_at_utc: str
    session_timezone: str | None = Field(default=None, max_length=128)
    compiled_local_date: str | None = Field(default=None, max_length=32)
    source_started_at_utc: str | None
    source_ended_at_utc: str | None
    source_observed_at_utc: str | None
    age_seconds: int | None = Field(default=None, ge=0)
    rendered_timing_header: str | None
    timing_overlay_contract_id: str = Field(min_length=1, max_length=128)
    timing_overlay_contract_version: str = Field(min_length=1, max_length=64)
    timing_overlay_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class TranscriptProviderSectionSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_section_semantic.v1"]
    normalized_lane: ProviderLane
    lowering_scope: LoweringScope
    ordered_message_semantic_fingerprints: tuple[Fingerprint, ...]
    timing_semantic: TranscriptProviderSectionTimingSemanticFact
    semantic_fingerprint: Fingerprint


class TranscriptProviderSectionProjectionFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_section_projection.v1"]
    section_id: str = Field(min_length=1, max_length=128)
    section_index: NonNegativeInt
    semantic_identity: TranscriptProviderSectionSemanticFact
    ordered_message_attribution_fingerprints: tuple[Fingerprint, ...]
    fact_fingerprint: Fingerprint


class TranscriptProviderProjectionSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_projection_semantic.v1"]
    stable_normalized_transcript_fingerprint: Fingerprint
    ordered_section_semantic_fingerprints: tuple[Fingerprint, ...]
    lowered_provider_messages_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class TranscriptProviderProjectionFact(FrozenFactBase):
    schema_version: Literal["transcript_provider_projection.v1"]
    context_id: str = Field(min_length=1, max_length=128)
    model_call_index: NonNegativeInt
    compile_attempt_index: NonNegativeInt
    semantic_identity: TranscriptProviderProjectionSemanticFact
    sections: tuple[TranscriptProviderSectionProjectionFact, ...]
    rendering_contract: TranscriptProviderInvocationRenderingContractFact
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _section_join(self) -> "TranscriptProviderProjectionFact":
        section_indexes = tuple(item.section_index for item in self.sections)
        if section_indexes != tuple(range(len(self.sections))):
            raise ValueError("provider projection section indexes are not contiguous")
        expected = tuple(item.semantic_identity.semantic_fingerprint for item in self.sections)
        if self.semantic_identity.ordered_section_semantic_fingerprints != expected:
            raise ValueError("provider projection section semantic mismatch")
        return self


class InlineNormalizedMessageContentFact(FrozenFactBase):
    schema_version: Literal["inline_normalized_message_content.v3"]
    content_kind: Literal["inline_normalized_message"]
    provider_semantic_identity: TranscriptMessageProviderSemanticFact
    blocks: tuple[TranscriptInlineBlockFact, ...]
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _block_join(self) -> "InlineNormalizedMessageContentFact":
        expected = tuple(item.provider_semantic_identity.semantic_fingerprint for item in self.blocks)
        if self.provider_semantic_identity.ordered_block_semantic_fingerprints != expected:
            raise ValueError("inline message block semantic mismatch")
        return self


class NormalizedMessageContentArtifactContractFact(FrozenFactBase):
    schema_version: Literal["normalized_message_content_artifact_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    document_schema_fingerprint: Fingerprint
    provider_message_semantic_contract_fingerprint: Fingerprint
    provider_block_union_contract_fingerprint: Fingerprint
    canonicalization_contract_fingerprint: Fingerprint
    max_document_bytes: PositiveInt
    max_block_count: PositiveInt
    contract_fingerprint: Fingerprint


class NormalizedMessageContentArtifactFact(FrozenFactBase):
    schema_version: Literal["normalized_message_content_artifact.v1"]
    artifact_contract_fingerprint: Fingerprint
    provider_semantic_identity: TranscriptMessageProviderSemanticFact
    blocks: tuple[TranscriptInlineBlockFact, ...]
    fact_fingerprint: Fingerprint


class NormalizedMessageContentArtifactReferenceFact(FrozenFactBase):
    schema_version: Literal["normalized_message_content_artifact_ref.v1"]
    content_kind: Literal["normalized_message_artifact_ref"]
    provider_semantic_identity: TranscriptMessageProviderSemanticFact
    document_fact_fingerprint: Fingerprint
    document_artifact_id: str = Field(min_length=1, max_length=256)
    document_sha256: Fingerprint
    document_byte_count: PositiveInt
    artifact_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class TerminalProjectionMessageContentRefFact(FrozenFactBase):
    schema_version: Literal["terminal_projection_message_content_ref.v3"]
    content_kind: Literal["terminal_projection_ref"]
    provider_semantic_identity: TranscriptMessageProviderSemanticFact
    projection_reference: TerminalProjectionReferenceFact
    selected_projection_orders: tuple[NonNegativeInt, ...]
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _orders(self) -> "TerminalProjectionMessageContentRefFact":
        _ordered_unique(self.selected_projection_orders, context="projection orders")
        return self


TranscriptMessageContentFact: TypeAlias = Annotated[
    InlineNormalizedMessageContentFact
    | NormalizedMessageContentArtifactReferenceFact
    | TerminalProjectionMessageContentRefFact,
    Field(discriminator="content_kind"),
]


class TranscriptMessageLeafSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_message_leaf_semantic.v2"]
    semantic_kind: Literal["message"]
    message_provider_semantic_identity: TranscriptMessageProviderSemanticFact
    semantic_fingerprint: Fingerprint


class TranscriptToolPairLeafSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_tool_pair_leaf_semantic.v2"]
    semantic_kind: Literal["tool_pair"]
    assistant_tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=256)
    assistant_message_semantic_fingerprint: Fingerprint
    tool_result_semantic_fingerprint: Fingerprint
    call_block_position: NonNegativeInt
    result_block_position: NonNegativeInt
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _pair_order(self) -> "TranscriptToolPairLeafSemanticFact":
        if self.call_block_position >= self.result_block_position:
            raise ValueError("tool result must follow its call")
        return self


class TranscriptToolResultLeafSemanticFact(FrozenFactBase):
    schema_version: Literal["transcript_tool_result_leaf_semantic.v2"]
    semantic_kind: Literal["tool_result_projection_ref"]
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=256)
    projection_semantic_identity: ToolTerminalProjectionSemanticFact
    semantic_fingerprint: Fingerprint


TranscriptProjectionLeafSemanticFact: TypeAlias = Annotated[
    TranscriptMessageLeafSemanticFact
    | TranscriptToolPairLeafSemanticFact
    | TranscriptToolResultLeafSemanticFact,
    Field(discriminator="semantic_kind"),
]


class TranscriptMessageLeafEntryFact(FrozenFactBase):
    schema_version: Literal["transcript_message_leaf_entry.v3"]
    entry_kind: Literal["message"]
    ordinal: TranscriptProjectionOrdinalFact
    semantic_identity: TranscriptMessageLeafSemanticFact
    attribution: TranscriptMessageAttributionFact
    content: TranscriptMessageContentFact
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _message_join(self) -> "TranscriptMessageLeafEntryFact":
        provider = self.semantic_identity.message_provider_semantic_identity
        if provider != self.content.provider_semantic_identity:
            raise ValueError("message leaf provider semantic mismatch")
        return self


class TranscriptToolPairLeafEntryFact(FrozenFactBase):
    schema_version: Literal["transcript_tool_pair_leaf_entry.v3"]
    entry_kind: Literal["tool_pair"]
    ordinal: TranscriptProjectionOrdinalFact
    pair_id: str = Field(min_length=1, max_length=128)
    semantic_identity: TranscriptToolPairLeafSemanticFact
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    fact_fingerprint: Fingerprint


class TranscriptToolResultLeafEntryFact(FrozenFactBase):
    schema_version: Literal["transcript_tool_result_leaf_entry.v3"]
    entry_kind: Literal["tool_result_projection_ref"]
    ordinal: TranscriptProjectionOrdinalFact
    semantic_identity: TranscriptToolResultLeafSemanticFact
    projection_reference: TerminalProjectionReferenceFact
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _projection_join(self) -> "TranscriptToolResultLeafEntryFact":
        if (
            self.semantic_identity.projection_semantic_identity.semantic_fingerprint
            != self.projection_reference.semantic_join.semantic_fingerprint
        ):
            raise ValueError("tool result leaf projection semantic mismatch")
        return self


TranscriptProjectionLeafEntryFact: TypeAlias = Annotated[
    TranscriptMessageLeafEntryFact
    | TranscriptToolPairLeafEntryFact
    | TranscriptToolResultLeafEntryFact,
    Field(discriminator="entry_kind"),
]


class TranscriptProjectionNodeRefFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_node_ref.v1"]
    node_kind: Literal["internal", "leaf"]
    node_artifact_id: str = Field(min_length=1, max_length=256)
    node_sha256: Fingerprint
    node_byte_count: PositiveInt
    first_ordinal: TranscriptProjectionOrdinalFact
    last_ordinal: TranscriptProjectionOrdinalFact
    subtree_entry_count: PositiveInt
    subtree_semantic_fingerprint: Fingerprint
    node_ref_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordinal_range(self) -> "TranscriptProjectionNodeRefFact":
        if self.first_ordinal.value > self.last_ordinal.value:
            raise ValueError("node ordinal range is reversed")
        return self


class TranscriptProjectionLeafNodeFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_leaf_node.v1"]
    first_ordinal: TranscriptProjectionOrdinalFact
    entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    subtree_semantic_fingerprint: Fingerprint
    node_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _entries(self) -> "TranscriptProjectionLeafNodeFact":
        if not self.entries:
            raise ValueError("leaf node cannot be empty")
        values = tuple(item.ordinal.value for item in self.entries)
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("leaf ordinals must be strictly increasing")
        if self.first_ordinal != self.entries[0].ordinal:
            raise ValueError("leaf first ordinal mismatch")
        return self


class TranscriptProjectionInternalNodeFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_internal_node.v1"]
    tree_level: PositiveInt
    child_refs: tuple[TranscriptProjectionNodeRefFact, ...]
    subtree_semantic_fingerprint: Fingerprint
    node_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _children(self) -> "TranscriptProjectionInternalNodeFact":
        if len(self.child_refs) < 2:
            raise ValueError("internal node requires at least two children")
        starts = tuple(item.first_ordinal.value for item in self.child_refs)
        if starts != tuple(sorted(starts)) or len(starts) != len(set(starts)):
            raise ValueError("internal child ranges must be ordered and unique")
        for previous, current in zip(self.child_refs, self.child_refs[1:]):
            if previous.last_ordinal.value >= current.first_ordinal.value:
                raise ValueError("internal child ordinal ranges overlap")
        return self


class EmptyTranscriptProjectionRootManifestFact(FrozenFactBase):
    schema_version: Literal["empty_transcript_projection_root.v2"]
    root_kind: Literal["empty"]
    root_manifest_contract_fingerprint: Fingerprint
    tree_contract_fingerprint: Fingerprint
    total_entry_count: Literal[0]
    normalized_transcript_fingerprint: Fingerprint
    materialization_fingerprint: Fingerprint


class NonEmptyTranscriptProjectionRootManifestFact(FrozenFactBase):
    schema_version: Literal["non_empty_transcript_projection_root.v2"]
    root_kind: Literal["non_empty"]
    root_manifest_contract_fingerprint: Fingerprint
    tree_contract_fingerprint: Fingerprint
    root_node_ref: TranscriptProjectionNodeRefFact
    tree_height: PositiveInt
    total_entry_count: PositiveInt
    normalized_transcript_fingerprint: Fingerprint
    materialization_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root_count(self) -> "NonEmptyTranscriptProjectionRootManifestFact":
        if self.total_entry_count != self.root_node_ref.subtree_entry_count:
            raise ValueError("root entry count mismatch")
        return self


TranscriptProjectionRootManifestFact: TypeAlias = Annotated[
    EmptyTranscriptProjectionRootManifestFact
    | NonEmptyTranscriptProjectionRootManifestFact,
    Field(discriminator="root_kind"),
]


class TranscriptProjectionRootManifestRefFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_root_ref.v3"]
    root_kind: Literal["empty", "non_empty"]
    root_artifact_id: str = Field(min_length=1, max_length=256)
    root_sha256: Fingerprint
    root_byte_count: PositiveInt
    normalized_transcript_fingerprint: Fingerprint
    materialization_fingerprint: Fingerprint
    root_manifest_contract_fingerprint: Fingerprint
    ref_fingerprint: Fingerprint


class EmptyTranscriptProjectionCheckpointMaterializationFact(FrozenFactBase):
    schema_version: Literal[
        "empty_transcript_projection_checkpoint_materialization.v1"
    ]
    root_kind: Literal["empty"]
    semantic_state_fingerprint: Fingerprint
    root_manifest_ref: TranscriptProjectionRootManifestRefFact
    tree_contract_fingerprint: Fingerprint
    total_entry_count: Literal[0]
    materialization_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root_kind(self) -> "EmptyTranscriptProjectionCheckpointMaterializationFact":
        if self.root_manifest_ref.root_kind != "empty":
            raise ValueError("empty materialization requires empty root")
        return self


class NonEmptyTranscriptProjectionCheckpointMaterializationFact(FrozenFactBase):
    schema_version: Literal[
        "non_empty_transcript_projection_checkpoint_materialization.v1"
    ]
    root_kind: Literal["non_empty"]
    semantic_state_fingerprint: Fingerprint
    root_manifest_ref: TranscriptProjectionRootManifestRefFact
    tree_contract_fingerprint: Fingerprint
    tree_height: PositiveInt
    total_entry_count: PositiveInt
    materialization_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root_kind(self) -> "NonEmptyTranscriptProjectionCheckpointMaterializationFact":
        if self.root_manifest_ref.root_kind != "non_empty":
            raise ValueError("non-empty materialization requires non-empty root")
        return self


TranscriptProjectionCheckpointMaterializationFact: TypeAlias = Annotated[
    EmptyTranscriptProjectionCheckpointMaterializationFact
    | NonEmptyTranscriptProjectionCheckpointMaterializationFact,
    Field(discriminator="root_kind"),
]


class RunTranscriptSeedSemanticFact(FrozenFactBase):
    schema_version: Literal["run_transcript_seed_semantic.v2"]
    prior_semantic_source: TranscriptProjectionSemanticSourceFact
    prior_stable_semantic_state: "TranscriptProjectionStableSemanticStateFact"
    normalized_prior_transcript_fingerprint: Fingerprint
    seed_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _source_state_join(self) -> "RunTranscriptSeedSemanticFact":
        source = self.prior_semantic_source
        state = self.prior_stable_semantic_state
        if source.semantic_source_event_count != state.semantic_source_event_count:
            raise ValueError("run seed semantic source count mismatch")
        if source.semantic_source_accumulator != state.semantic_source_accumulator:
            raise ValueError("run seed semantic source accumulator mismatch")
        if source.resulting_state_fingerprint != state.state_semantic_fingerprint:
            raise ValueError("run seed resulting state mismatch")
        if (
            self.normalized_prior_transcript_fingerprint
            != state.normalized_transcript_fingerprint
        ):
            raise ValueError("run seed normalized transcript mismatch")
        return self


class RunTranscriptSeedArtifactContractFact(FrozenFactBase):
    schema_version: Literal["run_transcript_seed_artifact_contract.v2"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    seed_artifact_schema_fingerprint: Fingerprint
    root_manifest_contract_fingerprint: Fingerprint
    canonicalization_contract_fingerprint: Fingerprint
    max_seed_artifact_bytes: PositiveInt
    contract_fingerprint: Fingerprint


class RunTranscriptSeedArtifactFact(FrozenFactBase):
    schema_version: Literal["run_transcript_seed_artifact.v2"]
    artifact_contract_fingerprint: Fingerprint
    seed_semantic: RunTranscriptSeedSemanticFact
    root_manifest: TranscriptProjectionRootManifestFact
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root_join(self) -> "RunTranscriptSeedArtifactFact":
        if (
            self.seed_semantic.normalized_prior_transcript_fingerprint
            != self.root_manifest.normalized_transcript_fingerprint
        ):
            raise ValueError("run seed root transcript mismatch")
        return self


class RunTranscriptSeedReferenceFact(FrozenFactBase):
    schema_version: Literal["run_transcript_seed_ref.v1"]
    seed_artifact_id: str = Field(min_length=1, max_length=256)
    seed_artifact_sha256: Fingerprint
    seed_artifact_bytes: PositiveInt
    seed_semantic_fingerprint: Fingerprint
    root_materialization_fingerprint: Fingerprint
    seed_artifact_contract_fingerprint: Fingerprint
    source_runtime_session_id: str = Field(min_length=1, max_length=128)
    source_ledger_through_sequence: NonNegativeInt
    source_ledger_continuity_accumulator: Fingerprint
    source_checkpoint_id: str | None = Field(default=None, max_length=128)
    reference_fingerprint: Fingerprint


class TranscriptProjectionAccelerationFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_acceleration.v1"]
    scope: TranscriptProjectionScopeFact
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_committed_event_id: str = Field(min_length=1, max_length=128)
    checkpoint_committed_event_sequence: PositiveInt
    checkpoint_candidate_ledger_through_sequence: NonNegativeInt
    checkpoint_candidate_ledger_continuity_accumulator: Fingerprint
    checkpoint_artifact_ref: TranscriptProjectionRootManifestRefFact
    previous_checkpoint_id: str | None = Field(default=None, max_length=128)
    ledger_materialization_generation: NonNegativeInt
    consumer_horizon_revision: NonNegativeInt
    delta_from_sequence: PositiveInt
    delta_through_sequence: NonNegativeInt
    delta_event_count: NonNegativeInt
    delta_payload_bytes: NonNegativeInt
    ledger_through_sequence: NonNegativeInt
    ledger_continuity_accumulator: Fingerprint
    event_domain_registry_contract_fingerprint: Fingerprint
    build_contract_fingerprint: Fingerprint
    acceleration_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _delta_range(self) -> "TranscriptProjectionAccelerationFact":
        if self.checkpoint_candidate_ledger_through_sequence >= (
            self.checkpoint_committed_event_sequence
        ):
            raise ValueError("checkpoint candidate must precede committed event")
        if self.delta_from_sequence != (
            self.checkpoint_candidate_ledger_through_sequence + 1
        ):
            raise ValueError("checkpoint delta start mismatch")
        if self.delta_through_sequence != self.ledger_through_sequence:
            raise ValueError("checkpoint delta through mismatch")
        expected_count = max(
            self.delta_through_sequence - self.delta_from_sequence + 1,
            0,
        )
        if self.delta_event_count != expected_count:
            raise ValueError("checkpoint delta event count mismatch")
        return self


@dataclass(frozen=True, slots=True)
class PreparedAuthorityArtifactWriteReservation(PreparedRuntimeValueBase):
    operation_id: str
    owner_kind: Literal["run_seed_materialization", "checkpoint_materialization"]
    max_artifact_count: int
    max_artifact_bytes: int
    max_artifact_batches: int
    absolute_deadline_monotonic: float
    limits_contract_fingerprint: str


from pulsara_agent.primitives.authority_materialization import (  # noqa: E402
    TranscriptDomainSparseReadProofFact,
    TranscriptProjectionStableSemanticStateFact,
)


class ProjectionBaseSemanticIdentityFact(FrozenFactBase):
    schema_version: Literal["projection_base_semantic_identity.v2"]
    run_seed_semantic_fingerprint: Fingerprint
    stable_state_semantic_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class ProjectionBaseCommonFact(FrozenFactBase):
    schema_version: Literal["projection_base_common.v2"]
    run_seed_semantic: RunTranscriptSeedSemanticFact
    run_seed_reference: RunTranscriptSeedReferenceFact
    stable_semantic_state: TranscriptProjectionStableSemanticStateFact
    semantic_identity: ProjectionBaseSemanticIdentityFact
    common_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "ProjectionBaseCommonFact":
        if (
            self.run_seed_reference.seed_semantic_fingerprint
            != self.run_seed_semantic.seed_semantic_fingerprint
        ):
            raise ValueError("projection base run-seed reference mismatch")
        if (
            self.semantic_identity.run_seed_semantic_fingerprint
            != self.run_seed_semantic.seed_semantic_fingerprint
            or self.semantic_identity.stable_state_semantic_fingerprint
            != self.stable_semantic_state.state_semantic_fingerprint
        ):
            raise ValueError("projection base semantic identity mismatch")
        return self


class RunSeedProjectionBaseFact(FrozenFactBase):
    schema_version: Literal["run_seed_projection_base.v2"]
    base_kind: Literal["run_seed"]
    common: ProjectionBaseCommonFact
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _seed_state(self) -> "RunSeedProjectionBaseFact":
        if (
            self.common.stable_semantic_state
            != self.common.run_seed_semantic.prior_stable_semantic_state
        ):
            raise ValueError("run-seed projection base state mismatch")
        return self


class CheckpointProjectionBaseFact(FrozenFactBase):
    schema_version: Literal["checkpoint_projection_base.v2"]
    base_kind: Literal["checkpoint"]
    common: ProjectionBaseCommonFact
    checkpoint_acceleration: TranscriptProjectionAccelerationFact
    checkpoint_materialization: TranscriptProjectionCheckpointMaterializationFact
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _checkpoint_state(self) -> "CheckpointProjectionBaseFact":
        state_fingerprint = self.common.stable_semantic_state.state_semantic_fingerprint
        if (
            self.checkpoint_materialization.semantic_state_fingerprint
            != state_fingerprint
            or self.checkpoint_acceleration.checkpoint_artifact_ref
            != self.checkpoint_materialization.root_manifest_ref
        ):
            raise ValueError("checkpoint projection base state mismatch")
        return self


TranscriptProjectionBaseFact: TypeAlias = Annotated[
    RunSeedProjectionBaseFact | CheckpointProjectionBaseFact,
    Field(discriminator="base_kind"),
]


class ModelVisibleNamedFactSemanticIdentityFact(FrozenFactBase):
    schema_version: Literal["model_visible_named_fact_semantic_identity.v1"]
    source_kind: str = Field(min_length=1, max_length=128)
    semantic_key: str = Field(min_length=1, max_length=256)
    semantic_payload_fingerprint: Fingerprint
    lowering_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class ModelVisibleNamedFactSemanticSelectionFact(FrozenFactBase):
    schema_version: Literal["model_visible_named_fact_semantic_selection.v1"]
    selection_contract_fingerprint: Fingerprint
    selected_items: tuple[ModelVisibleNamedFactSemanticIdentityFact, ...]
    named_facts_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _unique_items(self) -> "ModelVisibleNamedFactSemanticSelectionFact":
        keys = tuple((item.source_kind, item.semantic_key) for item in self.selected_items)
        if len(keys) != len(set(keys)):
            raise ValueError("named semantic selection contains duplicate keys")
        return self


class ModelVisibleNamedFactArtifactReferenceFact(FrozenFactBase):
    schema_version: Literal["model_visible_named_fact_artifact_ref.v1"]
    artifact_id: str = Field(min_length=1, max_length=256)
    artifact_sha256: Fingerprint
    artifact_byte_count: PositiveInt
    semantic_content_fingerprint: Fingerprint
    artifact_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class ModelVisibleNamedFactSelectionEntryFact(FrozenFactBase):
    schema_version: Literal["model_visible_named_fact_selection_entry.v1"]
    semantic_identity: ModelVisibleNamedFactSemanticIdentityFact
    source_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[ModelVisibleNamedFactArtifactReferenceFact, ...]
    entry_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _authority(self) -> "ModelVisibleNamedFactSelectionEntryFact":
        if not self.source_refs and not self.source_artifact_refs:
            raise ValueError("named fact selection entry lacks durable authority")
        event_keys = tuple(
            (item.runtime_session_id, item.sequence, item.event_id)
            for item in self.source_refs
        )
        if event_keys != tuple(sorted(event_keys)) or len(event_keys) != len(
            set(event_keys)
        ):
            raise ValueError("named fact event refs must be sorted and unique")
        artifact_keys = tuple(item.artifact_id for item in self.source_artifact_refs)
        if artifact_keys != tuple(sorted(artifact_keys)) or len(artifact_keys) != len(
            set(artifact_keys)
        ):
            raise ValueError("named fact artifact refs must be sorted and unique")
        return self


class ModelVisibleNamedFactSelectionFact(FrozenFactBase):
    schema_version: Literal["model_visible_named_fact_selection.v2"]
    semantic_selection: ModelVisibleNamedFactSemanticSelectionFact
    entries: tuple[ModelVisibleNamedFactSelectionEntryFact, ...]
    selection_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _entry_join(self) -> "ModelVisibleNamedFactSelectionFact":
        if tuple(item.semantic_identity for item in self.entries) != (
            self.semantic_selection.selected_items
        ):
            raise ValueError("named fact selection entries differ from semantics")
        return self


class ContextTranscriptProviderSemanticIdentityFact(FrozenFactBase):
    schema_version: Literal["context_transcript_provider_semantic_identity.v2"]
    projection_base_semantic_fingerprint: Fingerprint
    semantic_source_fingerprint: Fingerprint
    stable_state_semantic_fingerprint: Fingerprint
    final_normalized_transcript_fingerprint: Fingerprint
    invocation_provider_projection_semantic_fingerprint: Fingerprint
    named_facts_semantic_fingerprint: Fingerprint
    provider_semantic_fingerprint: Fingerprint


class ContextTranscriptAuthorityFact(FrozenFactBase):
    schema_version: Literal["context_transcript_authority.v6"]
    projection_base: TranscriptProjectionBaseFact
    semantic_source: TranscriptProjectionSemanticSourceFact
    provider_semantic_identity: ContextTranscriptProviderSemanticIdentityFact
    provider_projection: TranscriptProviderProjectionFact
    named_fact_selection: ModelVisibleNamedFactSelectionFact
    final_normalized_transcript_fingerprint: Fingerprint
    transcript_domain_delta_refs: tuple[ContextEventReferenceFact, ...]
    domain_completeness_proof: TranscriptDomainSparseReadProofFact
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _semantic_joins(self) -> "ContextTranscriptAuthorityFact":
        base = self.projection_base.common
        identity = self.provider_semantic_identity
        projection_identity = self.provider_projection.semantic_identity
        named = self.named_fact_selection.semantic_selection
        if (
            identity.projection_base_semantic_fingerprint
            != base.semantic_identity.semantic_fingerprint
            or identity.semantic_source_fingerprint
            != self.semantic_source.semantic_source_fingerprint
            or identity.stable_state_semantic_fingerprint
            != base.stable_semantic_state.state_semantic_fingerprint
            or identity.final_normalized_transcript_fingerprint
            != self.final_normalized_transcript_fingerprint
            or identity.invocation_provider_projection_semantic_fingerprint
            != projection_identity.semantic_fingerprint
            or identity.named_facts_semantic_fingerprint
            != named.named_facts_semantic_fingerprint
            or projection_identity.stable_normalized_transcript_fingerprint
            != self.final_normalized_transcript_fingerprint
        ):
            raise ValueError("context transcript authority semantic join mismatch")
        delta_keys = tuple(
            (item.runtime_session_id, item.sequence, item.event_id)
            for item in self.transcript_domain_delta_refs
        )
        if delta_keys != tuple(sorted(delta_keys)) or len(delta_keys) != len(
            set(delta_keys)
        ):
            raise ValueError("transcript domain delta refs must be sorted and unique")
        if len(self.transcript_domain_delta_refs) != (
            self.domain_completeness_proof.selected_transcript_semantic_event_count
        ):
            raise ValueError("transcript domain delta proof count mismatch")
        return self


_OWN: tuple[tuple[str, str | None, str], ...] = (
    ("transcript_projection_scope.v1", None, "transcript-projection-scope:v1"),
    ("transcript_projection_semantic_source.v1", "semantic_source_fingerprint", "transcript-projection-semantic-source:v1"),
    ("transcript_projection_ordinal.v1", None, "transcript-projection-ordinal:v1"),
    ("transcript_projection_tree_contract.v1", "tree_contract_fingerprint", "transcript-projection-tree-contract:v1"),
    ("transcript_projection_root_manifest_contract.v1", "contract_fingerprint", "transcript-projection-root-manifest-contract:v1"),
    ("transcript_provider_text_block_semantic.v1", "semantic_fingerprint", "transcript-provider-text-block-semantic:v1"),
    ("transcript_provider_thinking_block_semantic.v1", "semantic_fingerprint", "transcript-provider-thinking-block-semantic:v1"),
    ("transcript_provider_data_placeholder_semantic.v1", "semantic_fingerprint", "transcript-provider-data-placeholder-semantic:v1"),
    ("transcript_provider_tool_call_block_semantic.v1", "semantic_fingerprint", "transcript-provider-tool-call-block-semantic:v1"),
    ("transcript_provider_tool_result_ref_semantic.v1", "semantic_fingerprint", "transcript-provider-tool-result-ref-semantic:v1"),
    ("transcript_inline_block_attribution.v1", "attribution_fingerprint", "transcript-inline-block-attribution:v1"),
    ("transcript_inline_block.v1", "fact_fingerprint", "transcript-inline-block:v1"),
    ("transcript_message_provider_placement_rule.v1", "rule_fingerprint", "transcript-message-provider-placement-rule:v1"),
    ("transcript_message_provider_placement_contract.v2", "contract_fingerprint", "transcript-message-provider-placement-contract:v2"),
    ("transcript_message_provider_placement_semantic.v2", "semantic_fingerprint", "transcript-message-provider-placement-semantic:v2"),
    ("transcript_message_provider_semantic.v3", "semantic_fingerprint", "transcript-message-provider-semantic:v3"),
    ("transcript_message_attribution.v2", "attribution_fingerprint", "transcript-message-attribution:v2"),
    ("transcript_provider_lowering_order_rule.v1", "rule_fingerprint", "transcript-provider-lowering-order-rule:v1"),
    ("transcript_provider_lowering_order_contract.v1", "contract_fingerprint", "transcript-provider-lowering-order-contract:v1"),
    ("transcript_timing_overlay_rule.v1", "rule_fingerprint", "transcript-timing-overlay-rule:v1"),
    ("transcript_timing_overlay_contract.v1", "contract_fingerprint", "transcript-timing-overlay-contract:v1"),
    ("transcript_provider_invocation_rendering_contract.v1", "contract_fingerprint", "transcript-provider-invocation-rendering-contract:v1"),
    ("transcript_provider_section_timing_semantic.v1", "semantic_fingerprint", "transcript-provider-section-timing-semantic:v1"),
    ("transcript_provider_section_semantic.v1", "semantic_fingerprint", "transcript-provider-section-semantic:v1"),
    ("transcript_provider_section_projection.v1", "fact_fingerprint", "transcript-provider-section-projection:v1"),
    ("transcript_provider_projection_semantic.v1", "semantic_fingerprint", "transcript-provider-projection-semantic:v1"),
    ("transcript_provider_projection.v1", "fact_fingerprint", "transcript-provider-projection:v1"),
    ("inline_normalized_message_content.v3", "fact_fingerprint", "inline-normalized-message-content:v3"),
    ("normalized_message_content_artifact_contract.v1", "contract_fingerprint", "normalized-message-content-artifact-contract:v1"),
    ("normalized_message_content_artifact.v1", "fact_fingerprint", "normalized-message-content-artifact:v1"),
    ("normalized_message_content_artifact_ref.v1", "reference_fingerprint", "normalized-message-content-artifact-ref:v1"),
    ("terminal_projection_message_content_ref.v3", "reference_fingerprint", "terminal-projection-message-content-ref:v3"),
    ("transcript_message_leaf_semantic.v2", "semantic_fingerprint", "transcript-message-leaf-semantic:v2"),
    ("transcript_tool_pair_leaf_semantic.v2", "semantic_fingerprint", "transcript-tool-pair-leaf-semantic:v2"),
    ("transcript_tool_result_leaf_semantic.v2", "semantic_fingerprint", "transcript-tool-result-leaf-semantic:v2"),
    ("transcript_message_leaf_entry.v3", "fact_fingerprint", "transcript-message-leaf-entry:v3"),
    ("transcript_tool_pair_leaf_entry.v3", "fact_fingerprint", "transcript-tool-pair-leaf-entry:v3"),
    ("transcript_tool_result_leaf_entry.v3", "fact_fingerprint", "transcript-tool-result-leaf-entry:v3"),
    ("transcript_projection_node_ref.v1", "node_ref_fingerprint", "transcript-projection-node-ref:v1"),
    ("transcript_projection_leaf_node.v1", "node_fingerprint", "transcript-projection-leaf-node:v1"),
    ("transcript_projection_internal_node.v1", "node_fingerprint", "transcript-projection-internal-node:v1"),
    ("empty_transcript_projection_root.v2", "materialization_fingerprint", "empty-transcript-projection-root:v2"),
    ("non_empty_transcript_projection_root.v2", "materialization_fingerprint", "non-empty-transcript-projection-root:v2"),
    ("transcript_projection_root_ref.v3", "ref_fingerprint", "transcript-projection-root-ref:v3"),
    ("empty_transcript_projection_checkpoint_materialization.v1", "materialization_fingerprint", "empty-transcript-projection-checkpoint-materialization:v1"),
    ("non_empty_transcript_projection_checkpoint_materialization.v1", "materialization_fingerprint", "non-empty-transcript-projection-checkpoint-materialization:v1"),
    ("run_transcript_seed_semantic.v2", "seed_semantic_fingerprint", "run-transcript-seed-semantic:v2"),
    ("run_transcript_seed_artifact_contract.v2", "contract_fingerprint", "run-transcript-seed-artifact-contract:v2"),
    ("run_transcript_seed_artifact.v2", "fact_fingerprint", "run-transcript-seed-artifact:v2"),
    ("run_transcript_seed_ref.v1", "reference_fingerprint", "run-transcript-seed-ref:v1"),
    ("transcript_projection_acceleration.v1", "acceleration_fingerprint", "transcript-projection-acceleration:v1"),
    ("projection_base_semantic_identity.v2", "semantic_fingerprint", "projection-base-semantic-identity:v2"),
    ("projection_base_common.v2", "common_fact_fingerprint", "projection-base-common:v2"),
    ("run_seed_projection_base.v2", "fact_fingerprint", "run-seed-projection-base:v2"),
    ("checkpoint_projection_base.v2", "fact_fingerprint", "checkpoint-projection-base:v2"),
    ("model_visible_named_fact_semantic_identity.v1", "semantic_fingerprint", "model-visible-named-fact-semantic-identity:v1"),
    ("model_visible_named_fact_semantic_selection.v1", "named_facts_semantic_fingerprint", "model-visible-named-fact-semantic-selection:v1"),
    ("model_visible_named_fact_artifact_ref.v1", "reference_fingerprint", "model-visible-named-fact-artifact-ref:v1"),
    ("model_visible_named_fact_selection_entry.v1", "entry_fact_fingerprint", "model-visible-named-fact-selection-entry:v1"),
    ("model_visible_named_fact_selection.v2", "selection_fact_fingerprint", "model-visible-named-fact-selection:v2"),
    ("context_transcript_provider_semantic_identity.v2", "provider_semantic_fingerprint", "context-transcript-provider-semantic-identity:v2"),
    ("context_transcript_authority.v6", "fact_fingerprint", "context-transcript-authority:v6"),
)

for _schema, _field, _domain in _OWN:
    register_durable_fact(
        schema_version=_schema,
        own_fingerprint_field=_field,
        domain_separator=_domain,
    )


__all__ = [name for name in globals() if name.startswith(("Transcript", "RunTranscript", "Normalized", "Inline", "Empty", "NonEmpty", "PreparedAuthority"))]
