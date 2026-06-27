"""Conversation transcript reconstruction from AgentEvent logs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.event import ReplyEndEvent, RunEndEvent, RunStartEvent, TerminalProcessCompletedEvent, ToolResultEndEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.message import DataBlock, TextBlock, ToolCallBlock, ToolResultBlock
from pulsara_agent.message import Msg, SystemMsg, UserMsg
from pulsara_agent.runtime.recovery import (
    FAILURE_NOTE_TEXT as FAILURE_NOTE_TEXT,
    INTERRUPTED_NOTE_TEXT as INTERRUPTED_NOTE_TEXT,
    RECOVERABLE_RUN_STATUSES,
    RECOVERY_NOTE_ID_PREFIX_BY_STATUS,
    RECOVERY_NOTE_KIND_BY_STATUS,
    RecoveryProjection,
    project_recovery_from_events,
    render_recovery_text,
)

__all__ = [
    "FAILURE_NOTE_TEXT",
    "INTERRUPTED_NOTE_TEXT",
    "rebuild_prior_messages",
]

_MAX_COMPLETION_NOTES = 3


@dataclass(frozen=True, slots=True)
class _TerminalRunNoteTarget:
    run_id: str
    reply_id: str
    created_at: str | None
    status: str
    kind: Literal["previous_turn_failed", "previous_turn_aborted"]
    id_prefix: str


def rebuild_prior_messages(event_log: EventLog) -> list[Msg]:
    """Rebuild completed user/assistant turns from the canonical event log."""

    events = event_log.iter()
    recovery = project_recovery_from_events(events)
    note_target = _last_terminal_run_note_target(events, recovery)
    completion_note = _completion_note_after_last_run_start(events)
    terminal_run_ids = _terminal_run_ids(events)
    completed_tool_call_ids_by_run = _completed_tool_call_ids_by_run(events)
    messages: list[Msg] = []
    seen_replies: set[str] = set()
    noted_runs: set[str] = set()
    for event in events:
        if isinstance(event, RunStartEvent):
            user_input = event.metadata.get("user_input")
            if isinstance(user_input, str):
                messages.append(
                    UserMsg(
                        name="user",
                        content=user_input,
                        id=f"user-message:{event.run_id}",
                        created_at=event.created_at,
                        metadata={"run_id": event.run_id},
                    )
                )
        if _should_emit_terminal_note(event, note_target, noted_runs):
            messages.append(_note_message(note_target, recovery=recovery, created_at=event.created_at))
            noted_runs.add(event.run_id)
        if isinstance(event, ReplyEndEvent):
            if event.reply_id in seen_replies:
                continue
            seen_replies.add(event.reply_id)
            message = event_log.replay(event.reply_id)
            if event.run_id in terminal_run_ids:
                message = _strip_unfinished_tool_calls(
                    message,
                    completed_tool_call_ids=completed_tool_call_ids_by_run.get(event.run_id, set()),
                )
            if message is not None:
                messages.append(message)
            continue
        if event.reply_id in seen_replies:
            continue
    if note_target is not None and note_target.run_id not in noted_runs:
        messages.append(_note_message(note_target, recovery=recovery, created_at=note_target.created_at))
    if completion_note is not None:
        messages.append(completion_note)
    return messages


def _last_terminal_run_note_target(
    events,
    recovery: RecoveryProjection | None,
) -> _TerminalRunNoteTarget | None:
    last_run_end: RunEndEvent | None = None
    for event in events:
        if isinstance(event, RunEndEvent):
            last_run_end = event
    if last_run_end is None:
        return None
    if recovery is None or last_run_end.status not in RECOVERABLE_RUN_STATUSES:
        return None
    return _TerminalRunNoteTarget(
        run_id=last_run_end.run_id,
        reply_id=last_run_end.reply_id,
        created_at=last_run_end.created_at,
        status=last_run_end.status,
        kind=RECOVERY_NOTE_KIND_BY_STATUS[last_run_end.status],
        id_prefix=RECOVERY_NOTE_ID_PREFIX_BY_STATUS[last_run_end.status],
    )


def _should_emit_terminal_note(
    event,
    note_target: _TerminalRunNoteTarget | None,
    noted_runs: set[str],
) -> bool:
    if note_target is None:
        return False
    if event.run_id != note_target.run_id:
        return False
    if event.run_id in noted_runs:
        return False
    return isinstance(event, RunEndEvent)


def _terminal_run_ids(events: list) -> set[str]:
    return {
        event.run_id
        for event in events
        if isinstance(event, RunEndEvent) and event.status in RECOVERABLE_RUN_STATUSES
    }


def _completed_tool_call_ids_by_run(events: list) -> dict[str, set[str]]:
    completed: dict[str, set[str]] = {}
    for event in events:
        if isinstance(event, ToolResultEndEvent):
            completed.setdefault(event.run_id, set()).add(event.tool_call_id)
    return completed


def _strip_unfinished_tool_calls(
    message: Msg,
    *,
    completed_tool_call_ids: set[str],
) -> Msg | None:
    filtered = [
        block
        for block in message.content
        if not isinstance(block, ToolCallBlock) or block.id in completed_tool_call_ids
    ]
    if filtered == message.content:
        return message
    # A stopped pending-approval turn can end after the model emitted only a
    # tool call. Replaying that assistant turn without a following tool result
    # violates Chat Completions ordering, so keep only user input + stop note.
    if not any(isinstance(block, (TextBlock, DataBlock, ToolResultBlock)) for block in filtered):
        return None
    return message.model_copy(update={"content": filtered})


def _note_message(
    note_target: _TerminalRunNoteTarget,
    *,
    created_at: str | None,
    recovery: RecoveryProjection | None,
) -> SystemMsg:
    content = ""
    if recovery is not None:
        content = render_recovery_text(recovery, audience="transcript")
    return SystemMsg(
        name="pulsara",
        content=content,
        id=f"{note_target.id_prefix}:{note_target.run_id}",
        created_at=created_at,
        metadata={"run_id": note_target.run_id, "kind": note_target.kind},
    )


def _completion_note_after_last_run_start(events: list) -> SystemMsg | None:
    last_run_start_sequence = max(
        ((event.sequence or 0) for event in events if isinstance(event, RunStartEvent)),
        default=0,
    )
    if last_run_start_sequence <= 0:
        return None
    completions = [
        event
        for event in events
        if isinstance(event, TerminalProcessCompletedEvent)
        and (event.sequence or 0) > last_run_start_sequence
    ]
    if not completions:
        return None
    selected = completions[:_MAX_COMPLETION_NOTES]
    lines = [_completion_note_line(event) for event in selected]
    remaining = len(completions) - len(selected)
    if remaining > 0:
        lines.append(f"{remaining} more terminal task(s) completed; use terminal_process list if still retained.")
    latest = completions[-1]
    process_ids = ",".join(event.process_id for event in selected)
    return SystemMsg(
        name="pulsara",
        content="Pulsara note: terminal background task update. " + " ".join(lines),
        id=f"terminal-completion-note:{latest.sequence or latest.id}",
        created_at=latest.created_at,
        metadata={
            "kind": "terminal_process_completed",
            "process_ids": process_ids,
        },
    )


def _completion_note_line(event: TerminalProcessCompletedEvent) -> str:
    exit_code = event.exit_code
    return (
        f"Process {event.process_id} completed with status {event.status} "
        f"and exit code {exit_code}. This note is lifecycle-only, not the full output; "
        f"if still retained, inspect retained output with terminal_process log."
    )
