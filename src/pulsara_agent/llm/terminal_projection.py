"""Incremental, lossless terminal projections for committed model streams."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import TYPE_CHECKING, Literal

from pulsara_agent.event import (
    AgentEvent,
    DataBlockSegmentEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    PhysicalOperationChargeAppliedEvent,
    ProviderModelStreamErrorEvent,
    TextBlockSegmentEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockSegmentEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.event_log.serialization import (
    canonical_event_payload_bytes,
    freeze_event_write_candidate,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.primitives import context_fingerprint, freeze_json
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    ToolArgumentsParseErrorCode,
    canonical_json_bytes,
)
from pulsara_agent.primitives.frozen import (
    StableEventIdentityFact,
    build_frozen_fact,
)
from pulsara_agent.primitives.model_call import (
    ModelStreamSemanticCommitMeasurementFact,
    ModelStreamSettlementMeasurementFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.terminal_projection import (
    DataMediaTypeNormalizationContractFact,
    DataMediaTypeNormalizationRuleFact,
    ModelCallSemanticSourceFact,
    ModelCallTerminalProjectionEndReferenceFact,
    ModelDataBlockSemanticFact,
    ModelProjectionItemFact,
    ModelProviderErrorSemanticFact,
    ModelTerminalProjectionPayloadFact,
    ModelTerminalProjectionSemanticFact,
    ModelTerminalProjectionSemanticJoinFact,
    ModelTextBlockSemanticFact,
    ModelThinkingBlockSemanticFact,
    ModelToolCallBlockSemanticFact,
    TerminalContentArtifactCodecContractFact,
    TerminalContentCanonicalizationContractFact,
    TerminalContentSemanticFact,
    TerminalInlineContentFact,
    TerminalProjectionDocumentContractFact,
    TerminalProjectionDocumentFact,
    TerminalProjectionReferenceFact,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


TERMINAL_PROJECTION_MEDIA_TYPE = (
    "application/vnd.pulsara.terminal-projection+json; version=2"
)


class TerminalProjectionPersistenceContractError(RuntimeError):
    """A content-addressed terminal document failed deterministic confirmation."""


MODEL_TERMINAL_PROJECTION_REDUCER_CONTRACT_FINGERPRINT = context_fingerprint(
    "model-terminal-projection-reducer-contract:v2",
    {
        "cursor": "source-span+durable-index-contiguous:v2",
        "block_assembly": "typed-start-segment-end-interrupted:v2",
        "projection_order": "first-source-item-order:v1",
        "source_accumulator": "sanitized-source-receipt-chain:v2",
        "durable_accumulator": "canonical-segment-event-chain:v1",
    },
)


@dataclass(frozen=True, slots=True)
class TerminalProjectionContractBundle:
    media_type_normalization: DataMediaTypeNormalizationContractFact
    content_canonicalization: TerminalContentCanonicalizationContractFact
    artifact_codec: TerminalContentArtifactCodecContractFact
    document: TerminalProjectionDocumentContractFact


@dataclass(frozen=True, slots=True)
class PreparedModelTerminalProjection:
    document: TerminalProjectionDocumentFact
    canonical_document_bytes: bytes
    projection_reference: TerminalProjectionReferenceFact
    committed_event: ModelCallTerminalProjectionCommittedEvent
    end_reference: ModelCallTerminalProjectionEndReferenceFact


def build_default_terminal_projection_contract_bundle() -> (
    TerminalProjectionContractBundle
):
    rules = tuple(
        build_frozen_fact(
            DataMediaTypeNormalizationRuleFact,
            schema_version="data_media_type_normalization_rule.v1",
            source_kind=source_kind,
            type_subtype_case="lowercase",
            parameter_name_case="lowercase",
            parameter_order="lexicographic",
            parameter_whitespace="trim_ows",
            charset_normalization=(
                "lowercase_ascii"
                if source_kind in {"typed_text", "typed_json"}
                else "not_applicable"
            ),
            invalid_media_type_outcome="reject",
        )
        for source_kind in (
            "typed_data",
            "typed_json",
            "typed_text",
            "unknown_data",
        )
    )
    media_type = build_frozen_fact(
        DataMediaTypeNormalizationContractFact,
        schema_version="data_media_type_normalization_contract.v1",
        contract_id="pulsara.data-media-type-normalization",
        contract_version="1",
        rules=rules,
        max_input_media_type_utf8_bytes=256,
        max_normalized_media_type_utf8_bytes=256,
    )
    canonicalization = build_frozen_fact(
        TerminalContentCanonicalizationContractFact,
        schema_version="terminal_content_canonicalization_contract.v2",
        contract_id="pulsara.terminal-content-canonicalization",
        contract_version="2",
        text_media_type="text/plain; charset=utf-8",
        thinking_media_type="text/plain; charset=utf-8",
        canonical_json_media_type="application/json",
        text_encoding="utf-8",
        unicode_normalization="preserve",
        newline_normalization="preserve",
        digest_algorithm="sha256",
        data_media_type_normalization_contract=media_type,
    )
    codec = build_frozen_fact(
        TerminalContentArtifactCodecContractFact,
        schema_version="terminal_content_artifact_codec_contract.v1",
        contract_id="pulsara.terminal-content-artifact-codec",
        contract_version="1",
        codec="identity_utf8",
        artifact_service_contract_fingerprint=context_fingerprint(
            "terminal-projection-artifact-service-contract:v1",
            "put-if-absent-or-confirm-identical+session-owned-bounded-io",
        ),
        max_artifact_bytes=16 * 1024 * 1024,
    )
    document = build_frozen_fact(
        TerminalProjectionDocumentContractFact,
        schema_version="terminal_projection_document_contract.v2",
        contract_id="pulsara.terminal-projection-document",
        contract_version="2",
        max_document_bytes=32 * 1024 * 1024,
        max_model_blocks=16_384,
        max_inline_content_bytes_per_block=16 * 1024 * 1024,
        max_tool_artifact_refs=128,
        max_sanitized_diagnostics=8,
        max_sanitized_diagnostic_bytes=1_024,
        document_canonicalization_contract_fingerprint=context_fingerprint(
            "terminal-projection-document-canonicalization:v2",
            "canonical-json-utf8:pydantic-json-mode:v1",
        ),
        content_canonicalization_contract_fingerprint=(
            canonicalization.contract_fingerprint
        ),
        artifact_codec_contract_fingerprint=codec.contract_fingerprint,
    )
    return TerminalProjectionContractBundle(
        media_type_normalization=media_type,
        content_canonicalization=canonicalization,
        artifact_codec=codec,
        document=document,
    )


class ModelTerminalProjectionReducer:
    """Pure process-local fold over one call's committed semantic prefix."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        start_event: ModelCallStartEvent,
        contracts: TerminalProjectionContractBundle,
        model_stream_semantic_domain_contract_fingerprint: str,
        segment_policy_contract_fingerprint: str,
    ) -> None:
        if start_event.sequence is None:
            raise ValueError("model projection reducer requires committed Start")
        self._runtime_session_id = runtime_session_id
        self._start = start_event
        self._contracts = contracts
        self._domain_fingerprint = (
            model_stream_semantic_domain_contract_fingerprint
        )
        self._semantic_count = 0
        self._source_accumulator = context_fingerprint(
            "model-stream-sanitized-source:v2", "empty"
        )
        self._durable_event_count = 0
        self._durable_event_accumulator = context_fingerprint(
            "model-terminal-durable-event-accumulator:v1", "empty"
        )
        self._segment_policy_contract_fingerprint = (
            segment_policy_contract_fingerprint
        )
        self._semantic_event_identities: list[StableEventIdentityFact] = []
        self._adapter_source_item_count = 0
        self._adapter_source_payload_bytes = 0
        self._synthetic_source_item_count = 0
        self._synthetic_source_payload_bytes = 0
        self._singleton_event_count = 0
        self._segment_event_count = 0
        self._segment_content_utf8_bytes = 0
        self._segment_candidate_payload_bytes = 0
        self._singleton_candidate_payload_bytes = 0
        self._blocks: dict[tuple[str, str], dict[str, object]] = {}
        self._ordered_keys: list[tuple[str, str]] = []
        self._kind_counts: dict[str, int] = {}

    @property
    def semantic_item_count(self) -> int:
        return self._semantic_count

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None:
        for event in events:
            attribution = getattr(event, "model_stream_attribution", None)
            if attribution is None:
                raise ValueError("model projection reducer received non-semantic event")
            if event.sequence is None:
                raise ValueError("model projection reducer requires committed sequence")
            if (
                attribution.resolved_model_call_id
                != self._start.resolved_call.resolved_model_call_id
                or attribution.model_call_start_event_id != self._start.id
                or attribution.durable_semantic_event_index
                != self._durable_event_count
            ):
                raise ValueError("model projection semantic cursor drifted")
            span = attribution.source_span
            if (
                span.first_transport_sequence_index != self._semantic_count
                or span.source_accumulator_before != self._source_accumulator
            ):
                raise ValueError("model projection source span continuity drifted")
            if self._segment_policy_contract_fingerprint != (
                attribution.segment_policy_contract_fingerprint
            ):
                raise ValueError("model projection segment policy drifted")
            self._durable_event_accumulator = context_fingerprint(
                "model-terminal-durable-event-accumulator:v1",
                {
                    "previous": self._durable_event_accumulator,
                    "event_type": str(event.type),
                    "durable_semantic_event_index": (
                        attribution.durable_semantic_event_index
                    ),
                    "source_span_fingerprint": span.source_span_fingerprint,
                    "canonical_event": canonical_event_payload_bytes(
                        event.model_copy(update={"sequence": None})
                    ).decode("utf-8"),
                },
            )
            self._apply_one(event)
            self._semantic_event_identities.append(
                stable_event_identity(
                    event,
                    runtime_session_id=self._runtime_session_id,
                )
            )
            self._adapter_source_item_count += span.adapter_source_item_count
            self._adapter_source_payload_bytes += span.adapter_source_payload_bytes
            self._synthetic_source_item_count += span.synthetic_source_item_count
            self._synthetic_source_payload_bytes += (
                span.synthetic_source_payload_bytes
            )
            candidate_bytes = len(
                canonical_event_payload_bytes(event.model_copy(update={"sequence": None}))
            )
            if attribution.segment_seal_reason is None:
                self._singleton_event_count += 1
                self._singleton_candidate_payload_bytes += candidate_bytes
            else:
                self._segment_event_count += 1
                self._segment_content_utf8_bytes += int(
                    getattr(event, "content_utf8_bytes", 0)
                )
                self._segment_candidate_payload_bytes += candidate_bytes
            self._semantic_count += span.source_item_count
            self._source_accumulator = span.source_accumulator_after
            self._durable_event_count += 1

    def prepare_terminal(
        self,
        *,
        event_context: EventContext,
        terminal_outcome: Literal[
            "completed", "provider_error", "cancelled", "runtime_error"
        ],
        usage_report: TransportUsageReport,
        semantic_commit_measurements: tuple[
            ModelStreamSemanticCommitMeasurementFact, ...
        ] = (),
        physical_accounting_mode: Literal[
            "accounted", "unbootstrapped_test"
        ] = "unbootstrapped_test",
    ) -> PreparedModelTerminalProjection:
        items = self._projection_items(terminal_outcome=terminal_outcome)
        semantic = build_frozen_fact(
            ModelTerminalProjectionSemanticFact,
            schema_version="model_terminal_projection_semantic.v1",
            projection_kind="model_call",
            terminal_outcome=terminal_outcome,
            ordered_item_semantic_fingerprints=tuple(
                item.semantic_identity.semantic_fingerprint for item in items
            ),
        )
        payload = ModelTerminalProjectionPayloadFact(
            schema_version="model_terminal_projection_payload.v2",
            projection_kind="model_call",
            items=items,
        )
        source = build_frozen_fact(
            ModelCallSemanticSourceFact,
            schema_version="model_call_semantic_source.v3",
            resolved_model_call_id=(
                self._start.resolved_call.resolved_model_call_id
            ),
            model_call_start_event_identity=stable_event_identity(
                self._start,
                runtime_session_id=self._runtime_session_id,
            ),
            source_semantic_item_count=self._semantic_count,
            source_first_transport_index=(0 if self._semantic_count else None),
            source_last_transport_index=(
                self._semantic_count - 1 if self._semantic_count else None
            ),
            source_semantic_accumulator=self._source_accumulator,
            durable_semantic_event_count=self._durable_event_count,
            durable_event_accumulator=self._durable_event_accumulator,
            segment_policy_contract_fingerprint=(
                self._segment_policy_contract_fingerprint
            ),
            model_stream_semantic_domain_contract_fingerprint=(
                self._domain_fingerprint
            ),
            reducer_contract_fingerprint=(
                MODEL_TERMINAL_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
            ),
            stream_settlement_measurement=self.build_settlement_measurement(
                semantic_commit_measurements=semantic_commit_measurements,
                physical_accounting_mode=physical_accounting_mode,
            ),
        )
        document = build_frozen_fact(
            TerminalProjectionDocumentFact,
            schema_version="terminal_projection_document.v2",
            document_contract_fingerprint=(
                self._contracts.document.contract_fingerprint
            ),
            semantic_identity=semantic,
            payload=payload,
            source_fact=source,
            usage_status=usage_report.usage_status,
            usage=usage_report.usage,
            reported_model_id=usage_report.reported_model_id,
            tool_result_artifact_refs=(),
        )
        canonical_bytes = canonical_json_bytes(document.model_dump(mode="json"))
        if len(canonical_bytes) > self._contracts.document.max_document_bytes:
            raise ValueError("model terminal projection document exceeds contract")
        call_id = self._start.resolved_call.resolved_model_call_id
        artifact_id = (
            f"terminal-projection:model:{call_id}:"
            f"{document.fact_fingerprint.removeprefix('sha256:')[:24]}"
        )
        semantic_join = ModelTerminalProjectionSemanticJoinFact(
            schema_version="model_terminal_projection_semantic_join.v1",
            projection_kind="model_call",
            terminal_outcome=terminal_outcome,
            projection_item_count=len(items),
            semantic_fingerprint=semantic.semantic_fingerprint,
        )
        reference = build_frozen_fact(
            TerminalProjectionReferenceFact,
            schema_version="terminal_projection_reference.v2",
            projection_kind="model_call",
            semantic_join=semantic_join,
            document_fact_fingerprint=document.fact_fingerprint,
            document_artifact_id=artifact_id,
            document_sha256=f"sha256:{sha256(canonical_bytes).hexdigest()}",
            document_byte_count=len(canonical_bytes),
            document_contract_fingerprint=(
                self._contracts.document.contract_fingerprint
            ),
        )
        committed_event = ModelCallTerminalProjectionCommittedEvent(
            id=f"model_terminal_projection:{call_id}:committed",
            **event_context.event_fields(),
            created_at=self._start.created_at,
            resolved_model_call_id=call_id,
            model_call_start_event_identity=source.model_call_start_event_identity,
            projection_reference=reference,
        )
        end_reference = build_frozen_fact(
            ModelCallTerminalProjectionEndReferenceFact,
            schema_version="model_call_terminal_projection_end_ref.v2",
            projection_committed_event_identity=stable_event_identity(
                committed_event,
                runtime_session_id=self._runtime_session_id,
            ),
            projection_reference=reference,
        )
        return PreparedModelTerminalProjection(
            document=document,
            canonical_document_bytes=canonical_bytes,
            projection_reference=reference,
            committed_event=committed_event,
            end_reference=end_reference,
        )

    def build_settlement_measurement(
        self,
        *,
        semantic_commit_measurements: tuple[
            ModelStreamSemanticCommitMeasurementFact, ...
        ],
        physical_accounting_mode: Literal[
            "accounted", "unbootstrapped_test"
        ],
    ) -> ModelStreamSettlementMeasurementFact:
        if physical_accounting_mode == "accounted":
            measured = tuple(
                identity
                for batch in semantic_commit_measurements
                for identity in batch.semantic_event_identities
            )
            if measured != tuple(self._semantic_event_identities):
                raise ValueError("model stream commit measurement coverage drifted")
        payload = {
            "segment_policy_contract_fingerprint": (
                self._segment_policy_contract_fingerprint
            ),
            "physical_accounting_mode": physical_accounting_mode,
            "adapter_source_item_count": self._adapter_source_item_count,
            "adapter_source_payload_bytes": self._adapter_source_payload_bytes,
            "synthetic_source_item_count": self._synthetic_source_item_count,
            "synthetic_source_payload_bytes": self._synthetic_source_payload_bytes,
            "source_item_count": self._semantic_count,
            "source_payload_bytes": (
                self._adapter_source_payload_bytes
                + self._synthetic_source_payload_bytes
            ),
            "singleton_event_count": self._singleton_event_count,
            "segment_event_count": self._segment_event_count,
            "durable_semantic_event_count": self._durable_event_count,
            "segment_content_utf8_bytes": self._segment_content_utf8_bytes,
            "segment_candidate_payload_bytes": self._segment_candidate_payload_bytes,
            "singleton_candidate_payload_bytes": (
                self._singleton_candidate_payload_bytes
            ),
            "durable_candidate_payload_bytes": (
                self._segment_candidate_payload_bytes
                + self._singleton_candidate_payload_bytes
            ),
            "semantic_commit_batches": semantic_commit_measurements,
            "actual_semantic_commit_batch_count": (
                len(semantic_commit_measurements)
                if physical_accounting_mode == "accounted"
                else None
            ),
        }
        provisional = ModelStreamSettlementMeasurementFact.model_construct(
            **payload, measurement_fingerprint="pending"
        )
        canonical = provisional.model_dump(
            mode="json", exclude={"measurement_fingerprint"}
        )
        return ModelStreamSettlementMeasurementFact(
            **canonical,
            measurement_fingerprint=sha256_fingerprint(
                "model-stream-settlement-measurement:v1", canonical
            ),
        )

    def _apply_one(self, event: AgentEvent) -> None:
        if isinstance(event, TextBlockStartEvent):
            self._start_block("text", event.block_id, source_start_sequence=event.sequence)
        elif isinstance(event, TextBlockSegmentEvent):
            self._append_block("text", event.block_id, event.text)
        elif isinstance(event, TextBlockEndEvent):
            self._end_block("text", event.block_id, source_end_sequence=event.sequence)
        elif isinstance(event, ThinkingBlockStartEvent):
            self._start_block(
                "thinking", event.block_id, source_start_sequence=event.sequence
            )
        elif isinstance(event, ThinkingBlockSegmentEvent):
            self._append_block("thinking", event.block_id, event.thinking)
        elif isinstance(event, ThinkingBlockEndEvent):
            self._end_block(
                "thinking", event.block_id, source_end_sequence=event.sequence
            )
        elif isinstance(event, DataBlockStartEvent):
            self._start_block(
                "data",
                event.block_id,
                media_type=event.media_type,
                source_start_sequence=event.sequence,
            )
        elif isinstance(event, DataBlockSegmentEvent):
            value = self._require_open("data", event.block_id)
            if value["media_type"] != event.media_type:
                raise ValueError("model data media type drifted")
            self._append_block("data", event.block_id, event.data)
        elif isinstance(event, DataBlockEndEvent):
            self._end_block("data", event.block_id, source_end_sequence=event.sequence)
        elif isinstance(event, ToolCallStartEvent):
            self._start_block(
                "tool_call",
                event.tool_call_id,
                tool_name=event.tool_call_name,
                source_start_sequence=event.sequence,
            )
        elif isinstance(event, ToolCallArgumentsSegmentEvent):
            self._append_block(
                "tool_call", event.tool_call_id, event.arguments_json_fragment
            )
        elif isinstance(event, ToolCallEndEvent):
            self._end_block(
                "tool_call",
                event.tool_call_id,
                source_end_sequence=event.sequence,
            )
        elif isinstance(event, ProviderModelStreamErrorEvent):
            key = ("provider_error", str(self._semantic_count))
            self._ordered_keys.append(key)
            self._blocks[key] = {
                "projection_order": len(self._ordered_keys) - 1,
                "block_index": self._kind_counts.get("provider_error", 0),
                "error": event.error,
                "ended": True,
                "source_start_sequence": event.sequence,
                "source_end_sequence": event.sequence,
            }
            self._kind_counts["provider_error"] = (
                self._kind_counts.get("provider_error", 0) + 1
            )
        else:
            raise TypeError(
                f"unsupported model projection event: {type(event).__name__}"
            )

    def _start_block(self, kind: str, block_id: str, **extra: object) -> None:
        key = (kind, block_id)
        if key in self._blocks:
            raise ValueError("duplicate model projection block start")
        self._ordered_keys.append(key)
        block_index = self._kind_counts.get(kind, 0)
        self._kind_counts[kind] = block_index + 1
        self._blocks[key] = {
            "projection_order": len(self._ordered_keys) - 1,
            "block_index": block_index,
            "parts": [],
            "ended": False,
            **extra,
        }

    def _require_open(self, kind: str, block_id: str) -> dict[str, object]:
        value = self._blocks.get((kind, block_id))
        if value is None or bool(value["ended"]):
            raise ValueError("model projection delta/end outside open block")
        return value

    def _append_block(self, kind: str, block_id: str, delta: str) -> None:
        value = self._require_open(kind, block_id)
        parts = value["parts"]
        if not isinstance(parts, list):
            raise RuntimeError("model projection block parts state drifted")
        parts.append(delta)

    def _end_block(
        self, kind: str, block_id: str, *, source_end_sequence: int | None
    ) -> None:
        value = self._require_open(kind, block_id)
        value["ended"] = True
        value["source_end_sequence"] = source_end_sequence

    def _projection_items(
        self,
        *,
        terminal_outcome: Literal[
            "completed", "provider_error", "cancelled", "runtime_error"
        ],
    ) -> tuple[ModelProjectionItemFact, ...]:
        output: list[ModelProjectionItemFact] = []
        for kind, block_id in self._ordered_keys:
            value = self._blocks[(kind, block_id)]
            completed = bool(value["ended"])
            if terminal_outcome == "completed" and not completed:
                raise ValueError("completed model call contains an open block")
            projection_order = int(value["projection_order"])
            block_index = int(value["block_index"])
            if kind == "provider_error":
                error = value["error"]
                diagnostics = tuple(
                    item.diagnostic_fingerprint for item in error.diagnostics  # type: ignore[attr-defined]
                )
                semantic = build_frozen_fact(
                    ModelProviderErrorSemanticFact,
                    schema_version="model_provider_error_semantic.v1",
                    block_kind="provider_error",
                    projection_order=projection_order,
                    stable_error_code=error.code.value,  # type: ignore[attr-defined]
                    sanitized_diagnostics=diagnostics,
                )
                output.append(
                    build_frozen_fact(
                        ModelProjectionItemFact,
                        schema_version="model_projection_item.v2",
                        semantic_identity=semantic,
                        content=None,
                        source_start_sequence=int(value["source_start_sequence"]),
                        source_end_sequence=int(value["source_end_sequence"]),
                        provider_error=error,
                    )
                )
                continue
            completion_status = "completed" if completed else "interrupted"
            if kind == "tool_call":
                raw = _join_projection_parts(value)
                parsed: FrozenJsonObjectFact | None = None
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    argument_status = "invalid_json"
                    parse_error = ToolArgumentsParseErrorCode.INVALID_JSON_SYNTAX
                else:
                    if isinstance(decoded, dict):
                        frozen = freeze_json(decoded)
                        if not isinstance(frozen, FrozenJsonObjectFact):
                            raise AssertionError("JSON object did not freeze as object")
                        parsed = frozen
                        argument_status = "valid_object"
                        parse_error = None
                    else:
                        argument_status = "non_object_json"
                        parse_error = ToolArgumentsParseErrorCode.JSON_ROOT_NOT_OBJECT
                semantic = build_frozen_fact(
                    ModelToolCallBlockSemanticFact,
                    schema_version="model_tool_call_block_semantic.v1",
                    block_kind="tool_call",
                    block_id=block_id,
                    block_index=block_index,
                    projection_order=projection_order,
                    tool_call_id=block_id,
                    tool_name=str(value["tool_name"]),
                    completion_status=completion_status,
                    arguments_status=argument_status,
                    parsed_arguments=parsed,
                    parse_error_code=parse_error,
                    raw_arguments_json=raw,
                )
                output.append(
                    build_frozen_fact(
                        ModelProjectionItemFact,
                        schema_version="model_projection_item.v2",
                        semantic_identity=semantic,
                        content=None,
                        source_start_sequence=int(value["source_start_sequence"]),
                        source_end_sequence=(
                            int(value["source_end_sequence"])
                            if value.get("source_end_sequence") is not None
                            else None
                        ),
                        provider_error=None,
                    )
                )
                continue
            text = _join_projection_parts(value)
            media_type = (
                self._contracts.content_canonicalization.text_media_type
                if kind == "text"
                else self._contracts.content_canonicalization.thinking_media_type
                if kind == "thinking"
                else normalize_data_media_type(
                    str(value["media_type"]),
                    contract=self._contracts.media_type_normalization,
                )
            )
            content = build_terminal_inline_content(
                text,
                media_type=media_type,
                contract=self._contracts.content_canonicalization,
            )
            content_semantic = content.semantic_identity
            common = {
                "block_id": block_id,
                "block_index": block_index,
                "projection_order": projection_order,
                "completion_status": completion_status,
                "content_semantic_identity": content_semantic,
            }
            if kind == "text":
                semantic = build_frozen_fact(
                    ModelTextBlockSemanticFact,
                    schema_version="model_text_block_semantic.v1",
                    block_kind="text",
                    **common,
                )
            elif kind == "thinking":
                semantic = build_frozen_fact(
                    ModelThinkingBlockSemanticFact,
                    schema_version="model_thinking_block_semantic.v1",
                    block_kind="thinking",
                    **common,
                )
            else:
                semantic = build_frozen_fact(
                    ModelDataBlockSemanticFact,
                    schema_version="model_data_block_semantic.v1",
                    block_kind="data",
                    media_type=media_type,
                    **common,
                )
            output.append(
                build_frozen_fact(
                    ModelProjectionItemFact,
                    schema_version="model_projection_item.v2",
                    semantic_identity=semantic,
                    content=content,
                    source_start_sequence=int(value["source_start_sequence"]),
                    source_end_sequence=(
                        int(value["source_end_sequence"])
                        if value.get("source_end_sequence") is not None
                        else None
                    ),
                    provider_error=None,
                )
            )
        return tuple(output)


