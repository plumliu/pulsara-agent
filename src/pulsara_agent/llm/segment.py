"""Pure model-stream segment construction and exact candidate sizing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

from pulsara_agent.event import (
    AgentEvent,
    DataBlockEndEvent,
    DataBlockSegmentEvent,
    DataBlockStartEvent,
    EventContext,
    ProviderModelStreamErrorEvent,
    TextBlockEndEvent,
    TextBlockSegmentEvent,
    TextBlockStartEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockSegmentEvent,
    ThinkingBlockStartEvent,
    ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.event.events import utc_now
from pulsara_agent.event_log.serialization import (
    FrozenEventWriteCandidate,
    canonical_event_payload_bytes,
    freeze_event_write_candidate,
)
from pulsara_agent.llm.drafts import (
    ProviderDataBlockDeltaDraft,
    ProviderDataBlockEndDraft,
    ProviderDataBlockStartDraft,
    ProviderErrorDraft,
    ProviderTextBlockDeltaDraft,
    ProviderTextBlockEndDraft,
    ProviderTextBlockStartDraft,
    ProviderThinkingBlockDeltaDraft,
    ProviderThinkingBlockEndDraft,
    ProviderThinkingBlockStartDraft,
    ProviderToolCallDeltaDraft,
    ProviderToolCallEndDraft,
    ProviderToolCallStartDraft,
    SanitizedProviderSemanticEnvelope,
)
from pulsara_agent.primitives.model_call import (
    DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT,
    ModelStreamDurableSemanticKind,
    ModelStreamSegmentPolicyContractFact,
    ModelStreamSegmentSealReason,
    ModelStreamSemanticAttributionFact,
    ModelStreamSourceSpanFact,
    ProviderSemanticDraftKind,
    sha256_fingerprint,
)


MODEL_STREAM_SEGMENT_POLICY = DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT
MODEL_STREAM_TEXT_SEGMENT_TARGET_ESTIMATED_TOKENS = (
    MODEL_STREAM_SEGMENT_POLICY.text_target_estimated_tokens
)
MODEL_STREAM_TEXT_SEGMENT_TARGET_CODEPOINTS = (
    MODEL_STREAM_SEGMENT_POLICY.text_target_codepoints
)
MODEL_STREAM_STRING_SEGMENT_TARGET_UTF8_BYTES = (
    MODEL_STREAM_SEGMENT_POLICY.string_target_utf8_bytes
)
MODEL_STREAM_DATA_SEGMENT_TARGET_UTF8_BYTES = (
    MODEL_STREAM_SEGMENT_POLICY.data_target_utf8_bytes
)
MODEL_STREAM_SEGMENT_MAX_CONTENT_UTF8_BYTES = (
    MODEL_STREAM_SEGMENT_POLICY.max_content_utf8_bytes
)
MODEL_STREAM_SEGMENT_MAX_CANONICAL_EVENT_BYTES = (
    MODEL_STREAM_SEGMENT_POLICY.max_canonical_event_bytes
)
MODEL_STREAM_SEGMENT_MAX_SOURCE_ITEMS = (
    MODEL_STREAM_SEGMENT_POLICY.max_segment_source_items
)
MODEL_STREAM_COMMIT_MAX_DURABLE_EVENTS = (
    MODEL_STREAM_SEGMENT_POLICY.commit_max_durable_events
)
MODEL_STREAM_COMMIT_MAX_CANDIDATE_BYTES = (
    MODEL_STREAM_SEGMENT_POLICY.commit_max_candidate_bytes
)
MODEL_STREAM_MAX_UNCONFIRMED_AGE_SECONDS = (
    MODEL_STREAM_SEGMENT_POLICY.max_unconfirmed_age_millis / 1_000
)
MODEL_STREAM_MAX_SINGLE_SOURCE_ITEM_CANONICAL_BYTES = (
    MODEL_STREAM_SEGMENT_POLICY.max_single_source_item_canonical_bytes
)

_INITIAL_SOURCE_ACCUMULATOR = sha256_fingerprint(
    "model-stream-sanitized-source:v2", "empty"
)


class ModelStreamSegmentContractError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedModelStreamSemanticEvent:
    event: AgentEvent
    candidate: FrozenEventWriteCandidate
    source_item_count: int
    canonical_candidate_bytes: int
    is_segment: bool
    oldest_accepted_at_monotonic_ns: int


@dataclass(slots=True)
class _OpenSegment:
    segment_kind: Literal["text", "thinking", "data", "tool_call"]
    block_id: str
    media_type: str | None
    parts: list[str] = field(default_factory=list)
    codepoints: int = 0
    content_utf8_bytes: int = 0
    canonical_json_content_bytes: int = 0
    source_item_count: int = 0
    adapter_source_item_count: int = 0
    adapter_source_payload_bytes: int = 0
    synthetic_source_item_count: int = 0
    synthetic_source_payload_bytes: int = 0
    first_transport_sequence_index: int = 0
    last_transport_sequence_index: int = 0
    first_draft_kind: ProviderSemanticDraftKind = "text_block_delta"
    last_draft_kind: ProviderSemanticDraftKind = "text_block_delta"
    source_accumulator_before: str = ""
    source_accumulator_after: str = ""
    created_at: str = ""
    oldest_accepted_at_monotonic_ns: int = 0

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.segment_kind, self.block_id, self.media_type)


class ModelStreamSegmentAccumulator:
    """Pure state machine that maps adopted envelopes to immutable events."""

    def __init__(
        self,
        *,
        resolved_model_call_id: str,
        model_call_start_event_id: str,
        context: EventContext,
        initial_source_accumulator: str = _INITIAL_SOURCE_ACCUMULATOR,
        initial_source_item_count: int = 0,
        initial_durable_event_count: int = 0,
        policy: ModelStreamSegmentPolicyContractFact = MODEL_STREAM_SEGMENT_POLICY,
    ) -> None:
        self._resolved_model_call_id = resolved_model_call_id
        self._model_call_start_event_id = model_call_start_event_id
        self._context = context
        self._policy = policy
        self._next_source_index = initial_source_item_count
        self._source_accumulator = initial_source_accumulator
        self._next_durable_event_index = initial_durable_event_count
        self._open: _OpenSegment | None = None

    @property
    def has_open_segment(self) -> bool:
        return self._open is not None

    @property
    def oldest_unconfirmed_at_monotonic_ns(self) -> int | None:
        return (
            self._open.oldest_accepted_at_monotonic_ns
            if self._open is not None
            else None
        )

    @property
    def consumed_source_item_count(self) -> int:
        return self._next_source_index

    @property
    def source_accumulator(self) -> str:
        return self._source_accumulator

    @property
    def durable_event_count(self) -> int:
        return self._next_durable_event_index

    def push(
        self, envelope: SanitizedProviderSemanticEnvelope
    ) -> tuple[PreparedModelStreamSemanticEvent, ...]:
        self._require_next_envelope(envelope)
        draft = envelope.draft
        if isinstance(
            draft,
            (
                ProviderTextBlockDeltaDraft,
                ProviderThinkingBlockDeltaDraft,
                ProviderDataBlockDeltaDraft,
                ProviderToolCallDeltaDraft,
            ),
        ):
            return self._push_delta(envelope)

        prepared: list[PreparedModelStreamSemanticEvent] = []
        singleton_index = self._next_durable_event_index + int(self._open is not None)
        singleton = self._build_singleton(
            envelope,
            durable_event_index=singleton_index,
        )
        if self._open is not None:
            reason = (
                ModelStreamSegmentSealReason.TERMINAL_BOUNDARY
                if isinstance(draft, ProviderErrorDraft)
                else ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY
            )
            prepared.append(self._seal(reason))
        if self._next_durable_event_index != singleton_index:
            raise AssertionError("model singleton durable index prediction drifted")
        prepared.append(singleton)
        self._next_durable_event_index += 1
        self._adopt_source_envelope(envelope)
        return tuple(prepared)

    def seal(
        self, reason: ModelStreamSegmentSealReason
    ) -> PreparedModelStreamSemanticEvent | None:
        if self._open is None:
            return None
        return self._seal(reason)

    def _push_delta(
        self, envelope: SanitizedProviderSemanticEnvelope
    ) -> tuple[PreparedModelStreamSemanticEvent, ...]:
        key, content = self._delta_key_content(envelope)
        # Establish representability before sealing or otherwise mutating the
        # current prefix. A rejected source item must leave both owners on the
        # exact previous transition.
        standalone = self._new_open_segment(envelope, key)
        if (
            self._prospective_candidate_bytes(
                envelope,
                content,
                open_segment=standalone,
            )
            > self._policy.max_canonical_event_bytes
        ):
            raise ModelStreamSegmentContractError(
                "segment_single_source_item_unrepresentable"
            )
        prepared: list[PreparedModelStreamSemanticEvent] = []
        if self._open is not None and self._open.key != key:
            prepared.append(
                self._seal(ModelStreamSegmentSealReason.CONTIGUOUS_KEY_CHANGED)
            )

        if self._open is not None:
            soft_reason = self._prospective_soft_reason(content)
            if soft_reason is not None:
                prepared.append(self._seal(soft_reason))

        if self._open is None:
            self._open = self._new_open_segment(envelope, key)

        assert self._open is not None
        prospective_candidate_bytes = self._prospective_candidate_bytes(
            envelope, content
        )
        if (
            prospective_candidate_bytes
            > self._policy.max_canonical_event_bytes
        ):
            if self._open.source_item_count:
                prepared.append(
                    self._seal(
                        ModelStreamSegmentSealReason.CANONICAL_EVENT_BYTE_BOUNDARY
                    )
                )
                self._open = self._new_open_segment(envelope, key)
                prospective_candidate_bytes = self._prospective_candidate_bytes(
                    envelope, content
                )
            if prospective_candidate_bytes > self._policy.max_canonical_event_bytes:
                raise AssertionError("standalone segment sizing changed after validation")

        self._append_open(envelope, content)
        self._adopt_source_envelope(envelope)
        assert self._open is not None
        if self._open.content_utf8_bytes >= self._policy.max_content_utf8_bytes:
            prepared.append(
                self._seal(ModelStreamSegmentSealReason.HARD_CONTENT_BYTE_LIMIT)
            )
        elif (
            self._open.source_item_count
            >= self._policy.max_segment_source_items
        ):
            prepared.append(
                self._seal(ModelStreamSegmentSealReason.SOURCE_ITEM_LIMIT)
            )
        return tuple(prepared)

    def _require_next_envelope(
        self, envelope: SanitizedProviderSemanticEnvelope
    ) -> None:
        if envelope.proposed_transport_sequence_index != self._next_source_index:
            raise ModelStreamSegmentContractError("provider source index drift")
        if envelope.source_accumulator_before != self._source_accumulator:
            raise ModelStreamSegmentContractError("provider source accumulator drift")
        if envelope.draft.transport_sequence_index != self._next_source_index:
            raise ModelStreamSegmentContractError("provider draft index drift")

    def _adopt_source_envelope(
        self, envelope: SanitizedProviderSemanticEnvelope
    ) -> None:
        self._next_source_index += 1
        self._source_accumulator = envelope.source_accumulator_after

    @staticmethod
    def _delta_key_content(
        envelope: SanitizedProviderSemanticEnvelope,
    ) -> tuple[tuple[str, str, str | None], str]:
        draft = envelope.draft
        if isinstance(draft, ProviderTextBlockDeltaDraft):
            return ("text", draft.block_id, None), draft.delta
        if isinstance(draft, ProviderThinkingBlockDeltaDraft):
            return ("thinking", draft.block_id, None), draft.delta
        if isinstance(draft, ProviderDataBlockDeltaDraft):
            return ("data", draft.block_id, draft.media_type), draft.data
        if isinstance(draft, ProviderToolCallDeltaDraft):
            return ("tool_call", draft.tool_call_id, None), draft.delta
        raise TypeError(type(draft).__name__)

    def _new_open_segment(
        self,
        envelope: SanitizedProviderSemanticEnvelope,
        key: tuple[str, str, str | None],
    ) -> _OpenSegment:
        return _OpenSegment(
            segment_kind=key[0],
            block_id=key[1],
            media_type=key[2],
            first_transport_sequence_index=envelope.proposed_transport_sequence_index,
            last_transport_sequence_index=envelope.proposed_transport_sequence_index,
            first_draft_kind=envelope.draft.draft_kind,
            last_draft_kind=envelope.draft.draft_kind,
            source_accumulator_before=envelope.source_accumulator_before,
            source_accumulator_after=envelope.source_accumulator_before,
            created_at=utc_now(),
            oldest_accepted_at_monotonic_ns=envelope.accepted_at_monotonic_ns,
        )

    def _prospective_soft_reason(
        self, content: str
    ) -> ModelStreamSegmentSealReason | None:
        assert self._open is not None
        next_bytes = len(content.encode("utf-8"))
        if self._open.segment_kind == "data":
            if (
                self._open.content_utf8_bytes + next_bytes
                > self._policy.data_target_utf8_bytes
            ):
                return ModelStreamSegmentSealReason.SOFT_DATA_BYTE_TARGET
            return None
        if (
            self._open.content_utf8_bytes + next_bytes
            > self._policy.string_target_utf8_bytes
        ):
            return ModelStreamSegmentSealReason.SOFT_STRING_BYTE_TARGET
        if (
            self._open.codepoints + len(content)
            > self._policy.text_target_codepoints
        ):
            return ModelStreamSegmentSealReason.SOFT_TEXT_TOKEN_TARGET
        return None

    def _append_open(
        self,
        envelope: SanitizedProviderSemanticEnvelope,
        content: str,
    ) -> None:
        assert self._open is not None
        encoded = content.encode("utf-8")
        self._open.parts.append(content)
        self._open.codepoints += len(content)
        self._open.content_utf8_bytes += len(encoded)
        self._open.canonical_json_content_bytes += (
            self._canonical_json_string_content_bytes(content)
        )
        self._open.source_item_count += 1
        if envelope.counts_as_adapter_source_item:
            self._open.adapter_source_item_count += 1
            self._open.adapter_source_payload_bytes += (
                envelope.adapter_source_payload_bytes
            )
        else:
            self._open.synthetic_source_item_count += 1
            self._open.synthetic_source_payload_bytes += (
                envelope.adapter_source_payload_bytes
            )
        self._open.last_transport_sequence_index = (
            envelope.proposed_transport_sequence_index
        )
        self._open.last_draft_kind = envelope.draft.draft_kind
        self._open.source_accumulator_after = envelope.source_accumulator_after

    def _prospective_candidate_bytes(
        self,
        envelope: SanitizedProviderSemanticEnvelope,
        content: str,
        *,
        open_segment: _OpenSegment | None = None,
    ) -> int:
        active = self._open if open_segment is None else open_segment
        assert active is not None
        span = self._build_source_span(
            first_index=active.first_transport_sequence_index,
            last_index=envelope.proposed_transport_sequence_index,
            count=active.source_item_count + 1,
            adapter_count=(
                active.adapter_source_item_count
                + int(envelope.counts_as_adapter_source_item)
            ),
            adapter_payload_bytes=(
                active.adapter_source_payload_bytes
                + (
                    envelope.adapter_source_payload_bytes
                    if envelope.counts_as_adapter_source_item
                    else 0
                )
            ),
            synthetic_count=(
                active.synthetic_source_item_count
                + int(not envelope.counts_as_adapter_source_item)
            ),
            synthetic_payload_bytes=(
                active.synthetic_source_payload_bytes
                + (
                    0
                    if envelope.counts_as_adapter_source_item
                    else envelope.adapter_source_payload_bytes
                )
            ),
            first_kind=active.first_draft_kind,
            last_kind=envelope.draft.draft_kind,
            accumulator_before=active.source_accumulator_before,
            accumulator_after=envelope.source_accumulator_after,
        )
        # Use the longest legal reason for a conservative exact candidate size.
        reason = max(ModelStreamSegmentSealReason, key=lambda item: len(item.value))
        content_utf8_bytes = active.content_utf8_bytes + len(
            content.encode("utf-8")
        )
        content_codepoints = active.codepoints + len(content)
        sizing_event = self._construct_segment_event(
            open_segment=active,
            content="",
            span=span,
            reason=reason,
            content_utf8_bytes=content_utf8_bytes,
            content_codepoints=content_codepoints,
            content_sha256="sha256:" + ("0" * 64),
            validate_content=False,
        )
        empty_candidate_bytes = len(canonical_event_payload_bytes(sizing_event))
        return (
            empty_candidate_bytes
            + active.canonical_json_content_bytes
            + self._canonical_json_string_content_bytes(content)
        )

    @staticmethod
    def _canonical_json_string_content_bytes(content: str) -> int:
        encoded = json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        # The two surrounding quotes are already present in the empty sizing
        # candidate. JSON string escaping is compositional across source parts.
        return len(encoded) - 2

    def _seal(
        self, reason: ModelStreamSegmentSealReason
    ) -> PreparedModelStreamSemanticEvent:
        open_segment = self._open
        if open_segment is None or not open_segment.parts:
            raise ModelStreamSegmentContractError("cannot seal an empty segment")
        content = "".join(open_segment.parts)
        span = self._build_source_span(
            first_index=open_segment.first_transport_sequence_index,
            last_index=open_segment.last_transport_sequence_index,
            count=open_segment.source_item_count,
            adapter_count=open_segment.adapter_source_item_count,
            adapter_payload_bytes=open_segment.adapter_source_payload_bytes,
            synthetic_count=open_segment.synthetic_source_item_count,
            synthetic_payload_bytes=open_segment.synthetic_source_payload_bytes,
            first_kind=open_segment.first_draft_kind,
            last_kind=open_segment.last_draft_kind,
            accumulator_before=open_segment.source_accumulator_before,
            accumulator_after=open_segment.source_accumulator_after,
        )
        prepared = self._build_segment_event(
            open_segment=open_segment,
            content=content,
            span=span,
            reason=reason,
        )
        if prepared.canonical_candidate_bytes > self._policy.max_canonical_event_bytes:
            raise ModelStreamSegmentContractError(
                "sealed segment exceeds canonical event hard cap"
            )
        self._next_durable_event_index += 1
        self._open = None
        return prepared

    def _build_segment_event(
        self,
        *,
        open_segment: _OpenSegment,
        content: str,
        span: ModelStreamSourceSpanFact,
        reason: ModelStreamSegmentSealReason,
    ) -> PreparedModelStreamSemanticEvent:
        encoded = content.encode("utf-8")
        event = self._construct_segment_event(
            open_segment=open_segment,
            content=content,
            span=span,
            reason=reason,
            content_utf8_bytes=len(encoded),
            content_codepoints=len(content),
            content_sha256=f"sha256:{sha256(encoded).hexdigest()}",
            validate_content=True,
        )
        prepared = self._freeze(
            event,
            span.source_item_count,
            is_segment=True,
            oldest_accepted_at_monotonic_ns=(
                open_segment.oldest_accepted_at_monotonic_ns
            ),
        )
        return prepared

    def _construct_segment_event(
        self,
        *,
        open_segment: _OpenSegment,
        content: str,
        span: ModelStreamSourceSpanFact,
        reason: ModelStreamSegmentSealReason,
        content_utf8_bytes: int,
        content_codepoints: int,
        content_sha256: str,
        validate_content: bool,
    ) -> AgentEvent:
        kind_by_segment = {
            "text": ModelStreamDurableSemanticKind.TEXT_BLOCK_SEGMENT,
            "thinking": ModelStreamDurableSemanticKind.THINKING_BLOCK_SEGMENT,
            "data": ModelStreamDurableSemanticKind.DATA_BLOCK_SEGMENT,
            "tool_call": (
                ModelStreamDurableSemanticKind.TOOL_CALL_ARGUMENTS_SEGMENT
            ),
        }
        durable_kind = kind_by_segment[open_segment.segment_kind]
        attribution = self._build_attribution(
            durable_kind=durable_kind,
            source_span=span,
            reason=reason,
        )
        event_id = self._event_id(durable_kind, span)
        common = {
            "id": event_id,
            "created_at": open_segment.created_at,
            **self._context.event_fields(),
            "model_stream_attribution": attribution,
            "content_utf8_bytes": content_utf8_bytes,
            "content_sha256": content_sha256,
        }
        constructor_name = "__call__" if validate_content else "model_construct"

        def construct(event_type, **fields):
            if constructor_name == "__call__":
                return event_type(**fields)
            return event_type.model_construct(**fields)

        if open_segment.segment_kind == "text":
            event: AgentEvent = construct(
                TextBlockSegmentEvent,
                **common,
                block_id=open_segment.block_id,
                text=content,
                estimated_tokens_v1=max(1, (content_codepoints + 3) // 4),
            )
        elif open_segment.segment_kind == "thinking":
            event = construct(
                ThinkingBlockSegmentEvent,
                **common,
                block_id=open_segment.block_id,
                thinking=content,
                estimated_tokens_v1=max(1, (content_codepoints + 3) // 4),
            )
        elif open_segment.segment_kind == "data":
            assert open_segment.media_type is not None
            event = construct(
                DataBlockSegmentEvent,
                **common,
                block_id=open_segment.block_id,
                media_type=open_segment.media_type,
                data=content,
            )
        else:
            event = construct(
                ToolCallArgumentsSegmentEvent,
                **common,
                tool_call_id=open_segment.block_id,
                arguments_json_fragment=content,
                estimated_tokens_v1=max(1, (content_codepoints + 3) // 4),
            )
        return event

    def _build_singleton(
        self,
        envelope: SanitizedProviderSemanticEnvelope,
        *,
        durable_event_index: int,
    ) -> PreparedModelStreamSemanticEvent:
        draft = envelope.draft
        kind_map = {
            ProviderTextBlockStartDraft: ModelStreamDurableSemanticKind.TEXT_BLOCK_START,
            ProviderTextBlockEndDraft: ModelStreamDurableSemanticKind.TEXT_BLOCK_END,
            ProviderThinkingBlockStartDraft: (
                ModelStreamDurableSemanticKind.THINKING_BLOCK_START
            ),
            ProviderThinkingBlockEndDraft: (
                ModelStreamDurableSemanticKind.THINKING_BLOCK_END
            ),
            ProviderDataBlockStartDraft: ModelStreamDurableSemanticKind.DATA_BLOCK_START,
            ProviderDataBlockEndDraft: ModelStreamDurableSemanticKind.DATA_BLOCK_END,
            ProviderToolCallStartDraft: ModelStreamDurableSemanticKind.TOOL_CALL_START,
            ProviderToolCallEndDraft: ModelStreamDurableSemanticKind.TOOL_CALL_END,
            ProviderErrorDraft: ModelStreamDurableSemanticKind.PROVIDER_ERROR,
        }
        durable_kind = kind_map.get(type(draft))
        if durable_kind is None:
            raise TypeError(type(draft).__name__)
        span = self._build_source_span(
            first_index=envelope.proposed_transport_sequence_index,
            last_index=envelope.proposed_transport_sequence_index,
            count=1,
            adapter_count=int(envelope.counts_as_adapter_source_item),
            adapter_payload_bytes=(
                envelope.adapter_source_payload_bytes
                if envelope.counts_as_adapter_source_item
                else 0
            ),
            synthetic_count=int(not envelope.counts_as_adapter_source_item),
            synthetic_payload_bytes=(
                0
                if envelope.counts_as_adapter_source_item
                else envelope.adapter_source_payload_bytes
            ),
            first_kind=draft.draft_kind,
            last_kind=draft.draft_kind,
            accumulator_before=envelope.source_accumulator_before,
            accumulator_after=envelope.source_accumulator_after,
        )
        attribution = self._build_attribution(
            durable_kind=durable_kind,
            source_span=span,
            reason=None,
            durable_event_index=durable_event_index,
        )
        common = {
            "id": self._event_id(
                durable_kind,
                span,
                durable_event_index=durable_event_index,
            ),
            "created_at": utc_now(),
            **self._context.event_fields(),
            "model_stream_attribution": attribution,
        }
        if isinstance(draft, ProviderTextBlockStartDraft):
            event: AgentEvent = TextBlockStartEvent(**common, block_id=draft.block_id)
        elif isinstance(draft, ProviderTextBlockEndDraft):
            event = TextBlockEndEvent(**common, block_id=draft.block_id)
        elif isinstance(draft, ProviderThinkingBlockStartDraft):
            event = ThinkingBlockStartEvent(**common, block_id=draft.block_id)
        elif isinstance(draft, ProviderThinkingBlockEndDraft):
            event = ThinkingBlockEndEvent(**common, block_id=draft.block_id)
        elif isinstance(draft, ProviderDataBlockStartDraft):
            event = DataBlockStartEvent(
                **common, block_id=draft.block_id, media_type=draft.media_type
            )
        elif isinstance(draft, ProviderDataBlockEndDraft):
            event = DataBlockEndEvent(**common, block_id=draft.block_id)
        elif isinstance(draft, ProviderToolCallStartDraft):
            event = ToolCallStartEvent(
                **common,
                tool_call_id=draft.tool_call_id,
                tool_call_name=draft.tool_call_name,
            )
        elif isinstance(draft, ProviderToolCallEndDraft):
            event = ToolCallEndEvent(**common, tool_call_id=draft.tool_call_id)
        elif isinstance(draft, ProviderErrorDraft):
            event = ProviderModelStreamErrorEvent(**common, error=draft.error)
        else:  # pragma: no cover - guarded by kind_map
            raise TypeError(type(draft).__name__)
        prepared = self._freeze(
            event,
            1,
            is_segment=False,
            oldest_accepted_at_monotonic_ns=envelope.accepted_at_monotonic_ns,
        )
        if prepared.canonical_candidate_bytes > self._policy.max_canonical_event_bytes:
            raise ModelStreamSegmentContractError(
                "model singleton exceeds canonical event hard cap"
            )
        return prepared

    def _build_source_span(
        self,
        *,
        first_index: int,
        last_index: int,
        count: int,
        adapter_count: int,
        adapter_payload_bytes: int,
        synthetic_count: int,
        synthetic_payload_bytes: int,
        first_kind: ProviderSemanticDraftKind,
        last_kind: ProviderSemanticDraftKind,
        accumulator_before: str,
        accumulator_after: str,
    ) -> ModelStreamSourceSpanFact:
        payload = {
            "resolved_model_call_id": self._resolved_model_call_id,
            "model_call_start_event_id": self._model_call_start_event_id,
            "first_transport_sequence_index": first_index,
            "last_transport_sequence_index": last_index,
            "source_item_count": count,
            "adapter_source_item_count": adapter_count,
            "adapter_source_payload_bytes": adapter_payload_bytes,
            "synthetic_source_item_count": synthetic_count,
            "synthetic_source_payload_bytes": synthetic_payload_bytes,
            "first_draft_kind": first_kind,
            "last_draft_kind": last_kind,
            "source_accumulator_before": accumulator_before,
            "source_accumulator_after": accumulator_after,
        }
        provisional = ModelStreamSourceSpanFact.model_construct(
            **payload, source_span_fingerprint="pending"
        )
        canonical = provisional.model_dump(
            mode="json", exclude={"source_span_fingerprint"}
        )
        return ModelStreamSourceSpanFact(
            **canonical,
            source_span_fingerprint=sha256_fingerprint(
                "model-stream-source-span:v2", canonical
            ),
        )

    def _build_attribution(
        self,
        *,
        durable_kind: ModelStreamDurableSemanticKind,
        source_span: ModelStreamSourceSpanFact,
        reason: ModelStreamSegmentSealReason | None,
        durable_event_index: int | None = None,
    ) -> ModelStreamSemanticAttributionFact:
        resolved_event_index = (
            self._next_durable_event_index
            if durable_event_index is None
            else durable_event_index
        )
        payload = {
            "resolved_model_call_id": self._resolved_model_call_id,
            "model_call_start_event_id": self._model_call_start_event_id,
            "durable_semantic_event_index": resolved_event_index,
            "durable_kind": durable_kind,
            "source_span": source_span,
            "segment_seal_reason": reason,
            "segment_policy_contract_fingerprint": (
                self._policy.contract_fingerprint
            ),
        }
        provisional = ModelStreamSemanticAttributionFact.model_construct(
            **payload, attribution_fingerprint="pending"
        )
        canonical = provisional.model_dump(
            mode="json", exclude={"attribution_fingerprint"}
        )
        return ModelStreamSemanticAttributionFact(
            **canonical,
            attribution_fingerprint=sha256_fingerprint(
                "model-stream-semantic-attribution:v2", canonical
            ),
        )

    def _event_id(
        self,
        durable_kind: ModelStreamDurableSemanticKind,
        span: ModelStreamSourceSpanFact,
        *,
        durable_event_index: int | None = None,
    ) -> str:
        resolved_event_index = (
            self._next_durable_event_index
            if durable_event_index is None
            else durable_event_index
        )
        span_suffix = span.source_span_fingerprint.removeprefix("sha256:")[:16]
        return (
            f"model_segment:{self._resolved_model_call_id}:"
            f"{resolved_event_index}:{durable_kind.value}:{span_suffix}"
        )

    @staticmethod
    def _freeze(
        event: AgentEvent,
        source_item_count: int,
        *,
        is_segment: bool,
        oldest_accepted_at_monotonic_ns: int,
    ) -> PreparedModelStreamSemanticEvent:
        candidate = freeze_event_write_candidate(event)
        return PreparedModelStreamSemanticEvent(
            event=event,
            candidate=candidate,
            source_item_count=source_item_count,
            canonical_candidate_bytes=len(candidate.canonical_payload_bytes),
            is_segment=is_segment,
            oldest_accepted_at_monotonic_ns=oldest_accepted_at_monotonic_ns,
        )


__all__ = [
    "MODEL_STREAM_COMMIT_MAX_CANDIDATE_BYTES",
    "MODEL_STREAM_COMMIT_MAX_DURABLE_EVENTS",
    "MODEL_STREAM_DATA_SEGMENT_TARGET_UTF8_BYTES",
    "MODEL_STREAM_MAX_SINGLE_SOURCE_ITEM_CANONICAL_BYTES",
    "MODEL_STREAM_MAX_UNCONFIRMED_AGE_SECONDS",
    "MODEL_STREAM_SEGMENT_MAX_CANONICAL_EVENT_BYTES",
    "MODEL_STREAM_SEGMENT_MAX_CONTENT_UTF8_BYTES",
    "MODEL_STREAM_SEGMENT_MAX_SOURCE_ITEMS",
    "MODEL_STREAM_SEGMENT_POLICY",
    "MODEL_STREAM_STRING_SEGMENT_TARGET_UTF8_BYTES",
    "MODEL_STREAM_TEXT_SEGMENT_TARGET_CODEPOINTS",
    "MODEL_STREAM_TEXT_SEGMENT_TARGET_ESTIMATED_TOKENS",
    "ModelStreamSegmentAccumulator",
    "ModelStreamSegmentContractError",
    "PreparedModelStreamSemanticEvent",
]
