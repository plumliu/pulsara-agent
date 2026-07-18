from __future__ import annotations

import asyncio
from dataclasses import replace
from time import monotonic_ns

import pytest

from pulsara_agent.event import (
    DataBlockSegmentEvent,
    EventContext,
    TextBlockSegmentEvent,
    ThinkingBlockSegmentEvent,
    ToolCallArgumentsSegmentEvent,
)
from pulsara_agent.llm.coalescing import (
    ArbiterSignalStamp,
    ModelStreamInputArbiter,
    ModelStreamInputSignalKind,
    ModelStreamReadySignal,
    ModelStreamCoalescingCoordinator,
)
from pulsara_agent.llm.drafts import (
    ProviderDataBlockDeltaDraft,
    ProviderTextBlockDeltaDraft,
    ProviderThinkingBlockStartDraft,
    ProviderThinkingBlockDeltaDraft,
    ProviderToolCallDeltaDraft,
    SanitizedProviderSemanticEnvelope,
    build_semantic_draft,
)
from pulsara_agent.llm.execution import ModelStreamExecutionHandle
from pulsara_agent.llm.segment import (
    MODEL_STREAM_SEGMENT_MAX_CANONICAL_EVENT_BYTES,
    ModelStreamSegmentAccumulator,
    ModelStreamSegmentContractError,
)
from pulsara_agent.llm.raw_provider import RawProviderBlockStart, RawProviderTextDelta
from pulsara_agent.llm.sanitizing_transport import (
    SanitizingProviderTransportExecution,
)
from pulsara_agent.primitives.model_call import (
    ModelStreamDurableSemanticKind,
    ModelStreamSegmentSealReason,
    sha256_fingerprint,
)
from tests.support.model_stream import make_model_stream_attribution


_EMPTY_SOURCE = sha256_fingerprint("model-stream-sanitized-source:v2", "empty")
_CONTEXT = EventContext(
    run_id="run:segment",
    turn_id="turn:segment",
    reply_id="reply:segment",
)


def _envelope(
    draft_type,
    *,
    index: int,
    source_before: str,
    **payload: object,
) -> SanitizedProviderSemanticEnvelope:
    draft = build_semantic_draft(
        draft_type,
        transport_sequence_index=index,
        **payload,
    )
    source_after = sha256_fingerprint(
        "model-stream-sanitized-source-receipt:v2",
        {
            "source_accumulator_before": source_before,
            "transport_sequence_index": index,
            "draft_kind": draft.draft_kind,
            "draft_fingerprint": draft.draft_fingerprint,
        },
    )
    return SanitizedProviderSemanticEnvelope(
        envelope_id=f"envelope:{index}",
        draft=draft,
        proposed_transport_sequence_index=index,
        source_accumulator_before=source_before,
        source_accumulator_after=source_after,
        accepted_at_monotonic_ns=monotonic_ns(),
        adapter_source_payload_bytes=1,
        counts_as_adapter_source_item=True,
    )


@pytest.mark.parametrize(
    ("draft_type", "payloads", "event_type", "content_field"),
    (
        (
            ProviderTextBlockDeltaDraft,
            ({"block_id": "text", "delta": "hel"}, {"block_id": "text", "delta": "lo"}),
            TextBlockSegmentEvent,
            "text",
        ),
        (
            ProviderThinkingBlockDeltaDraft,
            (
                {"block_id": "thinking", "delta": "rea"},
                {"block_id": "thinking", "delta": "son"},
            ),
            ThinkingBlockSegmentEvent,
            "thinking",
        ),
        (
            ProviderDataBlockDeltaDraft,
            (
                {"block_id": "data", "media_type": "application/json", "data": "{\"a\":"},
                {"block_id": "data", "media_type": "application/json", "data": "1}"},
            ),
            DataBlockSegmentEvent,
            "data",
        ),
        (
            ProviderToolCallDeltaDraft,
            (
                {"tool_call_id": "call", "delta": "{\"query\":\""},
                {"tool_call_id": "call", "delta": "value\"}"},
            ),
            ToolCallArgumentsSegmentEvent,
            "arguments_json_fragment",
        ),
    ),
)
def test_segment_accumulator_coalesces_each_delta_kind_losslessly(
    draft_type,
    payloads: tuple[dict[str, object], ...],
    event_type,
    content_field: str,
) -> None:
    accumulator = ModelStreamSegmentAccumulator(
        resolved_model_call_id="call:segment",
        model_call_start_event_id="start:segment",
        context=_CONTEXT,
    )
    source = _EMPTY_SOURCE
    for index, payload in enumerate(payloads):
        envelope = _envelope(
            draft_type,
            index=index,
            source_before=source,
            **payload,
        )
        assert accumulator.push(envelope) == ()
        source = envelope.source_accumulator_after

    prepared = accumulator.seal(ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY)
    assert prepared is not None
    assert isinstance(prepared.event, event_type)
    expected = "".join(
        str(payload.get("delta", payload.get("data", ""))) for payload in payloads
    )
    assert getattr(prepared.event, content_field) == expected
    assert prepared.source_item_count == len(payloads)
    assert prepared.canonical_candidate_bytes <= MODEL_STREAM_SEGMENT_MAX_CANONICAL_EVENT_BYTES