def _join_projection_parts(value: dict[str, object]) -> str:
    parts = value.get("parts")
    if not isinstance(parts, list) or any(not isinstance(part, str) for part in parts):
        raise RuntimeError("model projection block parts state drifted")
    return "".join(parts)


def stable_event_identity(
    event: AgentEvent,
    *,
    runtime_session_id: str,
) -> StableEventIdentityFact:
    candidate = freeze_event_write_candidate(event.model_copy(update={"sequence": None}))
    return build_frozen_fact(
        StableEventIdentityFact,
        schema_version="stable_event_identity.v2",
        runtime_session_id=runtime_session_id,
        event_id=candidate.event_id,
        event_type=candidate.event_type,
        event_schema_version=candidate.event_schema_version,
        event_schema_fingerprint=candidate.event_schema_fingerprint,
        payload_fingerprint=candidate.payload_fingerprint,
    )


def build_model_stream_semantic_commit_measurement(
    *,
    runtime_session_id: str,
    commit_batch_index: int,
    committed_semantic_events: tuple[AgentEvent, ...],
    accounting_events: tuple[AgentEvent, ...],
) -> ModelStreamSemanticCommitMeasurementFact | None:
    charge_events = tuple(
        event
        for event in accounting_events
        if isinstance(event, PhysicalOperationChargeAppliedEvent)
    )
    if not charge_events:
        if accounting_events:
            raise ValueError("model semantic commit lacks its physical charge event")
        return None
    if len(charge_events) != 1:
        raise ValueError("model semantic commit has ambiguous physical charge events")
    charge_event = charge_events[0]
    semantic_identities = tuple(
        stable_event_identity(event, runtime_session_id=runtime_session_id)
        for event in committed_semantic_events
    )
    charged = charge_event.charge
    if (
        charged.owner_kind.value != "model_call"
        or tuple(
            sorted(
                charged.charged_business_event_identities,
                key=lambda item: (item.runtime_session_id, item.event_id),
            )
        )
        != tuple(
            sorted(
                semantic_identities,
                key=lambda item: (item.runtime_session_id, item.event_id),
            )
        )
    ):
        raise ValueError("model semantic commit/physical charge join mismatch")
    semantic_candidate_payload_bytes = (
        charged.business_candidate_charge_payload_bytes
    )
    payload = {
        "commit_batch_index": commit_batch_index,
        "semantic_event_identities": semantic_identities,
        "semantic_candidate_payload_bytes": semantic_candidate_payload_bytes,
        "physical_charge_event_identity": stable_event_identity(
            charge_event,
            runtime_session_id=runtime_session_id,
        ),
        "physical_charge_fingerprint": charged.charge_fingerprint,
    }
    provisional = ModelStreamSemanticCommitMeasurementFact.model_construct(
        **payload, batch_measurement_fingerprint="pending"
    )
    canonical = provisional.model_dump(
        mode="json", exclude={"batch_measurement_fingerprint"}
    )
    return ModelStreamSemanticCommitMeasurementFact(
        **canonical,
        batch_measurement_fingerprint=sha256_fingerprint(
            "model-stream-semantic-commit-measurement:v1", canonical
        ),
    )


