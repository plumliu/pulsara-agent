"""Context assembly helpers for the main agent loop."""

from __future__ import annotations

import json
from typing import Any

from pulsara_agent.llm.input import LLMMessage, LLMToolCall
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultArtifactRef,
    ToolResultBlock,
    URLSource,
)
from pulsara_agent.runtime.recovery import project_recovery_from_state, render_recovery_text
from pulsara_agent.runtime.state import LoopBudget, LoopState


DEFAULT_SYSTEM_PROMPT = (
    "You are Pulsara, an agentic coding runtime. Work carefully inside the current "
    "workspace, use tools when needed, and provide concise final answers."
)


def build_llm_context(
    state: LoopState,
    *,
    tools: tuple[ToolSpec, ...],
    system_prompt: str | None,
    budget: LoopBudget,
) -> LLMContext:
    prompt = _system_prompt_with_projection(system_prompt or DEFAULT_SYSTEM_PROMPT, state.memory_projection)
    messages = list(msg_to_llm_messages(state.messages, budget))
    recovery = project_recovery_from_state(state)
    if recovery is not None:
        messages.append(
            LLMMessage.user(render_recovery_text(recovery, audience="prompt"))
        )
    return LLMContext(
        system_prompt=prompt,
        messages=tuple(messages),
        tools=tools,
    )


def msg_to_llm_messages(messages: list[Msg], budget: LoopBudget) -> tuple[LLMMessage, ...]:
    llm_messages: list[LLMMessage] = []
    tool_budget = _ToolResultRenderBudget(budget.tool_result_context_chars)
    for message in messages:
        if message.role == "user":
            parts = _textual_parts(message, tool_budget)
            if parts:
                llm_messages.append(LLMMessage.user("\n".join(parts)))
        elif message.role == "assistant":
            llm_messages.extend(_assistant_messages(message, tool_budget))
        elif message.role == "system":
            parts = _textual_parts(message, tool_budget)
            if parts:
                llm_messages.append(LLMMessage.system("\n".join(parts)))
        elif message.role == "tool_result":
            llm_messages.extend(_tool_result_messages(message, tool_budget))
    return tuple(llm_messages)


def _assistant_messages(message: Msg, tool_budget: "_ToolResultRenderBudget") -> list[LLMMessage]:
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
            messages.extend(_tool_result_messages(Msg(role="tool_result", name=block.name, content=[block]), tool_budget))
    flush_assistant_turn()
    return messages


def _tool_result_messages(message: Msg, tool_budget: "_ToolResultRenderBudget") -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    for block in message.content:
        if not isinstance(block, ToolResultBlock):
            continue
        body = _render_tool_result_body(block, tool_budget)
        messages.append(
            LLMMessage.tool_result(
                f"[tool_result:{block.name}:{block.state.value}]\n{body}",
                tool_call_id=block.id,
            )
        )
    return messages


def _textual_parts(message: Msg, tool_budget: "_ToolResultRenderBudget") -> list[str]:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            body = _render_tool_result_body(block, tool_budget)
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


class _ToolResultRenderBudget:
    def __init__(self, chars: int) -> None:
        self.remaining = chars

    def consume(self, text: str) -> str:
        clipped, remaining = _clip_with_remaining(text, self.remaining)
        self.remaining = remaining
        return clipped


def _render_tool_result_body(block: ToolResultBlock, budget: _ToolResultRenderBudget) -> str:
    text = _tool_result_text(block)
    if not block.artifacts:
        return budget.consume(text)
    primary = block.artifacts[0]
    # Heuristic: preview and artifact can use different textual representations
    # (for example terminal JSON preview vs. plain-text output artifact), so this
    # can be conservative near escaping boundaries.
    if budget.remaining <= 0:
        return _compact_artifact_envelope(block)
    artifact_payloads = [artifact.to_model_payload() for artifact in block.artifacts]
    envelope_overhead = len(json.dumps(
        {
            "output_preview": "",
            "output_truncated": False,
            "artifacts": artifact_payloads,
        },
        ensure_ascii=False,
    ))
    body_budget = budget.remaining - envelope_overhead
    if body_budget <= 0:
        body_budget = budget.remaining
    clipped, _ = _clip_with_remaining(text, body_budget)
    output_truncated = len(clipped) < len(text) or len(clipped.encode("utf-8")) < primary.size_bytes
    envelope = {
        "output_preview": clipped,
        "output_truncated": output_truncated,
        "artifacts": artifact_payloads,
    }
    rendered = json.dumps(envelope, ensure_ascii=False)
    if len(rendered) > budget.remaining:
        compact = _compact_artifact_envelope(block)
        if budget.remaining <= 0 or len(compact) <= len(rendered):
            budget.remaining = 0
            return compact
        # Keep the artifact envelope parseable and preserve artifact refs even
        # when the remaining aggregate budget is smaller than the metadata
        # overhead. The output body has already been reduced as far as possible.
        budget.remaining = 0
        return rendered
    return budget.consume(rendered)


def _compact_artifact_envelope(block: ToolResultBlock) -> str:
    artifact = next((artifact for artifact in block.artifacts if artifact.preview is not None), None)
    if artifact is None and block.artifacts:
        artifact = block.artifacts[0]
    refs = [_compact_artifact_ref_payload(artifact)] if artifact is not None else []
    omitted = max(0, len(block.artifacts) - len(refs))
    return json.dumps(
        {
            "output_preview": "[TOOL RESULT OMITTED: aggregate context budget exhausted]",
            "output_truncated": True,
            "artifacts": refs,
            "artifact_refs_omitted": omitted,
        },
        ensure_ascii=False,
    )


def _compact_artifact_ref_payload(artifact: ToolResultArtifactRef) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_id": artifact.artifact_id,
        "role": artifact.role,
        "size_bytes": artifact.size_bytes,
        "read_more": {"tool": "artifact_read", "artifact_id": artifact.artifact_id},
    }
    if not artifact.stored_complete:
        payload["stored_complete"] = artifact.stored_complete
    if artifact.loss_reason is not None:
        payload["loss_reason"] = artifact.loss_reason
    if artifact.preview is not None:
        read_more = _compact_read_more_payload(artifact)
        payload["preview"] = {
            "preview_policy": artifact.preview.preview_policy,
            "visible_head_chars": artifact.preview.visible_head_chars,
            "read_more": read_more,
        }
        payload["read_more"] = read_more
    return payload


def _compact_read_more_payload(artifact: ToolResultArtifactRef) -> dict[str, object]:
    read_more: dict[str, object] = {"tool": "artifact_read", "artifact_id": artifact.artifact_id}
    if artifact.preview is None:
        return read_more
    for key in ("suggested_offset_chars", "suggested_max_chars"):
        value = artifact.preview.read_more.get(key)
        if isinstance(value, int):
            read_more[key] = value
    return read_more


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
    projection_kind = projection.get("projection_kind")
    if projection_kind in {"working_context", "mixed"}:
        heading = (
            "Recalled Memory and Recent Working Context "
            "(source=fenced_memory_context; do not write it back as new memory):\n"
            "Recent Working Context is independent from canonical memory search. "
            "An empty memory_search result does not invalidate recent activity shown here."
        )
    else:
        heading = "Recalled Memory (source=fenced_recalled_memory; do not write it back as new memory):"
    return "\n\n".join(
        [
            system_prompt,
            heading,
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
