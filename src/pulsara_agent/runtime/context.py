"""Context assembly helpers for the main agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pulsara_agent.llm.input import LLMMessage, LLMToolCall
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.llm.models import ModelRole
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
from pulsara_agent.runtime.context_engine import (
    CompiledContext,
    ContextCompileInputs,
    ContextCompileRequest,
    ContextLifecycleCoordinator,
    compile_context,
)
from pulsara_agent.runtime.context_engine.compiler import build_recovery_message
from pulsara_agent.runtime.state import LoopBudget, LoopState

if TYPE_CHECKING:
    from pulsara_agent.capability.exposure import CapabilityExposurePlan


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
    context_id: str | None = None,
    model_call_index: int = 0,
    model_role: ModelRole = ModelRole.PRO,
    exposure: "CapabilityExposurePlan | None" = None,
    current_user_anchor: str | None = None,
    runtime_session_id: str | None = None,
    component_prompts: tuple[tuple[str, str], ...] = (),
    lifecycle_coordinator: ContextLifecycleCoordinator | None = None,
) -> LLMContext:
    return build_compiled_context(
        state=state,
        tools=tools,
        system_prompt=system_prompt,
        budget=budget,
        context_id=context_id,
        model_call_index=model_call_index,
        model_role=model_role,
        exposure=exposure,
        current_user_anchor=current_user_anchor,
        runtime_session_id=runtime_session_id,
        component_prompts=component_prompts,
        lifecycle_coordinator=lifecycle_coordinator,
    ).llm_context


def build_compiled_context(
    state: LoopState,
    *,
    tools: tuple[ToolSpec, ...],
    system_prompt: str | None,
    budget: LoopBudget,
    context_id: str | None = None,
    model_call_index: int = 0,
    model_role: ModelRole = ModelRole.PRO,
    exposure: "CapabilityExposurePlan | None" = None,
    current_user_anchor: str | None = None,
    runtime_session_id: str | None = None,
    component_prompts: tuple[tuple[str, str], ...] = (),
    lifecycle_coordinator: ContextLifecycleCoordinator | None = None,
) -> CompiledContext:
    projection = _projection_component(state.memory_projection)
    prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    anchor = current_user_anchor or _current_user_anchor_for_state(state)
    segmented_messages = _segmented_llm_messages_for_anchor(state.messages, budget, anchor)
    messages = segmented_messages.full_messages
    current_user = _message_by_id(state, anchor) if anchor is not None else None
    current_user_input = _message_text(current_user) if current_user is not None else ""
    request = ContextCompileRequest(
        context_id=context_id or f"context:{uuid4().hex}",
        runtime_session_id=runtime_session_id or state.session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        model_call_index=model_call_index,
        model_role=model_role,
        state=state,
        current_user_message=current_user,
        current_user_input=current_user_input,
        current_user_anchor=anchor,
        tools=tools,
        exposure=exposure,
        budget=budget,
    )
    return compile_context(
        request,
        inputs=ContextCompileInputs(
            system_prompt=prompt,
            prior_messages=messages,
            prior_history_messages=segmented_messages.prior_history_messages,
            current_user_messages=segmented_messages.current_user_messages,
            current_run_tail_messages=segmented_messages.current_run_tail_messages,
            recovery_message=build_recovery_message(request),
            component_prompts=(*component_prompts, *((("memory:projection", projection),) if projection else ())),
        ),
        lifecycle_coordinator=lifecycle_coordinator,
    )


def msg_to_llm_messages(messages: list[Msg], budget: LoopBudget) -> tuple[LLMMessage, ...]:
    return _segmented_llm_messages_for_anchor(messages, budget, anchor=None).full_messages


@dataclass(frozen=True, slots=True)
class _SegmentedLLMMessages:
    full_messages: tuple[LLMMessage, ...]
    prior_history_messages: tuple[LLMMessage, ...] | None
    current_user_messages: tuple[LLMMessage, ...] | None
    current_run_tail_messages: tuple[LLMMessage, ...] | None


def _segmented_llm_messages_for_anchor(
    messages: list[Msg],
    budget: LoopBudget,
    anchor: str | None,
) -> _SegmentedLLMMessages:
    llm_messages: list[LLMMessage] = []
    prior_messages: list[LLMMessage] = []
    current_user_messages: list[LLMMessage] = []
    tail_messages: list[LLMMessage] = []
    anchor_index: int | None = None
    if anchor is not None:
        matches = [index for index, message in enumerate(messages) if message.id == anchor and message.role == "user"]
        if len(matches) == 1:
            anchor_index = matches[0]
    tool_budget = _ToolResultRenderBudget(budget.tool_result_context_chars)
    for index, message in enumerate(messages):
        converted = _message_to_llm_messages(message, tool_budget)
        llm_messages.extend(converted)
        if anchor_index is None:
            continue
        if index < anchor_index:
            prior_messages.extend(converted)
        elif index == anchor_index:
            current_user_messages.extend(converted)
        else:
            tail_messages.extend(converted)
    return _SegmentedLLMMessages(
        full_messages=tuple(llm_messages),
        prior_history_messages=tuple(prior_messages) if anchor_index is not None else None,
        current_user_messages=tuple(current_user_messages) if anchor_index is not None else None,
        current_run_tail_messages=tuple(tail_messages) if anchor_index is not None else None,
    )


def _message_to_llm_messages(message: Msg, tool_budget: "_ToolResultRenderBudget") -> list[LLMMessage]:
    if message.role == "user":
        parts = _textual_parts(message, tool_budget)
        return [LLMMessage.user("\n".join(parts))] if parts else []
    if message.role == "assistant":
        return _assistant_messages(message, tool_budget)
    if message.role == "system":
        parts = _textual_parts(message, tool_budget)
        return [LLMMessage.system("\n".join(parts))] if parts else []
    if message.role == "tool_result":
        return _tool_result_messages(message, tool_budget)
    return []


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


def _projection_component(projection: dict[str, Any] | None) -> str | None:
    if not projection:
        return None
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
    return "\n\n".join([heading, _projection_text(projection)])


def _current_user_anchor_for_state(state: LoopState) -> str | None:
    expected = f"user-message:{state.run_id}"
    if any(message.id == expected for message in state.messages):
        return expected
    return None


def _message_by_id(state: LoopState, message_id: str | None) -> Msg | None:
    if message_id is None:
        return None
    matches = [message for message in state.messages if message.id == message_id]
    if len(matches) != 1:
        return None
    return matches[0]


def _message_text(message: Msg | None) -> str:
    if message is None:
        return ""
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)
