"""Valid model-stream event fixtures for the segment hard cut."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, ClassVar

from pulsara_agent.event import (
    DataBlockEndEvent as _DataBlockEndEvent,
    DataBlockSegmentEvent as _DataBlockSegmentEvent,
    DataBlockStartEvent as _DataBlockStartEvent,
    TextBlockEndEvent as _TextBlockEndEvent,
    TextBlockSegmentEvent as _TextBlockSegmentEvent,
    TextBlockStartEvent as _TextBlockStartEvent,
    ThinkingBlockEndEvent as _ThinkingBlockEndEvent,
    ThinkingBlockSegmentEvent as _ThinkingBlockSegmentEvent,
    ThinkingBlockStartEvent as _ThinkingBlockStartEvent,
    ToolCallArgumentsSegmentEvent as _ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent as _ToolCallEndEvent,
    ToolCallStartEvent as _ToolCallStartEvent,
)
from pulsara_agent.primitives.model_call import (
    DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT,
    ModelStreamDurableSemanticKind,
    ModelStreamSegmentSealReason,
    ModelStreamSemanticAttributionFact,
    ModelStreamSourceSpanFact,
    ProviderSemanticDraftKind,
    sha256_fingerprint,
)

_INITIAL_SOURCE_ACCUMULATOR = sha256_fingerprint(
    "model-stream-sanitized-source:v2", "empty"
)


def make_model_stream_attribution(
    *,
    durable_kind: ModelStreamDurableSemanticKind,
    draft_kind: ProviderSemanticDraftKind,
    event_id: str,
    resolved_model_call_id: str = "model_call:test-fixture",
    model_call_start_event_id: str = "model_call_start:test-fixture",
    durable_semantic_event_index: int = 0,
    first_transport_sequence_index: int = 0,
    source_item_count: int = 1,
    source_accumulator_before: str = _INITIAL_SOURCE_ACCUMULATOR,
    source_accumulator_after: str | None = None,
    segment_seal_reason: ModelStreamSegmentSealReason | None = None,
) -> ModelStreamSemanticAttributionFact:
    """Build a self-consistent attribution for isolated durable event fixtures."""

    if source_accumulator_after is None:
        source_accumulator_after = sha256_fingerprint(
            "test-model-stream-source-receipt:v1",
            {
                "before": source_accumulator_before,
                "event_id": event_id,
                "first_transport_sequence_index": first_transport_sequence_index,
                "source_item_count": source_item_count,
            },
        )
    span_payload = {
        "resolved_model_call_id": resolved_model_call_id,
        "model_call_start_event_id": model_call_start_event_id,
        "first_transport_sequence_index": first_transport_sequence_index,
        "last_transport_sequence_index": (
            first_transport_sequence_index + source_item_count - 1
        ),
        "source_item_count": source_item_count,
        "adapter_source_item_count": source_item_count,
        "adapter_source_payload_bytes": source_item_count,
        "synthetic_source_item_count": 0,
        "synthetic_source_payload_bytes": 0,
        "first_draft_kind": draft_kind,
        "last_draft_kind": draft_kind,
        "source_accumulator_before": source_accumulator_before,
        "source_accumulator_after": source_accumulator_after,
    }
    provisional_span = ModelStreamSourceSpanFact.model_construct(
        **span_payload,
        source_span_fingerprint="pending",
    )
    canonical_span = provisional_span.model_dump(
        mode="json", exclude={"source_span_fingerprint"}
    )
    source_span = ModelStreamSourceSpanFact(
        **canonical_span,
            source_span_fingerprint=sha256_fingerprint(
                "model-stream-source-span:v2", canonical_span
        ),
    )
    attribution_payload = {
        "resolved_model_call_id": resolved_model_call_id,
        "model_call_start_event_id": model_call_start_event_id,
        "durable_semantic_event_index": durable_semantic_event_index,
        "durable_kind": durable_kind,
        "source_span": source_span,
        "segment_seal_reason": segment_seal_reason,
        "segment_policy_contract_fingerprint": (
            DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT.contract_fingerprint
        ),
    }
    provisional_attribution = ModelStreamSemanticAttributionFact.model_construct(
        **attribution_payload,
        attribution_fingerprint="pending",
    )
    canonical_attribution = provisional_attribution.model_dump(
        mode="json", exclude={"attribution_fingerprint"}
    )
    return ModelStreamSemanticAttributionFact(
        **canonical_attribution,
        attribution_fingerprint=sha256_fingerprint(
            "model-stream-semantic-attribution:v2", canonical_attribution
        ),
    )


class _FixtureEventMeta(type):
    event_type: ClassVar[type]
    durable_kind: ClassVar[ModelStreamDurableSemanticKind]
    draft_kind: ClassVar[ProviderSemanticDraftKind]
    seal_reason: ClassVar[ModelStreamSegmentSealReason | None] = None
    content_input: ClassVar[str | None] = None
    content_output: ClassVar[str | None] = None
    include_token_estimate: ClassVar[bool] = False

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, cls.event_type)

    def __call__(cls, **kwargs: Any) -> Any:
        attribution_fields = {
            key: kwargs.pop(key)
            for key in tuple(kwargs)
            if key
            in {
                "resolved_model_call_id",
                "model_call_start_event_id",
                "durable_semantic_event_index",
                "first_transport_sequence_index",
                "source_item_count",
                "source_accumulator_before",
                "source_accumulator_after",
            }
        }
        event_id = str(kwargs.get("id", "model_stream_fixture:event"))
        if "model_stream_attribution" not in kwargs:
            kwargs["model_stream_attribution"] = make_model_stream_attribution(
                durable_kind=cls.durable_kind,
                draft_kind=cls.draft_kind,
                event_id=event_id,
                segment_seal_reason=cls.seal_reason,
                **attribution_fields,
            )
        if cls.content_input is not None and cls.content_output is not None:
            if cls.content_input in kwargs:
                content = kwargs.pop(cls.content_input)
            else:
                content = kwargs[cls.content_output]
            kwargs[cls.content_output] = content
            encoded = content.encode("utf-8")
            kwargs.setdefault("content_utf8_bytes", len(encoded))
            kwargs.setdefault("content_sha256", f"sha256:{sha256(encoded).hexdigest()}")
            if cls.include_token_estimate:
                kwargs.setdefault("estimated_tokens_v1", max(1, (len(content) + 3) // 4))
        return cls.event_type(**kwargs)


def _fixture_event_class(
    name: str,
    *,
    event_type: type,
    durable_kind: ModelStreamDurableSemanticKind,
    draft_kind: ProviderSemanticDraftKind,
    seal_reason: ModelStreamSegmentSealReason | None = None,
    content_input: str | None = None,
    content_output: str | None = None,
    include_token_estimate: bool = False,
) -> type:
    return _FixtureEventMeta(
        name,
        (),
        {
            "event_type": event_type,
            "durable_kind": durable_kind,
            "draft_kind": draft_kind,
            "seal_reason": seal_reason,
            "content_input": content_input,
            "content_output": content_output,
            "include_token_estimate": include_token_estimate,
        },
    )


TextBlockStartEvent = _fixture_event_class(
    "TextBlockStartEvent",
    event_type=_TextBlockStartEvent,
    durable_kind=ModelStreamDurableSemanticKind.TEXT_BLOCK_START,
    draft_kind="text_block_start",
)
TextBlockSegmentEvent = _fixture_event_class(
    "TextBlockSegmentEvent",
    event_type=_TextBlockSegmentEvent,
    durable_kind=ModelStreamDurableSemanticKind.TEXT_BLOCK_SEGMENT,
    draft_kind="text_block_delta",
    seal_reason=ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY,
    content_input="delta",
    content_output="text",
    include_token_estimate=True,
)
TextBlockEndEvent = _fixture_event_class(
    "TextBlockEndEvent",
    event_type=_TextBlockEndEvent,
    durable_kind=ModelStreamDurableSemanticKind.TEXT_BLOCK_END,
    draft_kind="text_block_end",
)
ThinkingBlockStartEvent = _fixture_event_class(
    "ThinkingBlockStartEvent",
    event_type=_ThinkingBlockStartEvent,
    durable_kind=ModelStreamDurableSemanticKind.THINKING_BLOCK_START,
    draft_kind="thinking_block_start",
)
ThinkingBlockSegmentEvent = _fixture_event_class(
    "ThinkingBlockSegmentEvent",
    event_type=_ThinkingBlockSegmentEvent,
    durable_kind=ModelStreamDurableSemanticKind.THINKING_BLOCK_SEGMENT,
    draft_kind="thinking_block_delta",
    seal_reason=ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY,
    content_input="delta",
    content_output="thinking",
    include_token_estimate=True,
)
ThinkingBlockEndEvent = _fixture_event_class(
    "ThinkingBlockEndEvent",
    event_type=_ThinkingBlockEndEvent,
    durable_kind=ModelStreamDurableSemanticKind.THINKING_BLOCK_END,
    draft_kind="thinking_block_end",
)
DataBlockStartEvent = _fixture_event_class(
    "DataBlockStartEvent",
    event_type=_DataBlockStartEvent,
    durable_kind=ModelStreamDurableSemanticKind.DATA_BLOCK_START,
    draft_kind="data_block_start",
)
DataBlockSegmentEvent = _fixture_event_class(
    "DataBlockSegmentEvent",
    event_type=_DataBlockSegmentEvent,
    durable_kind=ModelStreamDurableSemanticKind.DATA_BLOCK_SEGMENT,
    draft_kind="data_block_delta",
    seal_reason=ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY,
    content_input="delta",
    content_output="data",
)
DataBlockEndEvent = _fixture_event_class(
    "DataBlockEndEvent",
    event_type=_DataBlockEndEvent,
    durable_kind=ModelStreamDurableSemanticKind.DATA_BLOCK_END,
    draft_kind="data_block_end",
)
ToolCallStartEvent = _fixture_event_class(
    "ToolCallStartEvent",
    event_type=_ToolCallStartEvent,
    durable_kind=ModelStreamDurableSemanticKind.TOOL_CALL_START,
    draft_kind="tool_call_start",
)
ToolCallArgumentsSegmentEvent = _fixture_event_class(
    "ToolCallArgumentsSegmentEvent",
    event_type=_ToolCallArgumentsSegmentEvent,
    durable_kind=ModelStreamDurableSemanticKind.TOOL_CALL_ARGUMENTS_SEGMENT,
    draft_kind="tool_call_delta",
    seal_reason=ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY,
    content_input="delta",
    content_output="arguments_json_fragment",
    include_token_estimate=True,
)
ToolCallEndEvent = _fixture_event_class(
    "ToolCallEndEvent",
    event_type=_ToolCallEndEvent,
    durable_kind=ModelStreamDurableSemanticKind.TOOL_CALL_END,
    draft_kind="tool_call_end",
)


def make_text_block_start_event(**kwargs: Any) -> _TextBlockStartEvent:
    return TextBlockStartEvent(**kwargs)


def make_text_block_segment_event(**kwargs: Any) -> _TextBlockSegmentEvent:
    return TextBlockSegmentEvent(**kwargs)


def make_text_block_end_event(**kwargs: Any) -> _TextBlockEndEvent:
    return TextBlockEndEvent(**kwargs)


def make_thinking_block_start_event(**kwargs: Any) -> _ThinkingBlockStartEvent:
    return ThinkingBlockStartEvent(**kwargs)


def make_thinking_block_segment_event(**kwargs: Any) -> _ThinkingBlockSegmentEvent:
    return ThinkingBlockSegmentEvent(**kwargs)


def make_thinking_block_end_event(**kwargs: Any) -> _ThinkingBlockEndEvent:
    return ThinkingBlockEndEvent(**kwargs)


def make_data_block_start_event(**kwargs: Any) -> _DataBlockStartEvent:
    return DataBlockStartEvent(**kwargs)


def make_data_block_segment_event(**kwargs: Any) -> _DataBlockSegmentEvent:
    return DataBlockSegmentEvent(**kwargs)


def make_data_block_end_event(**kwargs: Any) -> _DataBlockEndEvent:
    return DataBlockEndEvent(**kwargs)


def make_tool_call_start_event(**kwargs: Any) -> _ToolCallStartEvent:
    return ToolCallStartEvent(**kwargs)


def make_tool_call_arguments_segment_event(
    **kwargs: Any,
) -> _ToolCallArgumentsSegmentEvent:
    return ToolCallArgumentsSegmentEvent(**kwargs)


def make_tool_call_end_event(**kwargs: Any) -> _ToolCallEndEvent:
    return ToolCallEndEvent(**kwargs)


__all__ = [
    "DataBlockEndEvent",
    "DataBlockSegmentEvent",
    "DataBlockStartEvent",
    "TextBlockEndEvent",
    "TextBlockSegmentEvent",
    "TextBlockStartEvent",
    "ThinkingBlockEndEvent",
    "ThinkingBlockSegmentEvent",
    "ThinkingBlockStartEvent",
    "ToolCallArgumentsSegmentEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
    "make_model_stream_attribution",
    "make_data_block_end_event",
    "make_data_block_segment_event",
    "make_data_block_start_event",
    "make_text_block_end_event",
    "make_text_block_segment_event",
    "make_text_block_start_event",
    "make_thinking_block_end_event",
    "make_thinking_block_segment_event",
    "make_thinking_block_start_event",
    "make_tool_call_arguments_segment_event",
    "make_tool_call_end_event",
    "make_tool_call_start_event",
]
