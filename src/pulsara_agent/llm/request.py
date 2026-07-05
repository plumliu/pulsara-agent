"""Provider-neutral LLM request objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.llm.input import LLMMessage, ToolSpec


@dataclass(frozen=True, slots=True)
class LLMOptions:
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None


@dataclass(frozen=True, slots=True)
class LLMContext:
    messages: tuple[LLMMessage, ...]
    tools: tuple[ToolSpec, ...] = field(default_factory=tuple)
    system_prompt: str | None = None
    context_id: str | None = None
    model_call_index: int | None = None
