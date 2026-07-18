"""Deterministic adapter-private provider items for offline benchmarks."""

from __future__ import annotations

from typing import Any

from pulsara_agent.llm.raw_provider import (
    RawProviderBlockEnd,
    RawProviderBlockStart,
    RawProviderTextDelta,
    RawProviderThinkingDelta,
    RawProviderToolCallDelta,
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


def _clean(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key not in _EVENT_FIELDS}


def text_start(**kwargs: Any) -> RawProviderBlockStart:
    return RawProviderBlockStart(block_kind="text", **_clean(kwargs))


def text_delta(**kwargs: Any) -> RawProviderTextDelta:
    return RawProviderTextDelta(**_clean(kwargs))


def text_end(**kwargs: Any) -> RawProviderBlockEnd:
    return RawProviderBlockEnd(block_kind="text", **_clean(kwargs))


def thinking_start(**kwargs: Any) -> RawProviderBlockStart:
    return RawProviderBlockStart(block_kind="thinking", **_clean(kwargs))


def thinking_delta(**kwargs: Any) -> RawProviderThinkingDelta:
    return RawProviderThinkingDelta(**_clean(kwargs))


def thinking_end(**kwargs: Any) -> RawProviderBlockEnd:
    return RawProviderBlockEnd(block_kind="thinking", **_clean(kwargs))


def tool_call_start(**kwargs: Any) -> RawProviderBlockStart:
    cleaned = _clean(kwargs)
    cleaned["block_id"] = cleaned.pop("tool_call_id")
    return RawProviderBlockStart(block_kind="tool_call", **cleaned)


def tool_call_delta(**kwargs: Any) -> RawProviderToolCallDelta:
    return RawProviderToolCallDelta(**_clean(kwargs))


def tool_call_end(**kwargs: Any) -> RawProviderBlockEnd:
    cleaned = _clean(kwargs)
    cleaned["block_id"] = cleaned.pop("tool_call_id")
    return RawProviderBlockEnd(block_kind="tool_call", **cleaned)


__all__ = [
    "text_delta",
    "text_end",
    "text_start",
    "thinking_delta",
    "thinking_end",
    "thinking_start",
    "tool_call_delta",
    "tool_call_end",
    "tool_call_start",
]
