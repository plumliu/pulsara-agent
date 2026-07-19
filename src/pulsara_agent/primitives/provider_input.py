"""Durable contracts for append-only canonical provider input generations.

Provider-visible semantics, durable materialization, preparation ownership, and
post-commit attribution are deliberately separate.  In particular, no event
reference participates in the committed core fingerprint and no prepared owner
participates in provider-visible prefix identity.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, PositiveInt, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    FrozenJsonObjectFact,
    FrozenJsonValue,
    context_fingerprint,
)
from pulsara_agent.primitives.context_source import (
    CanonicalContextSourceRevisionFact,
    CapabilityToolCatalogRootFact,
    ContextArtifactReferenceFact,
    ContextSourceId,
    LedgerAuthorityHorizonFact,
    LedgerAuthorityHorizonSetReferenceFact,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    StableEventIdentityFact,
    register_durable_fact,
)
from pulsara_agent.primitives.model_call import ResolvedModelCallFact
from pulsara_agent.primitives.terminal_projection import TerminalProjectionReferenceFact
from pulsara_agent.primitives.transcript_projection import (
    TranscriptProjectionLeafEntryReferenceFact,
)


Fingerprint = str


def _ordered_accumulator(domain: str, values: tuple[str, ...]) -> str:
    accumulator = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        accumulator = context_fingerprint(f"{domain}:step", (accumulator, value))
    return accumulator


def _fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


class ProviderInputRolloverReason(StrEnum):
    SYSTEM_ROOT_SEMANTIC_CHANGED = "system_root_semantic_changed"
    TOOL_CATALOG_SEMANTIC_CHANGED = "tool_catalog_semantic_changed"
    PROVIDER_VISIBLE_COMPATIBILITY_CHANGED = (
        "provider_visible_compatibility_changed"
    )
    AUXILIARY_FRAME_REBASE = "auxiliary_frame_rebase"
    EXPLICIT_LONG_HORIZON_REWRITE = "explicit_long_horizon_rewrite"
    CONFIRMED_OFFLINE_AUTHORITY_REPAIR = "confirmed_offline_authority_repair"
    EXPLICIT_ADMINISTRATIVE_RESET = "explicit_administrative_reset"


class ProviderInputCausalValidationFailureReason(StrEnum):
    USER_AFTER_DESCENDANT = "user_after_descendant"
    TOOL_RESULT_BEFORE_CALL = "tool_result_before_call"
    CONTINUATION_BEFORE_TOOL_RESULT = "continuation_before_tool_result"
    DUPLICATE_TRANSCRIPT_MESSAGE = "duplicate_transcript_message"
    PROJECTION_SOURCE_JOIN_MISMATCH = "projection_source_join_mismatch"
    FRAME_PLACEMENT_MISMATCH = "frame_placement_mismatch"
    COMPACTION_REWRITE_PROOF_MISMATCH = "compaction_rewrite_proof_mismatch"


class ProviderInputPhysicalPolicyFailureReason(StrEnum):
    PROVIDER_TOOL_CALL_FAN_IN_EXCEEDED = "provider_tool_call_fan_in_exceeded"
    PROVIDER_INPUT_PROJECTION_UNIT_BOUND_EXCEEDED = (
        "provider_input_projection_unit_bound_exceeded"
    )
    PROVIDER_INPUT_PROJECTION_BYTE_BOUND_EXCEEDED = (
        "provider_input_projection_byte_bound_exceeded"
    )
    PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED = (
        "provider_input_append_unit_bound_exceeded"
    )
    PROVIDER_INPUT_APPEND_BYTE_BOUND_EXCEEDED = (
        "provider_input_append_byte_bound_exceeded"
    )
    PROVIDER_INPUT_PHYSICAL_POLICY_UNSATISFIED = (
        "provider_input_physical_policy_unsatisfied"
    )


class ProviderInputReconciliationReason(StrEnum):
    COMMIT_OUTCOME_PARTIAL = "commit_outcome_partial"
    COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"
    COMMITTED_EVENT_CONFLICT = "committed_event_conflict"
    MODEL_START_JOIN_MISMATCH = "model_start_join_mismatch"
    REDUCER_STATE_MISMATCH = "reducer_state_mismatch"
    REQUIRED_ARTIFACT_UNTRUSTED = "required_artifact_untrusted"


@_fact(
    "provider_visible_input_compatibility.v1",
    "semantic_fingerprint",
    "provider-visible-input-compatibility:v1",
)
class ProviderVisibleInputCompatibilityFact(FrozenFactBase):
    schema_version: Literal["provider_visible_input_compatibility.v1"] = (
        "provider_visible_input_compatibility.v1"
    )
    requested_model_identity: str = Field(min_length=1)
    provider_api_kind: str = Field(min_length=1)
    adapter_input_contract_id: str = Field(min_length=1)
    adapter_input_contract_version: str = Field(min_length=1)
    adapter_input_contract_fingerprint: Fingerprint
    tool_order_contract_fingerprint: Fingerprint
    transcript_lowering_contract_fingerprint: Fingerprint
    context_source_lowering_contract_fingerprint: Fingerprint
    provider_input_framing_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


@_fact(
    "provider_input_generation_compatibility.v1",
    "compatibility_fingerprint",
    "provider-input-generation-compatibility:v1",
)
class ProviderInputGenerationCompatibilityFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_compatibility.v1"] = (
        "provider_input_generation_compatibility.v1"
    )
    provider_visible: ProviderVisibleInputCompatibilityFact
    system_instruction_semantic_fingerprint: Fingerprint
    tool_catalog_semantic_fingerprint: Fingerprint
    compatibility_fingerprint: Fingerprint


@_fact(
    "provider_input_replay_binding_identity.v1",
    "identity_fingerprint",
    "provider-input-replay-binding-identity:v1",
)
class ProviderInputReplayBindingIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_input_replay_binding_identity.v1"] = (
        "provider_input_replay_binding_identity.v1"
    )
    binding_kind: Literal[
        "event_schema", "context_source", "provider_lowering", "artifact_codec"
    ]
    contract_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    schema_or_contract_fingerprint: Fingerprint
    identity_fingerprint: Fingerprint


@_fact(
    "provider_input_replay_binding_set_reference.v1",
    "reference_fingerprint",
    "provider-input-replay-binding-set-reference:v1",
)
class ProviderInputReplayBindingSetReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_replay_binding_set_reference.v1"] = (
        "provider_input_replay_binding_set_reference.v1"
    )
    binding_count: int = Field(ge=0)
    ordered_binding_accumulator: Fingerprint
    root_artifact_ref: ContextArtifactReferenceFact | None
    set_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _empty(self) -> "ProviderInputReplayBindingSetReferenceFact":
        if (self.binding_count == 0) != (self.root_artifact_ref is None):
            raise ValueError("replay binding empty-root matrix mismatch")
        return self


@_fact(
    "session_provider_input_continuity_scope.v1",
    "scope_fingerprint",
    "session-provider-input-continuity-scope:v1",
)
class SessionProviderInputContinuityScopeFact(FrozenFactBase):
    schema_version: Literal["session_provider_input_continuity_scope.v1"] = (
        "session_provider_input_continuity_scope.v1"
    )
    scope_kind: Literal["session_continuity"] = "session_continuity"
    runtime_session_id: str = Field(min_length=1)
    call_lane: Literal["main_agent", "subagent"]
    subagent_id: str | None
    compatibility_cohort_fingerprint: Fingerprint
    scope_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _lane(self) -> "SessionProviderInputContinuityScopeFact":
        if (self.call_lane == "subagent") != (self.subagent_id is not None):
            raise ValueError("provider continuity scope subagent matrix mismatch")
        return self


@_fact(
    "one_shot_generation_scope.v1",
    "scope_fingerprint",
    "one-shot-generation-scope:v1",
)
class OneShotGenerationScopeFact(FrozenFactBase):
    schema_version: Literal["one_shot_generation_scope.v1"] = (
        "one_shot_generation_scope.v1"
    )
    scope_kind: Literal["one_shot"] = "one_shot"
    operation_kind: Literal[
        "direct_model_call", "window_summarizer", "governance_model_call"
    ]
    operation_id: str = Field(min_length=1)
    attempt_index: int = Field(ge=0)
    scope_fingerprint: Fingerprint


ProviderInputGenerationScopeFact: TypeAlias = (
    SessionProviderInputContinuityScopeFact | OneShotGenerationScopeFact
)


@_fact(
    "provider_input_generation.v1",
    "generation_fingerprint",
    "provider-input-generation:v1",
)
class ProviderInputGenerationFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation.v1"] = (
        "provider_input_generation.v1"
    )
    generation_id: str = Field(min_length=1)
    call_lane: Literal[
        "main_agent",
        "subagent",
        "direct_one_shot",
        "window_summarizer",
        "governance_one_shot",
    ]
    scope: ProviderInputGenerationScopeFact
    compatibility: ProviderInputGenerationCompatibilityFact
    predecessor_generation_id: str | None
    predecessor_generation_fingerprint: Fingerprint | None
    rollover_reason: ProviderInputRolloverReason | None
    generation_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _predecessor(self) -> "ProviderInputGenerationFact":
        if (self.predecessor_generation_id is None) != (
            self.predecessor_generation_fingerprint is None
        ):
            raise ValueError("generation predecessor fields must be paired")
        if self.predecessor_generation_id is None and self.rollover_reason is not None:
            raise ValueError("first generation has an invalid rollover reason")
        if self.predecessor_generation_id is not None and self.rollover_reason is None:
            raise ValueError("successor generation requires rollover reason")
        return self


@_fact(
    "provider_input_text_block.v1",
    "semantic_fingerprint",
    "provider-input-text-block:v1",
)
class ProviderInputTextBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_text_block.v1"] = (
        "provider_input_text_block.v1"
    )
    block_kind: Literal["text"] = "text"
    text: str
    utf8_bytes: int = Field(ge=0)
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bytes(self) -> "ProviderInputTextBlockFact":
        if self.utf8_bytes != len(self.text.encode("utf-8")):
            raise ValueError("provider text block byte count mismatch")
        return self


@_fact(
    "provider_input_thinking_block.v1",
    "semantic_fingerprint",
    "provider-input-thinking-block:v1",
)
class ProviderInputThinkingBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_thinking_block.v1"] = (
        "provider_input_thinking_block.v1"
    )
    block_kind: Literal["thinking"] = "thinking"
    text: str
    utf8_bytes: int = Field(ge=0)
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bytes(self) -> "ProviderInputThinkingBlockFact":
        if self.utf8_bytes != len(self.text.encode("utf-8")):
            raise ValueError("provider thinking block byte count mismatch")
        return self


@_fact(
    "provider_input_data_block.v1",
    "semantic_fingerprint",
    "provider-input-data-block:v1",
)
class ProviderInputDataBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_data_block.v1"] = (
        "provider_input_data_block.v1"
    )
    block_kind: Literal["data"] = "data"
    media_type: str = Field(min_length=1)
    canonical_data: FrozenJsonValue
    semantic_fingerprint: Fingerprint


@_fact(
    "provider_input_tool_call_block.v1",
    "semantic_fingerprint",
    "provider-input-tool-call-block:v1",
)
class ProviderInputToolCallBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_tool_call_block.v1"] = (
        "provider_input_tool_call_block.v1"
    )
    block_kind: Literal["tool_call"] = "tool_call"
    tool_call_id: str = Field(min_length=1)
    model_tool_name: str = Field(min_length=1)
    arguments_state: Literal["valid_object", "invalid_json", "non_object_json"]
    canonical_arguments: FrozenJsonObjectFact | None
    raw_arguments_json: str
    parse_error_code: str | None
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _arguments(self) -> "ProviderInputToolCallBlockFact":
        valid = self.arguments_state == "valid_object"
        if valid != (self.canonical_arguments is not None):
            raise ValueError("tool-call argument state/object matrix mismatch")
        if valid == (self.parse_error_code is not None):
            raise ValueError("tool-call argument state/error matrix mismatch")
        return self


@_fact(
    "provider_input_tool_result_block.v1",
    "semantic_fingerprint",
    "provider-input-tool-result-block:v1",
)
class ProviderInputToolResultBlockFact(FrozenFactBase):
    schema_version: Literal["provider_input_tool_result_block.v1"] = (
        "provider_input_tool_result_block.v1"
    )
    block_kind: Literal["tool_result"] = "tool_result"
    tool_call_id: str = Field(min_length=1)
    model_tool_name: str | None
    result_state: Literal["success", "error", "interrupted", "denied"]
    terminal_projection_semantic_fingerprint: Fingerprint
    content: tuple[ProviderInputTextBlockFact | ProviderInputDataBlockFact, ...]
    semantic_fingerprint: Fingerprint


ProviderInputContentBlockFact: TypeAlias = (
    ProviderInputTextBlockFact
    | ProviderInputThinkingBlockFact
    | ProviderInputDataBlockFact
    | ProviderInputToolCallBlockFact
    | ProviderInputToolResultBlockFact
)


@_fact(
    "provider_message_fragment.v1",
    "semantic_fingerprint",
    "provider-message-fragment:v1",
)
class ProviderMessageFragmentFact(FrozenFactBase):
    schema_version: Literal["provider_message_fragment.v1"] = (
        "provider_message_fragment.v1"
    )
    fragment_kind: Literal["message"] = "message"
    role: Literal[
        "system", "user", "assistant", "tool_call", "tool_result", "runtime_observation"
    ]
    name: str | None
    tool_call_id: str | None
    content_blocks: tuple[ProviderInputContentBlockFact, ...]
    semantic_fingerprint: Fingerprint


@_fact(
    "provider_tool_spec_fragment.v1",
    "semantic_fingerprint",
    "provider-tool-spec-fragment:v1",
)
class ProviderToolSpecFragmentFact(FrozenFactBase):
    schema_version: Literal["provider_tool_spec_fragment.v1"] = (
        "provider_tool_spec_fragment.v1"
    )
    fragment_kind: Literal["tool_spec"] = "tool_spec"
    name: str = Field(min_length=1)
    description: str
    frozen_parameters: FrozenJsonObjectFact
    semantic_fingerprint: Fingerprint


ProviderInputTypedFragmentFact: TypeAlias = (
    ProviderMessageFragmentFact | ProviderToolSpecFragmentFact
)


@_fact(
    "provider_wire_message_semantic.v1",
    "wire_semantic_fingerprint",
    "provider-wire-message-semantic:v1",
)
class ProviderWireMessageSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_wire_message_semantic.v1"] = (
        "provider_wire_message_semantic.v1"
    )
    provider_message: ProviderMessageFragmentFact
    wire_framing_contract_fingerprint: Fingerprint
    wire_semantic_fingerprint: Fingerprint


@_fact(
    "direct_stable_message_semantic_source.v1",
    "source_semantic_fingerprint",
    "direct-stable-message-semantic-source:v1",
)
class DirectStableMessageSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["direct_stable_message_semantic_source.v1"] = (
        "direct_stable_message_semantic_source.v1"
    )
    source_kind: Literal["direct_stable_message"] = "direct_stable_message"
    stable_entry_kind: Literal["message"] = "message"
    canonical_message_id: str = Field(min_length=1)
    stable_entry_semantic_fingerprint: Fingerprint
    source_semantic_fingerprint: Fingerprint


@_fact(
    "derived_tool_result_message_semantic_source.v1",
    "source_semantic_fingerprint",
    "derived-tool-result-message-semantic-source:v1",
)
class DerivedToolResultMessageSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["derived_tool_result_message_semantic_source.v1"] = (
        "derived_tool_result_message_semantic_source.v1"
    )
    source_kind: Literal["derived_tool_result_message"] = (
        "derived_tool_result_message"
    )
    tool_result_leaf_semantic_fingerprint: Fingerprint
    tool_pair_semantic_fingerprint: Fingerprint
    terminal_projection_semantic_fingerprint: Fingerprint
    source_semantic_fingerprint: Fingerprint


@_fact(
    "provider_compaction_rewrite_authority_reference.v1",
    "reference_fingerprint",
    "provider-compaction-rewrite-authority-reference:v1",
)
class ProviderCompactionRewriteAuthorityReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "provider_compaction_rewrite_authority_reference.v1"
    ] = "provider_compaction_rewrite_authority_reference.v1"
    compaction_completed_event_reference: ContextEventReferenceFact
    source_document_fingerprint: Fingerprint
    summary_semantic_fingerprint: Fingerprint
    replaced_first_stable_ordinal: int = Field(ge=0)
    replaced_last_stable_ordinal: int = Field(ge=0)
    replaced_member_count: PositiveInt
    replaced_member_semantic_accumulator: Fingerprint
    resulting_stable_transcript_semantic_fingerprint: Fingerprint
    rewrite_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "ProviderCompactionRewriteAuthorityReferenceFact":
        if self.replaced_first_stable_ordinal > self.replaced_last_stable_ordinal:
            raise ValueError("compaction rewrite stable range is reversed")
        if (
            self.replaced_last_stable_ordinal
            - self.replaced_first_stable_ordinal
            + 1
            != self.replaced_member_count
        ):
            raise ValueError("compaction rewrite range/member count mismatch")
        return self


@_fact(
    "compaction_replacement_summary_semantic_source.v1",
    "source_semantic_fingerprint",
    "compaction-replacement-summary-semantic-source:v1",
)
class CompactionReplacementSummarySemanticSourceFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_replacement_summary_semantic_source.v1"
    ] = "compaction_replacement_summary_semantic_source.v1"
    source_kind: Literal["compaction_replacement_summary"] = (
        "compaction_replacement_summary"
    )
    summary_semantic_fingerprint: Fingerprint
    replaced_source_range_fingerprint: Fingerprint
    resulting_stable_transcript_semantic_fingerprint: Fingerprint
    rewrite_contract_fingerprint: Fingerprint
    source_semantic_fingerprint: Fingerprint


@_fact(
    "lifecycle_note_semantic_source.v1",
    "source_semantic_fingerprint",
    "lifecycle-note-semantic-source:v1",
)
class LifecycleNoteSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["lifecycle_note_semantic_source.v1"] = (
        "lifecycle_note_semantic_source.v1"
    )
    source_kind: Literal["lifecycle_note"] = "lifecycle_note"
    note_semantic_fingerprint: Fingerprint
    cause_semantic_fingerprint: Fingerprint
    lifecycle_note_contract_fingerprint: Fingerprint
    source_semantic_fingerprint: Fingerprint


ProviderTranscriptUnitSemanticSourceFact: TypeAlias = Annotated[
    DirectStableMessageSemanticSourceFact
    | DerivedToolResultMessageSemanticSourceFact
    | CompactionReplacementSummarySemanticSourceFact
    | LifecycleNoteSemanticSourceFact,
    Field(discriminator="source_kind"),
]


@_fact(
    "direct_stable_message_source_attribution.v1",
    "fact_fingerprint",
    "direct-stable-message-source-attribution:v1",
)
class DirectStableMessageSourceAttributionFact(FrozenFactBase):
    schema_version: Literal["direct_stable_message_source_attribution.v1"] = (
        "direct_stable_message_source_attribution.v1"
    )
    source_kind: Literal["direct_stable_message"] = "direct_stable_message"
    stable_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    source_semantic_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint


@_fact(
    "derived_tool_result_message_source_attribution.v1",
    "fact_fingerprint",
    "derived-tool-result-message-source-attribution:v1",
)
class DerivedToolResultMessageSourceAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "derived_tool_result_message_source_attribution.v1"
    ] = "derived_tool_result_message_source_attribution.v1"
    source_kind: Literal["derived_tool_result_message"] = (
        "derived_tool_result_message"
    )
    tool_result_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    tool_pair_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    terminal_projection_reference: TerminalProjectionReferenceFact
    source_semantic_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _entry_kinds(self) -> "DerivedToolResultMessageSourceAttributionFact":
        if self.tool_result_leaf_reference.entry_kind != "tool_result_projection_ref":
            raise ValueError("tool-result source requires result projection leaf")
        if self.tool_pair_leaf_reference.entry_kind != "tool_pair":
            raise ValueError("tool-result source requires pair companion leaf")
        return self


@_fact(
    "compaction_replacement_summary_source_attribution.v1",
    "fact_fingerprint",
    "compaction-replacement-summary-source-attribution:v1",
)
class CompactionReplacementSummarySourceAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_replacement_summary_source_attribution.v1"
    ] = "compaction_replacement_summary_source_attribution.v1"
    source_kind: Literal["compaction_replacement_summary"] = (
        "compaction_replacement_summary"
    )
    summary_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    rewrite_authority_reference: ProviderCompactionRewriteAuthorityReferenceFact
    source_semantic_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint


@_fact(
    "lifecycle_note_source_attribution.v1",
    "fact_fingerprint",
    "lifecycle-note-source-attribution:v1",
)
class LifecycleNoteSourceAttributionFact(FrozenFactBase):
    schema_version: Literal["lifecycle_note_source_attribution.v1"] = (
        "lifecycle_note_source_attribution.v1"
    )
    source_kind: Literal["lifecycle_note"] = "lifecycle_note"
    note_leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    note_event_reference: ContextEventReferenceFact
    cause_event_reference: ContextEventReferenceFact
    source_semantic_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint


ProviderTranscriptUnitSourceAttributionFact: TypeAlias = Annotated[
    DirectStableMessageSourceAttributionFact
    | DerivedToolResultMessageSourceAttributionFact
    | CompactionReplacementSummarySourceAttributionFact
    | LifecycleNoteSourceAttributionFact,
    Field(discriminator="source_kind"),
]


@_fact(
    "provider_transcript_source_selection_rule.v1",
    "rule_fingerprint",
    "provider-transcript-source-selection-rule:v1",
)
class ProviderTranscriptSourceSelectionRuleFact(FrozenFactBase):
    schema_version: Literal[
        "provider_transcript_source_selection_rule.v1"
    ] = "provider_transcript_source_selection_rule.v1"
    canonical_entry_kind: Literal[
        "message", "tool_pair", "tool_result_projection_ref"
    ]
    eligible_message_segments: tuple[
        Literal[
            "compaction_summary",
            "prior_history",
            "current_user",
            "current_run_tail",
            "recovery_note",
            "terminal_lifecycle_note",
        ],
        ...,
    ]
    selection_outcome: Literal["emit_provider_unit", "companion_only"]
    selected_source_kind: Literal[
        "direct_stable_message",
        "derived_tool_result_message",
        "compaction_replacement_summary",
        "lifecycle_note",
    ] | None
    required_companion_entry_kinds: tuple[
        Literal["message", "tool_pair", "tool_result_projection_ref"], ...
    ]
    rule_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _outcome(self) -> "ProviderTranscriptSourceSelectionRuleFact":
        emitted = self.selection_outcome == "emit_provider_unit"
        if emitted != (self.selected_source_kind is not None):
            raise ValueError("source-selection outcome/kind matrix mismatch")
        if self.canonical_entry_kind == "tool_pair":
            if emitted or self.eligible_message_segments:
                raise ValueError("tool-pair leaf must be companion-only")
        return self


@_fact(
    "provider_transcript_source_selection_contract.v1",
    "contract_fingerprint",
    "provider-transcript-source-selection-contract:v1",
)
class ProviderTranscriptSourceSelectionContractFact(FrozenFactBase):
    schema_version: Literal[
        "provider_transcript_source_selection_contract.v1"
    ] = "provider_transcript_source_selection_contract.v1"
    contract_id: Literal["pulsara.provider-transcript-source-selection"] = (
        "pulsara.provider-transcript-source-selection"
    )
    contract_version: Literal["1"] = "1"
    rules: tuple[ProviderTranscriptSourceSelectionRuleFact, ...]
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _rules(self) -> "ProviderTranscriptSourceSelectionContractFact":
        domains = tuple(
            (item.canonical_entry_kind, segment)
            for item in self.rules
            for segment in (item.eligible_message_segments or (None,))
        )
        if len(domains) != len(set(domains)):
            raise ValueError("provider transcript source-selection rules overlap")
        return self


@_fact(
    "provider_transcript_node_identity.v1",
    "node_identity_fingerprint",
    "provider-transcript-node-identity:v1",
)
class ProviderTranscriptNodeIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_transcript_node_identity.v1"] = (
        "provider_transcript_node_identity.v1"
    )
    source_identity_fingerprint: Fingerprint
    wire_semantic_fingerprint: Fingerprint
    node_identity_fingerprint: Fingerprint


@_fact(
    "provider_projection_position.v1",
    "position_fingerprint",
    "provider-projection-position:v1",
)
class ProviderProjectionPositionFact(FrozenFactBase):
    schema_version: Literal["provider_projection_position.v1"] = (
        "provider_projection_position.v1"
    )
    projection_index: int = Field(ge=0)
    predecessor_node_identity_fingerprint: Fingerprint | None
    position_contract_fingerprint: Fingerprint
    position_fingerprint: Fingerprint


@_fact(
    "provider_causal_placement_semantic.v1",
    "causal_semantic_fingerprint",
    "provider-causal-placement-semantic:v1",
)
class ProviderCausalPlacementSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_causal_placement_semantic.v1"] = (
        "provider_causal_placement_semantic.v1"
    )
    source: ProviderTranscriptUnitSemanticSourceFact
    node_identity: ProviderTranscriptNodeIdentityFact
    position: ProviderProjectionPositionFact
    visible_causal_predecessor_node_identity_fingerprints: tuple[
        Fingerprint, ...
    ]
    causal_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _identity(self) -> "ProviderCausalPlacementSemanticFact":
        if (
            self.source.source_semantic_fingerprint
            != self.node_identity.source_identity_fingerprint
        ):
            raise ValueError("causal source/node identity mismatch")
        if len(self.visible_causal_predecessor_node_identity_fingerprints) != len(
            set(self.visible_causal_predecessor_node_identity_fingerprints)
        ):
            raise ValueError("causal predecessor identities must be unique")
        return self


@_fact(
    "provider_invocation_classification_attribution.v1",
    "fact_fingerprint",
    "provider-invocation-classification-attribution:v1",
)
class ProviderInvocationClassificationAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "provider_invocation_classification_attribution.v1"
    ] = "provider_invocation_classification_attribution.v1"
    invocation_classification: Literal[
        "prior_history",
        "current_user",
        "current_run_tail",
        "compaction_summary",
        "lifecycle_note",
    ]
    compile_context_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    fact_fingerprint: Fingerprint


@_fact(
    "provider_ordered_transcript_unit.v2",
    "fact_fingerprint",
    "provider-ordered-transcript-unit:v2",
)
class ProviderOrderedTranscriptUnitFact(FrozenFactBase):
    schema_version: Literal["provider_ordered_transcript_unit.v2"] = (
        "provider_ordered_transcript_unit.v2"
    )
    wire_semantic: ProviderWireMessageSemanticFact
    causal_placement: ProviderCausalPlacementSemanticFact
    source_attribution: ProviderTranscriptUnitSourceAttributionFact
    invocation_attribution: ProviderInvocationClassificationAttributionFact
    unit_causal_semantic_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "ProviderOrderedTranscriptUnitFact":
        source = self.causal_placement.source
        if (
            self.wire_semantic.wire_semantic_fingerprint
            != self.causal_placement.node_identity.wire_semantic_fingerprint
            or source.source_kind != self.source_attribution.source_kind
            or source.source_semantic_fingerprint
            != self.source_attribution.source_semantic_fingerprint
        ):
            raise ValueError("ordered transcript unit identity mismatch")
        expected = context_fingerprint(
            "provider-ordered-transcript-unit-causal-semantic:v2",
            (
                self.wire_semantic.wire_semantic_fingerprint,
                self.causal_placement.causal_semantic_fingerprint,
            ),
        )
        if self.unit_causal_semantic_fingerprint != expected:
            raise ValueError("ordered transcript unit causal fingerprint mismatch")
        return self


@_fact(
    "provider_ordered_transcript_projection.v2",
    "fact_fingerprint",
    "provider-ordered-transcript-projection:v2",
)
class ProviderOrderedTranscriptProjectionFact(FrozenFactBase):
    schema_version: Literal["provider_ordered_transcript_projection.v2"] = (
        "provider_ordered_transcript_projection.v2"
    )
    rendering_contract_fingerprint: Fingerprint
    source_selection_contract_fingerprint: Fingerprint
    resolved_causal_physical_policy_fingerprint: Fingerprint
    stable_transcript_semantic_fingerprint: Fingerprint
    ordered_units: tuple[ProviderOrderedTranscriptUnitFact, ...]
    ordered_wire_semantic_accumulator: Fingerprint
    ordered_causal_semantic_accumulator: Fingerprint
    causal_order_proof_fingerprint: Fingerprint
    projection_semantic_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _order(self) -> "ProviderOrderedTranscriptProjectionFact":
        identities = tuple(
            item.causal_placement.node_identity.node_identity_fingerprint
            for item in self.ordered_units
        )
        if len(identities) != len(set(identities)):
            raise ValueError("provider transcript node identities are not unique")
        seen: set[str] = set()
        for index, item in enumerate(self.ordered_units):
            position = item.causal_placement.position
            predecessor = identities[index - 1] if index else None
            if (
                position.projection_index != index
                or position.predecessor_node_identity_fingerprint != predecessor
            ):
                raise ValueError("provider projection position is not contiguous")
            if any(
                value not in seen
                for value in item.causal_placement.visible_causal_predecessor_node_identity_fingerprints
            ):
                raise ValueError("provider causal predecessor does not precede unit")
            seen.add(identities[index])
        wire_values = tuple(
            item.wire_semantic.wire_semantic_fingerprint
            for item in self.ordered_units
        )
        causal_values = tuple(
            item.unit_causal_semantic_fingerprint for item in self.ordered_units
        )
        if self.ordered_wire_semantic_accumulator != _ordered_accumulator(
            "provider-ordered-transcript-wire:v2", wire_values
        ):
            raise ValueError("provider transcript wire accumulator mismatch")
        if self.ordered_causal_semantic_accumulator != _ordered_accumulator(
            "provider-ordered-transcript-causal:v2", causal_values
        ):
            raise ValueError("provider transcript causal accumulator mismatch")
        expected_proof = context_fingerprint(
            "provider-ordered-transcript-causal-order-proof:v2",
            tuple(
                (
                    item.causal_placement.node_identity.node_identity_fingerprint,
                    item.causal_placement.position.position_fingerprint,
                    item.causal_placement.visible_causal_predecessor_node_identity_fingerprints,
                )
                for item in self.ordered_units
            ),
        )
        if self.causal_order_proof_fingerprint != expected_proof:
            raise ValueError("provider transcript causal order proof mismatch")
        expected_semantic = context_fingerprint(
            "provider-ordered-transcript-projection-semantic:v2",
            {
                "rendering_contract_fingerprint": self.rendering_contract_fingerprint,
                "source_selection_contract_fingerprint": (
                    self.source_selection_contract_fingerprint
                ),
                "resolved_causal_physical_policy_fingerprint": (
                    self.resolved_causal_physical_policy_fingerprint
                ),
                "stable_transcript_semantic_fingerprint": (
                    self.stable_transcript_semantic_fingerprint
                ),
                "unit_count": len(self.ordered_units),
                "ordered_wire_semantic_accumulator": (
                    self.ordered_wire_semantic_accumulator
                ),
                "ordered_causal_semantic_accumulator": (
                    self.ordered_causal_semantic_accumulator
                ),
                "causal_order_proof_fingerprint": self.causal_order_proof_fingerprint,
            },
        )
        if self.projection_semantic_fingerprint != expected_semantic:
            raise ValueError("provider transcript projection semantic mismatch")
        return self


@_fact(
    "provider_ordered_transcript_projection_identity.v1",
    "identity_fingerprint",
    "provider-ordered-transcript-projection-identity:v1",
)
class ProviderOrderedTranscriptProjectionIdentityFact(FrozenFactBase):
    schema_version: Literal[
        "provider_ordered_transcript_projection_identity.v1"
    ] = "provider_ordered_transcript_projection_identity.v1"
    projection_semantic_fingerprint: Fingerprint
    unit_count: int = Field(ge=0)
    ordered_wire_semantic_accumulator: Fingerprint
    ordered_causal_semantic_accumulator: Fingerprint
    identity_fingerprint: Fingerprint


@_fact(
    "context_input_manifest_projection_reference.v1",
    "reference_fingerprint",
    "context-input-manifest-projection-reference:v1",
)
class ContextInputManifestProjectionReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "context_input_manifest_projection_reference.v1"
    ] = "context_input_manifest_projection_reference.v1"
    context_id: str = Field(min_length=1)
    input_manifest_artifact_id: str = Field(min_length=1)
    input_manifest_content_fingerprint: Fingerprint
    input_manifest_fact_fingerprint: Fingerprint
    projection_identity: ProviderOrderedTranscriptProjectionIdentityFact
    reference_fingerprint: Fingerprint


@_fact(
    "provider_input_unit_semantic.v2",
    "semantic_fingerprint",
    "provider-input-unit-semantic:v2",
)
class ProviderInputUnitSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_semantic.v2"] = (
        "provider_input_unit_semantic.v2"
    )
    unit_kind: Literal[
        "transcript_message",
        "tool_pair",
        "context_source",
        "runtime_clock",
        "tool_catalog",
        "rollup_observation",
        "recovery_observation",
    ]
    provider_content_semantic_fingerprint: Fingerprint
    lowering_contract_id: str = Field(min_length=1)
    lowering_contract_version: str = Field(min_length=1)
    lowering_contract_fingerprint: Fingerprint
    pairing_group_id: str | None
    semantic_fingerprint: Fingerprint


@_fact(
    "provider_input_unit_attribution.v1",
    "fact_fingerprint",
    "provider-input-unit-attribution:v1",
)
class ProviderInputUnitAttributionFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_attribution.v1"] = (
        "provider_input_unit_attribution.v1"
    )
    semantic: ProviderInputUnitSemanticFact
    owner_semantic_fingerprint: Fingerprint
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...]
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _authority(self) -> "ProviderInputUnitAttributionFact":
        owners = tuple(item.runtime_session_id for item in self.authority_horizons)
        if owners != tuple(sorted(set(owners))):
            raise ValueError("provider unit horizons are not ordered/unique")
        by_owner = {item.runtime_session_id: item for item in self.authority_horizons}
        for ref in self.source_event_refs:
            horizon = by_owner.get(ref.runtime_session_id)
            if horizon is None or ref.sequence > horizon.through_sequence:
                raise ValueError("provider unit source ref exceeds authority horizon")
        return self


@_fact(
    "provider_input_unit_materialization.v1",
    "materialization_fingerprint",
    "provider-input-unit-materialization:v1",
)
class ProviderInputUnitMaterializationFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_materialization.v1"] = (
        "provider_input_unit_materialization.v1"
    )
    attribution: ProviderInputUnitAttributionFact
    canonical_provider_fragment: ProviderInputTypedFragmentFact
    estimated_tokens: int = Field(ge=0)
    materialization_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _semantic(self) -> "ProviderInputUnitMaterializationFact":
        if (
            self.attribution.semantic.provider_content_semantic_fingerprint
            != self.canonical_provider_fragment.semantic_fingerprint
        ):
            raise ValueError("provider unit fragment semantic mismatch")
        return self


@_fact(
    "provider_input_unit_vector_node_reference.v1",
    "reference_fingerprint",
    "provider-input-unit-vector-node-reference:v1",
)
class ProviderInputUnitVectorNodeReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_vector_node_reference.v1"] = (
        "provider_input_unit_vector_node_reference.v1"
    )
    node_kind: Literal["leaf", "internal"]
    height: int = Field(ge=1, le=8)
    first_ordinal: int = Field(ge=0)
    last_ordinal: int = Field(ge=0)
    subtree_unit_count: int = Field(gt=0)
    subtree_accumulator: Fingerprint
    artifact_reference: ContextArtifactReferenceFact
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "ProviderInputUnitVectorNodeReferenceFact":
        if self.last_ordinal - self.first_ordinal + 1 != self.subtree_unit_count:
            raise ValueError("provider vector node ordinal/count mismatch")
        if (self.node_kind == "leaf") != (self.height == 1):
            raise ValueError("provider vector node kind/height mismatch")
        return self


@_fact(
    "provider_input_unit_vector_root_reference.v1",
    "reference_fingerprint",
    "provider-input-unit-vector-root-reference:v1",
)
class ProviderInputUnitVectorRootReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_unit_vector_root_reference.v1"] = (
        "provider_input_unit_vector_root_reference.v1"
    )
    unit_count: int = Field(ge=0)
    tree_height: int = Field(ge=0, le=8)
    root_node_ref: ProviderInputUnitVectorNodeReferenceFact | None
    ordered_unit_accumulator: Fingerprint
    vector_contract_fingerprint: Fingerprint
    vector_semantic_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _root(self) -> "ProviderInputUnitVectorRootReferenceFact":
        empty = self.unit_count == 0
        if empty != (self.root_node_ref is None) or empty != (self.tree_height == 0):
            raise ValueError("provider vector empty-root matrix mismatch")
        if self.root_node_ref is not None and (
            self.root_node_ref.subtree_unit_count != self.unit_count
            or self.root_node_ref.height != self.tree_height
            or self.root_node_ref.subtree_accumulator != self.ordered_unit_accumulator
        ):
            raise ValueError("provider vector root/node identity mismatch")
        return self


@_fact(
    "provider_input_generation_root_semantic.v1",
    "root_semantic_fingerprint",
    "provider-input-generation-root-semantic:v1",
)
class ProviderInputGenerationRootSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_root_semantic.v1"] = (
        "provider_input_generation_root_semantic.v1"
    )
    root_unit_count: int = Field(ge=0)
    root_ordered_unit_accumulator: Fingerprint
    root_unit_vector_semantic_fingerprint: Fingerprint
    root_lowering_contract_fingerprint: Fingerprint
    tool_catalog_root_semantic_fingerprint: Fingerprint
    root_semantic_fingerprint: Fingerprint


@_fact(
    "provider_input_generation_root_reference.v1",
    "reference_fingerprint",
    "provider-input-generation-root-reference:v1",
)
class ProviderInputGenerationRootReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_root_reference.v1"] = (
        "provider_input_generation_root_reference.v1"
    )
    generation: ProviderInputGenerationFact
    root_semantic: ProviderInputGenerationRootSemanticFact
    tool_catalog_root: CapabilityToolCatalogRootFact
    initial_unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    root_artifact_reference: ContextArtifactReferenceFact
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _vector(self) -> "ProviderInputGenerationRootReferenceFact":
        vector = self.initial_unit_vector_root
        semantic = self.root_semantic
        if (
            semantic.root_unit_count != vector.unit_count
            or semantic.root_ordered_unit_accumulator != vector.ordered_unit_accumulator
            or semantic.root_unit_vector_semantic_fingerprint
            != vector.vector_semantic_fingerprint
        ):
            raise ValueError("provider root semantic/vector mismatch")
        return self


@_fact(
    "provider_input_append_semantic.v1",
    "semantic_fingerprint",
    "provider-input-append-semantic:v1",
)
class ProviderInputAppendSemanticFact(FrozenFactBase):
    schema_version: Literal["provider_input_append_semantic.v1"] = (
        "provider_input_append_semantic.v1"
    )
    ordered_unit_semantic_fingerprints: tuple[Fingerprint, ...]
    append_ordering_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _nonempty(self) -> "ProviderInputAppendSemanticFact":
        if not self.ordered_unit_semantic_fingerprints:
            raise ValueError("provider append must contain at least one unit")
        if len(self.ordered_unit_semantic_fingerprints) > 512:
            raise ValueError("provider append exceeds 512-unit hard bound")
        return self


@_fact(
    "provider_input_append_batch_reference.v1",
    "reference_fingerprint",
    "provider-input-append-batch-reference:v1",
)
class ProviderInputAppendBatchReferenceFact(FrozenFactBase):
    schema_version: Literal["provider_input_append_batch_reference.v1"] = (
        "provider_input_append_batch_reference.v1"
    )
    generation: ProviderInputGenerationFact
    expected_generation_revision: int = Field(ge=0)
    append_index: int = Field(ge=1)
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    append_semantic: ProviderInputAppendSemanticFact
    batch_artifact_reference: ContextArtifactReferenceFact
    changed_vector_node_refs: tuple[ProviderInputUnitVectorNodeReferenceFact, ...]
    resulting_unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    resulting_authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    new_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...]
    resulting_replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    predecessor_prefix_fingerprint: Fingerprint
    resulting_prefix_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _revision(self) -> "ProviderInputAppendBatchReferenceFact":
        if self.append_index != self.expected_generation_revision + 1:
            raise ValueError("provider append index/revision mismatch")
        if len(self.changed_vector_node_refs) > 41:
            raise ValueError("provider append changed-node hard bound exceeded")
        binding_fingerprints = tuple(
            item.identity_fingerprint for item in self.new_replay_bindings
        )
        if binding_fingerprints != tuple(sorted(set(binding_fingerprints))):
            raise ValueError("provider append replay bindings must be sorted and unique")
        return self


@_fact(
    "provider_input_semantic_identity.v1",
    "semantic_fingerprint",
    "provider-input-semantic-identity:v1",
)
class ProviderInputSemanticIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_input_semantic_identity.v1"] = (
        "provider_input_semantic_identity.v1"
    )
    input_unit_count: int = Field(ge=0)
    ordered_unit_accumulator: Fingerprint
    unit_vector_semantic_fingerprint: Fingerprint
    system_instruction_fingerprint: Fingerprint
    tool_catalog_fingerprint: Fingerprint
    provider_message_sequence_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


@_fact(
    "canonical_provider_input_plan.v1",
    "plan_fingerprint",
    "canonical-provider-input-plan:v1",
)
class CanonicalProviderInputPlanFact(FrozenFactBase):
    schema_version: Literal["canonical_provider_input_plan.v1"] = (
        "canonical_provider_input_plan.v1"
    )
    resolved_model_call_fact: ResolvedModelCallFact
    generation_root_reference: ProviderInputGenerationRootReferenceFact
    resulting_prefix_fingerprint: Fingerprint
    resulting_generation_revision: int = Field(ge=1)
    unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    provider_input_semantic_identity: ProviderInputSemanticIdentityFact
    plan_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _vector(self) -> "CanonicalProviderInputPlanFact":
        identity = self.provider_input_semantic_identity
        vector = self.unit_vector_root
        if (
            identity.input_unit_count != vector.unit_count
            or identity.ordered_unit_accumulator != vector.ordered_unit_accumulator
            or identity.unit_vector_semantic_fingerprint
            != vector.vector_semantic_fingerprint
        ):
            raise ValueError("provider plan semantic/vector mismatch")
        return self


@_fact(
    "provider_transcript_frontier.v2",
    "provider_semantic_frontier_fingerprint",
    "provider-transcript-frontier:v2",
)
class ProviderTranscriptFrontierFact(FrozenFactBase):
    schema_version: Literal["provider_transcript_frontier.v2"] = (
        "provider_transcript_frontier.v2"
    )
    committed_transcript_unit_count: int = Field(ge=0)
    committed_ordered_wire_semantic_accumulator: Fingerprint
    committed_ordered_causal_semantic_accumulator: Fingerprint
    stable_transcript_prefix_fingerprint: Fingerprint
    provider_semantic_frontier_fingerprint: Fingerprint


@_fact(
    "provider_invocation_context_frame_semantic.v1",
    "frame_semantic_fingerprint",
    "provider-invocation-context-frame-semantic:v1",
)
class ProviderInvocationContextFrameSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "provider_invocation_context_frame_semantic.v1"
    ] = "provider_invocation_context_frame_semantic.v1"
    ordered_source_unit_wire_fingerprints: tuple[Fingerprint, ...] = Field(
        min_length=1
    )
    source_head_set_fingerprint: Fingerprint
    frame_semantic_fingerprint: Fingerprint


@_fact(
    "provider_invocation_context_frame_placement.v1",
    "frame_fact_fingerprint",
    "provider-invocation-context-frame-placement:v1",
)
class ProviderInvocationContextFramePlacementFact(FrozenFactBase):
    schema_version: Literal[
        "provider_invocation_context_frame_placement.v1"
    ] = "provider_invocation_context_frame_placement.v1"
    semantic: ProviderInvocationContextFrameSemanticFact
    insertion_kind: Literal[
        "before_new_current_user", "after_new_transcript_tail"
    ]
    preceding_transcript_node_identity_fingerprint: Fingerprint | None
    following_transcript_node_identity_fingerprint: Fingerprint | None
    insertion_policy_id: str = Field(min_length=1)
    insertion_policy_version: str = Field(min_length=1)
    insertion_policy_fingerprint: Fingerprint
    frame_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    resolved_model_call_id: str = Field(min_length=1)
    model_call_index: int = Field(ge=1)
    first_vector_ordinal: int = Field(ge=0)
    last_vector_ordinal: int = Field(ge=0)
    ordered_source_unit_range_accumulator: Fingerprint
    frame_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "ProviderInvocationContextFramePlacementFact":
        if self.first_vector_ordinal > self.last_vector_ordinal:
            raise ValueError("provider context-frame vector range is reversed")
        if (
            self.last_vector_ordinal - self.first_vector_ordinal + 1
            != len(self.semantic.ordered_source_unit_wire_fingerprints)
        ):
            raise ValueError("provider context-frame range/count mismatch")
        expected = _ordered_accumulator(
            "provider-invocation-context-frame-wire:v1",
            self.semantic.ordered_source_unit_wire_fingerprints,
        )
        if self.ordered_source_unit_range_accumulator != expected:
            raise ValueError("provider context-frame range accumulator mismatch")
        return self


@_fact(
    "provider_accepted_continuation_projection_join.v1",
    "fact_fingerprint",
    "provider-accepted-continuation-projection-join:v1",
)
class ProviderAcceptedContinuationProjectionJoinFact(FrozenFactBase):
    schema_version: Literal[
        "provider_accepted_continuation_projection_join.v1"
    ] = "provider_accepted_continuation_projection_join.v1"
    resolved_model_call_id: str = Field(min_length=1)
    reply_id: str = Field(min_length=1)
    terminal_projection_reference: TerminalProjectionReferenceFact
    accepted_disposition_event_reference: ContextEventReferenceFact
    ordered_projection_identity_fingerprint: Fingerprint
    matched_projection_index: int = Field(ge=0)
    matched_unit_causal_semantic_fingerprint: Fingerprint
    continuation_join_contract_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint


@_fact(
    "provider_input_causal_validation_result.v2",
    "result_fingerprint",
    "provider-input-causal-validation-result:v2",
)
class ProviderInputCausalValidationResult(FrozenFactBase):
    schema_version: Literal["provider_input_causal_validation_result.v2"] = (
        "provider_input_causal_validation_result.v2"
    )
    status: Literal["valid", "invalid"]
    projection_identity_fingerprint: Fingerprint
    checked_visible_edge_count: int = Field(ge=0)
    violation_reason: ProviderInputCausalValidationFailureReason | None
    violating_projection_indices: tuple[int, ...]
    validation_contract_fingerprint: Fingerprint
    resolved_causal_physical_policy_fingerprint: Fingerprint
    result_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _status(self) -> "ProviderInputCausalValidationResult":
        valid = self.status == "valid"
        if valid != (
            self.violation_reason is None and not self.violating_projection_indices
        ):
            raise ValueError("provider causal-validation status matrix mismatch")
        if self.violating_projection_indices != tuple(
            sorted(set(self.violating_projection_indices))
        ):
            raise ValueError("causal violation indices must be sorted and unique")
        return self


@_fact(
    "provider_transcript_delta_commit_proof.v1",
    "proof_fingerprint",
    "provider-transcript-delta-commit-proof:v1",
)
class ProviderTranscriptDeltaCommitProofFact(FrozenFactBase):
    schema_version: Literal["provider_transcript_delta_commit_proof.v1"] = (
        "provider_transcript_delta_commit_proof.v1"
    )
    projection_identity_fingerprint: Fingerprint
    predecessor_frontier_fingerprint: Fingerprint
    delta_first_projection_index: int | None = Field(default=None, ge=0)
    delta_last_projection_index: int | None = Field(default=None, ge=0)
    ordered_delta_wire_accumulator: Fingerprint
    ordered_delta_causal_accumulator: Fingerprint
    continuation_joins: tuple[ProviderAcceptedContinuationProjectionJoinFact, ...]
    resulting_frontier: ProviderTranscriptFrontierFact
    resolved_causal_physical_policy_fingerprint: Fingerprint
    proof_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "ProviderTranscriptDeltaCommitProofFact":
        first = self.delta_first_projection_index
        last = self.delta_last_projection_index
        if (first is None) != (last is None):
            raise ValueError("provider transcript delta range must be all-or-none")
        if first is not None:
            assert last is not None
            if first > last:
                raise ValueError("provider transcript delta range is reversed")
            if first != (
                self.resulting_frontier.committed_transcript_unit_count
                - (last - first + 1)
            ):
                raise ValueError("provider transcript delta/frontier count mismatch")
        joins = tuple(item.matched_projection_index for item in self.continuation_joins)
        if joins != tuple(sorted(set(joins))):
            raise ValueError("continuation joins must be projection ordered/unique")
        if first is None and self.continuation_joins:
            raise ValueError("empty transcript delta cannot consume continuation")
        return self


@_fact(
    "resolved_provider_input_causal_physical_policy.v1",
    "policy_fingerprint",
    "resolved-provider-input-causal-physical-policy:v1",
)
class ResolvedProviderInputCausalAndPhysicalPolicyFact(FrozenFactBase):
    schema_version: Literal[
        "resolved_provider_input_causal_physical_policy.v1"
    ] = "resolved_provider_input_causal_physical_policy.v1"
    max_parallel_tool_calls_per_model_call: PositiveInt
    max_non_tool_transcript_units_per_operation: PositiveInt
    max_visible_causal_predecessors_per_unit: PositiveInt
    max_projection_units_per_manifest: PositiveInt
    max_projection_canonical_bytes_per_manifest: PositiveInt
    max_generation_root_units: PositiveInt
    max_initial_generation_units: PositiveInt
    max_transcript_delta_units_per_append: PositiveInt
    max_context_frame_units_per_append: PositiveInt
    max_append_units: Literal[512] = 512
    max_append_candidate_canonical_bytes: PositiveInt
    allow_multi_append_before_model_start: Literal[False] = False
    provider_input_vector_contract_fingerprint: Fingerprint
    terminal_projection_contract_fingerprint: Fingerprint
    context_manifest_physical_policy_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounds(self) -> "ResolvedProviderInputCausalAndPhysicalPolicyFact":
        if self.max_visible_causal_predecessors_per_unit < (
            self.max_parallel_tool_calls_per_model_call + 2
        ):
            raise ValueError("provider causal predecessor bound is infeasible")
        if self.max_transcript_delta_units_per_append < (
            self.max_parallel_tool_calls_per_model_call
            + self.max_non_tool_transcript_units_per_operation
        ):
            raise ValueError("provider transcript delta bound is infeasible")
        if (
            self.max_transcript_delta_units_per_append
            + self.max_context_frame_units_per_append
            > self.max_append_units
        ):
            raise ValueError("provider append unit partition exceeds hard bound")
        if self.max_initial_generation_units < (
            self.max_generation_root_units
            + self.max_projection_units_per_manifest
            + self.max_context_frame_units_per_append
        ):
            raise ValueError("provider initial-generation bound is infeasible")
        return self


@_fact(
    "provider_system_root_change_authority.v1",
    "authority_fingerprint",
    "provider-system-root-change-authority:v1",
)
class ProviderSystemRootChangeAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_system_root_change_authority.v1"] = (
        "provider_system_root_change_authority.v1"
    )
    authority_kind: Literal["system_root_change"] = "system_root_change"
    predecessor_generation_id: str = Field(min_length=1)
    predecessor_core_state_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    previous_system_root_semantic_fingerprint: Fingerprint
    resulting_system_root_semantic_fingerprint: Fingerprint
    authority_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _changed(self) -> "ProviderSystemRootChangeAuthorityFact":
        if (
            self.previous_system_root_semantic_fingerprint
            == self.resulting_system_root_semantic_fingerprint
        ):
            raise ValueError("system-root rollover requires semantic change")
        return self


@_fact(
    "provider_tool_catalog_change_authority.v1",
    "authority_fingerprint",
    "provider-tool-catalog-change-authority:v1",
)
class ProviderToolCatalogChangeAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_tool_catalog_change_authority.v1"] = (
        "provider_tool_catalog_change_authority.v1"
    )
    authority_kind: Literal["tool_catalog_change"] = "tool_catalog_change"
    predecessor_generation_id: str = Field(min_length=1)
    predecessor_core_state_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    previous_tool_catalog_semantic_fingerprint: Fingerprint
    resulting_tool_catalog_semantic_fingerprint: Fingerprint
    authority_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _changed(self) -> "ProviderToolCatalogChangeAuthorityFact":
        if (
            self.previous_tool_catalog_semantic_fingerprint
            == self.resulting_tool_catalog_semantic_fingerprint
        ):
            raise ValueError("tool-catalog rollover requires semantic change")
        return self


@_fact(
    "provider_compatibility_change_authority.v1",
    "authority_fingerprint",
    "provider-compatibility-change-authority:v1",
)
class ProviderCompatibilityChangeAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_compatibility_change_authority.v1"] = (
        "provider_compatibility_change_authority.v1"
    )
    authority_kind: Literal["provider_compatibility_change"] = (
        "provider_compatibility_change"
    )
    predecessor_generation_id: str = Field(min_length=1)
    predecessor_core_state_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    previous_provider_visible_compatibility_fingerprint: Fingerprint
    resulting_provider_visible_compatibility_fingerprint: Fingerprint
    resolved_model_call_id: str = Field(min_length=1)
    authority_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _changed(self) -> "ProviderCompatibilityChangeAuthorityFact":
        if (
            self.previous_provider_visible_compatibility_fingerprint
            == self.resulting_provider_visible_compatibility_fingerprint
        ):
            raise ValueError("compatibility rollover requires semantic change")
        return self


@_fact(
    "provider_auxiliary_frame_rebase_authority.v1",
    "authority_fingerprint",
    "provider-auxiliary-frame-rebase-authority:v1",
)
class ProviderAuxiliaryFrameRebaseAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "provider_auxiliary_frame_rebase_authority.v1"
    ] = "provider_auxiliary_frame_rebase_authority.v1"
    authority_kind: Literal["auxiliary_frame_rebase"] = "auxiliary_frame_rebase"
    predecessor_generation_id: str = Field(min_length=1)
    predecessor_core_state_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    dropped_frame_fact_fingerprints: tuple[Fingerprint, ...] = Field(min_length=1)
    dropped_unit_range_fingerprints: tuple[Fingerprint, ...] = Field(min_length=1)
    dropped_unit_accumulator: Fingerprint
    previous_source_head_set_fingerprint: Fingerprint
    resulting_source_head_set_fingerprint: Fingerprint
    retained_transcript_unit_count: int = Field(ge=0)
    predecessor_transcript_frontier_fingerprint: Fingerprint
    resulting_retained_transcript_prefix_fingerprint: Fingerprint
    budget_decision_fingerprint: Fingerprint
    rebase_contract_fingerprint: Fingerprint
    authority_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ranges(self) -> "ProviderAuxiliaryFrameRebaseAuthorityFact":
        for values in (
            self.dropped_frame_fact_fingerprints,
            self.dropped_unit_range_fingerprints,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("auxiliary rebase ranges must be sorted and unique")
        if (
            self.predecessor_transcript_frontier_fingerprint
            != self.resulting_retained_transcript_prefix_fingerprint
        ):
            raise ValueError("auxiliary rebase changed the retained transcript prefix")
        return self


@_fact(
    "provider_long_horizon_rewrite_rollover_authority.v1",
    "authority_fingerprint",
    "provider-long-horizon-rewrite-rollover-authority:v1",
)
class ProviderLongHorizonRewriteRolloverAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "provider_long_horizon_rewrite_rollover_authority.v1"
    ] = "provider_long_horizon_rewrite_rollover_authority.v1"
    authority_kind: Literal["long_horizon_rewrite"] = "long_horizon_rewrite"
    predecessor_generation_id: str = Field(min_length=1)
    predecessor_core_state_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    rewrite_authority_reference: ProviderCompactionRewriteAuthorityReferenceFact
    resulting_transcript_projection_semantic_fingerprint: Fingerprint
    authority_fingerprint: Fingerprint


@_fact(
    "provider_offline_repair_rollover_authority.v1",
    "authority_fingerprint",
    "provider-offline-repair-rollover-authority:v1",
)
class ProviderOfflineRepairRolloverAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "provider_offline_repair_rollover_authority.v1"
    ] = "provider_offline_repair_rollover_authority.v1"
    authority_kind: Literal["offline_repair"] = "offline_repair"
    predecessor_generation_id: str = Field(min_length=1)
    predecessor_core_state_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    offline_repair_committed_event_reference: ContextEventReferenceFact
    offline_repair_artifact_reference: ContextArtifactReferenceFact
    repaired_generation_core_fingerprint: Fingerprint
    repair_contract_fingerprint: Fingerprint
    authority_fingerprint: Fingerprint


ProviderAdministrativeResetReasonCode: TypeAlias = Literal[
    "operator_requested", "database_epoch_reset", "test_fixture_reset"
]


@_fact(
    "provider_administrative_reset_authority.v1",
    "authority_fingerprint",
    "provider-administrative-reset-authority:v1",
)
class ProviderAdministrativeResetAuthorityFact(FrozenFactBase):
    schema_version: Literal["provider_administrative_reset_authority.v1"] = (
        "provider_administrative_reset_authority.v1"
    )
    authority_kind: Literal["administrative_reset"] = "administrative_reset"
    predecessor_generation_id: str = Field(min_length=1)
    predecessor_core_state_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    administrative_reset_event_reference: ContextEventReferenceFact
    reset_epoch: PositiveInt
    stable_reason_code: ProviderAdministrativeResetReasonCode
    reset_contract_fingerprint: Fingerprint
    authority_fingerprint: Fingerprint


ProviderInputRolloverAuthorityFact: TypeAlias = Annotated[
    ProviderSystemRootChangeAuthorityFact
    | ProviderToolCatalogChangeAuthorityFact
    | ProviderCompatibilityChangeAuthorityFact
    | ProviderAuxiliaryFrameRebaseAuthorityFact
    | ProviderLongHorizonRewriteRolloverAuthorityFact
    | ProviderOfflineRepairRolloverAuthorityFact
    | ProviderAdministrativeResetAuthorityFact,
    Field(discriminator="authority_kind"),
]


_ROLLOVER_AUTHORITY_KIND = {
    ProviderInputRolloverReason.SYSTEM_ROOT_SEMANTIC_CHANGED: "system_root_change",
    ProviderInputRolloverReason.TOOL_CATALOG_SEMANTIC_CHANGED: "tool_catalog_change",
    ProviderInputRolloverReason.PROVIDER_VISIBLE_COMPATIBILITY_CHANGED: (
        "provider_compatibility_change"
    ),
    ProviderInputRolloverReason.AUXILIARY_FRAME_REBASE: "auxiliary_frame_rebase",
    ProviderInputRolloverReason.EXPLICIT_LONG_HORIZON_REWRITE: (
        "long_horizon_rewrite"
    ),
    ProviderInputRolloverReason.CONFIRMED_OFFLINE_AUTHORITY_REPAIR: "offline_repair",
    ProviderInputRolloverReason.EXPLICIT_ADMINISTRATIVE_RESET: (
        "administrative_reset"
    ),
}


@_fact(
    "provider_input_rollover_intent.v1",
    "intent_fingerprint",
    "provider-input-rollover-intent:v1",
)
class ProviderInputRolloverIntentFact(FrozenFactBase):
    schema_version: Literal["provider_input_rollover_intent.v1"] = (
        "provider_input_rollover_intent.v1"
    )
    continuity_scope_fingerprint: Fingerprint
    predecessor_generation_id: str = Field(min_length=1)
    reason: ProviderInputRolloverReason
    authority: ProviderInputRolloverAuthorityFact
    authority_fingerprint: Fingerprint
    intent_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _authority(self) -> "ProviderInputRolloverIntentFact":
        expected_kind = _ROLLOVER_AUTHORITY_KIND.get(self.reason)
        if expected_kind is None or self.authority.authority_kind != expected_kind:
            raise ValueError("provider rollover reason/authority matrix mismatch")
        if (
            self.authority_fingerprint != self.authority.authority_fingerprint
            or self.predecessor_generation_id
            != self.authority.predecessor_generation_id
        ):
            raise ValueError("provider rollover intent authority join mismatch")
        return self


@_fact(
    "provider_input_rollover_request.v1",
    "request_fingerprint",
    "provider-input-rollover-request:v1",
)
class ProviderInputRolloverRequestFact(FrozenFactBase):
    schema_version: Literal["provider_input_rollover_request.v1"] = (
        "provider_input_rollover_request.v1"
    )
    rollover_request_id: str = Field(min_length=1)
    intent: ProviderInputRolloverIntentFact
    manifest_projection_reference: ContextInputManifestProjectionReferenceFact
    request_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _projection(self) -> "ProviderInputRolloverRequestFact":
        authority = self.intent.authority
        if (
            authority.ordered_projection_identity_fingerprint
            != self.manifest_projection_reference.projection_identity.identity_fingerprint
        ):
            raise ValueError("provider rollover request projection join mismatch")
        return self


@_fact(
    "provider_input_committed_source_head.v1",
    "head_fingerprint",
    "provider-input-committed-source-head:v1",
)
class ProviderInputCommittedSourceHeadFact(FrozenFactBase):
    schema_version: Literal["provider_input_committed_source_head.v1"] = (
        "provider_input_committed_source_head.v1"
    )
    source_id: ContextSourceId
    source_instance_id: str = Field(min_length=1)
    candidate_key: str = Field(min_length=1)
    canonical_source_revision: CanonicalContextSourceRevisionFact
    candidate_semantic_fingerprint: Fingerprint
    appended_unit_semantic_fingerprint: Fingerprint
    committed_append_index: int = Field(ge=0)
    head_fingerprint: Fingerprint


@_fact(
    "provider_input_clock_head.v1",
    "head_fingerprint",
    "provider-input-clock-head:v1",
)
class ProviderInputClockHeadFact(FrozenFactBase):
    schema_version: Literal["provider_input_clock_head.v1"] = (
        "provider_input_clock_head.v1"
    )
    observation_semantic_fingerprint: Fingerprint
    observed_at_utc: str
    committed_append_index: int = Field(ge=1)
    head_fingerprint: Fingerprint


@_fact(
    "provider_input_pending_continuation.v1",
    "continuation_fingerprint",
    "provider-input-pending-continuation:v1",
)
class ProviderInputPendingContinuationFact(FrozenFactBase):
    schema_version: Literal["provider_input_pending_continuation.v1"] = (
        "provider_input_pending_continuation.v1"
    )
    resolved_model_call_id: str = Field(min_length=1)
    terminal_projection_reference: TerminalProjectionReferenceFact
    accepted_disposition_event_ref: ContextEventReferenceFact
    continuation_semantic_fingerprint: Fingerprint
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    continuation_fingerprint: Fingerprint


@_fact(
    "provider_input_continuation_materialization_proof.v1",
    "proof_fingerprint",
    "provider-input-continuation-materialization-proof:v1",
)
class ProviderInputContinuationMaterializationProofFact(FrozenFactBase):
    schema_version: Literal["provider_input_continuation_materialization_proof.v1"] = (
        "provider_input_continuation_materialization_proof.v1"
    )
    pending_continuation_fingerprint: Fingerprint
    terminal_projection_reference: TerminalProjectionReferenceFact
    predecessor_transcript_frontier_fingerprint: Fingerprint
    resulting_transcript_frontier_fingerprint: Fingerprint
    appended_unit_ordinals: tuple[int, ...]
    ordered_appended_unit_semantic_fingerprints: tuple[Fingerprint, ...]
    ordered_appended_unit_materialization_fingerprints: tuple[Fingerprint, ...]
    ordered_appended_unit_owner_semantic_fingerprints: tuple[Fingerprint, ...]
    appended_unit_range_accumulator: Fingerprint
    proof_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "ProviderInputContinuationMaterializationProofFact":
        lengths = {
            len(self.appended_unit_ordinals),
            len(self.ordered_appended_unit_semantic_fingerprints),
            len(self.ordered_appended_unit_materialization_fingerprints),
            len(self.ordered_appended_unit_owner_semantic_fingerprints),
        }
        if (
            lengths != {len(self.appended_unit_ordinals)}
            or not self.appended_unit_ordinals
        ):
            raise ValueError("continuation materialization proof vectors differ")
        if self.appended_unit_ordinals != tuple(
            sorted(set(self.appended_unit_ordinals))
        ):
            raise ValueError("continuation materialization ordinals are not ordered")
        expected = context_fingerprint(
            "provider-input-continuation-unit-range:v1",
            tuple(
                zip(
                    self.appended_unit_ordinals,
                    self.ordered_appended_unit_semantic_fingerprints,
                    self.ordered_appended_unit_materialization_fingerprints,
                    self.ordered_appended_unit_owner_semantic_fingerprints,
                    strict=True,
                )
            ),
        )
        if self.appended_unit_range_accumulator != expected:
            raise ValueError("continuation materialization range accumulator mismatch")
        return self


@_fact(
    "provider_input_awaiting_control_disposition.v1",
    "awaiting_fingerprint",
    "provider-input-awaiting-control-disposition:v1",
)
class ProviderInputAwaitingControlDispositionFact(FrozenFactBase):
    schema_version: Literal["provider_input_awaiting_control_disposition.v1"] = (
        "provider_input_awaiting_control_disposition.v1"
    )
    resolved_model_call_id: str = Field(min_length=1)
    terminal_projection_reference: TerminalProjectionReferenceFact
    model_terminal_event_ref: ContextEventReferenceFact
    terminal_projection_committed_event_ref: ContextEventReferenceFact
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    awaiting_fingerprint: Fingerprint


@_fact(
    "committed_provider_input_generation_core_state.v1",
    "core_state_fingerprint",
    "committed-provider-input-generation-core-state:v1",
)
class CommittedProviderInputGenerationCoreStateFact(FrozenFactBase):
    schema_version: Literal["committed_provider_input_generation_core_state.v1"] = (
        "committed_provider_input_generation_core_state.v1"
    )
    generation: ProviderInputGenerationFact
    root_reference: ProviderInputGenerationRootReferenceFact
    status: Literal["open", "closing", "closed", "reconciliation_latched"]
    revision: int = Field(ge=0)
    next_append_index: int = Field(ge=1)
    committed_prefix_fingerprint: Fingerprint
    unit_count: int = Field(ge=0)
    unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    committed_authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    transcript_frontier: ProviderTranscriptFrontierFact
    committed_source_heads: tuple[ProviderInputCommittedSourceHeadFact, ...]
    clock_head: ProviderInputClockHeadFact | None
    awaiting_control_disposition: ProviderInputAwaitingControlDispositionFact | None
    accepted_but_not_appended_continuation: ProviderInputPendingContinuationFact | None
    reconciliation_reason: ProviderInputReconciliationReason | None
    core_state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _state(self) -> "CommittedProviderInputGenerationCoreStateFact":
        if self.next_append_index != self.revision + 1:
            raise ValueError("provider generation next append index mismatch")
        if self.unit_count != self.unit_vector_root.unit_count:
            raise ValueError("provider generation unit count/root mismatch")
        keys = tuple(
            (item.source_id.value, item.source_instance_id, item.candidate_key)
            for item in self.committed_source_heads
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("provider source heads are not ordered/unique")
        if any(
            item.committed_append_index > self.revision
            for item in self.committed_source_heads
        ):
            raise ValueError("provider source head is ahead of state revision")
        if (self.status == "reconciliation_latched") != (
            self.reconciliation_reason is not None
        ):
            raise ValueError("provider generation reconciliation matrix mismatch")
        if (
            self.awaiting_control_disposition is not None
            and self.accepted_but_not_appended_continuation is not None
        ):
            raise ValueError(
                "provider generation terminal states are mutually exclusive"
            )
        return self


@_fact(
    "provider_input_generation_attribution_state.v1",
    "attribution_fingerprint",
    "provider-input-generation-attribution-state:v1",
)
class ProviderInputGenerationAttributionStateFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_attribution_state.v1"] = (
        "provider_input_generation_attribution_state.v1"
    )
    core_state: CommittedProviderInputGenerationCoreStateFact
    latest_model_start_event_ref: ContextEventReferenceFact | None
    latest_model_start_committed_core_fingerprint: Fingerprint | None
    close_or_rollover_event_ref: ContextEventReferenceFact | None
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _start(self) -> "ProviderInputGenerationAttributionStateFact":
        if (self.latest_model_start_event_ref is None) != (
            self.latest_model_start_committed_core_fingerprint is None
        ):
            raise ValueError(
                "provider generation ModelStart attribution matrix mismatch"
            )
        return self


@_fact(
    "provider_input_preparation_ownership.v1",
    "ownership_fingerprint",
    "provider-input-preparation-ownership:v1",
)
class ProviderInputPreparationOwnershipFact(FrozenFactBase):
    schema_version: Literal["provider_input_preparation_ownership.v1"] = (
        "provider_input_preparation_ownership.v1"
    )
    preparation_id: str = Field(min_length=1)
    ownership_kind: Literal["initial_start", "existing_append", "rollover_start"]
    generation_id: str = Field(min_length=1)
    scope_fingerprint: Fingerprint
    expected_predecessor_scope_binding_fingerprint: Fingerprint
    resulting_scope_binding_fingerprint: Fingerprint
    expected_committed_core_state_fingerprint: Fingerprint | None
    expected_revision: int = Field(ge=0)
    append_batch_reference_fingerprint: Fingerprint
    provider_input_plan_fingerprint: Fingerprint
    resolved_model_call_id: str = Field(min_length=1)
    stable_companion_event_ids: tuple[str, ...]
    ownership_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _kind(self) -> "ProviderInputPreparationOwnershipFact":
        if self.ownership_kind == "initial_start":
            if self.expected_committed_core_state_fingerprint is not None:
                raise ValueError("initial provider preparation cannot expect a core")
            if self.expected_revision != 0:
                raise ValueError("initial provider preparation revision must be zero")
        elif self.expected_committed_core_state_fingerprint is None:
            raise ValueError("existing/rollover preparation requires predecessor core")
        return self


@_fact(
    "provider_input_preparation_ownership_attribution.v2",
    "attribution_fingerprint",
    "provider-input-preparation-ownership-attribution:v2",
)
class ProviderInputPreparationOwnershipAttributionFact(FrozenFactBase):
    schema_version: Literal["provider_input_preparation_ownership_attribution.v2"] = (
        "provider_input_preparation_ownership_attribution.v2"
    )
    ownership: ProviderInputPreparationOwnershipFact
    context_compiled_event_ref: ContextEventReferenceFact
    prepared_candidate_fingerprint: Fingerprint
    prepared_plan_fingerprint: Fingerprint
    manifest_projection_reference_fingerprint: Fingerprint
    rollover_request_fingerprint: Fingerprint | None
    attribution_fingerprint: Fingerprint


@_fact(
    "provider_input_generation_scope_binding.v1",
    "binding_fingerprint",
    "provider-input-generation-scope-binding:v1",
)
class ProviderInputGenerationScopeBindingFact(FrozenFactBase):
    schema_version: Literal["provider_input_generation_scope_binding.v1"] = (
        "provider_input_generation_scope_binding.v1"
    )
    scope_fingerprint: Fingerprint
    active_generation_id: str | None
    latest_closed_generation_id: str | None
    active_preparation_id: str | None
    binding_fingerprint: Fingerprint


@_fact(
    "provider_input_dispatch_barrier_identity.v1",
    "identity_fingerprint",
    "provider-input-dispatch-barrier-identity:v1",
)
class ProviderInputDispatchBarrierIdentityFact(FrozenFactBase):
    schema_version: Literal["provider_input_dispatch_barrier_identity.v1"] = (
        "provider_input_dispatch_barrier_identity.v1"
    )
    barrier_id: str = Field(min_length=1)
    scope_fingerprint: Fingerprint
    old_generation_id: str = Field(min_length=1)
    installed_at_core_revision: int = Field(ge=0)
    attempt_id: str = Field(min_length=1)
    identity_fingerprint: Fingerprint


@_fact(
    "initial_generation_commit_guard.v1",
    "guard_fingerprint",
    "initial-generation-commit-guard:v1",
)
class InitialGenerationCommitGuardFact(FrozenFactBase):
    schema_version: Literal["initial_generation_commit_guard.v1"] = (
        "initial_generation_commit_guard.v1"
    )
    guard_kind: Literal["initial_start"] = "initial_start"
    new_generation_id: str
    new_generation_fingerprint: Fingerprint
    new_root_reference_fingerprint: Fingerprint
    expected_scope_binding_fingerprint: Fingerprint
    expected_preparation_ownership_fingerprint: Fingerprint
    expected_authority_horizon_set_reference_fingerprint: Fingerprint
    expected_revision: Literal[0] = 0
    resolved_model_call_id: str
    guard_fingerprint: Fingerprint


@_fact(
    "existing_append_commit_guard.v1",
    "guard_fingerprint",
    "existing-append-commit-guard:v1",
)
class ExistingAppendCommitGuardFact(FrozenFactBase):
    schema_version: Literal["existing_append_commit_guard.v1"] = (
        "existing_append_commit_guard.v1"
    )
    guard_kind: Literal["existing_append"] = "existing_append"
    generation_id: str
    expected_committed_core_state_fingerprint: Fingerprint
    expected_preparation_ownership_fingerprint: Fingerprint
    expected_revision: int = Field(ge=1)
    expected_committed_prefix_fingerprint: Fingerprint
    expected_transcript_frontier_fingerprint: Fingerprint
    expected_awaiting_disposition_fingerprint: Fingerprint | None
    expected_pending_continuation_fingerprint: Fingerprint | None
    expected_scope_binding_fingerprint: Fingerprint
    resolved_model_call_id: str
    guard_fingerprint: Fingerprint


@_fact(
    "rollover_generation_commit_guard.v1",
    "guard_fingerprint",
    "rollover-generation-commit-guard:v1",
)
class RolloverGenerationCommitGuardFact(FrozenFactBase):
    schema_version: Literal["rollover_generation_commit_guard.v1"] = (
        "rollover_generation_commit_guard.v1"
    )
    guard_kind: Literal["rollover"] = "rollover"
    old_generation_id: str
    expected_old_core_state_fingerprint: Fingerprint
    expected_old_revision: int = Field(ge=1)
    expected_old_prefix_fingerprint: Fingerprint
    old_scope_fingerprint: Fingerprint
    expected_old_scope_binding_fingerprint: Fingerprint
    new_generation_id: str
    new_generation_fingerprint: Fingerprint
    new_root_reference_fingerprint: Fingerprint
    new_scope_fingerprint: Fingerprint
    expected_new_scope_binding_fingerprint: Fingerprint
    expected_preparation_ownership_fingerprint: Fingerprint
    rollover_authority_horizon_set_reference_fingerprint: Fingerprint
    rollover_request_fingerprint: Fingerprint
    dispatch_barrier_identity: ProviderInputDispatchBarrierIdentityFact
    resolved_model_call_id: str
    guard_fingerprint: Fingerprint


ProviderInputGenerationCommitGuardFact: TypeAlias = (
    InitialGenerationCommitGuardFact
    | ExistingAppendCommitGuardFact
    | RolloverGenerationCommitGuardFact
)


@_fact(
    "prepared_provider_input_plan.v1",
    "plan_fingerprint",
    "prepared-provider-input-plan:v1",
)
class PreparedProviderInputPlanFact(FrozenFactBase):
    schema_version: Literal["prepared_provider_input_plan.v1"] = (
        "prepared_provider_input_plan.v1"
    )
    plan_kind: Literal[
        "initial_generation",
        "existing_generation_append",
        "rollover_initial_append",
    ]
    resolved_model_call_id: str = Field(min_length=1)
    continuity_scope_fingerprint: Fingerprint
    target_generation_id: str = Field(min_length=1)
    predecessor_core_state_fingerprint: Fingerprint | None
    ordered_transcript_projection_identity: (
        ProviderOrderedTranscriptProjectionIdentityFact
    )
    causal_validation: ProviderInputCausalValidationResult
    frame_placement: ProviderInvocationContextFramePlacementFact | None
    transcript_delta_proof: ProviderTranscriptDeltaCommitProofFact
    rollover_intent: ProviderInputRolloverIntentFact | None
    resulting_unit_vector_root_fingerprint: Fingerprint
    resolved_causal_physical_policy_fingerprint: Fingerprint
    plan_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _matrix(self) -> "PreparedProviderInputPlanFact":
        if (
            self.ordered_transcript_projection_identity.identity_fingerprint
            != self.causal_validation.projection_identity_fingerprint
            or self.ordered_transcript_projection_identity.identity_fingerprint
            != self.transcript_delta_proof.projection_identity_fingerprint
            or self.causal_validation.status != "valid"
            or self.resolved_causal_physical_policy_fingerprint
            != self.causal_validation.resolved_causal_physical_policy_fingerprint
            or self.resolved_causal_physical_policy_fingerprint
            != self.transcript_delta_proof.resolved_causal_physical_policy_fingerprint
        ):
            raise ValueError("prepared provider plan projection/policy join mismatch")
        if self.plan_kind == "initial_generation":
            if (
                self.predecessor_core_state_fingerprint is not None
                or self.rollover_intent is not None
            ):
                raise ValueError("initial provider plan cannot carry predecessor")
        elif self.plan_kind == "existing_generation_append":
            if (
                self.predecessor_core_state_fingerprint is None
                or self.rollover_intent is not None
            ):
                raise ValueError("existing provider append plan matrix mismatch")
        elif (
            self.predecessor_core_state_fingerprint is None
            or self.rollover_intent is None
        ):
            raise ValueError("rollover provider plan requires predecessor and intent")
        return self


@_fact(
    "prepared_provider_input_append_candidate.v2",
    "candidate_fingerprint",
    "prepared-provider-input-append-candidate:v2",
)
class PreparedProviderInputAppendCandidateFact(FrozenFactBase):
    schema_version: Literal["prepared_provider_input_append_candidate.v2"] = (
        "prepared_provider_input_append_candidate.v2"
    )
    candidate_kind: Literal["compiled_manifest", "one_shot"]
    generation_id: str
    preparation_ownership: ProviderInputPreparationOwnershipFact
    expected_committed_core_state_fingerprint: Fingerprint | None
    append_batch_reference: ProviderInputAppendBatchReferenceFact
    provider_input_plan: CanonicalProviderInputPlanFact
    prepared_plan: PreparedProviderInputPlanFact | None
    manifest_projection_reference: ContextInputManifestProjectionReferenceFact | None
    rollover_request: ProviderInputRolloverRequestFact | None
    stable_companion_event_ids: tuple[str, ...]
    generation_commit_guard: ProviderInputGenerationCommitGuardFact
    candidate_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "PreparedProviderInputAppendCandidateFact":
        owner = self.preparation_ownership
        batch = self.append_batch_reference
        plan = self.provider_input_plan
        if (
            self.generation_id != owner.generation_id
            or self.generation_id != batch.generation.generation_id
            or self.expected_committed_core_state_fingerprint
            != owner.expected_committed_core_state_fingerprint
            or owner.append_batch_reference_fingerprint != batch.reference_fingerprint
            or owner.provider_input_plan_fingerprint != plan.plan_fingerprint
            or plan.resulting_prefix_fingerprint != batch.resulting_prefix_fingerprint
            or plan.resulting_generation_revision != batch.append_index
            or plan.unit_vector_root != batch.resulting_unit_vector_root
            or owner.stable_companion_event_ids != self.stable_companion_event_ids
        ):
            raise ValueError("prepared provider input candidate join mismatch")
        if self.candidate_kind == "compiled_manifest":
            if self.prepared_plan is None or self.manifest_projection_reference is None:
                raise ValueError("compiled provider candidate requires manifest plan")
            prepared = self.prepared_plan
            manifest = self.manifest_projection_reference
            if (
                prepared.resolved_model_call_id != plan.resolved_model_call_fact.resolved_model_call_id
                or prepared.target_generation_id != self.generation_id
                or prepared.resulting_unit_vector_root_fingerprint
                != plan.unit_vector_root.reference_fingerprint
                or prepared.ordered_transcript_projection_identity
                != manifest.projection_identity
            ):
                raise ValueError("compiled provider candidate manifest join mismatch")
            if (prepared.rollover_intent is None) != (self.rollover_request is None):
                raise ValueError("compiled provider candidate rollover matrix mismatch")
            if self.rollover_request is not None:
                if (
                    self.rollover_request.intent != prepared.rollover_intent
                    or self.rollover_request.manifest_projection_reference != manifest
                    or not isinstance(
                        self.generation_commit_guard,
                        RolloverGenerationCommitGuardFact,
                    )
                    or self.generation_commit_guard.rollover_request_fingerprint
                    != self.rollover_request.request_fingerprint
                ):
                    raise ValueError("compiled provider rollover request drifted")
        elif any(
            item is not None
            for item in (
                self.prepared_plan,
                self.manifest_projection_reference,
                self.rollover_request,
            )
        ):
            raise ValueError("one-shot provider candidate cannot carry context manifest")
        return self


@_fact(
    "committed_provider_input_reference.v2",
    "reference_fingerprint",
    "committed-provider-input-reference:v2",
)
class CommittedProviderInputReferenceFact(FrozenFactBase):
    schema_version: Literal["committed_provider_input_reference.v2"] = (
        "committed_provider_input_reference.v2"
    )
    reference_kind: Literal["compiled_manifest", "one_shot"]
    generation_id: str
    committed_generation_revision: int = Field(ge=1)
    resulting_generation_core_state_fingerprint: Fingerprint
    append_committed_event_identity: StableEventIdentityFact
    resulting_prefix_fingerprint: Fingerprint
    resulting_unit_vector_root: ProviderInputUnitVectorRootReferenceFact
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    replay_binding_set: ProviderInputReplayBindingSetReferenceFact
    provider_input_plan_fingerprint: Fingerprint
    manifest_projection_reference_fingerprint: Fingerprint | None
    causal_validation_fingerprint: Fingerprint | None
    transcript_frontier_fingerprint: Fingerprint | None
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _carrier_matrix(self) -> "CommittedProviderInputReferenceFact":
        compiled = self.reference_kind == "compiled_manifest"
        if compiled != (self.manifest_projection_reference_fingerprint is not None):
            raise ValueError("provider input reference manifest matrix mismatch")
        if compiled != (self.causal_validation_fingerprint is not None):
            raise ValueError("provider input reference validation matrix mismatch")
        if compiled != (self.transcript_frontier_fingerprint is not None):
            raise ValueError("provider input reference frontier matrix mismatch")
        return self


ProviderInputUnitVectorNodeReferenceFact.model_rebuild()
ProviderMessageFragmentFact.model_rebuild()
ProviderInputToolResultBlockFact.model_rebuild()


__all__ = [
    name
    for name in globals()
    if name.startswith("Provider")
    or name.startswith("Committed")
    or name.startswith("Canonical")
    or name.startswith("Initial")
    or name.startswith("Existing")
    or name.startswith("Rollover")
    or name.startswith("Session")
    or name.startswith("OneShot")
]