def test_canonical_json_expansion_seals_before_event_hard_cap() -> None:
    accumulator = ModelStreamSegmentAccumulator(
        resolved_model_call_id="call:canonical-boundary",
        model_call_start_event_id="start:canonical-boundary",
        context=_CONTEXT,
    )
    source = _EMPTY_SOURCE
    emitted = []
    for index in range(3):
        envelope = _envelope(
            ProviderDataBlockDeltaDraft,
            index=index,
            source_before=source,
            block_id="data",
            media_type="application/octet-stream",
            data="\x00" * 20_000,
        )
        emitted.extend(accumulator.push(envelope))
        source = envelope.source_accumulator_after

    tail = accumulator.seal(ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY)
    assert tail is not None
    emitted.append(tail)
    assert len(emitted) == 2
    assert emitted[0].event.model_stream_attribution.segment_seal_reason == (
        ModelStreamSegmentSealReason.CANONICAL_EVENT_BYTE_BOUNDARY
    )
    assert all(
        item.canonical_candidate_bytes
        <= MODEL_STREAM_SEGMENT_MAX_CANONICAL_EVENT_BYTES
        for item in emitted
    )
    assert "".join(item.event.data for item in emitted) == "\x00" * 60_000


def test_segment_accumulator_rejects_source_receipt_drift() -> None:
    accumulator = ModelStreamSegmentAccumulator(
        resolved_model_call_id="call:drift",
        model_call_start_event_id="start:drift",
        context=_CONTEXT,
    )
    envelope = _envelope(
        ProviderTextBlockDeltaDraft,
        index=0,
        source_before="sha256:not-the-active-prefix",
        block_id="text",
        delta="x",
    )
    with pytest.raises(ModelStreamSegmentContractError, match="accumulator drift"):
        accumulator.push(envelope)


def test_empty_segment_is_never_materialized() -> None:
    accumulator = ModelStreamSegmentAccumulator(
        resolved_model_call_id="call:empty",
        model_call_start_event_id="start:empty",
        context=_CONTEXT,
    )
    assert accumulator.seal(ModelStreamSegmentSealReason.TERMINAL_BOUNDARY) is None


def test_failed_seal_does_not_advance_durable_cursor() -> None:
    accumulator = ModelStreamSegmentAccumulator(
        resolved_model_call_id="call:failed-seal",
        model_call_start_event_id="start:failed-seal",
        context=_CONTEXT,
    )
    envelope = _envelope(
        ProviderTextBlockDeltaDraft,
        index=0,
        source_before=_EMPTY_SOURCE,
        block_id="text",
        delta="prefix",
    )
    assert accumulator.push(envelope) == ()
    accumulator._policy = accumulator._policy.model_copy(  # noqa: SLF001
        update={"max_canonical_event_bytes": 1}
    )

    with pytest.raises(ModelStreamSegmentContractError, match="canonical event hard cap"):
        accumulator.seal(ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY)

    assert accumulator.has_open_segment is True
    assert accumulator.durable_event_count == 0