def rebuild_model_stream_semantic_commit_measurements(
    *,
    runtime_session_id: str,
    resolved_model_call_id: str,
    semantic_events: tuple[AgentEvent, ...],
    ledger_events: tuple[AgentEvent, ...],
) -> tuple[ModelStreamSemanticCommitMeasurementFact, ...]:
    by_id = {event.id: event for event in semantic_events}
    output: list[ModelStreamSemanticCommitMeasurementFact] = []
    covered: set[str] = set()
    charges = sorted(
        (
            event
            for event in ledger_events
            if isinstance(event, PhysicalOperationChargeAppliedEvent)
            and event.charge.owner_kind.value == "model_call"
            and event.charge.owner_id == resolved_model_call_id
            and any(
                identity.event_id in by_id
                for identity in event.charge.charged_business_event_identities
            )
        ),
        key=lambda event: event.sequence or 0,
    )
    for charge_event in charges:
        batch_events = tuple(
            event
            for event in semantic_events
            if event.id
            in {
                identity.event_id
                for identity in charge_event.charge.charged_business_event_identities
            }
        )
        measurement = build_model_stream_semantic_commit_measurement(
            runtime_session_id=runtime_session_id,
            commit_batch_index=len(output),
            committed_semantic_events=batch_events,
            accounting_events=(charge_event,),
        )
        if measurement is None:
            raise AssertionError("recovered model stream charge measurement vanished")
        overlap = covered.intersection(
            identity.event_id for identity in measurement.semantic_event_identities
        )
        if overlap:
            raise ValueError("model stream semantic events were charged twice")
        covered.update(
            identity.event_id for identity in measurement.semantic_event_identities
        )
        output.append(measurement)
    if covered != set(by_id):
        raise ValueError("model stream semantic charge history is incomplete")
    return tuple(output)


