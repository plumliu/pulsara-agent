"""Small AgentRuntime loop helpers."""

from __future__ import annotations

from pulsara_agent.message import Msg, TextBlock
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.tool_loop import _tool_call_blocks


def _accumulate_usage(state: LoopState, message: Msg) -> None:
    if message.usage is None:
        return
    state.token_usage.input_tokens += message.usage.input_tokens
    state.token_usage.output_tokens += message.usage.output_tokens
    state.token_usage.total_tokens += message.usage.total_tokens


def _final_text(messages: list[Msg]) -> str:
    for message in reversed(messages):
        if message.role != "assistant" or _tool_call_blocks(message):
            continue
        return "\n".join(block.text for block in message.content if isinstance(block, TextBlock))
    return ""


def _projection_ids(projection: dict | None) -> list[str]:
    if not projection:
        return []
    ids = projection.get("included_memory_ids")
    if isinstance(ids, list):
        return [str(item) for item in ids]
    return []


def _projection_summary(projection: dict | None) -> str:
    if not projection:
        return ""
    summary = projection.get("summary")
    return summary if isinstance(summary, str) else str(projection)
