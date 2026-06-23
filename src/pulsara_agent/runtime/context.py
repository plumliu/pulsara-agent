"""Context assembly helpers for the main agent loop."""

from __future__ import annotations

import json
from typing import Any

from pulsara_agent.llm.input import LLMMessage, LLMToolCall
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    URLSource,
)
from pulsara_agent.runtime.state import LoopBudget, LoopState
from pulsara_agent.tools.registry import ToolRegistry


DEFAULT_SYSTEM_PROMPT = (
    "You are Pulsara, an agentic coding runtime. Work carefully inside the current "
    "workspace, use tools when needed, and provide concise final answers."
)


def build_llm_context(
    state: LoopState,
    registry: ToolRegistry,
    system_prompt: str | None,
    budget: LoopBudget,
) -> LLMContext:
    prompt = _system_prompt_with_projection(system_prompt or DEFAULT_SYSTEM_PROMPT, state.memory_projection)
    messages = list(msg_to_llm_messages(state.messages, budget))
    if state.recovery_mode:
        messages.append(
            LLMMessage.user(
                "The previous model/tool step failed. Recover by inspecting the latest observation "
                "and either retry with corrected tool arguments or provide a final answer."
            )
        )
    return LLMContext(
        system_prompt=prompt,
        messages=tuple(messages),
        tools=registry.tool_specs(),
    )


def msg_to_llm_messages(messages: list[Msg], budget: LoopBudget) -> tuple[LLMMessage, ...]:
    llm_messages: list[LLMMessage] = []
    for message in messages:
        if message.role == "user":
            parts = _textual_parts(message, budget)
            if parts:
                llm_messages.append(LLMMessage.user("\n".join(parts)))
        elif message.role == "assistant":
            llm_messages.extend(_assistant_messages(message, budget))
        elif message.role == "system":
            parts = _textual_parts(message, budget)
            if parts:
                llm_messages.append(LLMMessage.system("\n".join(parts)))
        elif message.role == "tool_result":
            llm_messages.extend(_tool_result_messages(message, budget))
    return tuple(llm_messages)


def _assistant_messages(message: Msg, budget: LoopBudget) -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []

    def flush_assistant_turn() -> None:
        nonlocal text_parts, thinking_parts, tool_calls
        if not text_parts and not thinking_parts and not tool_calls:
            return
        messages.append(
            LLMMessage.assistant_turn(
                text="\n".join(text_parts),
                thinking=tuple(thinking_parts),
                tool_calls=tuple(tool_calls),
            )
        )
        text_parts = []
        thinking_parts = []
        tool_calls = []

    for block in message.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            thinking_parts.append(block.thinking)
        elif isinstance(block, DataBlock):
            text_parts.append(_data_placeholder(block))
        elif isinstance(block, ToolCallBlock):
            tool_calls.append(
                LLMToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input or "{}",
                )
            )
        elif isinstance(block, ToolResultBlock):
            flush_assistant_turn()
            messages.extend(_tool_result_messages(Msg(role="tool_result", name=block.name, content=[block]), budget))
    flush_assistant_turn()
    return messages


def _tool_result_messages(message: Msg, budget: LoopBudget) -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    remaining_tool_chars = budget.tool_result_context_chars
    for block in message.content:
        if not isinstance(block, ToolResultBlock):
            continue
        body, remaining_tool_chars = _render_tool_result_body(block, remaining_tool_chars)
        messages.append(
            LLMMessage.tool_result(
                f"[tool_result:{block.name}:{block.state.value}]\n{body}",
                tool_call_id=block.id,
            )
        )
    return messages


def _textual_parts(message: Msg, budget: LoopBudget) -> list[str]:
    parts: list[str] = []
    remaining_tool_chars = budget.tool_result_context_chars
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            body, remaining_tool_chars = _render_tool_result_body(block, remaining_tool_chars)
            parts.append(f"[tool_result:{block.name}:{block.state.value}]\n{body}")
        elif isinstance(block, DataBlock):
            parts.append(_data_placeholder(block))
        else:
            # Thinking and tool_call blocks are provider metadata, not natural-language context.
            continue
    return [part for part in parts if part]


def _tool_result_text(block: ToolResultBlock) -> str:
    parts: list[str] = []
    for output in block.output:
        if isinstance(output, TextBlock):
            parts.append(output.text)
        elif isinstance(output, DataBlock):
            parts.append(_data_placeholder(output))
    return "\n".join(parts)


def _render_tool_result_body(block: ToolResultBlock, remaining_tool_chars: int) -> tuple[str, int]:
    text = _tool_result_text(block)
    clipped, remaining_tool_chars = _clip_with_remaining(text, remaining_tool_chars)
    if not block.artifacts:
        return clipped, remaining_tool_chars
    primary = block.artifacts[0]
    # Heuristic: preview and artifact can use different textual representations
    # (for example terminal JSON preview vs. plain-text output artifact), so this
    # can be conservative near escaping boundaries.
    output_truncated = len(clipped.encode("utf-8")) < primary.size_bytes
    envelope = {
        "output_preview": clipped,
        "output_truncated": output_truncated,
        "artifacts": [artifact.to_model_payload() for artifact in block.artifacts],
    }
    return json.dumps(envelope, ensure_ascii=False), remaining_tool_chars


def _clip_with_remaining(text: str, remaining: int) -> tuple[str, int]:
    if remaining <= 0:
        return "[TOOL RESULT OMITTED: context budget exhausted]", 0
    if len(text) <= remaining:
        return text, remaining - len(text)
    marker = f"\n[TOOL RESULT TRUNCATED: kept {remaining} of {len(text)} chars]"
    kept = max(0, remaining - len(marker))
    return text[:kept] + marker, 0


def _data_placeholder(block: DataBlock) -> str:
    media_type = "unknown"
    source_kind = "data"
    if isinstance(block.source, Base64Source):
        media_type = block.source.media_type
        source_kind = "base64"
    elif isinstance(block.source, URLSource):
        media_type = block.source.media_type
        source_kind = "url"
    name = f" name={block.name}" if block.name else ""
    return f"[data block omitted id={block.id}{name} media_type={media_type} source={source_kind}]"


def _system_prompt_with_projection(system_prompt: str, projection: dict[str, Any] | None) -> str:
    if not projection:
        return system_prompt
    return "\n\n".join(
        [
            system_prompt,
            "Recalled Memory (source=fenced_recalled_memory; do not write it back as new memory):",
            _projection_text(projection),
        ]
    )


def _projection_text(projection: dict[str, Any]) -> str:
    summary = projection.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    items = projection.get("items")
    if isinstance(items, list):
        return "\n".join(f"- {item}" for item in items)
    return str(projection)
