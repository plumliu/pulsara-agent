"""Provider-neutral LLM request objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.llm.input import LLMMessage, ToolSpec


@dataclass(frozen=True, slots=True)
class LLMOptions:
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class LLMContext:
    messages: tuple[LLMMessage, ...]
    context_id: str
    resolved_model_call_id: str
    target_fingerprint: str
    model_call_index: int | None
    tools: tuple[ToolSpec, ...] = field(default_factory=tuple)
    system_prompt: str | None = None
    compiler_estimated_input_tokens: int | None = None