def bind_model_terminal_projection_to_session(
    runtime_session: RuntimeSession,
    prepared: PreparedModelTerminalProjection,
) -> PreparedModelTerminalProjection:
    """Bind same-batch identity to the session's exact metadata overlay."""

    committed_event = runtime_session.prepare_event_for_write(
        prepared.committed_event
    )
    end_reference = build_frozen_fact(
        ModelCallTerminalProjectionEndReferenceFact,
        schema_version="model_call_terminal_projection_end_ref.v2",
        projection_committed_event_identity=stable_event_identity(
            committed_event,
            runtime_session_id=runtime_session.runtime_session_id,
        ),
        projection_reference=prepared.projection_reference,
    )
    return PreparedModelTerminalProjection(
        document=prepared.document,
        canonical_document_bytes=prepared.canonical_document_bytes,
        projection_reference=prepared.projection_reference,
        committed_event=committed_event,
        end_reference=end_reference,
    )


async def persist_model_terminal_projection(
    runtime_session: RuntimeSession,
    prepared: PreparedModelTerminalProjection,
    *,
    run_id: str,
    deadline_monotonic: float | None = None,
) -> None:
    deadline = deadline_monotonic or monotonic() + 30.0
    reference = prepared.projection_reference
    operation = await runtime_session.context_input_io_service.start_owned(
        operation_name="model-terminal-projection-write",
        operation=lambda: runtime_session.archive.put_text_if_absent_or_confirm_identical(
            reference.document_artifact_id,
            prepared.canonical_document_bytes.decode("utf-8"),
            session_id=runtime_session.runtime_session_id,
            run_id=run_id,
            media_type=TERMINAL_PROJECTION_MEDIA_TYPE,
            semantic_metadata={
                "projection_kind": "model_call",
                "document_fact_fingerprint": reference.document_fact_fingerprint,
                "document_contract_fingerprint": (
                    reference.document_contract_fingerprint
                ),
            },
            deadline_monotonic=deadline,
        ),
        deadline_monotonic=deadline,
    )
    while True:
        try:
            confirmation = await operation.wait_physical_completion()
            break
        except asyncio.CancelledError:
            if operation.physical_task_cancelled:
                raise TerminalProjectionPersistenceContractError(
                    "model terminal projection physical write was cancelled"
                ) from None
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
    if (
        confirmation.result.id != reference.document_artifact_id
        or confirmation.result.digest != reference.document_sha256
        or confirmation.result.size_bytes != reference.document_byte_count
    ):
        raise TerminalProjectionPersistenceContractError(
            "model terminal projection artifact confirmation drifted"
        )


