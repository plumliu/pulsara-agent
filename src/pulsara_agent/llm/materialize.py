"""Materialize canonical model-call results from committed EventLog facts."""

from __future__ import annotations

from pulsara_agent.event import (
    AgentEvent,
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ProviderModelStreamErrorEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives.model_call import (
    CommittedModelCallResult,
    CommittedModelDataBlockFact,
    CommittedModelTextBlockFact,
    CommittedModelThinkingBlockFact,
    CommittedModelToolCallFact,
    ModelCallResultControlDisposition,
    sha256_fingerprint,
)


class ModelStreamMaterializationError(RuntimeError):
    pass


MAX_MODEL_CALL_MATERIALIZATION_EVENTS = 16_384
MAX_MODEL_CALL_MATERIALIZATION_PAYLOAD_BYTES = 16 * 1024 * 1024


def materialize_committed_model_call_result(
    event_log: EventLog,
    *,
    resolved_model_call_id: str,
    deadline_monotonic: float | None = None,
) -> CommittedModelCallResult:
    raw = event_log.read_raw_model_call_events(
        resolved_model_call_id,
        max_events=MAX_MODEL_CALL_MATERIALIZATION_EVENTS,
        max_payload_bytes=MAX_MODEL_CALL_MATERIALIZATION_PAYLOAD_BYTES,
        deadline_monotonic=deadline_monotonic,
    )
    return materialize_committed_model_call_result_from_events(
        tuple(
            envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for envelope in raw
        ),
        resolved_model_call_id=resolved_model_call_id,
    )


def materialize_committed_model_call_result_from_events(
    events: tuple[AgentEvent, ...],
    *,
    resolved_model_call_id: str,
) -> CommittedModelCallResult:
    """Materialize against one caller-owned canonical event snapshot."""

    starts = tuple(
        event
        for event in events
        if isinstance(event, ModelCallStartEvent)
        and event.resolved_call.resolved_model_call_id == resolved_model_call_id
    )
    ends = tuple(
        event
        for event in events
        if isinstance(event, ModelCallEndEvent)
        and event.resolved_model_call_id == resolved_model_call_id
    )
    if len(starts) != 1 or len(ends) != 1:
        raise ModelStreamMaterializationError(
            "model result requires exactly one committed Start and End"
        )
    start, end = starts[0], ends[0]
    if start.sequence is None or end.sequence is None or end.sequence <= start.sequence:
        raise ModelStreamMaterializationError("model lifecycle sequence is invalid")
    semantic = tuple(
        event
        for event in events
        if start.sequence < (event.sequence or 0) < end.sequence
        and getattr(event, "model_stream_attribution", None) is not None
        and event.model_stream_attribution.resolved_model_call_id  # type: ignore[union-attr]
        == resolved_model_call_id
        and event.model_stream_attribution.model_call_start_event_id  # type: ignore[union-attr]
        == start.id
    )
    indexes = tuple(
        event.model_stream_attribution.transport_sequence_index  # type: ignore[union-attr]
        for event in semantic
    )
    if indexes != tuple(range(len(semantic))):
        raise ModelStreamMaterializationError(
            "model semantic cursor is not contiguous and ordered"
        )

    text = _TextAccumulator()
    thinking = _TextAccumulator()
    data = _DataAccumulator()
    tools = _ToolAccumulator()
    provider_errors = []
    for event in semantic:
        if isinstance(event, TextBlockStartEvent):
            text.start(event.block_id, _sequence(event))
        elif isinstance(event, TextBlockDeltaEvent):
            text.delta(event.block_id, event.delta)
        elif isinstance(event, TextBlockEndEvent):
            text.end(event.block_id, _sequence(event))
        elif isinstance(event, ThinkingBlockStartEvent):
            thinking.start(event.block_id, _sequence(event))
        elif isinstance(event, ThinkingBlockDeltaEvent):
            thinking.delta(event.block_id, event.delta)
        elif isinstance(event, ThinkingBlockEndEvent):
            thinking.end(event.block_id, _sequence(event))
        elif isinstance(event, DataBlockStartEvent):
            data.start(event.block_id, event.media_type, _sequence(event))
        elif isinstance(event, DataBlockDeltaEvent):
            data.delta(event.block_id, event.media_type, event.data)
        elif isinstance(event, DataBlockEndEvent):
            data.end(event.block_id, _sequence(event))
        elif isinstance(event, ToolCallStartEvent):
            tools.start(
                event.tool_call_id, event.tool_call_name, _sequence(event)
            )
        elif isinstance(event, ToolCallDeltaEvent):
            tools.delta(event.tool_call_id, event.delta)
        elif isinstance(event, ToolCallEndEvent):
            tools.end(event.tool_call_id, _sequence(event))
        elif isinstance(event, ProviderModelStreamErrorEvent):
            provider_errors.append(event.error)
        else:  # pragma: no cover - closed event schema guard
            raise ModelStreamMaterializationError(
                f"unsupported model semantic event: {type(event).__name__}"
            )

    completed = end.outcome == "completed"
    text_facts = text.facts(completed=completed, thinking=False)
    thinking_facts = thinking.facts(completed=completed, thinking=True)
    data_facts = data.facts(completed=completed)
    tool_facts = tools.facts(completed=completed)
    payload = {
        "schema_version": "committed_model_call_result.v1",
        "resolved_model_call_id": resolved_model_call_id,
        "model_call_start_event_id": start.id,
        "model_call_start_sequence": start.sequence,
        "model_call_end_event_id": end.id,
        "model_call_end_sequence": end.sequence,
        "terminal_outcome": end.outcome,
        "control_disposition": (
            ModelCallResultControlDisposition.SUCCESS_ELIGIBLE
            if completed
            else ModelCallResultControlDisposition.AUDIT_ONLY
        ),
        "text_blocks": text_facts,
        "combined_text": "".join(item.text for item in text_facts),
        "thinking_blocks": thinking_facts,
        "data_blocks": data_facts,
        "tool_calls": tool_facts,
        "provider_errors": tuple(provider_errors),
        "usage_status": end.usage_status,
        "usage": end.usage,
        "reported_model_id": end.reported_model_id,
        "semantic_item_count": len(semantic),
        "source_through_sequence": end.sequence,
    }
    canonical = {
        **payload,
        "control_disposition": payload["control_disposition"].value,  # type: ignore[union-attr]
        "text_blocks": tuple(item.model_dump(mode="json") for item in text_facts),
        "thinking_blocks": tuple(
            item.model_dump(mode="json") for item in thinking_facts
        ),
        "data_blocks": tuple(item.model_dump(mode="json") for item in data_facts),
        "tool_calls": tuple(item.model_dump(mode="json") for item in tool_facts),
        "provider_errors": tuple(
            item.model_dump(mode="json") for item in provider_errors
        ),
        "usage": end.usage.model_dump(mode="json") if end.usage is not None else None,
    }
    return CommittedModelCallResult(
        **payload,
        result_fingerprint=sha256_fingerprint(
            "committed-model-call-result:v1", canonical
        ),
    )


def _sequence(event) -> int:
    if event.sequence is None:
        raise ModelStreamMaterializationError("stored semantic event lacks sequence")
    return event.sequence


class _TextAccumulator:
    def __init__(self) -> None:
        self._order: list[str] = []
        self._values: dict[str, dict[str, object]] = {}

    def start(self, block_id: str, sequence: int) -> None:
        if block_id in self._values:
            raise ModelStreamMaterializationError("duplicate model text block start")
        self._order.append(block_id)
        self._values[block_id] = {
            "text": "",
            "start_sequence": sequence,
            "end_sequence": None,
        }

    def delta(self, block_id: str, delta: str) -> None:
        value = self._values.get(block_id)
        if value is None or value["end_sequence"] is not None:
            raise ModelStreamMaterializationError("text delta outside active block")
        value["text"] = str(value["text"]) + delta

    def end(self, block_id: str, sequence: int) -> None:
        value = self._values.get(block_id)
        if value is None or value["end_sequence"] is not None:
            raise ModelStreamMaterializationError("text end outside active block")
        value["end_sequence"] = sequence

    def facts(self, *, completed: bool, thinking: bool):
        output = []
        fact_type = (
            CommittedModelThinkingBlockFact
            if thinking
            else CommittedModelTextBlockFact
        )
        for block_id in self._order:
            value = self._values[block_id]
            end_sequence = value["end_sequence"]
            if completed and end_sequence is None:
                raise ModelStreamMaterializationError(
                    "completed model call contains an open text block"
                )
            output.append(
                fact_type(
                    block_id=block_id,
                    text=str(value["text"]),
                    start_sequence=int(value["start_sequence"]),
                    end_sequence=end_sequence,
                    completion_status=(
                        "completed" if end_sequence is not None else "interrupted"
                    ),
                )
            )
        return tuple(output)


class _DataAccumulator:
    def __init__(self) -> None:
        self._order: list[str] = []
        self._values: dict[str, dict[str, object]] = {}

    def start(self, block_id: str, media_type: str, sequence: int) -> None:
        if block_id in self._values:
            raise ModelStreamMaterializationError("duplicate model data block start")
        self._order.append(block_id)
        self._values[block_id] = {
            "media_type": media_type,
            "data": "",
            "start_sequence": sequence,
            "end_sequence": None,
        }

    def delta(self, block_id: str, media_type: str, data: str) -> None:
        value = self._values.get(block_id)
        if (
            value is None
            or value["end_sequence"] is not None
            or value["media_type"] != media_type
        ):
            raise ModelStreamMaterializationError("data delta outside active block")
        value["data"] = str(value["data"]) + data

    def end(self, block_id: str, sequence: int) -> None:
        value = self._values.get(block_id)
        if value is None or value["end_sequence"] is not None:
            raise ModelStreamMaterializationError("data end outside active block")
        value["end_sequence"] = sequence

    def facts(self, *, completed: bool) -> tuple[CommittedModelDataBlockFact, ...]:
        output = []
        for block_id in self._order:
            value = self._values[block_id]
            end_sequence = value["end_sequence"]
            if completed and end_sequence is None:
                raise ModelStreamMaterializationError(
                    "completed model call contains an open data block"
                )
            output.append(
                CommittedModelDataBlockFact(
                    block_id=block_id,
                    media_type=str(value["media_type"]),
                    data=str(value["data"]),
                    start_sequence=int(value["start_sequence"]),
                    end_sequence=end_sequence,
                    completion_status=(
                        "completed" if end_sequence is not None else "interrupted"
                    ),
                )
            )
        return tuple(output)


class _ToolAccumulator:
    def __init__(self) -> None:
        self._order: list[str] = []
        self._values: dict[str, dict[str, object]] = {}

    def start(self, tool_call_id: str, name: str, sequence: int) -> None:
        if tool_call_id in self._values:
            raise ModelStreamMaterializationError("duplicate model tool-call start")
        self._order.append(tool_call_id)
        self._values[tool_call_id] = {
            "name": name,
            "arguments": "",
            "start_sequence": sequence,
            "end_sequence": None,
        }

    def delta(self, tool_call_id: str, delta: str) -> None:
        value = self._values.get(tool_call_id)
        if value is None or value["end_sequence"] is not None:
            raise ModelStreamMaterializationError("tool delta outside active call")
        value["arguments"] = str(value["arguments"]) + delta

    def end(self, tool_call_id: str, sequence: int) -> None:
        value = self._values.get(tool_call_id)
        if value is None or value["end_sequence"] is not None:
            raise ModelStreamMaterializationError("tool end outside active call")
        value["end_sequence"] = sequence

    def facts(self, *, completed: bool) -> tuple[CommittedModelToolCallFact, ...]:
        output = []
        for tool_call_id in self._order:
            value = self._values[tool_call_id]
            end_sequence = value["end_sequence"]
            if completed and end_sequence is None:
                raise ModelStreamMaterializationError(
                    "completed model call contains an open tool call"
                )
            output.append(
                CommittedModelToolCallFact(
                    tool_call_id=tool_call_id,
                    tool_call_name=str(value["name"]),
                    raw_arguments_json=str(value["arguments"]),
                    start_sequence=int(value["start_sequence"]),
                    end_sequence=end_sequence,
                    completion_status=(
                        "completed" if end_sequence is not None else "interrupted"
                    ),
                )
            )
        return tuple(output)


__all__ = [
    "ModelStreamMaterializationError",
    "materialize_committed_model_call_result",
    "materialize_committed_model_call_result_from_events",
]
