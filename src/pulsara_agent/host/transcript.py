"""Conversation transcript reconstruction from AgentEvent logs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.event import ReplyEndEvent, RunEndEvent, RunStartEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.message import Msg, SystemMsg, UserMsg


FAILURE_NOTE_TEXT = (
    "Pulsara note: the previous turn did not complete because the runtime/provider step "
    "failed. The user's input above was preserved. Any assistant text above from that turn "
    "may be partial or empty; if the user asks to continue, continue from the preserved input."
)

INTERRUPTED_NOTE_TEXT = (
    "Pulsara note: the previous turn was stopped by the user. The user's input from that turn "
    "was preserved. Any assistant text or tool work from that turn may be partial; if the user "
    "asks to continue, continue from the preserved input."
)


@dataclass(frozen=True, slots=True)
class _TerminalRunNoteTarget:
    run_id: str
    reply_id: str
    created_at: str | None
    kind: Literal["previous_turn_failed", "previous_turn_aborted"]
    text: str
    id_prefix: str


_NOTE_STATUS: dict[str, tuple[Literal["previous_turn_failed", "previous_turn_aborted"], str, str]] = {
    "failed": ("previous_turn_failed", FAILURE_NOTE_TEXT, "failed-run-note"),
    "aborted": ("previous_turn_aborted", INTERRUPTED_NOTE_TEXT, "aborted-run-note"),
}


def rebuild_prior_messages(event_log: EventLog) -> list[Msg]:
    """Rebuild completed user/assistant turns from the canonical event log."""

    events = event_log.iter()
    note_target = _last_terminal_run_note_target(events)
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
            messages.append(_note_message(note_target, created_at=event.created_at))
            noted_runs.add(event.run_id)
        if isinstance(event, ReplyEndEvent):
            if event.reply_id in seen_replies:
                continue
            seen_replies.add(event.reply_id)
            messages.append(event_log.replay(event.reply_id))
            continue
        if event.reply_id in seen_replies:
            continue
    if note_target is not None and note_target.run_id not in noted_runs:
        messages.append(_note_message(note_target, created_at=note_target.created_at))
    return messages


def _last_terminal_run_note_target(events) -> _TerminalRunNoteTarget | None:
    last_run_end: RunEndEvent | None = None
    for event in events:
        if isinstance(event, RunEndEvent):
            last_run_end = event
    if last_run_end is None:
        return None
    note = _NOTE_STATUS.get(last_run_end.status)
    if note is None:
        return None
    kind, text, id_prefix = note
    return _TerminalRunNoteTarget(
        run_id=last_run_end.run_id,
        reply_id=last_run_end.reply_id,
        created_at=last_run_end.created_at,
        kind=kind,
        text=text,
        id_prefix=id_prefix,
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


def _note_message(note_target: _TerminalRunNoteTarget, *, created_at: str | None) -> SystemMsg:
    return SystemMsg(
        name="pulsara",
        content=note_target.text,
        id=f"{note_target.id_prefix}:{note_target.run_id}",
        created_at=created_at,
        metadata={"run_id": note_target.run_id, "kind": note_target.kind},
    )