async def hydrate_terminal_projection(
    runtime_session: RuntimeSession,
    reference: TerminalProjectionReferenceFact,
    *,
    deadline_monotonic: float | None = None,
) -> TerminalProjectionDocumentFact:
    """Hydrate and fully revalidate one content-addressed projection document."""

    deadline = deadline_monotonic or monotonic() + 30.0
    text = await runtime_session.context_input_io_service.execute(
        operation_name="terminal-projection-document-read",
        operation=lambda: runtime_session.archive.get_text(
            reference.document_artifact_id,
            session_id=runtime_session.runtime_session_id,
            deadline_monotonic=deadline,
        ),
        deadline_monotonic=deadline,
    )
    return hydrate_terminal_projection_text(reference, text)


def hydrate_terminal_projection_text(
    reference: TerminalProjectionReferenceFact,
    text: str,
) -> TerminalProjectionDocumentFact:
    """Validate one already-read immutable terminal projection document."""

    encoded = text.encode("utf-8")
    if (
        len(encoded) != reference.document_byte_count
        or f"sha256:{sha256(encoded).hexdigest()}" != reference.document_sha256
    ):
        raise ValueError("terminal projection document content drifted")
    document = TerminalProjectionDocumentFact.model_validate_json(text)
    if (
        document.fact_fingerprint != reference.document_fact_fingerprint
        or document.document_contract_fingerprint
        != reference.document_contract_fingerprint
        or document.semantic_identity.projection_kind != reference.projection_kind
        or document.semantic_identity.semantic_fingerprint
        != reference.semantic_join.semantic_fingerprint
    ):
        raise ValueError("terminal projection document reference drifted")
    return document


