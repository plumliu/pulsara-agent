"""Test-only builders for the immutable memory-hook boundary."""

from __future__ import annotations

from pulsara_agent.ports.memory_hooks import (
    MemoryHookRunView,
    build_memory_hook_run_view,
)
from pulsara_agent.runtime.state import RunActivationWorkingState


def memory_hook_view(state: RunActivationWorkingState) -> MemoryHookRunView:
    current_index = (
        state.model_tool_progress.current_model_call_index
        or state.model_tool_progress.model_call_index + 1
    )
    return build_memory_hook_run_view(
        runtime_session_id=state.session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        status=state.status.value,
        messages=state.messages,
        usage=state.token_usage,
        current_projection=state.memory_projection,
        model_step_key=f"{state.run_id}:{current_index}",
    )


__all__ = ["memory_hook_view"]
