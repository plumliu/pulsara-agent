"""Conversation transcript reconstruction from AgentEvent logs."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event import ReplyEndEvent, RunEndEvent, RunStartEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.message import Msg, SystemMsg, UserMsg


FAILURE_NOTE_TEXT = (
    "Pulsara note: the previous turn did not complete because the runtime/provider step "
    "failed. The user's input above was preserved. Any assistant text above from that turn "
    "may be partial or empty; if the user asks to continue, continue from the preserved input."
)


@dataclass(frozen=True, slots=True)
class _FailedRunNoteTarget:
    run_id: str
    reply_id: str
    created_at: str | None


def rebuild_prior_messages(event_log: EventLog) -> list[Msg]:
    """Rebuild completed user/assistant turns from the canonical event log."""

    events = event_log.iter()
    failed_run_note_target = _last_failed_run_note_target(events)
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
        if _should_emit_failure_note(event, failed_run_note_target, noted_runs):
            messages.append(
                SystemMsg(
                    name="pulsara",
                    content=FAILURE_NOTE_TEXT,
                    id=f"failed-run-note:{event.run_id}",
                    created_at=event.created_at,
                    metadata={"run_id": event.run_id, "kind": "previous_turn_failed"},
                )
            )
            noted_runs.add(event.run_id)
        if isinstance(event, ReplyEndEvent):
            if event.reply_id in seen_replies:
                continue
            seen_replies.add(event.reply_id)
            messages.append(event_log.replay(event.reply_id))
            continue
        if event.reply_id in seen_replies:
            continue
    if failed_run_note_target is not None and failed_run_note_target.run_id not in noted_runs:
        messages.append(
            SystemMsg(
                name="pulsara",
                content=FAILURE_NOTE_TEXT,
                id=f"failed-run-note:{failed_run_note_target.run_id}",
                created_at=failed_run_note_target.created_at,
                metadata={"run_id": failed_run_note_target.run_id, "kind": "previous_turn_failed"},
            )
        )
    return messages


def _last_failed_run_note_target(events) -> _FailedRunNoteTarget | None:
    last_run_end: RunEndEvent | None = None
    for event in events:
        if isinstance(event, RunEndEvent):
            last_run_end = event
    if last_run_end is None or last_run_end.status != "failed":
        return None
    return _FailedRunNoteTarget(
        run_id=last_run_end.run_id,
        reply_id=last_run_end.reply_id,
        created_at=last_run_end.created_at,
    )


def _should_emit_failure_note(
    event,
    failed_run_note_target: _FailedRunNoteTarget | None,
    noted_runs: set[str],
) -> bool:
    if failed_run_note_target is None:
        return False
    if event.run_id != failed_run_note_target.run_id:
        return False
    if event.run_id in noted_runs:
        return False
    return isinstance(event, RunEndEvent)