def test_singleton_preparation_failure_does_not_seal_open_segment() -> None:
    class RejectingSingletonAccumulator(ModelStreamSegmentAccumulator):
        def _build_singleton(self, envelope, *, durable_event_index):
            del envelope, durable_event_index
            raise ModelStreamSegmentContractError("synthetic singleton rejection")

    accumulator = RejectingSingletonAccumulator(
        resolved_model_call_id="call:singleton-atomicity",
        model_call_start_event_id="start:singleton-atomicity",
        context=_CONTEXT,
    )
    delta = _envelope(
        ProviderTextBlockDeltaDraft,
        index=0,
        source_before=_EMPTY_SOURCE,
        block_id="text",
        delta="prefix",
    )
    assert accumulator.push(delta) == ()
    before_source = accumulator.source_accumulator

    singleton = _envelope(
        ProviderThinkingBlockStartDraft,
        index=1,
        source_before=before_source,
        block_id="thinking",
    )
    with pytest.raises(ModelStreamSegmentContractError, match="singleton rejection"):
        accumulator.push(singleton)

    assert accumulator.has_open_segment is True
    assert accumulator.consumed_source_item_count == 1
    assert accumulator.source_accumulator == before_source
    assert accumulator.durable_event_count == 0


def test_input_arbiter_has_stable_deadline_read_cancel_tie_break() -> None:
    signals = (
        ModelStreamReadySignal(
            kind=ModelStreamInputSignalKind.CANCEL,
            stamp=ArbiterSignalStamp(monotonic_ns=100, linearization_ordinal=1),
            deadline_monotonic_ns=None,
            payload="user_stop",
        ),
        ModelStreamReadySignal(
            kind=ModelStreamInputSignalKind.READ,
            stamp=ArbiterSignalStamp(monotonic_ns=100, linearization_ordinal=0),
            deadline_monotonic_ns=None,
            payload="item",
        ),
        ModelStreamReadySignal(
            kind=ModelStreamInputSignalKind.DEADLINE,
            stamp=None,
            deadline_monotonic_ns=100,
            payload=None,
        ),
    )
    ordered = ModelStreamInputArbiter.order_ready(signals)
    assert tuple(item.kind for item in ordered) == (
        ModelStreamInputSignalKind.DEADLINE,
        ModelStreamInputSignalKind.READ,
        ModelStreamInputSignalKind.CANCEL,
    )


def test_input_arbiter_uses_envelope_acceptance_time_for_deadline_order() -> None:
    arbiter = ModelStreamInputArbiter()
    read_stamp = arbiter.stamp(observed_monotonic_ns=99)
    ordered = arbiter.order_ready(
        (
            ModelStreamReadySignal(
                kind=ModelStreamInputSignalKind.DEADLINE,
                stamp=None,
                deadline_monotonic_ns=100,
                payload=None,
            ),
            ModelStreamReadySignal(
                kind=ModelStreamInputSignalKind.READ,
                stamp=read_stamp,
                deadline_monotonic_ns=None,
                payload="accepted-before-deadline",
            ),
        )
    )
    assert tuple(item.kind for item in ordered) == (
        ModelStreamInputSignalKind.READ,
        ModelStreamInputSignalKind.DEADLINE,
    )


def test_coordinator_rejection_does_not_advance_sanitizer_source_truth() -> None:
    async def scenario() -> None:
        async def raw_stream():
            yield RawProviderBlockStart(block_kind="text", block_id="text")

        transport = SanitizingProviderTransportExecution(
            raw_stream=raw_stream(),
            resolved_model_call_id="call:atomic-adopt",
        )
        envelope = await transport.read_next()
        assert isinstance(envelope, SanitizedProviderSemanticEnvelope)

        class RejectingAccumulator:
            oldest_unconfirmed_at_monotonic_ns = None

            @staticmethod
            def push(_envelope):
                raise ModelStreamSegmentContractError("synthetic segment rejection")

        coordinator = ModelStreamCoalescingCoordinator(
            transport=transport,
            segment_accumulator=RejectingAccumulator(),  # type: ignore[arg-type]
        )
        with pytest.raises(ModelStreamSegmentContractError):
            coordinator.adopt(envelope)

        assert transport.next_transport_sequence_index == 0
        assert transport.source_accumulator == _EMPTY_SOURCE
        assert transport.has_outstanding_envelope is True
        coordinator.discard_unadopted(envelope)

    asyncio.run(scenario())


