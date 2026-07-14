"""Conversation transcript reconstruction from AgentEvent logs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionCompletedEvent,
    EventType,
    ReplyStartEvent,
    ReplyEndEvent,
    RunEndEvent,
    RunStartEvent,
    TerminalProcessCompletedEvent,
    ToolResultEndEvent,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY, EventLog
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import DataBlock, TextBlock, ToolCallBlock, ToolResultBlock
from pulsara_agent.message import AssistantMsg, Msg, SystemMsg, UserMsg
from pulsara_agent.message.reducer import (
    MessageReducer,
    MessageReplayControlError,
    require_canonical_reply_control,
)
from pulsara_agent.runtime.compaction.planner import (
    build_compaction_summary_message,
    latest_completed_boundary,
)
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
    "rebuild_prior_messages_before_sequence",
    "rebuild_prior_messages_bounded",
]

_MAX_COMPLETION_NOTES = 3
_MAX_TRANSCRIPT_CONTROL_EVENTS = 16_384
_MAX_TRANSCRIPT_CONTROL_BYTES = 16 * 1024 * 1024
_MAX_REPLY_EVENTS = 16_384
_MAX_REPLY_BYTES = 16 * 1024 * 1024
_MAX_COMPACTION_CHECKPOINT_CANDIDATES = 8


@dataclass(frozen=True, slots=True)
class BoundedPriorTranscript:
    messages: tuple[Msg, ...]
    source_through_sequence: int
    source_event_count: int
    checkpoint_event: ContextCompactionCompletedEvent | None


def rebuild_prior_messages_bounded(
    event_log: EventLog,
    *,
    archive: ArtifactStore,
    session_id: str,
    deadline_monotonic: float,
) -> BoundedPriorTranscript:
    """Rebuild from the latest durable compaction checkpoint plus bounded facts."""

    boundary = None
    candidates = event_log.read_raw_events_by_type(
        str(EventType.CONTEXT_COMPACTION_COMPLETED),
        limit=_MAX_COMPACTION_CHECKPOINT_CANDIDATES,
        deadline_monotonic=deadline_monotonic,
    )
    for raw in candidates:
        event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(event, ContextCompactionCompletedEvent):
            raise ValueError("compaction checkpoint query returned another event type")
        try:
            summary = archive.get_text(
                event.summary_artifact_id,
                session_id=session_id,
                deadline_monotonic=deadline_monotonic,
            )
        except (KeyError, ValueError):
            continue
        from pulsara_agent.runtime.compaction.planner import CompactionBoundary

        boundary = CompactionBoundary(event=event, summary_text=summary)
        break
    minimum_sequence = boundary.keep_after_sequence + 1 if boundary else 1
    relevant_types = (
        str(EventType.RUN_START),
        str(EventType.RUN_END),
        str(EventType.MODEL_CALL_START),
        str(EventType.MODEL_CALL_END),
        str(EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED),
        str(EventType.REPLY_END),
        str(EventType.TOOL_CALL_START),
        str(EventType.TOOL_RESULT_START),
        str(EventType.TOOL_RESULT_END),
        str(EventType.REQUIRE_USER_CONFIRM),
        str(EventType.TERMINAL_PROCESS_COMPLETED),
        str(EventType.PLAN_MODE_ENTERED),
        str(EventType.PLAN_MODE_EXITED),
    )
    sparse = event_log.read_raw_events_by_types(
        relevant_types,
        minimum_sequence=minimum_sequence,
        max_events=_MAX_TRANSCRIPT_CONTROL_EVENTS,
        max_payload_bytes=_MAX_TRANSCRIPT_CONTROL_BYTES,
        deadline_monotonic=deadline_monotonic,
    )
    events = [
        item.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY) for item in sparse.events
    ]
    reply_ids = tuple(
        dict.fromkeys(
            event.reply_id for event in events if isinstance(event, ReplyEndEvent)
        )
    )
    reply_events_by_id: dict[str, tuple[AgentEvent, ...]] = {}
    if reply_ids:
        reply_snapshot = event_log.read_raw_replies_snapshot(
            reply_ids,
            through_sequence=sparse.through_sequence,
            max_total_events=_MAX_REPLY_EVENTS,
            max_total_payload_bytes=_MAX_REPLY_BYTES,
            deadline_monotonic=deadline_monotonic,
        )
        reply_events_by_id = {
            group.reply_id: tuple(
                item.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                for item in group.events
            )
            for group in reply_snapshot.groups
        }

    def load_reply(reply_id: str) -> Msg:
        decoded = reply_events_by_id.get(reply_id)
        if decoded is None:
            raise MessageReplayControlError(
                "bounded transcript reply is absent from its frozen snapshot"
            )
        require_canonical_reply_control(decoded)
        start = next(
            (item for item in decoded if isinstance(item, ReplyStartEvent)), None
        )
        message = AssistantMsg(
            id=reply_id,
            name=start.name if start else "assistant",
            content=[],
            created_at=start.created_at if start else None,
        )
        reducer = MessageReducer(message)
        for item in decoded:
            reducer.append(item)
        return reducer.message

    messages = _rebuild_messages_from_events(
        event_log,
        events,
        include_completion_note=True,
        reply_loader=load_reply,
    )
    if boundary is not None:
        messages.insert(0, build_compaction_summary_message(boundary))
    return BoundedPriorTranscript(
        messages=tuple(messages),
        source_through_sequence=sparse.through_sequence,
        source_event_count=sparse.through_sequence,
        checkpoint_event=boundary.event if boundary is not None else None,
    )


@dataclass(frozen=True, slots=True)
class _TerminalRunNoteTarget:
    run_id: str
    reply_id: str
    created_at: str | None
    status: str
    kind: Literal["previous_turn_failed", "previous_turn_aborted"]
    id_prefix: str


def rebuild_prior_messages(
    event_log: EventLog,
    *,
    archive: ArtifactStore | None = None,
    session_id: str | None = None,
) -> list[Msg]:
    """Rebuild completed user/assistant turns from the canonical event log."""

    events = event_log.iter()
    boundary = latest_completed_boundary(events, archive=archive, session_id=session_id)
    prefix: list[Msg] = []
    if boundary is not None:
        prefix.append(build_compaction_summary_message(boundary))
        events = [
            event
            for event in events
            if event.sequence is not None and event.sequence > boundary.keep_after_sequence
        ]
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
            messages.append(
                UserMsg(
                    name="user",
                    content=event.current_user_message.text,
                    id=event.current_user_message.message_id,
                    created_at=event.current_user_message.observed_at_utc,
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
            try:
                message = event_log.replay(event.reply_id)
            except MessageReplayControlError:
                if event.run_id not in terminal_run_ids:
                    raise
                message = None
            if message is not None and event.run_id in terminal_run_ids:
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
    return prefix + messages


def rebuild_prior_messages_before_sequence(
    event_log: EventLog,
    *,
    archive: ArtifactStore | None = None,
    session_id: str | None = None,
    before_sequence: int,
) -> list[Msg]:
    """Rebuild compact-aware prior messages strictly before ``before_sequence``.

    Mid-turn compaction writes its completed boundary after current-run events,
    while the boundary's ``keep_after_sequence`` points to a historical prefix
    before the current run. This helper may use that boundary, but it must only
    replay events whose sequence is lower than ``before_sequence`` so the
    active run tail can be preserved from in-memory ``LoopState.messages``.
    """

    events = event_log.iter()
    boundary = _latest_completed_boundary_before_sequence(
        events,
        archive=archive,
        session_id=session_id,
        before_sequence=before_sequence,
    )
    prefix: list[Msg] = []
    keep_after_sequence = 0
    if boundary is not None:
        prefix.append(build_compaction_summary_message(boundary))
        keep_after_sequence = boundary.keep_after_sequence
    replay_events = [
        event
        for event in events
        if event.sequence is not None
        and keep_after_sequence < event.sequence < before_sequence
    ]
    return prefix + _rebuild_messages_from_events(event_log, replay_events, include_completion_note=False)


def _latest_completed_boundary_before_sequence(
    events: list[AgentEvent],
    *,
    archive: ArtifactStore | None,
    session_id: str | None,
    before_sequence: int,
):
    if archive is None:
        return None
    for event in reversed(events):
        if not isinstance(event, ContextCompactionCompletedEvent):
            continue
        if event.keep_after_sequence >= before_sequence:
            continue
        try:
            summary = archive.get_text(event.summary_artifact_id, session_id=session_id)
        except Exception:
            continue
        from pulsara_agent.runtime.compaction.planner import CompactionBoundary

        return CompactionBoundary(event=event, summary_text=summary)
    return None


def _rebuild_messages_from_events(
    event_log: EventLog,
    events: list[AgentEvent],
    *,
    include_completion_note: bool,
    reply_loader: Callable[[str], Msg] | None = None,
) -> list[Msg]:
    recovery = project_recovery_from_events(events)
    note_target = _last_terminal_run_note_target(events, recovery)
    completion_note = _completion_note_after_last_run_start(events) if include_completion_note else None
    terminal_run_ids = _terminal_run_ids(events)
    completed_tool_call_ids_by_run = _completed_tool_call_ids_by_run(events)
    messages: list[Msg] = []
    seen_replies: set[str] = set()
    noted_runs: set[str] = set()
    for event in events:
        if isinstance(event, RunStartEvent):
            messages.append(
                UserMsg(
                    name="user",
                    content=event.current_user_message.text,
                    id=event.current_user_message.message_id,
                    created_at=event.current_user_message.observed_at_utc,
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
            try:
                message = (
                    reply_loader(event.reply_id)
                    if reply_loader is not None
                    else event_log.replay(event.reply_id)
                )
            except MessageReplayControlError:
                if event.run_id not in terminal_run_ids:
                    raise
                message = None
            if message is not None and event.run_id in terminal_run_ids:
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
