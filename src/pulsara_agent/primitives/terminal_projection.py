"""Lossless terminal model/tool projection contracts."""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import FrozenJsonObjectFact
from pulsara_agent.primitives.authority_materialization import Fingerprint
from pulsara_agent.primitives._context_base import ToolArgumentsParseErrorCode
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    StableEventIdentityFact,
    register_durable_fact,
)
from pulsara_agent.primitives.model_call import (
    ModelTokenUsageFact,
    ProviderSanitizedErrorFact,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    ContextToolResultArtifactRefFact,
    ToolResultExecutionSemanticsFact,
    ToolResultStateFact,
)


NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class DataMediaTypeNormalizationRuleFact(FrozenFactBase):
    schema_version: Literal["data_media_type_normalization_rule.v1"]
    source_kind: Literal["typed_text", "typed_json", "typed_data", "unknown_data"]
    type_subtype_case: Literal["lowercase"]
    parameter_name_case: Literal["lowercase"]
    parameter_order: Literal["lexicographic"]
    parameter_whitespace: Literal["trim_ows"]
    charset_normalization: Literal["lowercase_ascii", "not_applicable"]
    invalid_media_type_outcome: Literal["reject", "application_octet_stream"]
    rule_fingerprint: Fingerprint


class DataMediaTypeNormalizationContractFact(FrozenFactBase):
    schema_version: Literal["data_media_type_normalization_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    rules: tuple[DataMediaTypeNormalizationRuleFact, ...]
    max_input_media_type_utf8_bytes: PositiveInt
    max_normalized_media_type_utf8_bytes: PositiveInt
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _rules(self) -> "DataMediaTypeNormalizationContractFact":
        keys = tuple(item.source_kind for item in self.rules)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("media type rules must be sorted and unique")
        return self


class TerminalContentCanonicalizationContractFact(FrozenFactBase):
    schema_version: Literal["terminal_content_canonicalization_contract.v2"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    text_media_type: Literal["text/plain; charset=utf-8"]
    thinking_media_type: Literal["text/plain; charset=utf-8"]
    canonical_json_media_type: Literal["application/json"]
    text_encoding: Literal["utf-8"]
    unicode_normalization: Literal["preserve"]
    newline_normalization: Literal["preserve"]
    digest_algorithm: Literal["sha256"]
    data_media_type_normalization_contract: DataMediaTypeNormalizationContractFact
    contract_fingerprint: Fingerprint


class TerminalContentArtifactCodecContractFact(FrozenFactBase):
    schema_version: Literal["terminal_content_artifact_codec_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    codec: Literal["identity_utf8"]
    artifact_service_contract_fingerprint: Fingerprint
    max_artifact_bytes: PositiveInt
    contract_fingerprint: Fingerprint


class TerminalContentSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_content_semantic.v2"]
    canonical_content_sha256: Fingerprint
    utf8_bytes: NonNegativeInt
    media_type: str = Field(min_length=1, max_length=256)
    content_canonicalization_contract_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint


class TerminalInlineContentFact(FrozenFactBase):
    schema_version: Literal["terminal_inline_content.v2"]
    storage_kind: Literal["inline"]
    semantic_identity: TerminalContentSemanticFact
    text: str
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_identity(self) -> "TerminalInlineContentFact":
        encoded = self.text.encode("utf-8")
        digest = f"sha256:{sha256(encoded).hexdigest()}"
        if self.semantic_identity.canonical_content_sha256 != digest:
            raise ValueError("inline terminal content SHA mismatch")
        if self.semantic_identity.utf8_bytes != len(encoded):
            raise ValueError("inline terminal content byte count mismatch")
        return self


class TerminalArtifactContentReferenceFact(FrozenFactBase):
    schema_version: Literal["terminal_artifact_content_ref.v2"]
    storage_kind: Literal["artifact"]
    semantic_identity: TerminalContentSemanticFact
    artifact_id: str = Field(min_length=1, max_length=256)
    artifact_sha256: Fingerprint
    artifact_bytes: NonNegativeInt
    media_type: str = Field(min_length=1, max_length=256)
    artifact_codec: Literal["identity_utf8"]
    artifact_codec_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_identity(self) -> "TerminalArtifactContentReferenceFact":
        semantic = self.semantic_identity
        if self.artifact_sha256 != semantic.canonical_content_sha256:
            raise ValueError("terminal artifact SHA mismatch")
        if self.artifact_bytes != semantic.utf8_bytes:
            raise ValueError("terminal artifact byte count mismatch")
        if self.media_type != semantic.media_type:
            raise ValueError("terminal artifact media type mismatch")
        return self


TerminalContentFact: TypeAlias = Annotated[
    TerminalInlineContentFact | TerminalArtifactContentReferenceFact,
    Field(discriminator="storage_kind"),
]


class ModelTextBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["model_text_block_semantic.v1"]
    block_kind: Literal["text"]
    block_id: str = Field(min_length=1, max_length=128)
    block_index: NonNegativeInt
    projection_order: NonNegativeInt
    completion_status: Literal["completed", "interrupted"]
    content_semantic_identity: TerminalContentSemanticFact
    semantic_fingerprint: Fingerprint


class ModelThinkingBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["model_thinking_block_semantic.v1"]
    block_kind: Literal["thinking"]
    block_id: str = Field(min_length=1, max_length=128)
    block_index: NonNegativeInt
    projection_order: NonNegativeInt
    completion_status: Literal["completed", "interrupted"]
    content_semantic_identity: TerminalContentSemanticFact
    semantic_fingerprint: Fingerprint


class ModelDataBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["model_data_block_semantic.v1"]
    block_kind: Literal["data"]
    block_id: str = Field(min_length=1, max_length=128)
    block_index: NonNegativeInt
    projection_order: NonNegativeInt
    media_type: str = Field(min_length=1, max_length=256)
    completion_status: Literal["completed", "interrupted"]
    content_semantic_identity: TerminalContentSemanticFact
    semantic_fingerprint: Fingerprint


class ModelToolCallBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["model_tool_call_block_semantic.v1"]
    block_kind: Literal["tool_call"]
    block_id: str = Field(min_length=1, max_length=128)
    block_index: NonNegativeInt
    projection_order: NonNegativeInt
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=256)
    completion_status: Literal["completed", "interrupted"]
    arguments_status: Literal["valid_object", "invalid_json", "non_object_json"]
    parsed_arguments: FrozenJsonObjectFact | None
    parse_error_code: ToolArgumentsParseErrorCode | None
    raw_arguments_json: str
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _argument_state(self) -> "ModelToolCallBlockSemanticFact":
        if self.arguments_status == "valid_object":
            if self.parsed_arguments is None or self.parse_error_code is not None:
                raise ValueError("valid tool arguments require parsed object only")
        else:
            if self.parsed_arguments is not None or self.parse_error_code is None:
                raise ValueError("invalid tool arguments require parse error only")
            expected = (
                ToolArgumentsParseErrorCode.INVALID_JSON_SYNTAX
                if self.arguments_status == "invalid_json"
                else ToolArgumentsParseErrorCode.JSON_ROOT_NOT_OBJECT
            )
            if self.parse_error_code is not expected:
                raise ValueError("tool arguments parse error code mismatch")
        return self


class ModelProviderErrorSemanticFact(FrozenFactBase):
    schema_version: Literal["model_provider_error_semantic.v1"]
    block_kind: Literal["provider_error"]
    projection_order: NonNegativeInt
    stable_error_code: str = Field(min_length=1, max_length=128)
    sanitized_diagnostics: Annotated[tuple[str, ...], Field(max_length=8)]
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounded_diagnostics(self) -> "ModelProviderErrorSemanticFact":
        if any(len(item.encode("utf-8")) > 1_024 for item in self.sanitized_diagnostics):
            raise ValueError("provider diagnostic exceeds UTF-8 byte bound")
        return self


ModelProjectionItemSemanticFact: TypeAlias = Annotated[
    ModelTextBlockSemanticFact
    | ModelThinkingBlockSemanticFact
    | ModelDataBlockSemanticFact
    | ModelToolCallBlockSemanticFact
    | ModelProviderErrorSemanticFact,
    Field(discriminator="block_kind"),
]


class ModelProjectionItemFact(FrozenFactBase):
    schema_version: Literal["model_projection_item.v2"]
    semantic_identity: ModelProjectionItemSemanticFact
    content: TerminalContentFact | None
    source_start_sequence: PositiveInt
    source_end_sequence: PositiveInt | None
    provider_error: ProviderSanitizedErrorFact | None
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_join(self) -> "ModelProjectionItemFact":
        semantic = self.semantic_identity
        if isinstance(
            semantic,
            ModelTextBlockSemanticFact
            | ModelThinkingBlockSemanticFact
            | ModelDataBlockSemanticFact,
        ):
            if self.content is None:
                raise ValueError("content-bearing model block requires content")
            if self.content.semantic_identity != semantic.content_semantic_identity:
                raise ValueError("model block content semantic identity mismatch")
            if isinstance(semantic, ModelDataBlockSemanticFact):
                if semantic.media_type != self.content.semantic_identity.media_type:
                    raise ValueError("model data media type mismatch")
        elif self.content is not None:
            raise ValueError("tool-call/provider-error item cannot carry content")
        if isinstance(semantic, ModelProviderErrorSemanticFact):
            if (
                self.provider_error is None
                or self.source_end_sequence != self.source_start_sequence
                or self.provider_error.code.value != semantic.stable_error_code
                or tuple(
                    item.diagnostic_fingerprint
                    for item in self.provider_error.diagnostics
                )
                != semantic.sanitized_diagnostics
            ):
                raise ValueError("provider-error projection fact attribution mismatch")
        elif self.provider_error is not None:
            raise ValueError("non-provider-error item cannot carry provider error")
        completion_status = getattr(semantic, "completion_status", "completed")
        if (completion_status == "completed") != (self.source_end_sequence is not None):
            raise ValueError("model projection source end/completion mismatch")
        if (
            self.source_end_sequence is not None
            and self.source_end_sequence < self.source_start_sequence
        ):
            raise ValueError("model projection source sequence range is reversed")
        return self


class ModelCallSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["model_call_semantic_source.v1"]
    resolved_model_call_id: str = Field(min_length=1, max_length=128)
    model_call_start_event_identity: StableEventIdentityFact
    source_semantic_item_count: NonNegativeInt
    source_first_transport_index: int | None = Field(default=None, ge=0)
    source_last_transport_index: int | None = Field(default=None, ge=0)
    source_semantic_accumulator: Fingerprint
    model_stream_semantic_domain_contract_fingerprint: Fingerprint
    reducer_contract_fingerprint: Fingerprint
    source_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _source_range(self) -> "ModelCallSemanticSourceFact":
        first = self.source_first_transport_index
        last = self.source_last_transport_index
        if self.source_semantic_item_count == 0:
            if first is not None or last is not None:
                raise ValueError("empty model source cannot carry transport range")
        elif first is None or last is None or last - first + 1 != (
            self.source_semantic_item_count
        ):
            raise ValueError("model source transport range mismatch")
        return self


class ModelTerminalProjectionSemanticFact(FrozenFactBase):
    schema_version: Literal["model_terminal_projection_semantic.v1"]
    projection_kind: Literal["model_call"]
    terminal_outcome: Literal[
        "completed", "provider_error", "cancelled", "runtime_error"
    ]
    ordered_item_semantic_fingerprints: tuple[Fingerprint, ...]
    semantic_fingerprint: Fingerprint


class CanonicalToolResultTextBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["canonical_tool_result_text_block_semantic.v1"]
    content_kind: Literal["text"]
    block_id: str = Field(min_length=1, max_length=128)
    block_index: NonNegativeInt
    content_semantic_identity: TerminalContentSemanticFact
    semantic_fingerprint: Fingerprint


class CanonicalToolResultDataBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["canonical_tool_result_data_block_semantic.v2"]
    content_kind: Literal["data"]
    block_id: str = Field(min_length=1, max_length=128)
    block_index: NonNegativeInt
    name: str | None = Field(default=None, max_length=256)
    media_type: str = Field(min_length=1, max_length=256)
    source_kind: str = Field(min_length=1, max_length=128)
    content_semantic_identity: TerminalContentSemanticFact | None
    artifact_content_fingerprints: tuple[Fingerprint, ...]
    semantic_fingerprint: Fingerprint


CanonicalToolResultContentBlockSemanticFact: TypeAlias = Annotated[
    CanonicalToolResultTextBlockSemanticFact
    | CanonicalToolResultDataBlockSemanticFact,
    Field(discriminator="content_kind"),
]


class CanonicalToolResultContentBlockFact(FrozenFactBase):
    schema_version: Literal["canonical_tool_result_content_block.v1"]
    semantic_identity: CanonicalToolResultContentBlockSemanticFact
    content: TerminalContentFact | None
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_join(self) -> "CanonicalToolResultContentBlockFact":
        semantic = self.semantic_identity
        if isinstance(semantic, CanonicalToolResultTextBlockSemanticFact):
            if self.content is None:
                raise ValueError("tool text block requires content")
            if self.content.semantic_identity != semantic.content_semantic_identity:
                raise ValueError("tool text content semantic identity mismatch")
        else:
            if self.content is None:
                if semantic.content_semantic_identity is not None:
                    raise ValueError("artifact-only tool data cannot carry inline identity")
                if not semantic.artifact_content_fingerprints:
                    raise ValueError("tool data requires content or artifact identity")
            elif self.content.semantic_identity != semantic.content_semantic_identity:
                raise ValueError("tool data content semantic identity mismatch")
        return self


class CanonicalToolResultBlockSemanticFact(FrozenFactBase):
    schema_version: Literal["canonical_tool_result_block_semantic.v1"]
    tool_call_id: str = Field(min_length=1, max_length=128)
    model_tool_name: str = Field(min_length=1, max_length=256)
    result_state: ToolResultStateFact
    ordered_content_semantic_fingerprints: tuple[Fingerprint, ...]
    artifact_content_fingerprints: tuple[Fingerprint, ...]
    semantic_fingerprint: Fingerprint


class CanonicalToolResultBlockFact(FrozenFactBase):
    schema_version: Literal["canonical_tool_result_block.v1"]
    semantic_identity: CanonicalToolResultBlockSemanticFact
    content_blocks: tuple[CanonicalToolResultContentBlockFact, ...]
    artifact_refs: tuple[ContextToolResultArtifactRefFact, ...]
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ordered_content_join(self) -> "CanonicalToolResultBlockFact":
        expected = tuple(
            item.semantic_identity.semantic_fingerprint for item in self.content_blocks
        )
        if self.semantic_identity.ordered_content_semantic_fingerprints != expected:
            raise ValueError("tool result ordered content fingerprint mismatch")
        artifact_fingerprints = tuple(item.ref_fingerprint for item in self.artifact_refs)
        if self.semantic_identity.artifact_content_fingerprints != artifact_fingerprints:
            raise ValueError("tool result artifact fingerprint mismatch")
        return self


class ToolTerminalProjectionSemanticFact(FrozenFactBase):
    schema_version: Literal["tool_terminal_projection_semantic.v1"]
    projection_kind: Literal["tool_result"]
    canonical_result_block_semantic: CanonicalToolResultBlockSemanticFact
    execution_semantics: ToolResultExecutionSemanticsFact
    observation_timing: ToolObservationTimingFact
    semantic_artifact_content_fingerprints: tuple[Fingerprint, ...]
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _result_state(self) -> "ToolTerminalProjectionSemanticFact":
        if (
            self.execution_semantics.result_state
            is not self.canonical_result_block_semantic.result_state
        ):
            raise ValueError("tool projection result state mismatch")
        if (
            self.semantic_artifact_content_fingerprints
            != self.canonical_result_block_semantic.artifact_content_fingerprints
        ):
            raise ValueError("tool projection artifact semantics mismatch")
        return self


TerminalProjectionSemanticIdentityFact: TypeAlias = Annotated[
    ModelTerminalProjectionSemanticFact | ToolTerminalProjectionSemanticFact,
    Field(discriminator="projection_kind"),
]


class ModelTerminalProjectionSemanticJoinFact(FrozenFactBase):
    schema_version: Literal["model_terminal_projection_semantic_join.v1"]
    projection_kind: Literal["model_call"]
    terminal_outcome: Literal[
        "completed", "provider_error", "cancelled", "runtime_error"
    ]
    projection_item_count: NonNegativeInt
    semantic_fingerprint: Fingerprint


class ToolTerminalProjectionSemanticJoinFact(FrozenFactBase):
    schema_version: Literal["tool_terminal_projection_semantic_join.v1"]
    projection_kind: Literal["tool_result"]
    tool_call_id: str = Field(min_length=1, max_length=128)
    model_tool_name: str = Field(min_length=1, max_length=256)
    result_state: ToolResultStateFact
    semantic_fingerprint: Fingerprint


TerminalProjectionSemanticJoinFact: TypeAlias = Annotated[
    ModelTerminalProjectionSemanticJoinFact
    | ToolTerminalProjectionSemanticJoinFact,
    Field(discriminator="projection_kind"),
]


class ModelTerminalProjectionPayloadFact(FrozenFactBase):
    schema_version: Literal["model_terminal_projection_payload.v2"]
    projection_kind: Literal["model_call"]
    items: tuple[ModelProjectionItemFact, ...]

    @model_validator(mode="after")
    def _projection_order(self) -> "ModelTerminalProjectionPayloadFact":
        orders = tuple(item.semantic_identity.projection_order for item in self.items)
        if orders != tuple(sorted(orders)) or len(orders) != len(set(orders)):
            raise ValueError("model projection order must be strictly increasing")
        return self


class ToolTerminalProjectionPayloadFact(FrozenFactBase):
    schema_version: Literal["tool_terminal_projection_payload.v2"]
    projection_kind: Literal["tool_result"]
    canonical_result_block: CanonicalToolResultBlockFact


TerminalProjectionPayloadFact: TypeAlias = Annotated[
    ModelTerminalProjectionPayloadFact | ToolTerminalProjectionPayloadFact,
    Field(discriminator="projection_kind"),
]


class ToolResultSemanticSourceFact(FrozenFactBase):
    schema_version: Literal["tool_result_semantic_source.v1"]
    source_kind: Literal["tool_result_stream", "external_requirement"]
    tool_call_id: str = Field(min_length=1, max_length=128)
    source_event_identity: StableEventIdentityFact
    source_delta_count: NonNegativeInt
    source_first_delta_index: int | None = Field(default=None, ge=0)
    source_last_delta_index: int | None = Field(default=None, ge=0)
    source_semantic_accumulator: Fingerprint
    tool_result_semantic_domain_contract_fingerprint: Fingerprint
    reducer_contract_fingerprint: Fingerprint
    source_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _source_range(self) -> "ToolResultSemanticSourceFact":
        expected_event_type = {
            "tool_result_stream": "TOOL_RESULT_START",
            "external_requirement": "REQUIRE_EXTERNAL_EXECUTION",
        }[self.source_kind]
        if self.source_event_identity.event_type != expected_event_type:
            raise ValueError("tool result semantic source event type mismatch")
        first = self.source_first_delta_index
        last = self.source_last_delta_index
        if self.source_kind == "external_requirement" and self.source_delta_count != 0:
            raise ValueError("external result source cannot carry tool deltas")
        if self.source_delta_count == 0:
            if first is not None or last is not None:
                raise ValueError("empty tool source cannot carry delta range")
        elif first is None or last is None or last - first + 1 != self.source_delta_count:
            raise ValueError("tool result delta range mismatch")
        return self


class TerminalProjectionDocumentContractFact(FrozenFactBase):
    schema_version: Literal["terminal_projection_document_contract.v2"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    max_document_bytes: PositiveInt
    max_model_blocks: PositiveInt
    max_inline_content_bytes_per_block: PositiveInt
    max_tool_artifact_refs: NonNegativeInt
    max_sanitized_diagnostics: NonNegativeInt
    max_sanitized_diagnostic_bytes: NonNegativeInt
    document_canonicalization_contract_fingerprint: Fingerprint
    content_canonicalization_contract_fingerprint: Fingerprint
    artifact_codec_contract_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


class TerminalProjectionDocumentFact(FrozenFactBase):
    schema_version: Literal["terminal_projection_document.v2"]
    document_contract_fingerprint: Fingerprint
    semantic_identity: TerminalProjectionSemanticIdentityFact
    payload: TerminalProjectionPayloadFact
    source_fact: ModelCallSemanticSourceFact | ToolResultSemanticSourceFact
    usage_status: Literal["reported", "missing"] | None
    usage: ModelTokenUsageFact | None
    reported_model_id: str | None = Field(default=None, max_length=256)
    tool_result_artifact_refs: tuple[ContextToolResultArtifactRefFact, ...]
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _document_join(self) -> "TerminalProjectionDocumentFact":
        if self.semantic_identity.projection_kind != self.payload.projection_kind:
            raise ValueError("terminal document semantic/payload kind mismatch")
        if self.semantic_identity.projection_kind == "model_call":
            if not isinstance(self.source_fact, ModelCallSemanticSourceFact):
                raise ValueError("model document requires model source")
            if self.usage_status == "reported" and self.usage is None:
                raise ValueError("reported model usage requires usage fact")
            if self.usage_status == "missing" and self.usage is not None:
                raise ValueError("missing model usage cannot carry usage fact")
            if self.usage_status is None:
                raise ValueError("model document requires usage status")
            if self.tool_result_artifact_refs:
                raise ValueError("model document cannot carry tool artifacts")
            assert isinstance(self.semantic_identity, ModelTerminalProjectionSemanticFact)
            assert isinstance(self.payload, ModelTerminalProjectionPayloadFact)
            expected = tuple(
                item.semantic_identity.semantic_fingerprint
                for item in self.payload.items
            )
            if self.semantic_identity.ordered_item_semantic_fingerprints != expected:
                raise ValueError("model ordered semantic fingerprint mismatch")
            errors = tuple(
                item
                for item in self.payload.items
                if isinstance(item.semantic_identity, ModelProviderErrorSemanticFact)
            )
            if self.semantic_identity.terminal_outcome == "completed":
                if errors or any(
                    getattr(item.semantic_identity, "completion_status", "completed")
                    != "completed"
                    for item in self.payload.items
                ):
                    raise ValueError("completed model projection has incomplete items")
            elif self.semantic_identity.terminal_outcome == "provider_error":
                if len(errors) != 1 or errors[-1] != self.payload.items[-1]:
                    raise ValueError("provider error must be the final projection item")
            elif errors:
                raise ValueError("non-provider-error outcome cannot carry provider error")
        else:
            if not isinstance(self.source_fact, ToolResultSemanticSourceFact):
                raise ValueError("tool document requires tool source")
            if any(
                value is not None
                for value in (self.usage_status, self.usage, self.reported_model_id)
            ):
                raise ValueError("tool document cannot carry model usage")
            assert isinstance(self.payload, ToolTerminalProjectionPayloadFact)
            if self.payload.canonical_result_block.artifact_refs != (
                self.tool_result_artifact_refs
            ):
                raise ValueError("tool document artifact references mismatch")
        return self


class TerminalProjectionReferenceFact(FrozenFactBase):
    schema_version: Literal["terminal_projection_reference.v2"]
    projection_kind: Literal["model_call", "tool_result"]
    semantic_join: TerminalProjectionSemanticJoinFact
    document_fact_fingerprint: Fingerprint
    document_artifact_id: str = Field(min_length=1, max_length=256)
    document_sha256: Fingerprint
    document_byte_count: PositiveInt
    document_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _kind_join(self) -> "TerminalProjectionReferenceFact":
        if self.projection_kind != self.semantic_join.projection_kind:
            raise ValueError("terminal reference projection kind mismatch")
        return self


class ModelCallTerminalProjectionEndReferenceFact(FrozenFactBase):
    schema_version: Literal["model_call_terminal_projection_end_ref.v2"]
    projection_committed_event_identity: StableEventIdentityFact
    projection_reference: TerminalProjectionReferenceFact
    end_reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _model_projection(self) -> "ModelCallTerminalProjectionEndReferenceFact":
        if self.projection_reference.projection_kind != "model_call":
            raise ValueError("model End requires a model terminal projection")
        if (
            self.projection_committed_event_identity.event_type
            != "MODEL_CALL_TERMINAL_PROJECTION_COMMITTED"
        ):
            raise ValueError("model End projection event type mismatch")
        return self


class ToolResultTerminalProjectionEndReferenceFact(FrozenFactBase):
    schema_version: Literal["tool_result_terminal_projection_end_ref.v2"]
    projection_committed_event_identity: StableEventIdentityFact
    projection_reference: TerminalProjectionReferenceFact
    end_reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _tool_projection(self) -> "ToolResultTerminalProjectionEndReferenceFact":
        if self.projection_reference.projection_kind != "tool_result":
            raise ValueError("tool End requires a tool terminal projection")
        if (
            self.projection_committed_event_identity.event_type
            != "TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED"
        ):
            raise ValueError("tool End projection event type mismatch")
        return self


_OWN: tuple[tuple[str, str | None, str], ...] = (
    ("data_media_type_normalization_rule.v1", "rule_fingerprint", "data-media-type-normalization-rule:v1"),
    ("data_media_type_normalization_contract.v1", "contract_fingerprint", "data-media-type-normalization-contract:v1"),
    ("terminal_content_canonicalization_contract.v2", "contract_fingerprint", "terminal-content-canonicalization-contract:v2"),
    ("terminal_content_artifact_codec_contract.v1", "contract_fingerprint", "terminal-content-artifact-codec-contract:v1"),
    ("terminal_content_semantic.v2", "semantic_fingerprint", "terminal-content-semantic:v2"),
    ("terminal_inline_content.v2", "reference_fingerprint", "terminal-inline-content:v2"),
    ("terminal_artifact_content_ref.v2", "reference_fingerprint", "terminal-artifact-content-ref:v2"),
    ("model_text_block_semantic.v1", "semantic_fingerprint", "model-text-block-semantic:v1"),
    ("model_thinking_block_semantic.v1", "semantic_fingerprint", "model-thinking-block-semantic:v1"),
    ("model_data_block_semantic.v1", "semantic_fingerprint", "model-data-block-semantic:v1"),
    ("model_tool_call_block_semantic.v1", "semantic_fingerprint", "model-tool-call-block-semantic:v1"),
    ("model_provider_error_semantic.v1", "semantic_fingerprint", "model-provider-error-semantic:v1"),
    ("model_projection_item.v2", "fact_fingerprint", "model-projection-item:v2"),
    ("model_call_semantic_source.v1", "source_fingerprint", "model-call-semantic-source:v1"),
    ("model_terminal_projection_semantic.v1", "semantic_fingerprint", "model-terminal-projection-semantic:v1"),
    ("canonical_tool_result_text_block_semantic.v1", "semantic_fingerprint", "canonical-tool-result-text-block-semantic:v1"),
    ("canonical_tool_result_data_block_semantic.v2", "semantic_fingerprint", "canonical-tool-result-data-block-semantic:v2"),
    ("canonical_tool_result_content_block.v1", "fact_fingerprint", "canonical-tool-result-content-block:v1"),
    ("canonical_tool_result_block_semantic.v1", "semantic_fingerprint", "canonical-tool-result-block-semantic:v1"),
    ("canonical_tool_result_block.v1", "fact_fingerprint", "canonical-tool-result-block:v1"),
    ("tool_terminal_projection_semantic.v1", "semantic_fingerprint", "tool-terminal-projection-semantic:v1"),
    ("model_terminal_projection_semantic_join.v1", None, "model-terminal-projection-semantic-join:v1"),
    ("tool_terminal_projection_semantic_join.v1", None, "tool-terminal-projection-semantic-join:v1"),
    ("model_terminal_projection_payload.v2", None, "model-terminal-projection-payload:v2"),
    ("tool_terminal_projection_payload.v2", None, "tool-terminal-projection-payload:v2"),
    ("tool_result_semantic_source.v1", "source_fingerprint", "tool-result-semantic-source:v1"),
    ("terminal_projection_document_contract.v2", "contract_fingerprint", "terminal-projection-document-contract:v2"),
    ("terminal_projection_document.v2", "fact_fingerprint", "terminal-projection-document:v2"),
    ("terminal_projection_reference.v2", "reference_fingerprint", "terminal-projection-reference:v2"),
    ("model_call_terminal_projection_end_ref.v2", "end_reference_fingerprint", "model-call-terminal-projection-end-ref:v2"),
    ("tool_result_terminal_projection_end_ref.v2", "end_reference_fingerprint", "tool-result-terminal-projection-end-ref:v2"),
)

for _schema, _field, _domain in _OWN:
    register_durable_fact(
        schema_version=_schema,
        own_fingerprint_field=_field,
        domain_separator=_domain,
    )


__all__ = [
    "CanonicalToolResultBlockFact",
    "CanonicalToolResultBlockSemanticFact",
    "CanonicalToolResultContentBlockFact",
    "CanonicalToolResultContentBlockSemanticFact",
    "CanonicalToolResultDataBlockSemanticFact",
    "CanonicalToolResultTextBlockSemanticFact",
    "DataMediaTypeNormalizationContractFact",
    "DataMediaTypeNormalizationRuleFact",
    "ModelCallSemanticSourceFact",
    "ModelCallTerminalProjectionEndReferenceFact",
    "ModelDataBlockSemanticFact",
    "ModelProjectionItemFact",
    "ModelProjectionItemSemanticFact",
    "ModelProviderErrorSemanticFact",
    "ModelTerminalProjectionPayloadFact",
    "ModelTerminalProjectionSemanticFact",
    "ModelTerminalProjectionSemanticJoinFact",
    "ModelTextBlockSemanticFact",
    "ModelThinkingBlockSemanticFact",
    "ModelToolCallBlockSemanticFact",
    "TerminalArtifactContentReferenceFact",
    "TerminalContentArtifactCodecContractFact",
    "TerminalContentCanonicalizationContractFact",
    "TerminalContentFact",
    "TerminalContentSemanticFact",
    "TerminalInlineContentFact",
    "TerminalProjectionDocumentContractFact",
    "TerminalProjectionDocumentFact",
    "TerminalProjectionPayloadFact",
    "TerminalProjectionReferenceFact",
    "TerminalProjectionSemanticIdentityFact",
    "TerminalProjectionSemanticJoinFact",
    "ToolResultSemanticSourceFact",
    "ToolResultTerminalProjectionEndReferenceFact",
    "ToolTerminalProjectionPayloadFact",
    "ToolTerminalProjectionSemanticFact",
    "ToolTerminalProjectionSemanticJoinFact",
]