def test_coordinator_rejects_value_drift_with_reused_envelope_id() -> None:
    async def scenario() -> None:
        async def raw_stream():
            yield RawProviderBlockStart(block_kind="text", block_id="text")

        transport = SanitizingProviderTransportExecution(
            raw_stream=raw_stream(),
            resolved_model_call_id="call:forged-envelope",
        )
        envelope = await transport.read_next()
        assert isinstance(envelope, SanitizedProviderSemanticEnvelope)
        forged = replace(
            envelope,
            source_accumulator_after="sha256:forged",
        )
        coordinator = ModelStreamCoalescingCoordinator(
            transport=transport,
            segment_accumulator=ModelStreamSegmentAccumulator(
                resolved_model_call_id="call:forged-envelope",
                model_call_start_event_id="start:forged-envelope",
                context=_CONTEXT,
            ),
        )
        with pytest.raises(RuntimeError, match="adoption identity mismatch"):
            coordinator.adopt(forged)
        assert transport.next_transport_sequence_index == 0
        coordinator.discard_unadopted(envelope)

    asyncio.run(scenario())


def test_coordinator_owns_complete_transition_before_sanitizer_adoption() -> None:
    async def scenario() -> None:
        async def raw_stream():
            for index in range(15):
                yield RawProviderBlockStart(
                    block_kind="text",
                    block_id=f"text:{index}",
                )
            yield RawProviderTextDelta(block_id="text:0", delta="pending segment")
            yield RawProviderBlockStart(
                block_kind="thinking",
                block_id="thinking:boundary",
            )

        transport = SanitizingProviderTransportExecution(
            raw_stream=raw_stream(),
            resolved_model_call_id="call:complete-transition-ownership",
        )
        coordinator = ModelStreamCoalescingCoordinator(
            transport=transport,
            segment_accumulator=ModelStreamSegmentAccumulator(
                resolved_model_call_id="call:complete-transition-ownership",
                model_call_start_event_id="start:complete-transition-ownership",
                context=_CONTEXT,
            ),
        )
        for _ in range(15):
            item = await transport.read_next()
            assert isinstance(item, SanitizedProviderSemanticEnvelope)
            coordinator.adopt(item)
        segment_item = await transport.read_next()
        assert isinstance(segment_item, SanitizedProviderSemanticEnvelope)
        coordinator.adopt(segment_item)

        boundary_item = await transport.read_next()
        assert isinstance(boundary_item, SanitizedProviderSemanticEnvelope)
        coordinator.adopt(boundary_item)

        assert transport.next_transport_sequence_index == 17
        assert coordinator.segment_accumulator.consumed_source_item_count == 17
        assert coordinator.batch.event_count == 16
        assert coordinator.has_pending_candidates is True
        assert coordinator.owned_candidate_count == 17

    asyncio.run(scenario())


def test_cancellation_intent_is_stamped_at_request_linearization() -> None:
    async def scenario() -> None:
        handle = ModelStreamExecutionHandle(
            handle_id="handle:cancel-linearization",
            handle_generation=1,
            run_id="run:cancel-linearization",
            resolved_model_call_id="call:cancel-linearization",
            mailbox_size=4,
            subscription_start_sequence=0,
        )
        await handle.request_cancel(reason="user_stop")
        reason, cancel_stamp = await handle.wait_cancellation_requested()
        read_stamp = handle.input_arbiter.stamp(
            observed_monotonic_ns=cancel_stamp.monotonic_ns
        )
        ordered = handle.input_arbiter.order_ready(
            (
                ModelStreamReadySignal(
                    kind=ModelStreamInputSignalKind.READ,
                    stamp=read_stamp,
                    deadline_monotonic_ns=None,
                    payload="late-read",
                ),
                ModelStreamReadySignal(
                    kind=ModelStreamInputSignalKind.CANCEL,
                    stamp=cancel_stamp,
                    deadline_monotonic_ns=None,
                    payload=reason,
                ),
            )
        )
        assert tuple(item.kind for item in ordered) == (
            ModelStreamInputSignalKind.CANCEL,
            ModelStreamInputSignalKind.READ,
        )

    asyncio.run(scenario())


def test_durable_kind_rejects_a_different_source_draft_kind() -> None:
    with pytest.raises(ValueError, match="durable/source draft kind mismatch"):
        make_model_stream_attribution(
            durable_kind=ModelStreamDurableSemanticKind.TEXT_BLOCK_SEGMENT,
            draft_kind="tool_call_delta",
            event_id="segment:kind-drift",
            segment_seal_reason=ModelStreamSegmentSealReason.STRUCTURAL_BOUNDARY,
        )
