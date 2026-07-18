"""Provider-neutral LLM request objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.primitives.context import context_fingerprint


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


def llm_context_fingerprint(context: LLMContext) -> str:
    """Canonical provider-neutral identity for one fully resolved input."""

    return context_fingerprint(
        "provider-neutral-llm-context:v1",
        {
            "system_prompt": context.system_prompt,
            "messages": tuple(
                {
                    "role": message.role.value,
                    "content": message.content,
                    "thinking": message.thinking,
                    "tool_calls": tuple(
                        {
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        for call in message.tool_calls
                    ),
                    "tool_call_id": message.tool_call_id,
                    "name": message.name,
                    "arguments": message.arguments,
                }
                for message in context.messages
            ),
            "tools": tuple(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in context.tools
            ),
            "context_id": context.context_id,
            "resolved_model_call_id": context.resolved_model_call_id,
            "target_fingerprint": context.target_fingerprint,
            "model_call_index": context.model_call_index,
            "compiler_estimated_input_tokens": (
                context.compiler_estimated_input_tokens
            ),
        },
    )