def validate_model_terminal_projection_document(
    *,
    runtime_session_id: str,
    start: ModelCallStartEvent,
    committed: ModelCallTerminalProjectionCommittedEvent,
    end: ModelCallEndEvent,
    document: TerminalProjectionDocumentFact,
) -> None:
    """Join one hydrated model document to every durable terminal carrier."""

    end_reference = end.terminal_projection
    reference = end_reference.projection_reference
    if (
        committed.resolved_model_call_id != end.resolved_model_call_id
        or committed.projection_reference != reference
        or end_reference.projection_committed_event_identity
        != stable_event_identity(committed, runtime_session_id=runtime_session_id)
    ):
        raise ValueError("model terminal projection carrier identity drifted")
    if not isinstance(document.source_fact, ModelCallSemanticSourceFact):
        raise ValueError("model terminal projection document source kind drifted")
    if (
        document.source_fact.resolved_model_call_id != end.resolved_model_call_id
        or document.source_fact.model_call_start_event_identity
        != stable_event_identity(start, runtime_session_id=runtime_session_id)
        or committed.model_call_start_event_identity
        != document.source_fact.model_call_start_event_identity
    ):
        raise ValueError("model terminal projection Start attribution drifted")
    if not isinstance(document.semantic_identity, ModelTerminalProjectionSemanticFact):
        raise ValueError("model terminal projection semantic kind drifted")
    if not isinstance(document.payload, ModelTerminalProjectionPayloadFact):
        raise ValueError("model terminal projection payload kind drifted")
    if (
        document.semantic_identity.terminal_outcome != end.outcome
        or reference.semantic_join.terminal_outcome != end.outcome
        or reference.semantic_join.projection_item_count != len(document.payload.items)
        or document.usage_status != end.usage_status
        or document.usage != end.usage
        or document.reported_model_id != end.reported_model_id
    ):
        raise ValueError("model terminal projection terminal facts drifted")
    if start.sequence is None:
        raise ValueError("model terminal projection requires committed Start")
    for item in document.payload.items:
        if item.source_start_sequence <= start.sequence:
            raise ValueError("model projection item precedes its ModelCallStart")


