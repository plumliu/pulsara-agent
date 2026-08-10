"""Small provider-neutral message vocabulary for the conversation Kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str = "{}"


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One complete provider input item; it owns no runtime lifecycle state."""

    role: MessageRole
    content: tuple[str, ...] = field(default_factory=tuple)
    thinking: tuple[str, ...] = field(default_factory=tuple)
    tool_calls: tuple[LLMToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None
    name: str | None = None
    arguments: str | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(part, str) for part in self.content + self.thinking):
            raise TypeError("model message text parts must be strings")
        if self.role in {MessageRole.SYSTEM, MessageRole.USER} and (
            self.tool_calls
            or self.tool_call_id is not None
            or self.name is not None
            or self.arguments is not None
        ):
            raise ValueError("textual provider message has tool-only fields")

    @classmethod
    def system(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.SYSTEM, content=(text,))

    @classmethod
    def user(
        cls,
        text: str,
        *,
        causal_occurrence_semantic_fingerprint: str | None = None,
    ) -> "LLMMessage":
        # Occurrence attribution belongs to canonical conversation rows.  It is
        # intentionally not duplicated into the process-local provider carrier.
        del causal_occurrence_semantic_fingerprint
        return cls(role=MessageRole.USER, content=(text,))

    @classmethod
    def assistant(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.ASSISTANT, content=(text,))

    @classmethod
    def assistant_turn(
        cls,
        *,
        text: str | None = None,
        thinking: str | tuple[str, ...] = (),
        tool_calls: tuple[LLMToolCall, ...] = (),
    ) -> "LLMMessage":
        content = (text,) if text else ()
        thinking_parts = (
            (thinking,) if isinstance(thinking, str) and thinking else tuple(thinking)
        )
        return cls(
            role=MessageRole.ASSISTANT,
            content=content,
            thinking=thinking_parts,
            tool_calls=tool_calls,
        )

    @classmethod
    def tool_call(
        cls, *, tool_call_id: str, name: str, arguments: str
    ) -> "LLMMessage":
        return cls(
            role=MessageRole.TOOL_CALL,
            tool_call_id=tool_call_id,
            name=name,
            arguments=arguments,
        )

    @classmethod
    def tool_result(
        cls, text: str, *, tool_call_id: str | None = None
    ) -> "LLMMessage":
        return cls(
            role=MessageRole.TOOL_RESULT,
            content=(text,),
            tool_call_id=tool_call_id,
        )


__all__ = ["LLMMessage", "LLMToolCall", "MessageRole", "ToolSpec"]
