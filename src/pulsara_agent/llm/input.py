"""Provider-neutral LLM input objects."""

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
    RUNTIME_OBSERVATION = "runtime_observation"


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
    """Provider-neutral input item sent to a model provider.

    Runtime messages are rebuilt from AgentEvent streams in ``pulsara_agent.message``.
    Text messages use ``content``. Tool transcripts use ``tool_call_id``,
    ``name``, and ``arguments`` so each provider adapter can emit its native
    tool-call and tool-result wire format.
    """

    role: MessageRole
    content: tuple[str, ...] = field(default_factory=tuple)
    thinking: tuple[str, ...] = field(default_factory=tuple)
    tool_calls: tuple[LLMToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None
    name: str | None = None
    arguments: str | None = None

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
    def assistant_turn(
        cls,
        *,
        text: str | None = None,
        thinking: str | tuple[str, ...] = (),
        tool_calls: tuple[LLMToolCall, ...] = (),
    ) -> "LLMMessage":
        content = (text,) if text else ()
        thinking_parts = (thinking,) if isinstance(thinking, str) and thinking else tuple(thinking)
        return cls(
            role=MessageRole.ASSISTANT,
            content=content,
            thinking=thinking_parts,
            tool_calls=tool_calls,
        )

    @classmethod
    def tool_call(cls, *, tool_call_id: str, name: str, arguments: str) -> "LLMMessage":
        return cls(
            role=MessageRole.TOOL_CALL,
            tool_call_id=tool_call_id,
            name=name,
            arguments=arguments,
        )

    @classmethod
    def tool_result(cls, text: str, *, tool_call_id: str | None = None) -> "LLMMessage":
        return cls(role=MessageRole.TOOL_RESULT, content=(text,), tool_call_id=tool_call_id)

    @classmethod
    def runtime_observation(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.RUNTIME_OBSERVATION, content=(text,))