def normalize_data_media_type(
    raw: str,
    *,
    contract: DataMediaTypeNormalizationContractFact,
) -> str:
    if len(raw.encode("utf-8")) > contract.max_input_media_type_utf8_bytes:
        raise ValueError("data media type exceeds input contract")
    parts = [part.strip() for part in raw.split(";")]
    if not parts or "/" not in parts[0]:
        raise ValueError("invalid data media type")
    type_name, subtype = (part.strip().lower() for part in parts[0].split("/", 1))
    if not type_name or not subtype:
        raise ValueError("invalid data media type")
    parameters: list[tuple[str, str]] = []
    for raw_parameter in parts[1:]:
        if not raw_parameter:
            continue
        if "=" not in raw_parameter:
            raise ValueError("invalid data media type parameter")
        name, value = raw_parameter.split("=", 1)
        name = name.strip().lower()
        value = value.strip()
        if name == "charset":
            value = value.lower()
        if not name:
            raise ValueError("invalid data media type parameter")
        parameters.append((name, value))
    if len(parameters) != len({name for name, _ in parameters}):
        raise ValueError("duplicate data media type parameter")
    normalized = f"{type_name}/{subtype}"
    for name, value in sorted(parameters):
        normalized += f"; {name}={value}"
    if len(normalized.encode("utf-8")) > contract.max_normalized_media_type_utf8_bytes:
        raise ValueError("normalized data media type exceeds contract")
    return normalized


