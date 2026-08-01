"""Renderer-neutral durable presentation history contracts.

The types in this module deliberately stop at semantic presentation and stable
history placement.  They contain no terminal layout, colour, key binding, or
renderer-specific state.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import ConfigDict, Field, model_validator

from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    build_frozen_fact,
    register_durable_fact,
)
from pulsara_agent.primitives.presentation_placement_contract import (
    PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT,
    PRESENTATION_PLACEMENT_KEY_CONTRACT_ID,
    PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION,
)
from pulsara_agent.primitives.terminal_presentation import (
    CanonicalTranscriptPlacementAnchorReferenceFact,
    CanonicalTranscriptPlacementAnchorTombstoneFact,
)
from pulsara_agent.primitives.transcript_projection import (
    TranscriptProjectionLeafEntryReferenceFact,
)


Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]

PLACEMENT_MAGIC = b"PHK1"
PLACEMENT_VERSION = 1
PLACEMENT_ENCODED_BYTES = 75
UINT32_MAX = 2**32 - 1
UINT64_MAX = 2**64 - 1


class PresentationTextContentBlockFact(FrozenFactBase):
    schema_version: Literal["presentation_text_content_block.v1"]
    block_kind: Literal["text"]
    text: str = Field(max_length=32_000)
    text_utf8_bytes: NonNegativeInt
    semantic_role: Literal[
        "primary",
        "secondary",
        "diagnostic",
        "code",
        "tool_arguments",
        "tool_result",
    ]
    block_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bytes(self) -> "PresentationTextContentBlockFact":
        if self.text_utf8_bytes != len(self.text.encode("utf-8")):
            raise ValueError("presentation text byte count mismatch")
        return self


class PresentationDataContentBlockFact(FrozenFactBase):
    schema_version: Literal["presentation_data_content_block.v1"]
    block_kind: Literal["data"]
    media_type: str = Field(min_length=1, max_length=256)
    public_canonical_text: str = Field(max_length=32_000)
    public_utf8_bytes: NonNegativeInt
    block_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bytes(self) -> "PresentationDataContentBlockFact":
        if self.public_utf8_bytes != len(self.public_canonical_text.encode("utf-8")):
            raise ValueError("presentation data byte count mismatch")
        return self


PresentationContentBlockFact: TypeAlias = Annotated[
    PresentationTextContentBlockFact | PresentationDataContentBlockFact,
    Field(discriminator="block_kind"),
]


class _DurableHistoryCellBase(FrozenFactBase):
    stable_cell_id: str = Field(min_length=1, max_length=256)
    semantic_revision: PositiveInt
    ordered_source_event_references: tuple[ContextEventReferenceFact, ...] = Field(
        min_length=1, max_length=64
    )
    source_accumulator: Fingerprint
    visibility_policy: Literal["always", "normal", "diagnostic_only"]
    content_blocks: tuple[PresentationContentBlockFact, ...] = Field(max_length=64)
    semantic_group_id: str | None = Field(default=None, max_length=256)
    cell_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordered_sources(self) -> "_DurableHistoryCellBase":
        keys = tuple(
            (item.sequence, item.event_id)
            for item in self.ordered_source_event_references
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("history cell sources must be ordered and unique")
        return self


class UserPromptCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_user_prompt_cell.v1"]
    cell_kind: Literal["user_prompt"]


class AssistantMessageCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_assistant_message_cell.v1"]
    cell_kind: Literal["assistant_message"]


class ToolTerminalCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_tool_terminal_cell.v1"]
    cell_kind: Literal["tool_terminal"]
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=256)
    result_state: Literal["success", "error", "denied", "interrupted"]


class ErrorCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_error_cell.v1"]
    cell_kind: Literal["error"]
    stable_error_code: str = Field(min_length=1, max_length=128)


class InteractionCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_interaction_cell.v1"]
    cell_kind: Literal["interaction"]
    interaction_kind: Literal["approval", "plan", "mcp_input", "external_input"]
    interaction_state: Literal["pending", "resolved", "cancelled", "failed"]


class CompactionBoundaryCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_compaction_boundary_cell.v1"]
    cell_kind: Literal["compaction_boundary"]


class RecoveryCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_recovery_cell.v1"]
    cell_kind: Literal["recovery"]
    recovery_kind: str = Field(min_length=1, max_length=128)


class AuditCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_audit_cell.v1"]
    cell_kind: Literal["audit"]
    audit_kind: Literal[
        "run_lifecycle",
        "suppressed_model_output",
        "permission",
        "interaction_lifecycle",
        "subagent_lifecycle",
        "compaction_lifecycle",
        "recovery_lifecycle",
    ]
    severity: Literal["info", "warning", "error"]


class SystemNoticeCell(_DurableHistoryCellBase):
    schema_version: Literal["presentation_system_notice_cell.v1"]
    cell_kind: Literal["system_notice"]
    notice_kind: str = Field(min_length=1, max_length=128)


DurableHistoryCell: TypeAlias = Annotated[
    UserPromptCell
    | AssistantMessageCell
    | ToolTerminalCell
    | ErrorCell
    | InteractionCell
    | CompactionBoundaryCell
    | RecoveryCell
    | AuditCell
    | SystemNoticeCell,
    Field(discriminator="cell_kind"),
]


class _OperationalActivityCellBase(FrozenFactBase):
    owner_kind: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=256)
    owner_generation: NonNegativeInt
    operational_generation: PositiveInt
    operational_cursor: PositiveInt
    coalesce_key: str = Field(min_length=1, max_length=256)
    replacement_semantics: Literal["replace_same_key", "expire_at_terminal"]
    bounded_public_text: str = Field(max_length=8_000)
    activity_fingerprint: Fingerprint


class ModelActivityCell(_OperationalActivityCellBase):
    schema_version: Literal["presentation_model_activity_cell.v1"]
    activity_kind: Literal["model_activity"]


class ToolActivityCell(_OperationalActivityCellBase):
    schema_version: Literal["presentation_tool_activity_cell.v1"]
    activity_kind: Literal["tool_activity"]


class TerminalProcessActivityCell(_OperationalActivityCellBase):
    schema_version: Literal["presentation_terminal_process_activity_cell.v1"]
    activity_kind: Literal["terminal_process_activity"]


class SubagentActivityCell(_OperationalActivityCellBase):
    schema_version: Literal["presentation_subagent_activity_cell.v1"]
    activity_kind: Literal["subagent_activity"]


OperationalActivityCell: TypeAlias = Annotated[
    ModelActivityCell
    | ToolActivityCell
    | TerminalProcessActivityCell
    | SubagentActivityCell,
    Field(discriminator="activity_kind"),
]


class PresentationEventPurposePolicyFact(FrozenFactBase):
    schema_version: Literal["presentation_event_purpose_policy.v1"]
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: Fingerprint
    transcript_purpose: Literal["semantic", "acceleration", "none"]
    durable_audit_purpose: Literal["extract", "none"]
    audit_extractor_id: str | None
    permitted_audit_field_names: tuple[str, ...]
    policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _extractor(self) -> "PresentationEventPurposePolicyFact":
        if (self.durable_audit_purpose == "extract") != (
            self.audit_extractor_id is not None
        ):
            raise ValueError("presentation audit purpose/extractor mismatch")
        if self.permitted_audit_field_names != tuple(
            sorted(set(self.permitted_audit_field_names))
        ):
            raise ValueError("presentation audit field allowlist is not canonical")
        return self


class PresentationPurposePolicyRegistryFact(FrozenFactBase):
    schema_version: Literal["presentation_purpose_policy_registry.v1"]
    registry_id: str
    registry_version: str
    ordered_policies: tuple[PresentationEventPurposePolicyFact, ...]
    registry_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _entries(self) -> "PresentationPurposePolicyRegistryFact":
        keys = tuple(
            (item.event_type, item.event_schema_version, item.event_schema_fingerprint)
            for item in self.ordered_policies
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("presentation purpose policies are not ordered and unique")
        return self


class PresentationAuditExtractorContractFact(FrozenFactBase):
    schema_version: Literal["presentation_audit_extractor_contract.v1"]
    extractor_id: str
    extractor_version: str
    output_union_contract_fingerprint: Fingerprint
    ordered_supported_event_types: tuple[str, ...]
    maximum_outputs_per_event: PositiveInt
    maximum_public_text_utf8_bytes: PositiveInt
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _types(self) -> "PresentationAuditExtractorContractFact":
        if self.ordered_supported_event_types != tuple(
            sorted(set(self.ordered_supported_event_types))
        ):
            raise ValueError("audit extractor event types are not canonical")
        return self


RelativePositionKind: TypeAlias = Literal[
    "before_first",
    "before_leaf",
    "canonical_leaf",
    "after_leaf",
    "ledger_gap",
    "after_last",
]


class PresentationHistoryPlacementKeyContractFact(FrozenFactBase):
    schema_version: Literal["presentation_history_placement_key_contract.v1"]
    placement_key_contract_id: str = Field(min_length=1, max_length=128)
    placement_key_contract_version: str = Field(min_length=1, max_length=64)
    framing_id: Literal["presentation-history-placement-key-fixed:v1"]
    framing_magic_ascii: Literal["PHK1"]
    framing_version_uint16: Literal[1]
    encoded_byte_count: Literal[75]
    spine_coordinate_type: Literal["uint64"]
    spine_coordinate_width_bytes: Literal[8]
    integer_byte_order: Literal["big_endian"]
    spine_coordinate_genesis: Literal[1]
    spine_coordinate_left_none_sentinel: Literal[0]
    spine_coordinate_right_none_sentinel: Literal[18446744073709551615]
    spine_coordinate_max_append_value: Literal[18446744073709551614]
    relative_position_kind_order: tuple[RelativePositionKind, ...]
    relative_position_kind_width_bytes: Literal[1]
    source_sequence_type: Literal["uint64"]
    source_sequence_width_bytes: Literal[8]
    local_ordinal_type: Literal["uint32"]
    local_ordinal_width_bytes: Literal[4]
    stable_tiebreaker_contract_id: str
    stable_tiebreaker_input_normalization: Literal["canonical_utf8_stable_id"]
    stable_tiebreaker_byte_count: Literal[32]
    canonical_layout: Literal[
        "magic[4]||version[2]||primary[8]||kind[1]||sequence[8]||ordinal[4]||left[8]||right[8]||tiebreaker[32]"
    ]
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _closed_order(self) -> "PresentationHistoryPlacementKeyContractFact":
        expected = (
            "before_first",
            "before_leaf",
            "canonical_leaf",
            "after_leaf",
            "ledger_gap",
            "after_last",
        )
        if self.relative_position_kind_order != expected:
            raise ValueError("presentation placement kind order drifted")
        return self


class PresentationHistoryPlacementKeyFact(FrozenFactBase):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    schema_version: Literal["presentation_history_placement_key.v1"]
    placement_key_contract_id: str
    placement_key_contract_version: str
    placement_key_contract_fingerprint: Fingerprint
    canonical_spine_left_coordinate: int | None = Field(
        default=None, ge=1, le=UINT64_MAX - 1
    )
    canonical_spine_right_coordinate: int | None = Field(
        default=None, ge=1, le=UINT64_MAX - 1
    )
    relative_position_kind: RelativePositionKind
    source_ledger_sequence_or_zero: int = Field(ge=0, le=UINT64_MAX)
    relative_local_ordinal: int = Field(ge=0, le=UINT32_MAX)
    stable_source_tiebreaker: Fingerprint
    canonical_comparable_key_bytes: bytes = Field(min_length=75, max_length=75)
    placement_key_fingerprint: Fingerprint


class CanonicalTranscriptHistorySourceFact(FrozenFactBase):
    schema_version: Literal["canonical_transcript_history_source.v1"]
    source_kind: Literal["canonical_transcript"]
    transcript_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    transcript_placement_anchor: CanonicalTranscriptPlacementAnchorReferenceFact
    transcript_reducer_id: str
    transcript_reducer_version: str
    transcript_reducer_contract_fingerprint: Fingerprint
    source_fold_delta_fingerprint: Fingerprint
    source_leaf_change_ordinal: NonNegativeInt
    source_fingerprint: Fingerprint


class BeforeTranscriptLeafAuditAnchorFact(FrozenFactBase):
    schema_version: Literal["presentation_before_leaf_audit_anchor.v1"]
    anchor_kind: Literal["before_leaf"]
    target_transcript_anchor: CanonicalTranscriptPlacementAnchorReferenceFact
    audit_local_ordinal: NonNegativeInt
    anchor_fingerprint: Fingerprint


class AfterTranscriptLeafAuditAnchorFact(FrozenFactBase):
    schema_version: Literal["presentation_after_leaf_audit_anchor.v1"]
    anchor_kind: Literal["after_leaf"]
    target_transcript_anchor: CanonicalTranscriptPlacementAnchorReferenceFact
    audit_local_ordinal: NonNegativeInt
    anchor_fingerprint: Fingerprint


class LedgerSequenceAuditAnchorFact(FrozenFactBase):
    schema_version: Literal["presentation_ledger_sequence_audit_anchor.v1"]
    anchor_kind: Literal["ledger_sequence"]
    source_event_reference: ContextEventReferenceFact
    resolved_left_transcript_anchor: (
        CanonicalTranscriptPlacementAnchorReferenceFact | None
    )
    resolved_right_transcript_anchor: (
        CanonicalTranscriptPlacementAnchorReferenceFact | None
    )
    transcript_gap_proof_fingerprint: Fingerprint
    audit_local_ordinal: NonNegativeInt
    anchor_fingerprint: Fingerprint


AuditHistoryPlacementAnchorFact: TypeAlias = Annotated[
    BeforeTranscriptLeafAuditAnchorFact
    | AfterTranscriptLeafAuditAnchorFact
    | LedgerSequenceAuditAnchorFact,
    Field(discriminator="anchor_kind"),
]


class DurableAuditHistorySourceFact(FrozenFactBase):
    schema_version: Literal["durable_audit_history_source.v1"]
    source_kind: Literal["durable_audit"]
    audit_cell_id: str
    audit_cell_semantic_revision: PositiveInt
    audit_cell_fingerprint: Fingerprint
    ordered_source_event_references: tuple[ContextEventReferenceFact, ...]
    presentation_policy_fingerprint: Fingerprint
    extractor_id: str
    extractor_version: str
    extractor_contract_fingerprint: Fingerprint
    extractor_output_ordinal: NonNegativeInt
    audit_placement_anchor: AuditHistoryPlacementAnchorFact
    source_fingerprint: Fingerprint


PresentationHistoryEntrySourceFact: TypeAlias = Annotated[
    CanonicalTranscriptHistorySourceFact | DurableAuditHistorySourceFact,
    Field(discriminator="source_kind"),
]


class PresentationHistoryEntryFact(FrozenFactBase):
    schema_version: Literal["presentation_history_entry.v1"]
    runtime_session_id: str
    history_entry_id: str
    placement_key: PresentationHistoryPlacementKeyFact
    source: PresentationHistoryEntrySourceFact
    cell: DurableHistoryCell
    entry_fingerprint: Fingerprint


class PresentationHistoryTreeContractFact(FrozenFactBase):
    schema_version: Literal["presentation_history_tree_contract.v1"]
    tree_contract_id: str
    tree_contract_version: str
    placement_key_contract: PresentationHistoryPlacementKeyContractFact
    max_inline_entry_bytes: PositiveInt
    max_leaf_entries: PositiveInt
    max_leaf_node_bytes: PositiveInt
    max_internal_fanout: PositiveInt
    max_internal_node_bytes: PositiveInt
    max_tree_height: PositiveInt
    maximum_representable_entries: PositiveInt
    node_canonicalization_contract_fingerprint: Fingerprint
    ordering_contract_fingerprint: Fingerprint
    tree_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _tree_capacity(self) -> "PresentationHistoryTreeContractFact":
        if self.max_internal_fanout < 2 or self.max_leaf_entries < 2:
            raise ValueError("presentation history tree fanout must be at least two")
        maximum = self.max_leaf_entries * (
            self.max_internal_fanout ** max(self.max_tree_height - 1, 0)
        )
        if self.maximum_representable_entries != maximum:
            raise ValueError("presentation history maximum entry count drifted")
        return self


class PresentationHistoryGrowthQuoteKindBoundFact(FrozenFactBase):
    schema_version: Literal["presentation_history_growth_quote_kind_bound.v1"]
    admission_kind: Literal[
        "prompt_submission",
        "run_activation",
        "queue_steer_delivery",
        "queue_follow_up_delivery",
        "interaction_continuation",
    ]
    maximum_new_history_entries: PositiveInt
    derivation_input_contract_fingerprint: Fingerprint
    kind_bound_fingerprint: Fingerprint


class PresentationHistoryGrowthQuotePolicyFact(FrozenFactBase):
    schema_version: Literal["presentation_history_growth_quote_policy.v1"]
    quote_policy_id: str
    quote_policy_version: str
    ordered_kind_bounds: tuple[PresentationHistoryGrowthQuoteKindBoundFact, ...]
    maximum_active_committed_runs_per_session: Literal[1]
    maximum_nonterminal_growth_reservations_per_session: PositiveInt
    quote_derivation_contract_fingerprint: Fingerprint
    quote_policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _exhaustive(self) -> "PresentationHistoryGrowthQuotePolicyFact":
        expected = (
            "interaction_continuation",
            "prompt_submission",
            "queue_follow_up_delivery",
            "queue_steer_delivery",
            "run_activation",
        )
        keys = tuple(item.admission_kind for item in self.ordered_kind_bounds)
        if keys != expected:
            raise ValueError(
                "presentation history growth quote policy is not exhaustive"
            )
        return self


class PresentationHistoryMaterializationPolicyFact(FrozenFactBase):
    schema_version: Literal["presentation_history_materialization_policy.v1"]
    policy_id: str
    policy_version: str
    tree_contract: PresentationHistoryTreeContractFact
    growth_quote_policy: PresentationHistoryGrowthQuotePolicyFact
    max_root_fact_bytes: PositiveInt
    checkpoint_max_new_nodes: PositiveInt
    checkpoint_max_new_node_bytes: PositiveInt
    checkpoint_max_confirmation_lineage_reads: PositiveInt
    tail_soft_max_events: PositiveInt
    tail_soft_max_entries: PositiveInt
    tail_soft_max_bytes: PositiveInt
    tail_hard_max_events: PositiveInt
    tail_hard_max_entries: PositiveInt
    tail_hard_max_bytes: PositiveInt
    capacity_soft_rotation_threshold_entries: PositiveInt
    terminalization_maintenance_reserve_entries: PositiveInt
    minimum_ordinary_growth_quote_entries: PositiveInt
    capacity_growth_and_reserve_contract_fingerprint: Fingerprint
    max_retained_root_generations: PositiveInt
    root_retention_ttl_seconds: PositiveInt
    read_max_entries: PositiveInt
    read_max_page_canonical_bytes: PositiveInt
    read_max_page_rendered_bytes: PositiveInt
    read_max_node_reads: PositiveInt
    read_max_tree_height: PositiveInt
    retention_contract_fingerprint: Fingerprint
    read_contract_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounds(self) -> "PresentationHistoryMaterializationPolicyFact":
        if not (
            self.tail_soft_max_events < self.tail_hard_max_events
            and self.tail_soft_max_entries < self.tail_hard_max_entries
            and self.tail_soft_max_bytes < self.tail_hard_max_bytes
        ):
            raise ValueError("presentation history tail soft/hard bounds are invalid")
        if (
            self.capacity_soft_rotation_threshold_entries
            + self.terminalization_maintenance_reserve_entries
            > self.tree_contract.maximum_representable_entries
        ):
            raise ValueError("presentation history terminal reserve exceeds capacity")
        if self.read_max_tree_height > self.tree_contract.max_tree_height:
            raise ValueError("presentation history read height exceeds tree contract")
        if (
            self.checkpoint_max_confirmation_lineage_reads
            > self.max_retained_root_generations
        ):
            raise ValueError("presentation history confirmation lineage is unretained")
        return self


class PresentationHistoryTreeNodeReferenceFact(FrozenFactBase):
    schema_version: Literal["presentation_history_tree_node_reference.v1"]
    node_kind: Literal["leaf", "internal"]
    node_artifact_id: str
    node_sha256: Fingerprint
    node_byte_count: PositiveInt
    first_placement_key: PresentationHistoryPlacementKeyFact
    last_placement_key: PresentationHistoryPlacementKeyFact
    subtree_entry_count: PositiveInt
    subtree_entry_accumulator: Fingerprint
    node_reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordered_range(self) -> "PresentationHistoryTreeNodeReferenceFact":
        if (
            self.first_placement_key.canonical_comparable_key_bytes
            > self.last_placement_key.canonical_comparable_key_bytes
        ):
            raise ValueError("presentation history node range is reversed")
        return self


class PresentationHistoryLeafNodeFact(FrozenFactBase):
    schema_version: Literal["presentation_history_leaf_node.v1"]
    node_kind: Literal["leaf"]
    ordered_entries: tuple[PresentationHistoryEntryFact, ...] = Field(min_length=1)
    subtree_entry_accumulator: Fingerprint
    node_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordered(self) -> "PresentationHistoryLeafNodeFact":
        keys = tuple(
            item.placement_key.canonical_comparable_key_bytes
            for item in self.ordered_entries
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("presentation history leaf keys are not strictly ordered")
        return self


class PresentationHistoryInternalNodeFact(FrozenFactBase):
    schema_version: Literal["presentation_history_internal_node.v1"]
    node_kind: Literal["internal"]
    tree_level: PositiveInt
    ordered_child_references: tuple[PresentationHistoryTreeNodeReferenceFact, ...] = (
        Field(min_length=1)
    )
    subtree_entry_accumulator: Fingerprint
    node_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _children(self) -> "PresentationHistoryInternalNodeFact":
        for left, right in zip(
            self.ordered_child_references,
            self.ordered_child_references[1:],
        ):
            if (
                left.last_placement_key.canonical_comparable_key_bytes
                >= right.first_placement_key.canonical_comparable_key_bytes
            ):
                raise ValueError("presentation history child ranges overlap")
        return self


class PresentationHistorySourcePrefixTransitionProofFact(FrozenFactBase):
    schema_version: Literal["presentation_history_source_prefix_transition.v1"]
    predecessor_through_sequence: NonNegativeInt
    predecessor_segment_count: NonNegativeInt
    predecessor_prefix_accumulator: Fingerprint
    ordered_added_segment_fingerprints: tuple[Fingerprint, ...]
    added_segment_count: NonNegativeInt
    resulting_through_sequence: NonNegativeInt
    resulting_segment_count: NonNegativeInt
    resulting_prefix_accumulator: Fingerprint
    transition_proof_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _continuity(self) -> "PresentationHistorySourcePrefixTransitionProofFact":
        if self.added_segment_count != len(self.ordered_added_segment_fingerprints):
            raise ValueError("presentation source-prefix segment count mismatch")
        if (
            self.resulting_through_sequence
            != self.predecessor_through_sequence + self.added_segment_count
            or self.resulting_segment_count
            != self.predecessor_segment_count + self.added_segment_count
        ):
            raise ValueError("presentation source-prefix transition is not contiguous")
        return self


class PresentationHistoryProjectionRootFact(FrozenFactBase):
    schema_version: Literal["presentation_history_projection_root.v1"]
    runtime_session_id: str
    root_codec_id: str
    root_codec_version: str
    root_codec_contract_fingerprint: Fingerprint
    history_projection_id: str
    history_projection_version: str
    history_projection_contract_fingerprint: Fingerprint
    materialization_policy_fingerprint: Fingerprint
    tree_contract_fingerprint: Fingerprint
    placement_key_contract_id: str
    placement_key_contract_version: str
    placement_key_contract_fingerprint: Fingerprint
    canonical_transcript_reducer_contract_fingerprint: Fingerprint
    event_domain_registry_contract_fingerprint: Fingerprint
    presentation_policy_registry_contract_fingerprint: Fingerprint
    audit_extractor_registry_contract_fingerprint: Fingerprint
    projection_generation: NonNegativeInt
    through_authority_sequence: NonNegativeInt
    presentation_source_segment_count: NonNegativeInt
    presentation_source_prefix_accumulator: Fingerprint
    source_prefix_transition_proof: (
        PresentationHistorySourcePrefixTransitionProofFact | None
    )
    previous_projection_root_reference: (
        "PresentationHistoryProjectionRootReferenceFact | None"
    )
    root_kind: Literal["empty", "non_empty"]
    tree_root_node_reference: PresentationHistoryTreeNodeReferenceFact | None
    tree_height: NonNegativeInt
    entry_count: NonNegativeInt
    first_placement_key: PresentationHistoryPlacementKeyFact | None
    last_placement_key: PresentationHistoryPlacementKeyFact | None
    canonical_transcript_spine_fingerprint: Fingerprint
    ordered_history_entry_accumulator: Fingerprint
    projection_root_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root_shape(self) -> "PresentationHistoryProjectionRootFact":
        if self.presentation_source_segment_count != self.through_authority_sequence:
            raise ValueError("presentation source segment count/high-water mismatch")
        if self.root_kind == "empty":
            if (
                any(
                    value is not None
                    for value in (
                        self.tree_root_node_reference,
                        self.first_placement_key,
                        self.last_placement_key,
                    )
                )
                or self.tree_height != 0
                or self.entry_count != 0
            ):
                raise ValueError("empty presentation root has a non-empty shape")
        else:
            if (
                self.tree_root_node_reference is None
                or self.first_placement_key is None
                or self.last_placement_key is None
                or self.tree_height <= 0
                or self.entry_count <= 0
                or self.tree_root_node_reference.subtree_entry_count != self.entry_count
            ):
                raise ValueError("non-empty presentation root shape mismatch")
        if self.projection_generation == 0:
            if (
                self.source_prefix_transition_proof is not None
                or self.previous_projection_root_reference is not None
                or self.through_authority_sequence != 0
            ):
                raise ValueError("presentation root genesis lineage is invalid")
        elif (
            self.source_prefix_transition_proof is None
            or self.previous_projection_root_reference is None
        ):
            raise ValueError("non-genesis presentation root lacks lineage")
        return self


class PresentationHistoryProjectionRootReferenceFact(FrozenFactBase):
    schema_version: Literal["presentation_history_projection_root_reference.v1"]
    root_kind: Literal["empty", "non_empty"]
    root_artifact_id: str
    root_sha256: Fingerprint
    root_byte_count: PositiveInt
    projection_root_fingerprint: Fingerprint
    materialization_policy_fingerprint: Fingerprint
    tree_contract_fingerprint: Fingerprint
    root_reference_fingerprint: Fingerprint


class PresentationHistoryProjectionCheckpointFact(FrozenFactBase):
    schema_version: Literal["presentation_history_projection_checkpoint.v1"]
    runtime_session_id: str
    checkpoint_kind: Literal["terminal_presentation_history"]
    checkpoint_generation: NonNegativeInt
    previous_checkpoint_fingerprint: Fingerprint | None
    through_authority_sequence: NonNegativeInt
    presentation_source_segment_count: NonNegativeInt
    presentation_source_prefix_accumulator: Fingerprint
    projection_revision: NonNegativeInt
    projection_root_reference: PresentationHistoryProjectionRootReferenceFact
    projection_root_fingerprint: Fingerprint
    checkpoint_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root_join(self) -> "PresentationHistoryProjectionCheckpointFact":
        if (
            self.presentation_source_segment_count != self.through_authority_sequence
            or self.projection_root_fingerprint
            != self.projection_root_reference.projection_root_fingerprint
        ):
            raise ValueError("presentation checkpoint/root join mismatch")
        return self


class PresentationHistoryCheckpointCandidateCutFact(FrozenFactBase):
    schema_version: Literal["presentation_history_checkpoint_candidate_cut.v1"]
    source_active_head_fingerprint: Fingerprint
    source_confirmed_root_fingerprint: Fingerprint
    cut_from_sequence_exclusive: NonNegativeInt
    cut_through_sequence: NonNegativeInt
    ordered_segment_fingerprints: tuple[Fingerprint, ...]
    segment_count: NonNegativeInt
    source_range_accumulator: Fingerprint
    segment_accumulator: Fingerprint
    mutation_count: NonNegativeInt
    mutation_accumulator: Fingerprint
    resulting_source_prefix_accumulator: Fingerprint
    resulting_resident_entry_accumulator: Fingerprint
    candidate_cut_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _segment_cut(self) -> "PresentationHistoryCheckpointCandidateCutFact":
        if (
            self.segment_count != len(self.ordered_segment_fingerprints)
            or self.cut_through_sequence
            != self.cut_from_sequence_exclusive + self.segment_count
        ):
            raise ValueError("presentation checkpoint candidate cut is not contiguous")
        return self


class PresentationHistoryCheckpointStableCandidateFact(FrozenFactBase):
    schema_version: Literal["presentation_history_checkpoint_stable_candidate.v1"]
    checkpoint_candidate_id: str
    runtime_session_id: str
    expected_predecessor_checkpoint: PresentationHistoryProjectionCheckpointFact
    candidate_cut: PresentationHistoryCheckpointCandidateCutFact
    resulting_projection_root: PresentationHistoryProjectionRootFact
    resulting_projection_root_reference: PresentationHistoryProjectionRootReferenceFact
    resulting_checkpoint: PresentationHistoryProjectionCheckpointFact
    ordered_required_artifact_ids: tuple[str, ...]
    stable_candidate_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "PresentationHistoryCheckpointStableCandidateFact":
        if (
            self.runtime_session_id
            != self.expected_predecessor_checkpoint.runtime_session_id
            or self.runtime_session_id
            != self.resulting_projection_root.runtime_session_id
            or self.runtime_session_id != self.resulting_checkpoint.runtime_session_id
            or self.resulting_projection_root.projection_root_fingerprint
            != self.resulting_projection_root_reference.projection_root_fingerprint
            or self.resulting_projection_root_reference
            != self.resulting_checkpoint.projection_root_reference
            or self.resulting_checkpoint.previous_checkpoint_fingerprint
            != self.expected_predecessor_checkpoint.checkpoint_fingerprint
            or self.candidate_cut.cut_from_sequence_exclusive
            != self.expected_predecessor_checkpoint.through_authority_sequence
            or self.candidate_cut.cut_through_sequence
            != self.resulting_checkpoint.through_authority_sequence
        ):
            raise ValueError("presentation checkpoint stable candidate join mismatch")
        if self.ordered_required_artifact_ids != tuple(
            sorted(set(self.ordered_required_artifact_ids))
        ):
            raise ValueError("presentation checkpoint artifact IDs are not canonical")
        return self


class PresentationHistoryRootIdentityFact(FrozenFactBase):
    schema_version: Literal["presentation_history_root_identity.v1"]
    runtime_session_id: str
    history_projection_contract_fingerprint: Fingerprint
    materialization_policy_fingerprint: Fingerprint
    tree_contract_fingerprint: Fingerprint
    placement_key_contract_id: str
    placement_key_contract_version: str
    placement_key_contract_fingerprint: Fingerprint
    checkpoint_generation: NonNegativeInt
    checkpoint_fingerprint: Fingerprint
    projection_root_reference: PresentationHistoryProjectionRootReferenceFact
    projection_generation: NonNegativeInt
    projection_root_fingerprint: Fingerprint
    through_authority_sequence: NonNegativeInt
    presentation_source_segment_count: NonNegativeInt
    presentation_source_prefix_accumulator: Fingerprint
    presentation_policy_registry_contract_fingerprint: Fingerprint
    audit_extractor_registry_contract_fingerprint: Fingerprint
    root_identity_fingerprint: Fingerprint


class PresentationHistoryPageCursorFact(FrozenFactBase):
    schema_version: Literal["presentation_history_page_cursor.v1"]
    runtime_session_id: str
    history_root_identity: PresentationHistoryRootIdentityFact
    anchor_history_entry_id: str | None
    anchor_placement_key: PresentationHistoryPlacementKeyFact | None
    cursor_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _anchor_pair(self) -> "PresentationHistoryPageCursorFact":
        if (self.anchor_history_entry_id is None) != (
            self.anchor_placement_key is None
        ):
            raise ValueError("presentation cursor anchor is partial")
        if self.runtime_session_id != self.history_root_identity.runtime_session_id:
            raise ValueError("presentation cursor crosses runtime sessions")
        return self


class ConfirmedRootRankBasisFact(FrozenFactBase):
    schema_version: Literal["presentation_confirmed_root_rank_basis.v1"]
    rank_basis_kind: Literal["confirmed_root"]
    history_root_identity_fingerprint: Fingerprint
    rank_basis_fingerprint: Fingerprint


class ActiveHeadRankBasisFact(FrozenFactBase):
    schema_version: Literal["presentation_active_head_rank_basis.v1"]
    rank_basis_kind: Literal["active_head"]
    history_active_head_fingerprint: Fingerprint
    through_authority_sequence: NonNegativeInt
    rank_basis_fingerprint: Fingerprint


PresentationHistoryRankBasisFact: TypeAlias = Annotated[
    ConfirmedRootRankBasisFact | ActiveHeadRankBasisFact,
    Field(discriminator="rank_basis_kind"),
]


class PresentationHistoryRankedEntryView(FrozenFactBase):
    schema_version: Literal["presentation_history_ranked_entry_view.v1"]
    history_entry: PresentationHistoryEntryFact
    root_local_display_rank: NonNegativeInt
    rank_basis: PresentationHistoryRankBasisFact
    ranked_view_fingerprint: Fingerprint


class UpsertPresentationHistoryEntryMutationFact(FrozenFactBase):
    schema_version: Literal["presentation_history_upsert_mutation.v1"]
    mutation_kind: Literal["upsert"]
    mutation_id: str
    source_from_sequence_exclusive: NonNegativeInt
    source_through_sequence: PositiveInt
    history_entry_id: str
    placement_key: PresentationHistoryPlacementKeyFact
    expected_previous_entry_fingerprint: Fingerprint | None
    resulting_entry: PresentationHistoryEntryFact
    mutation_fingerprint: Fingerprint


class RemovePresentationHistoryEntryMutationFact(FrozenFactBase):
    schema_version: Literal["presentation_history_remove_mutation.v1"]
    mutation_kind: Literal["remove"]
    mutation_id: str
    source_from_sequence_exclusive: NonNegativeInt
    source_through_sequence: PositiveInt
    history_entry_id: str
    placement_key: PresentationHistoryPlacementKeyFact
    expected_previous_entry_fingerprint: Fingerprint
    resulting_anchor_tombstone_reference: (
        CanonicalTranscriptPlacementAnchorTombstoneFact | None
    )
    mutation_fingerprint: Fingerprint


PresentationHistoryTailMutationFact: TypeAlias = Annotated[
    UpsertPresentationHistoryEntryMutationFact
    | RemovePresentationHistoryEntryMutationFact,
    Field(discriminator="mutation_kind"),
]


class PresentationHistoryTailFoldSegmentFact(FrozenFactBase):
    schema_version: Literal["presentation_history_tail_fold_segment.v1"]
    runtime_session_id: str
    from_sequence_exclusive: NonNegativeInt
    through_sequence: PositiveInt
    source_range_fingerprint: Fingerprint
    source_range_accumulator: Fingerprint
    ordered_mutations: tuple[PresentationHistoryTailMutationFact, ...]
    mutation_count: NonNegativeInt
    mutation_accumulator: Fingerprint
    segment_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _one_sequence(self) -> "PresentationHistoryTailFoldSegmentFact":
        if self.through_sequence != self.from_sequence_exclusive + 1:
            raise ValueError("presentation history segment must cover one sequence")
        if self.mutation_count != len(self.ordered_mutations):
            raise ValueError("presentation history segment mutation count mismatch")
        if any(
            item.source_from_sequence_exclusive != self.from_sequence_exclusive
            or item.source_through_sequence != self.through_sequence
            for item in self.ordered_mutations
        ):
            raise ValueError("presentation mutation crosses its one-sequence segment")
        return self


class AvailableHistoryCapacityFact(FrozenFactBase):
    schema_version: Literal["presentation_history_capacity_available.v1"]
    capacity_kind: Literal["available"]
    confirmed_entry_count: NonNegativeInt
    current_tail_worst_case_entry_count: NonNegativeInt
    active_growth_reservation_remaining_entry_count: NonNegativeInt
    projected_ordinary_entry_count: NonNegativeInt
    soft_rotation_threshold_entries: PositiveInt
    minimum_ordinary_growth_quote_entries: PositiveInt
    capacity_state_fingerprint: Fingerprint


class HistorySessionRotationRequiredFact(FrozenFactBase):
    schema_version: Literal["presentation_history_session_rotation_required.v1"]
    capacity_kind: Literal["session_rotation_required"]
    confirmed_entry_count: NonNegativeInt
    current_tail_worst_case_entry_count: NonNegativeInt
    active_growth_reservation_remaining_entry_count: NonNegativeInt
    projected_ordinary_entry_count: NonNegativeInt
    soft_rotation_threshold_entries: PositiveInt
    stable_reason: Literal[
        "soft_threshold_reached", "minimum_quote_unavailable", "spine_exhausted"
    ]
    capacity_state_fingerprint: Fingerprint


class HistoryTreeCapacityExhaustedFact(FrozenFactBase):
    schema_version: Literal["presentation_history_tree_capacity_exhausted.v1"]
    capacity_kind: Literal["tree_capacity_exhausted"]
    observed_entry_count: NonNegativeInt
    maximum_representable_entries: PositiveInt
    stable_fault_code: Literal["HISTORY_TREE_CAPACITY_EXHAUSTED"]
    capacity_state_fingerprint: Fingerprint


class HistoryCapacityReconciliationRequiredFact(FrozenFactBase):
    schema_version: Literal["presentation_history_capacity_reconciliation_required.v1"]
    capacity_kind: Literal["capacity_reconciliation_required"]
    stable_fault_code: Literal[
        "HISTORY_GROWTH_QUOTE_EXCEEDED",
        "CAPACITY_POLICY_DRIFT",
        "RESERVATION_AUTHORITY_CONFLICT",
    ]
    trusted_active_head_fingerprint: Fingerprint | None
    capacity_state_fingerprint: Fingerprint


PresentationHistoryCapacityStateFact: TypeAlias = Annotated[
    AvailableHistoryCapacityFact
    | HistorySessionRotationRequiredFact
    | HistoryTreeCapacityExhaustedFact
    | HistoryCapacityReconciliationRequiredFact,
    Field(discriminator="capacity_kind"),
]


class PresentationHistoryActiveHeadFact(FrozenFactBase):
    schema_version: Literal["presentation_history_active_head.v1"]
    runtime_session_id: str
    confirmed_root_identity: PresentationHistoryRootIdentityFact
    tail_from_sequence_exclusive: NonNegativeInt
    through_authority_sequence: NonNegativeInt
    tail_source_range_accumulator: Fingerprint
    tail_segment_count: NonNegativeInt
    ordered_tail_segment_accumulator: Fingerprint
    tail_mutation_count: NonNegativeInt
    ordered_tail_mutation_accumulator: Fingerprint
    resulting_resident_entry_count: NonNegativeInt
    resulting_resident_entry_accumulator: Fingerprint
    capacity_state: PresentationHistoryCapacityStateFact
    active_head_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _tail_range(self) -> "PresentationHistoryActiveHeadFact":
        if self.runtime_session_id != self.confirmed_root_identity.runtime_session_id:
            raise ValueError("presentation active head crosses runtime sessions")
        if (
            self.tail_from_sequence_exclusive
            != self.confirmed_root_identity.through_authority_sequence
            or self.through_authority_sequence
            != self.tail_from_sequence_exclusive + self.tail_segment_count
        ):
            raise ValueError("presentation active-head tail is not contiguous")
        return self


class PresentationHistoryCapacityAdmissionDecisionFact(FrozenFactBase):
    schema_version: Literal["presentation_history_capacity_admission_decision.v1"]
    runtime_session_id: str
    source_active_head_fingerprint: Fingerprint
    requested_growth_quote_fingerprint: Fingerprint
    confirmed_entry_count: NonNegativeInt
    current_tail_worst_case_entry_count: NonNegativeInt
    active_growth_reservation_remaining_entry_count: NonNegativeInt
    requested_admission_growth_quote_entry_count: PositiveInt
    projected_ordinary_entries: PositiveInt
    soft_rotation_threshold_entries: PositiveInt
    terminalization_maintenance_reserve_entries: PositiveInt
    maximum_representable_entries: PositiveInt
    disposition: Literal[
        "available",
        "session_rotation_required",
        "tree_capacity_exhausted",
        "capacity_reconciliation_required",
    ]
    resulting_capacity_state: PresentationHistoryCapacityStateFact
    decision_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _formula(self) -> "PresentationHistoryCapacityAdmissionDecisionFact":
        expected = (
            self.confirmed_entry_count
            + self.current_tail_worst_case_entry_count
            + self.active_growth_reservation_remaining_entry_count
            + self.requested_admission_growth_quote_entry_count
        )
        if self.projected_ordinary_entries != expected:
            raise ValueError("presentation capacity admission formula drifted")
        if (
            self.soft_rotation_threshold_entries
            + self.terminalization_maintenance_reserve_entries
            > self.maximum_representable_entries
        ):
            raise ValueError("presentation terminalization reserve is not isolated")
        if self.disposition != self.resulting_capacity_state.capacity_kind:
            raise ValueError("presentation capacity decision/state mismatch")
        return self


class PresentationHistoryGrowthQuoteFact(FrozenFactBase):
    schema_version: Literal["presentation_history_growth_quote.v1"]
    growth_quote_id: str
    runtime_session_id: str
    admission_kind: Literal[
        "prompt_submission",
        "run_activation",
        "queue_steer_delivery",
        "queue_follow_up_delivery",
        "interaction_continuation",
    ]
    source_authority_fingerprint: Fingerprint
    quote_policy_id: str
    quote_policy_version: str
    quote_policy_fingerprint: Fingerprint
    maximum_new_history_entries: PositiveInt
    quote_fingerprint: Fingerprint


class PresentationHistoryGrowthReservationFact(FrozenFactBase):
    schema_version: Literal["presentation_history_growth_reservation.v1"]
    growth_reservation_id: str
    quote: PresentationHistoryGrowthQuoteFact
    owner_kind: str
    owner_id: str
    owner_generation: NonNegativeInt
    reservation_revision: NonNegativeInt
    previous_reservation_fingerprint: Fingerprint | None
    settled_materialized_entry_count: NonNegativeInt
    remaining_unmaterialized_entry_count: NonNegativeInt
    reservation_state: Literal[
        "reserved", "settled", "released", "reconciliation_required"
    ]
    reservation_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounded(self) -> "PresentationHistoryGrowthReservationFact":
        if (
            self.settled_materialized_entry_count
            + self.remaining_unmaterialized_entry_count
            > self.quote.maximum_new_history_entries
        ):
            raise ValueError("presentation history reservation exceeded its quote")
        return self


_FACT_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "presentation_text_content_block.v1",
        "block_fingerprint",
        "presentation-text-content-block:v1",
    ),
    (
        "presentation_data_content_block.v1",
        "block_fingerprint",
        "presentation-data-content-block:v1",
    ),
    (
        "presentation_user_prompt_cell.v1",
        "cell_fingerprint",
        "presentation-user-prompt-cell:v1",
    ),
    (
        "presentation_assistant_message_cell.v1",
        "cell_fingerprint",
        "presentation-assistant-message-cell:v1",
    ),
    (
        "presentation_tool_terminal_cell.v1",
        "cell_fingerprint",
        "presentation-tool-terminal-cell:v1",
    ),
    ("presentation_error_cell.v1", "cell_fingerprint", "presentation-error-cell:v1"),
    (
        "presentation_interaction_cell.v1",
        "cell_fingerprint",
        "presentation-interaction-cell:v1",
    ),
    (
        "presentation_compaction_boundary_cell.v1",
        "cell_fingerprint",
        "presentation-compaction-boundary-cell:v1",
    ),
    (
        "presentation_recovery_cell.v1",
        "cell_fingerprint",
        "presentation-recovery-cell:v1",
    ),
    ("presentation_audit_cell.v1", "cell_fingerprint", "presentation-audit-cell:v1"),
    (
        "presentation_system_notice_cell.v1",
        "cell_fingerprint",
        "presentation-system-notice-cell:v1",
    ),
    (
        "presentation_model_activity_cell.v1",
        "activity_fingerprint",
        "presentation-model-activity-cell:v1",
    ),
    (
        "presentation_tool_activity_cell.v1",
        "activity_fingerprint",
        "presentation-tool-activity-cell:v1",
    ),
    (
        "presentation_terminal_process_activity_cell.v1",
        "activity_fingerprint",
        "presentation-terminal-process-activity-cell:v1",
    ),
    (
        "presentation_subagent_activity_cell.v1",
        "activity_fingerprint",
        "presentation-subagent-activity-cell:v1",
    ),
    (
        "presentation_event_purpose_policy.v1",
        "policy_fingerprint",
        "presentation-event-purpose-policy:v1",
    ),
    (
        "presentation_purpose_policy_registry.v1",
        "registry_fingerprint",
        "presentation-purpose-policy-registry:v1",
    ),
    (
        "presentation_audit_extractor_contract.v1",
        "contract_fingerprint",
        "presentation-audit-extractor-contract:v1",
    ),
    (
        "presentation_history_placement_key_contract.v1",
        "contract_fingerprint",
        "presentation-history-placement-key-contract:v1",
    ),
    (
        "presentation_history_placement_key.v1",
        "placement_key_fingerprint",
        "presentation-history-placement-key:v1",
    ),
    (
        "canonical_transcript_history_source.v1",
        "source_fingerprint",
        "canonical-transcript-history-source:v1",
    ),
    (
        "presentation_before_leaf_audit_anchor.v1",
        "anchor_fingerprint",
        "presentation-before-leaf-audit-anchor:v1",
    ),
    (
        "presentation_after_leaf_audit_anchor.v1",
        "anchor_fingerprint",
        "presentation-after-leaf-audit-anchor:v1",
    ),
    (
        "presentation_ledger_sequence_audit_anchor.v1",
        "anchor_fingerprint",
        "presentation-ledger-sequence-audit-anchor:v1",
    ),
    (
        "durable_audit_history_source.v1",
        "source_fingerprint",
        "durable-audit-history-source:v1",
    ),
    (
        "presentation_history_entry.v1",
        "entry_fingerprint",
        "presentation-history-entry:v1",
    ),
    (
        "presentation_history_tree_contract.v1",
        "tree_contract_fingerprint",
        "presentation-history-tree-contract:v1",
    ),
    (
        "presentation_history_growth_quote_kind_bound.v1",
        "kind_bound_fingerprint",
        "presentation-history-growth-quote-kind-bound:v1",
    ),
    (
        "presentation_history_growth_quote_policy.v1",
        "quote_policy_fingerprint",
        "presentation-history-growth-quote-policy:v1",
    ),
    (
        "presentation_history_materialization_policy.v1",
        "policy_fingerprint",
        "presentation-history-materialization-policy:v1",
    ),
    (
        "presentation_history_tree_node_reference.v1",
        "node_reference_fingerprint",
        "presentation-history-tree-node-reference:v1",
    ),
    (
        "presentation_history_leaf_node.v1",
        "node_fingerprint",
        "presentation-history-leaf-node:v1",
    ),
    (
        "presentation_history_internal_node.v1",
        "node_fingerprint",
        "presentation-history-internal-node:v1",
    ),
    (
        "presentation_history_source_prefix_transition.v1",
        "transition_proof_fingerprint",
        "presentation-history-source-prefix-transition:v1",
    ),
    (
        "presentation_history_projection_root.v1",
        "projection_root_fingerprint",
        "presentation-history-projection-root:v1",
    ),
    (
        "presentation_history_projection_root_reference.v1",
        "root_reference_fingerprint",
        "presentation-history-projection-root-reference:v1",
    ),
    (
        "presentation_history_projection_checkpoint.v1",
        "checkpoint_fingerprint",
        "presentation-history-projection-checkpoint:v1",
    ),
    (
        "presentation_history_checkpoint_candidate_cut.v1",
        "candidate_cut_fingerprint",
        "presentation-history-checkpoint-candidate-cut:v1",
    ),
    (
        "presentation_history_checkpoint_stable_candidate.v1",
        "stable_candidate_fingerprint",
        "presentation-history-checkpoint-stable-candidate:v1",
    ),
    (
        "presentation_history_root_identity.v1",
        "root_identity_fingerprint",
        "presentation-history-root-identity:v1",
    ),
    (
        "presentation_history_page_cursor.v1",
        "cursor_fingerprint",
        "presentation-history-page-cursor:v1",
    ),
    (
        "presentation_confirmed_root_rank_basis.v1",
        "rank_basis_fingerprint",
        "presentation-confirmed-root-rank-basis:v1",
    ),
    (
        "presentation_active_head_rank_basis.v1",
        "rank_basis_fingerprint",
        "presentation-active-head-rank-basis:v1",
    ),
    (
        "presentation_history_ranked_entry_view.v1",
        "ranked_view_fingerprint",
        "presentation-history-ranked-entry-view:v1",
    ),
    (
        "presentation_history_upsert_mutation.v1",
        "mutation_fingerprint",
        "presentation-history-upsert-mutation:v1",
    ),
    (
        "presentation_history_remove_mutation.v1",
        "mutation_fingerprint",
        "presentation-history-remove-mutation:v1",
    ),
    (
        "presentation_history_tail_fold_segment.v1",
        "segment_fingerprint",
        "presentation-history-tail-fold-segment:v1",
    ),
    (
        "presentation_history_capacity_available.v1",
        "capacity_state_fingerprint",
        "presentation-history-capacity-available:v1",
    ),
    (
        "presentation_history_session_rotation_required.v1",
        "capacity_state_fingerprint",
        "presentation-history-session-rotation-required:v1",
    ),
    (
        "presentation_history_tree_capacity_exhausted.v1",
        "capacity_state_fingerprint",
        "presentation-history-tree-capacity-exhausted:v1",
    ),
    (
        "presentation_history_capacity_reconciliation_required.v1",
        "capacity_state_fingerprint",
        "presentation-history-capacity-reconciliation-required:v1",
    ),
    (
        "presentation_history_active_head.v1",
        "active_head_fingerprint",
        "presentation-history-active-head:v1",
    ),
    (
        "presentation_history_capacity_admission_decision.v1",
        "decision_fingerprint",
        "presentation-history-capacity-admission-decision:v1",
    ),
    (
        "presentation_history_growth_quote.v1",
        "quote_fingerprint",
        "presentation-history-growth-quote:v1",
    ),
    (
        "presentation_history_growth_reservation.v1",
        "reservation_fingerprint",
        "presentation-history-growth-reservation:v1",
    ),
)

for _schema, _field, _domain in _FACT_SPECS:
    register_durable_fact(
        schema_version=_schema,
        own_fingerprint_field=_field,
        domain_separator=_domain,
    )


def build_default_placement_key_contract() -> (
    PresentationHistoryPlacementKeyContractFact
):
    contract = build_frozen_fact(
        PresentationHistoryPlacementKeyContractFact,
        schema_version="presentation_history_placement_key_contract.v1",
        placement_key_contract_id=PRESENTATION_PLACEMENT_KEY_CONTRACT_ID,
        placement_key_contract_version=PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION,
        framing_id="presentation-history-placement-key-fixed:v1",
        framing_magic_ascii="PHK1",
        framing_version_uint16=PLACEMENT_VERSION,
        encoded_byte_count=PLACEMENT_ENCODED_BYTES,
        spine_coordinate_type="uint64",
        spine_coordinate_width_bytes=8,
        integer_byte_order="big_endian",
        spine_coordinate_genesis=1,
        spine_coordinate_left_none_sentinel=0,
        spine_coordinate_right_none_sentinel=UINT64_MAX,
        spine_coordinate_max_append_value=UINT64_MAX - 1,
        relative_position_kind_order=(
            "before_first",
            "before_leaf",
            "canonical_leaf",
            "after_leaf",
            "ledger_gap",
            "after_last",
        ),
        relative_position_kind_width_bytes=1,
        source_sequence_type="uint64",
        source_sequence_width_bytes=8,
        local_ordinal_type="uint32",
        local_ordinal_width_bytes=4,
        stable_tiebreaker_contract_id="sha256-event-safe-stable-id:v1",
        stable_tiebreaker_input_normalization="canonical_utf8_stable_id",
        stable_tiebreaker_byte_count=32,
        canonical_layout=(
            "magic[4]||version[2]||primary[8]||kind[1]||sequence[8]||"
            "ordinal[4]||left[8]||right[8]||tiebreaker[32]"
        ),
    )
    if contract.contract_fingerprint != PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT:
        raise RuntimeError("presentation placement contract golden drifted")
    return contract


def build_default_history_materialization_policy() -> (
    PresentationHistoryMaterializationPolicyFact
):
    placement = build_default_placement_key_contract()
    tree = build_frozen_fact(
        PresentationHistoryTreeContractFact,
        schema_version="presentation_history_tree_contract.v1",
        tree_contract_id="pulsara.presentation-history-tree",
        tree_contract_version="1",
        placement_key_contract=placement,
        max_inline_entry_bytes=64 * 1024,
        max_leaf_entries=64,
        max_leaf_node_bytes=4 * 1024 * 1024,
        max_internal_fanout=32,
        max_internal_node_bytes=512 * 1024,
        max_tree_height=4,
        maximum_representable_entries=64 * 32**3,
        node_canonicalization_contract_fingerprint=_simple_fingerprint(
            "presentation-history-node-canonicalization:v1",
            "canonical-json-utf8+content-addressed",
        ),
        ordering_contract_fingerprint=_simple_fingerprint(
            "presentation-history-tree-ordering:v1",
            "unsigned-lexicographic-fixed-75-byte-key",
        ),
    )
    bounds = []
    for kind, maximum in (
        ("interaction_continuation", 16),
        ("prompt_submission", 16),
        ("queue_follow_up_delivery", 16),
        ("queue_steer_delivery", 16),
        ("run_activation", 256),
    ):
        bounds.append(
            build_frozen_fact(
                PresentationHistoryGrowthQuoteKindBoundFact,
                schema_version="presentation_history_growth_quote_kind_bound.v1",
                admission_kind=kind,
                maximum_new_history_entries=maximum,
                derivation_input_contract_fingerprint=_simple_fingerprint(
                    "presentation-history-growth-derivation-input:v1", kind
                ),
            )
        )
    quote = build_frozen_fact(
        PresentationHistoryGrowthQuotePolicyFact,
        schema_version="presentation_history_growth_quote_policy.v1",
        quote_policy_id="pulsara.presentation-history-growth",
        quote_policy_version="1",
        ordered_kind_bounds=tuple(bounds),
        maximum_active_committed_runs_per_session=1,
        maximum_nonterminal_growth_reservations_per_session=8,
        quote_derivation_contract_fingerprint=_simple_fingerprint(
            "presentation-history-growth-quote-derivation:v1",
            "closed-admission-kind-max-entry-bound",
        ),
    )
    return build_frozen_fact(
        PresentationHistoryMaterializationPolicyFact,
        schema_version="presentation_history_materialization_policy.v1",
        policy_id="pulsara.presentation-history-materialization",
        policy_version="1",
        tree_contract=tree,
        growth_quote_policy=quote,
        max_root_fact_bytes=4 * 1024 * 1024,
        checkpoint_max_new_nodes=1_024,
        checkpoint_max_new_node_bytes=64 * 1024 * 1024,
        checkpoint_max_confirmation_lineage_reads=8,
        tail_soft_max_events=1_024,
        tail_soft_max_entries=4_096,
        tail_soft_max_bytes=64 * 1024 * 1024,
        tail_hard_max_events=4_096,
        tail_hard_max_entries=16_384,
        tail_hard_max_bytes=256 * 1024 * 1024,
        capacity_soft_rotation_threshold_entries=(64 * 32**3) - 2_048,
        terminalization_maintenance_reserve_entries=1_024,
        minimum_ordinary_growth_quote_entries=1,
        capacity_growth_and_reserve_contract_fingerprint=_simple_fingerprint(
            "presentation-history-capacity-contract:v1",
            "confirmed+tail+active-remaining+requested;terminal-reserve-separate",
        ),
        max_retained_root_generations=8,
        root_retention_ttl_seconds=24 * 60 * 60,
        read_max_entries=512,
        read_max_page_canonical_bytes=8 * 1024 * 1024,
        read_max_page_rendered_bytes=8 * 1024 * 1024,
        read_max_node_reads=128,
        read_max_tree_height=4,
        retention_contract_fingerprint=_simple_fingerprint(
            "presentation-history-retention-contract:v1",
            "lease+generation+ttl",
        ),
        read_contract_fingerprint=_simple_fingerprint(
            "presentation-history-read-contract:v1",
            "bounded-height+entries+canonical-bytes+rendered-bytes",
        ),
    )


def _simple_fingerprint(domain: str, value: object) -> str:
    from pulsara_agent.primitives.context import context_fingerprint

    return context_fingerprint(domain, value)


def build_presentation_history_placement_key(
    *,
    contract: PresentationHistoryPlacementKeyContractFact,
    canonical_spine_left_coordinate: int | None,
    canonical_spine_right_coordinate: int | None,
    relative_position_kind: RelativePositionKind,
    source_ledger_sequence_or_zero: int,
    relative_local_ordinal: int,
    stable_source_id: str,
) -> PresentationHistoryPlacementKeyFact:
    """Build the sole canonical 75-byte history placement key."""

    contract.__class__.model_validate(contract)
    left = canonical_spine_left_coordinate
    right = canonical_spine_right_coordinate
    if left is not None and not 1 <= left <= UINT64_MAX - 1:
        raise ValueError("left presentation coordinate is out of range")
    if right is not None and not 1 <= right <= UINT64_MAX - 1:
        raise ValueError("right presentation coordinate is out of range")
    if left is not None and right is not None and left > right:
        raise ValueError("presentation placement coordinates are reversed")
    if not 0 <= source_ledger_sequence_or_zero <= UINT64_MAX:
        raise ValueError("presentation source sequence is out of range")
    if not 0 <= relative_local_ordinal <= UINT32_MAX:
        raise ValueError("presentation local ordinal is out of range")

    if relative_position_kind == "canonical_leaf":
        if (
            left is None
            or right is None
            or source_ledger_sequence_or_zero != 0
            or relative_local_ordinal != 0
        ):
            raise ValueError("canonical placement key field matrix mismatch")
        primary = left
    else:
        if source_ledger_sequence_or_zero == 0:
            raise ValueError("audit placement requires an exact source sequence")
        if relative_position_kind == "before_first":
            if left is not None:
                raise ValueError("before-first placement cannot have a left coordinate")
            primary = 0
        elif relative_position_kind == "before_leaf":
            if right is None:
                raise ValueError("before-leaf placement requires its target coordinate")
            primary = right
        elif relative_position_kind == "after_leaf":
            if left is None:
                raise ValueError("after-leaf placement requires its target coordinate")
            primary = left
        elif relative_position_kind == "ledger_gap":
            if left is None or right is None or left >= right:
                raise ValueError("ledger-gap placement requires a strict bounded gap")
            primary = left
        elif relative_position_kind == "after_last":
            if right is not None:
                raise ValueError("after-last placement cannot have a right coordinate")
            primary = left or 0
        else:  # pragma: no cover - Literal is closed
            raise ValueError("unknown presentation placement kind")

    rank = contract.relative_position_kind_order.index(relative_position_kind)
    from pulsara_agent.primitives.context import context_fingerprint

    stable_fingerprint = context_fingerprint(
        "presentation-history-stable-tiebreaker:v1",
        {"normalized_stable_id": stable_source_id},
    )
    stable_digest = bytes.fromhex(stable_fingerprint.removeprefix("sha256:"))
    encoded = b"".join(
        (
            PLACEMENT_MAGIC,
            PLACEMENT_VERSION.to_bytes(2, "big"),
            primary.to_bytes(8, "big"),
            rank.to_bytes(1, "big"),
            source_ledger_sequence_or_zero.to_bytes(8, "big"),
            relative_local_ordinal.to_bytes(4, "big"),
            (left or 0).to_bytes(8, "big"),
            (right if right is not None else UINT64_MAX).to_bytes(8, "big"),
            stable_digest,
        )
    )
    if len(encoded) != PLACEMENT_ENCODED_BYTES:
        raise AssertionError("presentation placement encoder width drifted")
    return build_frozen_fact(
        PresentationHistoryPlacementKeyFact,
        schema_version="presentation_history_placement_key.v1",
        placement_key_contract_id=contract.placement_key_contract_id,
        placement_key_contract_version=contract.placement_key_contract_version,
        placement_key_contract_fingerprint=contract.contract_fingerprint,
        canonical_spine_left_coordinate=left,
        canonical_spine_right_coordinate=right,
        relative_position_kind=relative_position_kind,
        source_ledger_sequence_or_zero=source_ledger_sequence_or_zero,
        relative_local_ordinal=relative_local_ordinal,
        stable_source_tiebreaker=stable_fingerprint,
        canonical_comparable_key_bytes=encoded,
    )


__all__ = [
    "AssistantMessageCell",
    "AuditCell",
    "CanonicalTranscriptHistorySourceFact",
    "ConfirmedRootRankBasisFact",
    "DurableAuditHistorySourceFact",
    "DurableHistoryCell",
    "OperationalActivityCell",
    "PresentationContentBlockFact",
    "ActiveHeadRankBasisFact",
    "AvailableHistoryCapacityFact",
    "HistoryCapacityReconciliationRequiredFact",
    "HistorySessionRotationRequiredFact",
    "HistoryTreeCapacityExhaustedFact",
    "PresentationHistoryActiveHeadFact",
    "PresentationHistoryCapacityAdmissionDecisionFact",
    "PresentationHistoryCapacityStateFact",
    "PresentationHistoryCheckpointCandidateCutFact",
    "PresentationHistoryCheckpointStableCandidateFact",
    "PresentationAuditExtractorContractFact",
    "PresentationHistoryPageCursorFact",
    "PresentationHistoryEntryFact",
    "PresentationHistoryGrowthQuoteFact",
    "PresentationHistoryGrowthReservationFact",
    "PresentationHistoryInternalNodeFact",
    "PresentationHistoryLeafNodeFact",
    "PresentationHistoryPlacementKeyContractFact",
    "PresentationHistoryPlacementKeyFact",
    "PresentationHistoryMaterializationPolicyFact",
    "PresentationHistoryProjectionCheckpointFact",
    "PresentationHistoryProjectionRootFact",
    "PresentationHistoryProjectionRootReferenceFact",
    "PresentationHistoryRankedEntryView",
    "PresentationHistoryRankBasisFact",
    "PresentationHistoryRootIdentityFact",
    "PresentationHistoryTailFoldSegmentFact",
    "PresentationHistoryTailMutationFact",
    "PresentationHistorySourcePrefixTransitionProofFact",
    "PresentationHistoryTreeContractFact",
    "PresentationHistoryTreeNodeReferenceFact",
    "PresentationEventPurposePolicyFact",
    "PresentationPurposePolicyRegistryFact",
    "PresentationTextContentBlockFact",
    "ToolTerminalCell",
    "UpsertPresentationHistoryEntryMutationFact",
    "UserPromptCell",
    "build_default_placement_key_contract",
    "build_default_history_materialization_policy",
    "build_presentation_history_placement_key",
]
