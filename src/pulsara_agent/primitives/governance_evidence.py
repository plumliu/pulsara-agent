"""Durable source-evidence contracts for memory governance.

The module deliberately separates model-visible semantics, durable source
attribution, and bounded prompt projection.  Storage references therefore
cannot accidentally become part of governance semantic identity.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, model_validator

from pulsara_agent.event.candidates import CandidatePayload
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    StableEventIdentityFact,
    register_durable_fact,
)
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ModelContextMode,
    ResolvedModelCallFact,
    canonical_json_bytes,
)
from pulsara_agent.primitives.terminal_projection import (
    ModelToolCallBlockSemanticFact,
    TerminalProjectionReferenceFact,
    ToolTerminalProjectionSemanticFact,
)
from pulsara_agent.primitives.transcript_projection import (
    TranscriptProjectionLeafEntryReferenceFact,
)


Fingerprint = Annotated[str, Field(min_length=1, max_length=256)]
_CANDIDATE_PAYLOAD_ADAPTER = TypeAdapter(CandidatePayload)
_PROMPT_FIELD_CODES = (
    "verified_user_quote",
    "accepted_assistant_text",
    "selected_tool_arguments",
    "tool_result_essential",
    "reflection_report",
    "compaction_summary",
)


class GovernanceEvidenceFrozenFact(FrozenFactBase):
    """Marker for the governance evidence schema family."""


class GovernanceStoredEventReferenceFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_stored_event_reference.v1"]
    stable_identity: StableEventIdentityFact
    sequence: int = Field(ge=1)
    stored_envelope_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class GovernanceEvidenceArtifactReferenceFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_evidence_artifact_reference.v1"]
    artifact_kind: Literal[
        "governance_batch_input",
        "terminal_projection",
        "compaction_summary",
        "quoted_evidence",
        "tool_result",
        "related_memory_content",
    ]
    artifact_id: str = Field(min_length=1, max_length=256)
    media_type: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    artifact_contract_id: str = Field(min_length=1, max_length=128)
    artifact_contract_version: str = Field(min_length=1, max_length=64)
    artifact_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class GovernanceBatchInputArtifactContractFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_batch_input_artifact_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    document_schema_fingerprint: Fingerprint
    canonicalization_contract_fingerprint: Fingerprint
    media_type: Literal["application/vnd.pulsara.governance-batch-input+json"]
    max_artifact_utf8_bytes: int = Field(ge=1, le=2 * 1024 * 1024)
    contract_fingerprint: Fingerprint


class GovernanceEvidenceBuildReason(StrEnum):
    FULL_MAIN_TOOL_JOIN = "full_main_tool_join"
    FULL_REFLECTION_JOIN = "full_reflection_join"
    FULL_COMPACTION_JOIN = "full_compaction_join"
    WAIT_REDUCER_BEHIND = "wait_reducer_behind"
    WAIT_PROJECTION_OUTBOX = "wait_projection_outbox"
    WAIT_ARTIFACT_CONFIRMATION = "wait_artifact_confirmation"
    INVALID_SOURCE_CALL_MISSING = "invalid_source_call_missing"
    INVALID_TERMINAL_RUN_WITHOUT_PAIR = "invalid_terminal_run_without_pair"
    INVALID_CANDIDATE_PAYLOAD_MISMATCH = "invalid_candidate_payload_mismatch"
    INVALID_PRODUCER_OMITS_CANDIDATE = "invalid_producer_omits_candidate"
    INVALID_RAW_CANDIDATE_INDEX = "invalid_raw_candidate_index"
    INVALID_ORIGIN_FIELDS = "invalid_origin_fields"
    UNTRUSTED_ARTIFACT_HASH = "untrusted_artifact_hash"
    UNTRUSTED_REDUCER_EVENT_MISMATCH = "untrusted_reducer_event_mismatch"
    UNTRUSTED_DECODER_BINDING = "untrusted_decoder_binding"
    UNTRUSTED_ID_PAYLOAD_CONFLICT = "untrusted_id_payload_conflict"
    NOT_APPLICABLE_AUDIT_ORIGIN = "not_applicable_audit_origin"


class CandidateEvidenceRejectionReason(StrEnum):
    SOURCE_CALL_MISSING = "source_call_missing"
    TERMINAL_RUN_WITHOUT_PAIR = "terminal_run_without_pair"
    CANDIDATE_PAYLOAD_MISMATCH = "candidate_payload_mismatch"
    PRODUCER_OMITS_CANDIDATE = "producer_omits_candidate"
    RAW_CANDIDATE_INDEX_MISSING = "raw_candidate_index_missing"
    ORIGIN_FIELDS_INVALID = "origin_fields_invalid"


class CandidateQuotedEvidenceLocatorFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["candidate_quoted_evidence_locator.v1"]
    locator_kind: Literal[
        "canonical_user_message_span",
        "reflection_quote_index",
        "compaction_summary_span",
    ]
    source_message_id: str | None = Field(default=None, max_length=256)
    source_event_reference: GovernanceStoredEventReferenceFact | None = None
    source_artifact_reference: GovernanceEvidenceArtifactReferenceFact | None = None
    source_quote_index: int | None = Field(default=None, ge=0, le=255)
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, ge=0)
    quoted_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _locator_shape(self) -> "CandidateQuotedEvidenceLocatorFact":
        if self.locator_kind == "canonical_user_message_span":
            if self.source_message_id is None or self.start_char is None or self.end_char is None:
                raise ValueError("canonical user locator requires message and span")
            if self.source_quote_index is not None or self.start_char > self.end_char:
                raise ValueError("canonical user locator span is invalid")
        elif self.locator_kind == "reflection_quote_index":
            if self.source_quote_index is None or self.start_char is not None or self.end_char is not None:
                raise ValueError("reflection locator requires quote index only")
        elif self.source_artifact_reference is None or self.start_char is None or self.end_char is None:
            raise ValueError("compaction locator requires artifact and span")
        return self


class GovernanceCandidatePayloadSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_candidate_payload_semantic.v1"]
    candidate_origin: Literal["main_agent_tool", "reflection", "compaction"]
    payload_kind: str = Field(min_length=1, max_length=64)
    canonical_candidate_payload: CandidatePayload
    canonical_payload_utf8_bytes: int = Field(ge=1, le=16 * 1024)
    intent_fingerprint: str | None = Field(default=None, max_length=256)
    payload_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _payload_bytes(self) -> "GovernanceCandidatePayloadSemanticFact":
        actual = len(
            canonical_json_bytes(
                _CANDIDATE_PAYLOAD_ADAPTER.dump_python(
                    self.canonical_candidate_payload,
                    mode="json",
                )
            )
        )
        if self.canonical_payload_utf8_bytes != actual:
            raise ValueError("candidate canonical payload byte count mismatch")
        if self.payload_kind != self.canonical_candidate_payload.payload_kind:
            raise ValueError("candidate payload kind mismatch")
        return self


class GovernanceCandidateAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_candidate_attribution.v1"]
    entry_id: str = Field(min_length=1, max_length=256)
    runtime_session_id: str = Field(min_length=1, max_length=256)
    source_run_id: str = Field(min_length=1, max_length=256)
    source_turn_id: str = Field(min_length=1, max_length=256)
    source_reply_id: str = Field(min_length=1, max_length=256)
    source_tool_call_id: str | None = Field(default=None, max_length=256)
    source_event_reference: GovernanceStoredEventReferenceFact | None = None
    source_artifact_reference: GovernanceEvidenceArtifactReferenceFact | None = None
    quoted_evidence_locator: CandidateQuotedEvidenceLocatorFact | None = None
    created_at_utc: str = Field(min_length=1, max_length=64)
    attribution_fingerprint: Fingerprint


class GovernanceQuotedEvidenceSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_quoted_evidence_semantic.v1"]
    quote_kind: Literal[
        "canonical_user_span", "reflection_reported", "compaction_summary_span"
    ]
    text: str = Field(max_length=16_384)
    text_utf8_bytes: int = Field(ge=0, le=64 * 1024)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_status: Literal["canonical_match", "origin_reported"]
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _text_identity(self) -> "GovernanceQuotedEvidenceSemanticFact":
        encoded = self.text.encode("utf-8")
        if self.text_utf8_bytes != len(encoded):
            raise ValueError("quoted evidence byte count mismatch")
        if self.text_sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError("quoted evidence SHA mismatch")
        if self.verification_status == "canonical_match" and self.quote_kind != "canonical_user_span":
            raise ValueError("only canonical user spans may be canonical matches")
        return self


class GovernanceQuotedEvidenceAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_quoted_evidence_attribution.v1"]
    quote_semantic_fingerprint: Fingerprint
    source_entry_ref: TranscriptProjectionLeafEntryReferenceFact | None = None
    source_artifact_ref: GovernanceEvidenceArtifactReferenceFact | None = None
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, ge=0)
    producer_event_reference: GovernanceStoredEventReferenceFact | None = None
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _span(self) -> "GovernanceQuotedEvidenceAttributionFact":
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError("quoted evidence span must be all present or absent")
        if self.start_char is not None and self.start_char > self.end_char:
            raise ValueError("quoted evidence span is invalid")
        return self


class MainAgentToolGovernanceSourceSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["main_agent_tool_governance_source_semantic.v1"]
    evidence_kind: Literal["main_agent_tool"]
    candidate_payload_semantic_fingerprint: Fingerprint
    model_control_acceptance: Literal["accepted"]
    selected_tool_call_semantic: ModelToolCallBlockSemanticFact
    tool_result_semantic: ToolTerminalProjectionSemanticFact
    quoted_evidence_semantic: GovernanceQuotedEvidenceSemanticFact | None = None
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _tool_join(self) -> "MainAgentToolGovernanceSourceSemanticFact":
        call = self.selected_tool_call_semantic
        result = self.tool_result_semantic.canonical_result_block_semantic
        if call.completion_status != "completed":
            raise ValueError("governance tool-call evidence must be complete")
        if result.tool_call_id != call.tool_call_id or result.model_tool_name != call.tool_name:
            raise ValueError("governance tool call/result identity mismatch")
        return self


class ReflectionGovernanceSourceSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["reflection_governance_source_semantic.v1"]
    evidence_kind: Literal["reflection"]
    candidate_payload_semantic_fingerprint: Fingerprint
    reflection_policy_id: str = Field(min_length=1, max_length=128)
    reflection_policy_version: str = Field(min_length=1, max_length=64)
    reflection_policy_contract_fingerprint: Fingerprint
    reflection_model_result_semantic_fingerprint: Fingerprint
    candidate_index: int = Field(ge=0, le=255)
    ordered_quoted_evidence_semantics: tuple[GovernanceQuotedEvidenceSemanticFact, ...] = Field(max_length=32)
    semantic_fingerprint: Fingerprint


class CompactionMemoryCandidateExtractorContractFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["compaction_memory_candidate_extractor_contract.v1"]
    extractor_id: str = Field(min_length=1, max_length=128)
    extractor_version: str = Field(min_length=1, max_length=64)
    accepted_input_schema_fingerprint: Fingerprint
    output_candidate_schema_fingerprint: Fingerprint
    parsing_rules_fingerprint: Fingerprint
    normalization_rules_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


class CompactionGovernanceSourceSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["compaction_governance_source_semantic.v1"]
    evidence_kind: Literal["compaction"]
    candidate_payload_semantic_fingerprint: Fingerprint
    summary_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary_content_semantic_fingerprint: Fingerprint
    extractor_contract: CompactionMemoryCandidateExtractorContractFact
    raw_candidate_index: int = Field(ge=0, le=255)
    canonical_parsed_candidate_payload_fingerprint: Fingerprint
    intent_fingerprint: Fingerprint
    quoted_evidence_semantic: GovernanceQuotedEvidenceSemanticFact | None = None
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _payload_join(self) -> "CompactionGovernanceSourceSemanticFact":
        if self.canonical_parsed_candidate_payload_fingerprint != self.candidate_payload_semantic_fingerprint:
            raise ValueError("compaction parsed candidate fingerprint mismatch")
        return self


GovernanceSourceEvidenceSemanticFact: TypeAlias = Annotated[
    MainAgentToolGovernanceSourceSemanticFact
    | ReflectionGovernanceSourceSemanticFact
    | CompactionGovernanceSourceSemanticFact,
    Field(discriminator="evidence_kind"),
]


class GovernanceSourceEvidenceAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_source_evidence_attribution.v1"]
    evidence_kind: Literal["main_agent_tool", "reflection", "compaction"]
    evidence_semantic_fingerprint: Fingerprint
    runtime_session_id: str = Field(min_length=1, max_length=256)
    authority_ledger_through_sequence: int = Field(ge=0)
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    producer_event_references: tuple[GovernanceStoredEventReferenceFact, ...] = Field(min_length=1, max_length=8)
    model_terminal_projection_reference: TerminalProjectionReferenceFact | None = None
    model_disposition_event_reference: GovernanceStoredEventReferenceFact | None = None
    tool_terminal_projection_reference: TerminalProjectionReferenceFact | None = None
    quoted_evidence_attributions: tuple[GovernanceQuotedEvidenceAttributionFact, ...] = Field(max_length=32)
    source_artifact_references: tuple[GovernanceEvidenceArtifactReferenceFact, ...] = Field(max_length=16)
    producer_contract_fingerprints: tuple[Fingerprint, ...] = Field(max_length=16)
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _attribution_matrix(self) -> "GovernanceSourceEvidenceAttributionFact":
        refs = self.producer_event_references
        if any(ref.stable_identity.runtime_session_id != self.runtime_session_id for ref in refs):
            raise ValueError("governance producer references cross runtime sessions")
        if any(ref.sequence > self.authority_ledger_through_sequence for ref in refs):
            raise ValueError("governance producer reference exceeds authority high-water")
        sequences = tuple(ref.sequence for ref in refs)
        event_ids = tuple(ref.stable_identity.event_id for ref in refs)
        if sequences != tuple(sorted(sequences)) or len(event_ids) != len(set(event_ids)):
            raise ValueError("governance producer references must be ordered and unique")
        if self.model_disposition_event_reference is not None and self.model_disposition_event_reference.sequence > self.authority_ledger_through_sequence:
            raise ValueError("governance disposition reference exceeds authority high-water")
        if self.evidence_kind == "main_agent_tool":
            if self.model_terminal_projection_reference is None or self.model_disposition_event_reference is None or self.tool_terminal_projection_reference is None:
                raise ValueError("main-tool evidence requires model, disposition, and tool references")
        elif self.model_terminal_projection_reference is not None or self.model_disposition_event_reference is not None or self.tool_terminal_projection_reference is not None:
            raise ValueError("non-tool evidence forbids model/tool projection references")
        if self.evidence_kind == "compaction" and not self.source_artifact_references:
            raise ValueError("compaction evidence requires summary artifact")
        return self


class GovernancePromptEvidenceTextFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_prompt_evidence_text.v1"]
    field_code: Literal[
        "verified_user_quote", "accepted_assistant_text", "selected_tool_arguments",
        "tool_result_essential", "reflection_report", "compaction_summary",
    ]
    text: str = Field(max_length=2_000)
    source_semantic_fingerprint: Fingerprint
    verification_status: Literal["canonical_match", "origin_reported"]
    text_fingerprint: Fingerprint


class GovernanceCandidatePromptPayloadFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_candidate_prompt_payload.v1"]
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    candidate_payload_semantic_fingerprint: Fingerprint
    canonical_candidate_payload: CandidatePayload
    evidence_kind: Literal["main_agent_tool", "reflection", "compaction"]
    accepted: bool
    ordered_evidence_texts: tuple[GovernancePromptEvidenceTextFact, ...] = Field(max_length=16)
    tool_name: str | None = Field(default=None, max_length=256)
    tool_result_state: str | None = Field(default=None, max_length=64)
    observation_timing_fingerprint: str | None = Field(default=None, max_length=256)
    artifact_references: tuple[GovernanceEvidenceArtifactReferenceFact, ...] = Field(max_length=16)
    payload_utf8_bytes: int = Field(ge=1, le=16 * 1024)
    payload_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _payload_shape(self) -> "GovernanceCandidatePromptPayloadFact":
        expected_bytes = len(
            canonical_json_bytes(
                {
                    "candidate_entry_id": self.candidate_entry_id,
                    "candidate_payload_semantic_fingerprint": (
                        self.candidate_payload_semantic_fingerprint
                    ),
                    "canonical_candidate_payload": (
                        _CANDIDATE_PAYLOAD_ADAPTER.dump_python(
                            self.canonical_candidate_payload,
                            mode="json",
                        )
                    ),
                    "evidence_kind": self.evidence_kind,
                    "accepted": self.accepted,
                    "ordered_evidence_texts": tuple(
                        item.model_dump(mode="json")
                        for item in self.ordered_evidence_texts
                    ),
                    "tool_name": self.tool_name,
                    "tool_result_state": self.tool_result_state,
                    "observation_timing_fingerprint": (
                        self.observation_timing_fingerprint
                    ),
                    "artifact_references": tuple(
                        item.model_dump(mode="json")
                        for item in self.artifact_references
                    ),
                }
            )
        )
        if self.payload_utf8_bytes != expected_bytes:
            raise ValueError("governance prompt payload byte count mismatch")
        field_codes = tuple(item.field_code for item in self.ordered_evidence_texts)
        if self.evidence_kind == "main_agent_tool":
            if (
                not self.accepted
                or self.tool_name is None
                or self.tool_result_state is None
                or self.observation_timing_fingerprint is None
                or "selected_tool_arguments" not in field_codes
                or "tool_result_essential" not in field_codes
            ):
                raise ValueError("main-tool prompt payload is incomplete")
        elif (
            self.accepted
            or self.tool_name is not None
            or self.tool_result_state is not None
            or self.observation_timing_fingerprint is not None
        ):
            raise ValueError("non-tool prompt payload carries tool acceptance")
        return self


class GovernanceEvidencePromptProjectionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_evidence_prompt_projection.v1"]
    source_evidence_semantic_fingerprint: Fingerprint
    projection_contract_id: str = Field(min_length=1, max_length=128)
    projection_contract_version: str = Field(min_length=1, max_length=64)
    projection_contract_fingerprint: Fingerprint
    model_visible_payload: GovernanceCandidatePromptPayloadFact
    included_field_codes: tuple[str, ...] = Field(max_length=16)
    omitted_field_codes: tuple[str, ...] = Field(max_length=16)
    truncation_reason_codes: tuple[str, ...] = Field(max_length=16)
    projected_utf8_bytes: int = Field(ge=1, le=64 * 1024)
    projection_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _projection_shape(self) -> "GovernanceEvidencePromptProjectionFact":
        included = tuple(
            dict.fromkeys(
                item.field_code
                for item in self.model_visible_payload.ordered_evidence_texts
            )
        )
        if self.included_field_codes != included:
            raise ValueError("governance prompt included fields drifted")
        expected_omitted = tuple(
            field_code for field_code in _PROMPT_FIELD_CODES if field_code not in included
        )
        if self.omitted_field_codes != expected_omitted:
            raise ValueError("governance prompt omitted fields drifted")
        if self.projected_utf8_bytes != self.model_visible_payload.payload_utf8_bytes:
            raise ValueError("governance prompt projected bytes drifted")
        return self


class ImmutableGovernanceCandidateSnapshotFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["immutable_governance_candidate_snapshot.v1"]
    payload_semantic: GovernanceCandidatePayloadSemanticFact
    candidate_attribution: GovernanceCandidateAttributionFact
    source_evidence_semantic: GovernanceSourceEvidenceSemanticFact
    source_evidence_attribution: GovernanceSourceEvidenceAttributionFact
    prompt_projection: GovernanceEvidencePromptProjectionFact
    candidate_snapshot_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "ImmutableGovernanceCandidateSnapshotFact":
        payload_fp = self.payload_semantic.payload_semantic_fingerprint
        if self.source_evidence_semantic.candidate_payload_semantic_fingerprint != payload_fp:
            raise ValueError("candidate/source semantic payload mismatch")
        if self.source_evidence_attribution.candidate_entry_id != self.candidate_attribution.entry_id:
            raise ValueError("candidate/source attribution entry mismatch")
        if self.source_evidence_attribution.evidence_semantic_fingerprint != self.source_evidence_semantic.semantic_fingerprint:
            raise ValueError("candidate source semantic/attribution mismatch")
        if self.prompt_projection.source_evidence_semantic_fingerprint != self.source_evidence_semantic.semantic_fingerprint:
            raise ValueError("candidate prompt/source semantic mismatch")
        origin_to_evidence = {
            "main_agent_tool": "main_agent_tool",
            "reflection": "reflection",
            "compaction": "compaction",
        }
        if (
            origin_to_evidence[self.payload_semantic.candidate_origin]
            != self.source_evidence_semantic.evidence_kind
            or self.source_evidence_attribution.evidence_kind
            != self.source_evidence_semantic.evidence_kind
            or self.prompt_projection.model_visible_payload.evidence_kind
            != self.source_evidence_semantic.evidence_kind
        ):
            raise ValueError("candidate evidence kind/origin mismatch")
        semantic_quotes: tuple[GovernanceQuotedEvidenceSemanticFact, ...]
        source = self.source_evidence_semantic
        if isinstance(source, ReflectionGovernanceSourceSemanticFact):
            semantic_quotes = source.ordered_quoted_evidence_semantics
        else:
            semantic_quotes = (
                ()
                if source.quoted_evidence_semantic is None
                else (source.quoted_evidence_semantic,)
            )
        attributions = self.source_evidence_attribution.quoted_evidence_attributions
        if len(semantic_quotes) != len(attributions) or any(
            semantic.semantic_fingerprint != attribution.quote_semantic_fingerprint
            for semantic, attribution in zip(
                semantic_quotes,
                attributions,
                strict=True,
            )
        ):
            raise ValueError("candidate quoted evidence semantic/attribution mismatch")
        return self


class GovernanceEvidencePromptProjectionContractFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_evidence_prompt_projection_contract.v1"]
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    max_quote_characters_per_candidate: int = Field(ge=0, le=8_192)
    max_assistant_text_characters_per_candidate: int = Field(ge=0, le=8_192)
    max_tool_result_characters_per_candidate: int = Field(ge=0, le=8_192)
    max_artifact_refs_per_candidate: int = Field(ge=0, le=16)
    max_candidates_per_batch: int = Field(ge=1, le=32)
    max_related_memories_per_candidate: int = Field(ge=0, le=16)
    max_candidate_projection_utf8_bytes: int = Field(ge=1, le=64 * 1024)
    max_batch_projection_utf8_bytes: int = Field(ge=1, le=512 * 1024)
    truncation_policy: Literal["typed_head_tail_v1"]
    essential_envelope_contract_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


class GovernanceRelatedMemorySemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_related_memory_semantic.v1"]
    memory_id: str = Field(min_length=1, max_length=256)
    memory_type: str = Field(min_length=1, max_length=128)
    canonical_statement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_statement_utf8_bytes: int = Field(ge=1, le=64 * 1024)
    scope: str = Field(min_length=1, max_length=512)
    status: str = Field(min_length=1, max_length=64)
    verification_status: str | None = Field(default=None, max_length=64)
    source_authority: str | None = Field(default=None, max_length=64)
    applies_when: str | None = Field(default=None, max_length=8_192)
    do_not_apply_when: str | None = Field(default=None, max_length=8_192)
    semantic_fingerprint: Fingerprint


class GovernanceRelatedMemoryPromptProjectionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_related_memory_prompt_projection.v1"]
    memory_semantic_fingerprint: Fingerprint
    projected_statement: str = Field(max_length=2_000)
    relationship_codes: tuple[str, ...] = Field(max_length=16)
    exact_duplicate: bool
    projection_contract_fingerprint: Fingerprint
    projected_utf8_bytes: int = Field(ge=1, le=4 * 1024)
    projection_fingerprint: Fingerprint


class GovernanceRelatednessCandidateFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_relatedness_candidate.v1"]
    graph_id: str = Field(min_length=1, max_length=256)
    memory_node_revision: int = Field(ge=1)
    canonical_memory: GovernanceRelatedMemorySemanticFact
    canonical_statement_inline: str | None = Field(default=None, max_length=4_096)
    canonical_content_reference: GovernanceEvidenceArtifactReferenceFact | None = None
    prompt_projection: GovernanceRelatedMemoryPromptProjectionFact
    source_projection_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_carrier(self) -> "GovernanceRelatednessCandidateFact":
        if (self.canonical_statement_inline is None) == (self.canonical_content_reference is None):
            raise ValueError("related memory requires exactly one content carrier")
        if self.prompt_projection.memory_semantic_fingerprint != self.canonical_memory.semantic_fingerprint:
            raise ValueError("related memory prompt semantic mismatch")
        if self.canonical_statement_inline is not None:
            encoded = self.canonical_statement_inline.encode("utf-8")
            if len(encoded) != self.canonical_memory.canonical_statement_utf8_bytes or hashlib.sha256(encoded).hexdigest() != self.canonical_memory.canonical_statement_sha256:
                raise ValueError("related memory inline statement identity mismatch")
        return self


class GovernanceRelatednessSnapshotFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_relatedness_snapshot.v1"]
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    availability: Literal["full", "partial", "unavailable"]
    ordered_candidates: tuple[GovernanceRelatednessCandidateFact, ...] = Field(max_length=16)
    provider_contract_fingerprint: Fingerprint
    snapshot_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordered(self) -> "GovernanceRelatednessSnapshotFact":
        keys = tuple((item.canonical_memory.memory_id, item.memory_node_revision) for item in self.ordered_candidates)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("relatedness candidates must be ordered and unique")
        return self


class ReflectionCandidateAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["reflection_candidate_attribution.v1"]
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    candidate_index: int = Field(ge=0, le=255)
    candidate_payload: CandidatePayload
    candidate_payload_fingerprint: Fingerprint
    intent_fingerprint: str | None = Field(default=None, max_length=256)
    ordered_quoted_evidence_indices: tuple[int, ...] = Field(max_length=32)
    attribution_fingerprint: Fingerprint


class CompactionCandidateAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["compaction_candidate_attribution.v1"]
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    raw_candidate_index: int = Field(ge=0, le=255)
    candidate_payload: CandidatePayload
    candidate_payload_fingerprint: Fingerprint
    intent_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


class CandidateProjectionProducerKind(StrEnum):
    REFLECTION = "reflection"
    COMPACTION = "compaction"


class CandidateProjectionOutboxItemFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["candidate_projection_outbox_item.v1"]
    producer_kind: CandidateProjectionProducerKind
    producer_event_identity: StableEventIdentityFact
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    candidate_index: int = Field(ge=0, le=255)
    candidate_payload: CandidatePayload
    candidate_payload_fingerprint: Fingerprint
    candidate_attribution_fingerprint: Fingerprint
    item_fingerprint: Fingerprint


class MainAgentMemoryCandidateBuilderContractFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["main_agent_memory_candidate_builder_contract.v1"]
    builder_id: str = Field(min_length=1, max_length=128)
    builder_version: str = Field(min_length=1, max_length=64)
    input_schema_fingerprint: Fingerprint
    output_schema_fingerprint: Fingerprint
    candidate_id_policy: Literal["source_identity_sha256_v1"]
    normalization_contract_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


class GovernanceEvidenceBuildStatus(StrEnum):
    FULL = "full"
    NOT_READY = "not_ready"
    CANDIDATE_SOURCE_INVALID = "candidate_source_invalid"
    AUTHORITY_UNTRUSTED = "authority_untrusted"
    NOT_APPLICABLE = "not_applicable"


class GovernanceEvidenceBuildResult(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_evidence_build_result.v1"]
    status: GovernanceEvidenceBuildStatus
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    source_high_water: int = Field(ge=0)
    evidence_semantic: GovernanceSourceEvidenceSemanticFact | None = None
    evidence_attribution: GovernanceSourceEvidenceAttributionFact | None = None
    stable_reason_code: GovernanceEvidenceBuildReason
    retry_after_seconds: float | None = Field(default=None, gt=0, le=30)
    result_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _status_matrix(self) -> "GovernanceEvidenceBuildResult":
        reason = self.stable_reason_code.value
        if self.status is GovernanceEvidenceBuildStatus.FULL:
            valid = reason.startswith("full_") and self.evidence_semantic is not None and self.evidence_attribution is not None and self.retry_after_seconds is None
        elif self.status is GovernanceEvidenceBuildStatus.NOT_READY:
            valid = reason.startswith("wait_") and self.evidence_semantic is None and self.evidence_attribution is None and self.retry_after_seconds is not None
        elif self.status is GovernanceEvidenceBuildStatus.CANDIDATE_SOURCE_INVALID:
            valid = reason.startswith("invalid_") and self.evidence_semantic is None and self.evidence_attribution is None and self.retry_after_seconds is None
        elif self.status is GovernanceEvidenceBuildStatus.AUTHORITY_UNTRUSTED:
            valid = reason.startswith("untrusted_") and self.evidence_semantic is None and self.evidence_attribution is None and self.retry_after_seconds is None
        else:
            valid = self.stable_reason_code is GovernanceEvidenceBuildReason.NOT_APPLICABLE_AUDIT_ORIGIN and self.evidence_semantic is None and self.evidence_attribution is None and self.retry_after_seconds is None
        if not valid:
            raise ValueError("governance evidence build status/reason matrix mismatch")
        return self


class MemoryCandidateEvidenceRejectedRecord(GovernanceEvidenceFrozenFact):
    schema_version: Literal["memory_candidate_evidence_rejected.v1"]
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    source_high_water: int = Field(ge=0)
    stable_reason_code: CandidateEvidenceRejectionReason
    observed_source_fingerprints: tuple[Fingerprint, ...] = Field(max_length=16)
    rejection_fingerprint: Fingerprint


class GovernanceCandidateClaimStatus(StrEnum):
    PREPARING = "preparing"
    PREPARED = "prepared"
    TERMINAL = "terminal"
    RELEASED = "released"


class MemoryGovernanceCandidateClaimFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["memory_governance_candidate_claim.v1"]
    candidate_entry_id: str = Field(min_length=1, max_length=256)
    candidate_row_fingerprint: Fingerprint
    governance_batch_id: str = Field(min_length=1, max_length=256)
    claim_generation: int = Field(ge=1)
    status: GovernanceCandidateClaimStatus
    prepared_event_id: str | None = Field(default=None, max_length=256)
    terminal_record_id: str | None = Field(default=None, max_length=256)
    previous_claim_fingerprint: str | None = Field(default=None, max_length=256)
    claim_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _claim_matrix(self) -> "MemoryGovernanceCandidateClaimFact":
        if self.status is GovernanceCandidateClaimStatus.PREPARING and (self.prepared_event_id is not None or self.terminal_record_id is not None):
            raise ValueError("preparing claim cannot carry terminal references")
        if self.status is GovernanceCandidateClaimStatus.PREPARED and (self.prepared_event_id is None or self.terminal_record_id is not None):
            raise ValueError("prepared claim requires only prepared event")
        if self.status is GovernanceCandidateClaimStatus.TERMINAL and self.terminal_record_id is None:
            raise ValueError("terminal claim requires terminal record")
        if self.status is GovernanceCandidateClaimStatus.RELEASED and self.prepared_event_id is not None:
            raise ValueError("released claim cannot have prepared event")
        return self


class GovernanceSystemPromptContractFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_system_prompt_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    template_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assembly_contract_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


class GovernanceFrozenLLMToolCallFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_frozen_llm_tool_call.v1"]
    tool_call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    arguments: str = Field(max_length=64 * 1024)
    semantic_fingerprint: Fingerprint


class GovernanceFrozenLLMMessageFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_frozen_llm_message.v1"]
    role: Literal["system", "user", "assistant", "tool_call", "tool_result", "runtime_observation"]
    content: tuple[str, ...] = Field(max_length=16)
    thinking: tuple[str, ...] = Field(max_length=16)
    tool_calls: tuple[GovernanceFrozenLLMToolCallFact, ...] = Field(max_length=32)
    tool_call_id: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, max_length=128)
    arguments: str | None = Field(default=None, max_length=64 * 1024)
    message_fingerprint: Fingerprint


class GovernanceModelInputFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_model_input.v1"]
    governance_batch_id: str = Field(min_length=1, max_length=256)
    resolved_call: ResolvedModelCallFact
    target_fingerprint: Fingerprint
    context_id: str = Field(min_length=1, max_length=512)
    model_call_index: Literal[None] = None
    system_prompt_contract: GovernanceSystemPromptContractFact
    exact_system_prompt: str = Field(max_length=64 * 1024)
    ordered_messages: tuple[GovernanceFrozenLLMMessageFact, ...] = Field(min_length=1, max_length=128)
    tool_spec_count: Literal[0] = 0
    compiler_estimated_input_tokens: int = Field(ge=1)
    estimator_contract_fingerprint: Fingerprint
    provider_neutral_context_fingerprint: Fingerprint
    model_input_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _call_binding(self) -> "GovernanceModelInputFact":
        if self.resolved_call.purpose is not ModelCallPurpose.MEMORY_GOVERNANCE or self.resolved_call.context_mode is not ModelContextMode.DIRECT:
            raise ValueError("governance model input requires direct governance call")
        if self.target_fingerprint != self.resolved_call.target.target_fingerprint:
            raise ValueError("governance model input target mismatch")
        if self.context_id != f"memory_governance:{self.governance_batch_id}":
            raise ValueError("governance model input context ID mismatch")
        if self.system_prompt_contract.template_content_sha256 != hashlib.sha256(self.exact_system_prompt.encode("utf-8")).hexdigest():
            raise ValueError("governance system prompt content mismatch")
        expected_context = context_fingerprint(
            "provider-neutral-llm-context:v1",
            {
                "system_prompt": self.exact_system_prompt,
                "messages": tuple(
                    {
                        "role": message.role,
                        "content": message.content,
                        "thinking": message.thinking,
                        "tool_calls": tuple(
                            {
                                "id": call.tool_call_id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                            for call in message.tool_calls
                        ),
                        "tool_call_id": message.tool_call_id,
                        "name": message.name,
                        "arguments": message.arguments,
                    }
                    for message in self.ordered_messages
                ),
                "tools": (),
                "context_id": self.context_id,
                "resolved_model_call_id": self.resolved_call.resolved_model_call_id,
                "target_fingerprint": self.target_fingerprint,
                "model_call_index": self.model_call_index,
                "compiler_estimated_input_tokens": self.compiler_estimated_input_tokens,
            },
        )
        if self.provider_neutral_context_fingerprint != expected_context:
            raise ValueError("governance provider-neutral context fingerprint mismatch")
        return self


class GovernanceBatchInputSnapshotFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_batch_input_snapshot.v1"]
    artifact_contract: GovernanceBatchInputArtifactContractFact
    runtime_session_id: str = Field(min_length=1, max_length=256)
    governance_batch_id: str = Field(min_length=1, max_length=256)
    source_ledger_through_sequence: int = Field(ge=0)
    transcript_authority_snapshot_fingerprint: Fingerprint
    ordered_preparing_claims: tuple[MemoryGovernanceCandidateClaimFact, ...] = Field(min_length=1, max_length=32)
    ordered_candidate_snapshots: tuple[ImmutableGovernanceCandidateSnapshotFact, ...] = Field(min_length=1, max_length=32)
    ordered_relatedness_snapshots: tuple[GovernanceRelatednessSnapshotFact, ...] = Field(min_length=1, max_length=32)
    allowed_scopes: tuple[str, ...] = Field(max_length=64)
    prompt_projection_contract_fingerprint: Fingerprint
    model_input: GovernanceModelInputFact
    final_model_visible_input_fingerprint: Fingerprint
    batch_input_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _batch_joins(self) -> "GovernanceBatchInputSnapshotFact":
        claim_ids = tuple(item.candidate_entry_id for item in self.ordered_preparing_claims)
        candidate_ids = tuple(item.candidate_attribution.entry_id for item in self.ordered_candidate_snapshots)
        related_ids = tuple(item.candidate_entry_id for item in self.ordered_relatedness_snapshots)
        if claim_ids != candidate_ids or claim_ids != related_ids or len(claim_ids) != len(set(claim_ids)):
            raise ValueError("governance batch candidate/claim/relatedness join mismatch")
        if any(item.status is not GovernanceCandidateClaimStatus.PREPARING or item.governance_batch_id != self.governance_batch_id for item in self.ordered_preparing_claims):
            raise ValueError("governance batch requires matching preparing claims")
        if tuple(sorted(set(self.allowed_scopes))) != self.allowed_scopes:
            raise ValueError("governance allowed scopes must be sorted and unique")
        if self.model_input.governance_batch_id != self.governance_batch_id or self.final_model_visible_input_fingerprint != self.model_input.provider_neutral_context_fingerprint:
            raise ValueError("governance batch model input identity mismatch")
        if any(item.source_evidence_attribution.authority_ledger_through_sequence != self.source_ledger_through_sequence for item in self.ordered_candidate_snapshots):
            raise ValueError("governance batch source high-water mismatch")
        return self


class GovernanceBatchInputReferenceFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_batch_input_reference.v1"]
    governance_batch_id: str = Field(min_length=1, max_length=256)
    artifact_id: str = Field(min_length=1, max_length=256)
    artifact_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_utf8_bytes: int = Field(ge=1, le=2 * 1024 * 1024)
    artifact_contract_id: str = Field(min_length=1, max_length=128)
    artifact_contract_version: str = Field(min_length=1, max_length=64)
    artifact_contract_fingerprint: Fingerprint
    batch_input_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class GovernanceModelInputAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_model_input_attribution.v1"]
    governance_batch_prepared_event_reference: GovernanceStoredEventReferenceFact
    batch_input_reference: GovernanceBatchInputReferenceFact
    resolved_model_call_id: str = Field(min_length=1, max_length=256)
    target_fingerprint: Fingerprint
    final_model_visible_input_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


class GovernanceDerivedWriteAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_derived_write_attribution.v1"]
    parent_candidate_entry_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    governance_batch_id: str = Field(min_length=1, max_length=256)
    batch_input_fingerprint: Fingerprint
    decision_id: str = Field(min_length=1, max_length=256)
    decision_payload_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


_SCHEMAS: tuple[tuple[str, str, str], ...] = (
    ("governance_stored_event_reference.v1", "reference_fingerprint", "governance-stored-event-reference:v1"),
    ("governance_evidence_artifact_reference.v1", "reference_fingerprint", "governance-evidence-artifact-reference:v1"),
    ("governance_batch_input_artifact_contract.v1", "contract_fingerprint", "governance-batch-input-artifact-contract:v1"),
    ("candidate_quoted_evidence_locator.v1", "locator_fingerprint", "candidate-quoted-evidence-locator:v1"),
    ("governance_candidate_payload_semantic.v1", "payload_semantic_fingerprint", "governance-candidate-payload-semantic:v1"),
    ("governance_candidate_attribution.v1", "attribution_fingerprint", "governance-candidate-attribution:v1"),
    ("governance_quoted_evidence_semantic.v1", "semantic_fingerprint", "governance-quoted-evidence-semantic:v1"),
    ("governance_quoted_evidence_attribution.v1", "attribution_fingerprint", "governance-quoted-evidence-attribution:v1"),
    ("main_agent_tool_governance_source_semantic.v1", "semantic_fingerprint", "governance-main-tool-source-semantic:v1"),
    ("reflection_governance_source_semantic.v1", "semantic_fingerprint", "governance-reflection-source-semantic:v1"),
    ("compaction_memory_candidate_extractor_contract.v1", "contract_fingerprint", "compaction-memory-candidate-extractor-contract:v1"),
    ("compaction_governance_source_semantic.v1", "semantic_fingerprint", "governance-compaction-source-semantic:v1"),
    ("governance_source_evidence_attribution.v1", "fact_fingerprint", "governance-source-attribution:v1"),
    ("governance_prompt_evidence_text.v1", "text_fingerprint", "governance-prompt-evidence-text:v1"),
    ("governance_candidate_prompt_payload.v1", "payload_fingerprint", "governance-candidate-prompt-payload:v1"),
    ("governance_evidence_prompt_projection.v1", "projection_fingerprint", "governance-evidence-prompt-projection:v1"),
    ("immutable_governance_candidate_snapshot.v1", "candidate_snapshot_fingerprint", "governance-immutable-candidate-snapshot:v1"),
    ("governance_evidence_prompt_projection_contract.v1", "contract_fingerprint", "governance-evidence-prompt-projection-contract:v1"),
    ("governance_related_memory_semantic.v1", "semantic_fingerprint", "governance-related-memory-semantic:v1"),
    ("governance_related_memory_prompt_projection.v1", "projection_fingerprint", "governance-related-memory-prompt-projection:v1"),
    ("governance_relatedness_candidate.v1", "fact_fingerprint", "governance-relatedness-candidate:v1"),
    ("governance_relatedness_snapshot.v1", "snapshot_fingerprint", "governance-relatedness-snapshot:v1"),
    ("reflection_candidate_attribution.v1", "attribution_fingerprint", "reflection-candidate-attribution:v1"),
    ("compaction_candidate_attribution.v1", "attribution_fingerprint", "compaction-candidate-attribution:v1"),
    ("candidate_projection_outbox_item.v1", "item_fingerprint", "candidate-projection-outbox-item:v1"),
    ("main_agent_memory_candidate_builder_contract.v1", "contract_fingerprint", "main-agent-memory-candidate-builder-contract:v1"),
    ("governance_evidence_build_result.v1", "result_fingerprint", "governance-evidence-build-result:v1"),
    ("memory_candidate_evidence_rejected.v1", "rejection_fingerprint", "memory-candidate-evidence-rejected:v1"),
    ("memory_governance_candidate_claim.v1", "claim_fingerprint", "memory-governance-candidate-claim:v1"),
    ("governance_system_prompt_contract.v1", "contract_fingerprint", "governance-system-prompt-contract:v1"),
    ("governance_frozen_llm_tool_call.v1", "semantic_fingerprint", "governance-frozen-llm-tool-call:v1"),
    ("governance_frozen_llm_message.v1", "message_fingerprint", "governance-frozen-llm-message:v1"),
    ("governance_model_input.v1", "model_input_fingerprint", "governance-model-input:v1"),
    ("governance_batch_input_snapshot.v1", "batch_input_fingerprint", "governance-batch-input-snapshot:v1"),
    ("governance_batch_input_reference.v1", "reference_fingerprint", "governance-batch-input-reference:v1"),
    ("governance_model_input_attribution.v1", "attribution_fingerprint", "governance-model-input-attribution:v1"),
    ("governance_derived_write_attribution.v1", "attribution_fingerprint", "governance-derived-write-attribution:v1"),
)

for _schema_version, _fingerprint_field, _domain in _SCHEMAS:
    register_durable_fact(
        schema_version=_schema_version,
        own_fingerprint_field=_fingerprint_field,
        domain_separator=_domain,
    )


__all__ = [name for name in globals() if not name.startswith("_")]
