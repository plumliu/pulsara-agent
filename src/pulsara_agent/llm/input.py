"""Provider-neutral LLM input objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """Text input sent to a model provider.

    Runtime messages are rebuilt from AgentEvent streams in ``pulsara_agent.message``.
    This object is intentionally narrower: it only represents prompt/input content
    before a provider adapter translates it to a wire format.
    """

    role: MessageRole
    content: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def system(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.SYSTEM, content=(text,))

    @classmethod
    def user(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.USER, content=(text,))

    @classmethod
    def assistant(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.ASSISTANT, content=(text,))

    @classmethod
    def tool_result(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.TOOL_RESULT, content=(text,))
