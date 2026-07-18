"""Adapter-private raw provider item fixtures."""

from __future__ import annotations

from typing import Any

from pulsara_agent.llm.raw_provider import (
    RawProviderBlockEnd,
    RawProviderBlockStart,
    RawProviderDataDelta as _RawProviderDataDelta,
    RawProviderTextDelta as _RawProviderTextDelta,
    RawProviderThinkingDelta as _RawProviderThinkingDelta,
    RawProviderToolCallDelta as _RawProviderToolCallDelta,
)

_EVENT_FIELDS = {
    "created_at",
    "id",
    "metadata",
    "reply_id",
    "run_id",
    "sequence",
    "turn_id",
}


def _strip_event_fields(kwargs: dict[str, Any]) -> None:
    for field in _EVENT_FIELDS:
        kwargs.pop(field, None)


def RawProviderTextBlockStart(**kwargs: Any) -> RawProviderBlockStart:
    _strip_event_fields(kwargs)
    return RawProviderBlockStart(block_kind="text", **kwargs)


def RawProviderTextDelta(**kwargs: Any) -> _RawProviderTextDelta:
    _strip_event_fields(kwargs)
    return _RawProviderTextDelta(**kwargs)


def RawProviderTextBlockEnd(**kwargs: Any) -> RawProviderBlockEnd:
    _strip_event_fields(kwargs)
    return RawProviderBlockEnd(block_kind="text", **kwargs)


def RawProviderThinkingBlockStart(**kwargs: Any) -> RawProviderBlockStart:
    _strip_event_fields(kwargs)
    return RawProviderBlockStart(block_kind="thinking", **kwargs)


def RawProviderThinkingDelta(**kwargs: Any) -> _RawProviderThinkingDelta:
    _strip_event_fields(kwargs)
    return _RawProviderThinkingDelta(**kwargs)


def RawProviderThinkingBlockEnd(**kwargs: Any) -> RawProviderBlockEnd:
    _strip_event_fields(kwargs)
    return RawProviderBlockEnd(block_kind="thinking", **kwargs)


def RawProviderDataBlockStart(**kwargs: Any) -> RawProviderBlockStart:
    _strip_event_fields(kwargs)
    return RawProviderBlockStart(block_kind="data", **kwargs)


def RawProviderDataDelta(**kwargs: Any) -> _RawProviderDataDelta:
    _strip_event_fields(kwargs)
    if "delta" in kwargs:
        kwargs["data"] = kwargs.pop("delta")
    return _RawProviderDataDelta(**kwargs)


def RawProviderDataBlockEnd(**kwargs: Any) -> RawProviderBlockEnd:
    _strip_event_fields(kwargs)
    return RawProviderBlockEnd(block_kind="data", **kwargs)


def RawProviderToolCallStart(**kwargs: Any) -> RawProviderBlockStart:
    _strip_event_fields(kwargs)
    kwargs["block_id"] = kwargs.pop("tool_call_id")
    return RawProviderBlockStart(block_kind="tool_call", **kwargs)


def RawProviderToolCallDelta(**kwargs: Any) -> _RawProviderToolCallDelta:
    _strip_event_fields(kwargs)
    return _RawProviderToolCallDelta(**kwargs)


def RawProviderToolCallEnd(**kwargs: Any) -> RawProviderBlockEnd:
    _strip_event_fields(kwargs)
    kwargs["block_id"] = kwargs.pop("tool_call_id")
    return RawProviderBlockEnd(block_kind="tool_call", **kwargs)


__all__ = [
    "RawProviderDataBlockEnd",
    "RawProviderDataBlockStart",
    "RawProviderDataDelta",
    "RawProviderTextBlockEnd",
    "RawProviderTextBlockStart",
    "RawProviderTextDelta",
    "RawProviderThinkingBlockEnd",
    "RawProviderThinkingBlockStart",
    "RawProviderThinkingDelta",
    "RawProviderToolCallDelta",
    "RawProviderToolCallEnd",
    "RawProviderToolCallStart",
]
