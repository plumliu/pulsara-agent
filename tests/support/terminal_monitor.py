"""Canonical terminal-monitor event fixtures."""

from __future__ import annotations

from pulsara_agent.event import EventContext, TerminalProcessCompletedEvent
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_observation import (
    TerminalProcessCompletionSemanticFact,
    build_terminal_lifecycle_outcome,
)
from pulsara_agent.runtime.terminal.output import SanitizedOutputJournal


def terminal_process_completed_event(
    *,
    event_context: EventContext,
    process_id: str,
    terminal_session_id: str = "default",
    command: str = "pytest -q",
    status: str = "success",
    exit_code: int = 0,
    cwd: str = "/workspace",
    duration_seconds: float = 1.0,
    output_preview: str = "",
    output_truncated: bool = False,
    tool_call_id: str | None = None,
    completion_reason: str | None = None,
    event_id: str | None = None,
    owner_host_session_id: str | None = None,
) -> TerminalProcessCompletedEvent:
    journal = SanitizedOutputJournal(process_id=process_id)
    if output_preview:
        journal.append(output_preview.encode("utf-8"))
    journal.finish()
    outcome = build_terminal_lifecycle_outcome(
        status=status,
        exit_code=exit_code,
        kill_reason=completion_reason,
    )
    semantic = build_frozen_fact(
        TerminalProcessCompletionSemanticFact,
        schema_version="terminal_process_completion_semantic.v1",
        terminal_output_cursor=journal.end_cursor,
        outcome=outcome,
    )
    return TerminalProcessCompletedEvent(
        id=event_id or f"terminal_process_completed:{process_id}",
        **event_context.event_fields(),
        completion_semantic=semantic,
        terminal_session_id=terminal_session_id,
        command=command,
        cwd=cwd,
        duration_seconds=duration_seconds,
        output_preview=output_preview,
        output_truncated=output_truncated,
        tool_call_id=tool_call_id,
        output_recovery_reference=journal.recovery_reference(),
        owner_host_session_id=owner_host_session_id,
        owner_conversation_id=None,
        origin_runtime_session_id="runtime:test",
        origin_run_entry_kind=(
            "host_main_run" if owner_host_session_id is not None else "test"
        ),
    )


__all__ = ["terminal_process_completed_event"]
