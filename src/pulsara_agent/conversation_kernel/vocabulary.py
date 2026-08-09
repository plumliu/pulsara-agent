"""Closed Stage 2 event vocabulary and subject/guard descriptor.

The descriptor is the single Python owner for the exact 26/23/13/2 oracle.
SQL checks, repository validation, protocol projection mapping, and generated
test fixtures consume these values; callers cannot register new entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AppendGuardKind(StrEnum):
    HOST_WRITER = "HostWriterGuard"
    JOB_ATTEMPT_CLAIM = "JobAttemptClaimGuard"


class SubjectSlot(StrEnum):
    TURN = "subject_turn_id"
    ENTRY = "subject_entry_id"
    TOOL_ATTEMPT = "subject_tool_attempt_id"
    JOB = "subject_job_id"
    JOB_ATTEMPT = "subject_job_attempt_id"
    QUEUE_ITEM = "subject_queue_item_id"
    INTERACTION_DECISION = "subject_interaction_decision_id"
    CONTEXT_BINDING_REVISION = "subject_context_binding_revision_id"
    SUBAGENT_TASK = "subject_subagent_task_id"
    SUBAGENT_MESSAGE = "subject_subagent_message_id"
    SUBAGENT_RESULT = "subject_subagent_result_id"
    MEMORY_FACT = "subject_memory_fact_id"
    MEMORY_RELATION = "subject_memory_relation_id"


class CommittedEventType(StrEnum):
    USER_MESSAGE_ACCEPTED = "UserMessageAccepted"
    ASSISTANT_MESSAGE_ACCEPTED = "AssistantMessageAccepted"
    ASSISTANT_TOOL_REQUEST_ACCEPTED = "AssistantToolRequestAccepted"
    TOOL_RESULT_ACCEPTED = "ToolResultAccepted"
    TURN_COMPLETED = "TurnCompleted"
    TURN_INTERRUPTED = "TurnInterrupted"
    USER_STEER_ACCEPTED = "UserSteerAccepted"
    CAPABILITY_DECISION_ACCEPTED = "CapabilityDecisionAccepted"
    INTERACTION_DECISION_ACCEPTED = "InteractionDecisionAccepted"
    TOOL_ATTEMPT_ACCEPTED = "ToolAttemptAccepted"
    TOOL_REMOTE_IDENTITY_PUBLISHED = "ToolRemoteIdentityPublished"
    PROMPT_QUEUED = "PromptQueued"
    PROMPT_CONSUMED = "PromptConsumed"
    PROMPT_CANCELLED = "PromptCancelled"
    PROMPT_REJECTED = "PromptRejected"
    COMPACTION_ADOPTED = "CompactionAdopted"
    SUBAGENT_TASK_ACCEPTED = "SubagentTaskAccepted"
    SUBAGENT_TASK_STATUS_ACCEPTED = "SubagentTaskStatusAccepted"
    SUBAGENT_MESSAGE_ACCEPTED = "SubagentMessageAccepted"
    SUBAGENT_RESULT_ACCEPTED = "SubagentResultAccepted"
    JOB_QUEUED = "JobQueued"
    JOB_ATTEMPT_ACCEPTED = "JobAttemptAccepted"
    JOB_TERMINAL_ACCEPTED = "JobTerminalAccepted"
    MEMORY_FACT_ACCEPTED = "MemoryFactAccepted"
    MEMORY_FACT_LIFECYCLE_CHANGED = "MemoryFactLifecycleChanged"
    MEMORY_RELATION_ACCEPTED = "MemoryRelationAccepted"


class LiveEventType(StrEnum):
    TEXT_START = "TextStart"
    TEXT_DELTA = "TextDelta"
    TEXT_END = "TextEnd"
    THINKING_START = "ThinkingStart"
    THINKING_DELTA = "ThinkingDelta"
    THINKING_END = "ThinkingEnd"
    DATA_START = "DataStart"
    DATA_DELTA = "DataDelta"
    DATA_END = "DataEnd"
    TOOL_CALL_START = "ToolCallStart"
    TOOL_CALL_DELTA = "ToolCallDelta"
    TOOL_CALL_END = "ToolCallEnd"
    TOOL_RESULT_START = "ToolResultStart"
    TOOL_RESULT_DELTA = "ToolResultDelta"
    TOOL_RESULT_END = "ToolResultEnd"
    INTERACTION_OPENED = "InteractionOpened"
    INTERACTION_REPLACED = "InteractionReplaced"
    INTERACTION_CLOSED = "InteractionClosed"
    TERMINAL_PROCESS_COMPLETED = "TerminalProcessCompleted"
    TERMINAL_MONITOR_OPENED = "TerminalMonitorOpened"
    TERMINAL_MONITOR_OBSERVATION = "TerminalMonitorObservation"
    TERMINAL_MONITOR_CLOSED = "TerminalMonitorClosed"
    SUBAGENT_PROGRESS = "SubagentProgress"


@dataclass(frozen=True, slots=True)
class CommittedEventDescriptor:
    event_type: CommittedEventType
    subject_slot: SubjectSlot
    append_guards: tuple[AppendGuardKind, ...]


def _host(
    event_type: CommittedEventType, subject: SubjectSlot
) -> CommittedEventDescriptor:
    return CommittedEventDescriptor(
        event_type=event_type,
        subject_slot=subject,
        append_guards=(AppendGuardKind.HOST_WRITER,),
    )


COMMITTED_EVENT_DESCRIPTORS = (
    _host(CommittedEventType.USER_MESSAGE_ACCEPTED, SubjectSlot.ENTRY),
    _host(CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED, SubjectSlot.ENTRY),
    _host(CommittedEventType.ASSISTANT_TOOL_REQUEST_ACCEPTED, SubjectSlot.ENTRY),
    _host(CommittedEventType.TOOL_RESULT_ACCEPTED, SubjectSlot.ENTRY),
    _host(CommittedEventType.TURN_COMPLETED, SubjectSlot.TURN),
    _host(CommittedEventType.TURN_INTERRUPTED, SubjectSlot.TURN),
    _host(CommittedEventType.USER_STEER_ACCEPTED, SubjectSlot.ENTRY),
    _host(
        CommittedEventType.CAPABILITY_DECISION_ACCEPTED,
        SubjectSlot.INTERACTION_DECISION,
    ),
    _host(
        CommittedEventType.INTERACTION_DECISION_ACCEPTED,
        SubjectSlot.INTERACTION_DECISION,
    ),
    _host(CommittedEventType.TOOL_ATTEMPT_ACCEPTED, SubjectSlot.TOOL_ATTEMPT),
    _host(
        CommittedEventType.TOOL_REMOTE_IDENTITY_PUBLISHED,
        SubjectSlot.TOOL_ATTEMPT,
    ),
    _host(CommittedEventType.PROMPT_QUEUED, SubjectSlot.QUEUE_ITEM),
    _host(CommittedEventType.PROMPT_CONSUMED, SubjectSlot.QUEUE_ITEM),
    _host(CommittedEventType.PROMPT_CANCELLED, SubjectSlot.QUEUE_ITEM),
    _host(CommittedEventType.PROMPT_REJECTED, SubjectSlot.QUEUE_ITEM),
    _host(
        CommittedEventType.COMPACTION_ADOPTED,
        SubjectSlot.CONTEXT_BINDING_REVISION,
    ),
    _host(CommittedEventType.SUBAGENT_TASK_ACCEPTED, SubjectSlot.SUBAGENT_TASK),
    _host(
        CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
        SubjectSlot.SUBAGENT_TASK,
    ),
    _host(
        CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED,
        SubjectSlot.SUBAGENT_MESSAGE,
    ),
    _host(
        CommittedEventType.SUBAGENT_RESULT_ACCEPTED,
        SubjectSlot.SUBAGENT_RESULT,
    ),
    CommittedEventDescriptor(
        event_type=CommittedEventType.JOB_QUEUED,
        subject_slot=SubjectSlot.JOB,
        append_guards=(
            AppendGuardKind.HOST_WRITER,
            AppendGuardKind.JOB_ATTEMPT_CLAIM,
        ),
    ),
    CommittedEventDescriptor(
        event_type=CommittedEventType.JOB_ATTEMPT_ACCEPTED,
        subject_slot=SubjectSlot.JOB_ATTEMPT,
        append_guards=(AppendGuardKind.JOB_ATTEMPT_CLAIM,),
    ),
    CommittedEventDescriptor(
        event_type=CommittedEventType.JOB_TERMINAL_ACCEPTED,
        subject_slot=SubjectSlot.JOB,
        append_guards=(AppendGuardKind.JOB_ATTEMPT_CLAIM,),
    ),
    CommittedEventDescriptor(
        event_type=CommittedEventType.MEMORY_FACT_ACCEPTED,
        subject_slot=SubjectSlot.MEMORY_FACT,
        append_guards=(AppendGuardKind.JOB_ATTEMPT_CLAIM,),
    ),
    CommittedEventDescriptor(
        event_type=CommittedEventType.MEMORY_FACT_LIFECYCLE_CHANGED,
        subject_slot=SubjectSlot.MEMORY_FACT,
        append_guards=(AppendGuardKind.JOB_ATTEMPT_CLAIM,),
    ),
    CommittedEventDescriptor(
        event_type=CommittedEventType.MEMORY_RELATION_ACCEPTED,
        subject_slot=SubjectSlot.MEMORY_RELATION,
        append_guards=(AppendGuardKind.JOB_ATTEMPT_CLAIM,),
    ),
)

LIVE_EVENT_TYPES = tuple(item.value for item in LiveEventType)
SUBJECT_SLOTS = tuple(item.value for item in SubjectSlot)
APPEND_GUARDS = tuple(item.value for item in AppendGuardKind)

DESCRIPTOR_BY_TYPE = {item.event_type: item for item in COMMITTED_EVENT_DESCRIPTORS}

if len(COMMITTED_EVENT_DESCRIPTORS) != 26 or len(DESCRIPTOR_BY_TYPE) != 26:
    raise RuntimeError("committed event descriptor must contain exact 26 types")
if len(LIVE_EVENT_TYPES) != 23 or len(set(LIVE_EVENT_TYPES)) != 23:
    raise RuntimeError("live event registry must contain exact 23 types")
if len(SUBJECT_SLOTS) != 13 or len(set(SUBJECT_SLOTS)) != 13:
    raise RuntimeError("subject registry must contain exact 13 slots")
if len(APPEND_GUARDS) != 2:
    raise RuntimeError("append guard registry must contain exact 2 guards")


__all__ = [
    "APPEND_GUARDS",
    "COMMITTED_EVENT_DESCRIPTORS",
    "DESCRIPTOR_BY_TYPE",
    "LIVE_EVENT_TYPES",
    "SUBJECT_SLOTS",
    "AppendGuardKind",
    "CommittedEventDescriptor",
    "CommittedEventType",
    "LiveEventType",
    "SubjectSlot",
]