def build_terminal_inline_content(
    text: str,
    *,
    media_type: str,
    contract: TerminalContentCanonicalizationContractFact,
) -> TerminalInlineContentFact:
    encoded = text.encode("utf-8")
    semantic = build_frozen_fact(
        TerminalContentSemanticFact,
        schema_version="terminal_content_semantic.v2",
        canonical_content_sha256=f"sha256:{sha256(encoded).hexdigest()}",
        utf8_bytes=len(encoded),
        media_type=media_type,
        content_canonicalization_contract_fingerprint=(
            contract.contract_fingerprint
        ),
    )
    return build_frozen_fact(
        TerminalInlineContentFact,
        schema_version="terminal_inline_content.v2",
        storage_kind="inline",
        semantic_identity=semantic,
        text=text,
    )


__all__ = [
    "MODEL_TERMINAL_PROJECTION_REDUCER_CONTRACT_FINGERPRINT",
    "ModelTerminalProjectionReducer",
    "PreparedModelTerminalProjection",
    "TERMINAL_PROJECTION_MEDIA_TYPE",
    "TerminalProjectionContractBundle",
    "TerminalProjectionPersistenceContractError",
    "build_default_terminal_projection_contract_bundle",
    "build_terminal_inline_content",
    "bind_model_terminal_projection_to_session",
    "hydrate_terminal_projection",
    "hydrate_terminal_projection_text",
    "normalize_data_media_type",
    "persist_model_terminal_projection",
    "stable_event_identity",
    "validate_model_terminal_projection_document",
]
