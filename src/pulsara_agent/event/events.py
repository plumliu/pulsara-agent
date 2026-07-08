"""Agent runtime events for Pulsara."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from pulsara_agent.event.candidates import MemoryCandidate
from pulsara_agent.message.blocks import (
    ToolCallBlock,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultState,
)
from pulsara_agent.ontology import memory


class EventType(StrEnum):
    RUN_START = "RUN_START"
    RUN_END = "RUN_END"
    REPLY_START = "REPLY_START"
    REPLY_END = "REPLY_END"
    RUN_ERROR = "RUN_ERROR"
    EXCEED_MAX_ITERS = "EXCEED_MAX_ITERS"

    MODEL_CALL_START = "MODEL_CALL_START"
    MODEL_CALL_END = "MODEL_CALL_END"
    CONTEXT_COMPILED = "CONTEXT_COMPILED"
    CAPABILITY_GATE_DECISION = "CAPABILITY_GATE_DECISION"

    TEXT_BLOCK_START = "TEXT_BLOCK_START"
    TEXT_BLOCK_DELTA = "TEXT_BLOCK_DELTA"
    TEXT_BLOCK_END = "TEXT_BLOCK_END"

    DATA_BLOCK_START = "DATA_BLOCK_START"
    DATA_BLOCK_DELTA = "DATA_BLOCK_DELTA"
    DATA_BLOCK_END = "DATA_BLOCK_END"

    THINKING_BLOCK_START = "THINKING_BLOCK_START"
    THINKING_BLOCK_DELTA = "THINKING_BLOCK_DELTA"
    THINKING_BLOCK_END = "THINKING_BLOCK_END"

    HINT_BLOCK = "HINT_BLOCK"

    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_DELTA = "TOOL_CALL_DELTA"
    TOOL_CALL_END = "TOOL_CALL_END"

    TOOL_RESULT_START = "TOOL_RESULT_START"
    TOOL_RESULT_TEXT_DELTA = "TOOL_RESULT_TEXT_DELTA"
    TOOL_RESULT_DATA_DELTA = "TOOL_RESULT_DATA_DELTA"
    TOOL_RESULT_END = "TOOL_RESULT_END"

    REQUIRE_USER_CONFIRM = "REQUIRE_USER_CONFIRM"
    USER_CONFIRM_RESULT = "USER_CONFIRM_RESULT"
    REQUIRE_EXTERNAL_EXECUTION = "REQUIRE_EXTERNAL_EXECUTION"
    EXTERNAL_EXECUTION_RESULT = "EXTERNAL_EXECUTION_RESULT"
    TERMINAL_PROCESS_COMPLETED = "TERMINAL_PROCESS_COMPLETED"
    PLAN_MODE_ENTERED = "PLAN_MODE_ENTERED"
    PLAN_QUESTION_ASKED = "PLAN_QUESTION_ASKED"
    PLAN_QUESTION_ANSWERED = "PLAN_QUESTION_ANSWERED"
    PLAN_EXIT_REQUESTED = "PLAN_EXIT_REQUESTED"
    PLAN_EXIT_RESOLVED = "PLAN_EXIT_RESOLVED"
    PLAN_MODE_EXITED = "PLAN_MODE_EXITED"

    MEMORY_CANDIDATE_PROPOSED = "MEMORY_CANDIDATE_PROPOSED"
    MEMORY_WRITE_RESULT = "MEMORY_WRITE_RESULT"
    MEMORY_WRITE_FAILED = "MEMORY_WRITE_FAILED"
    MEMORY_REFLECTION_COMPLETED = "MEMORY_REFLECTION_COMPLETED"
    MEMORY_REFLECTION_FAILED = "MEMORY_REFLECTION_FAILED"
    MEMORY_SUPERSEDED = "MEMORY_SUPERSEDED"
    MEMORY_CONTRADICTION_LINKED = "MEMORY_CONTRADICTION_LINKED"
    MEMORY_MARKED_STALE = "MEMORY_MARKED_STALE"
    MEMORY_MAINTENANCE_PROPOSED = "MEMORY_MAINTENANCE_PROPOSED"
    MEMORY_MAINTENANCE_APPLIED = "MEMORY_MAINTENANCE_APPLIED"
    MEMORY_MAINTENANCE_REJECTED = "MEMORY_MAINTENANCE_REJECTED"

    PROJECTION_REQUESTED = "PROJECTION_REQUESTED"
    PROJECTION_READY = "PROJECTION_READY"
    PROJECTION_FAILED = "PROJECTION_FAILED"

    CONTEXT_COMPACTION_STARTED = "CONTEXT_COMPACTION_STARTED"
    CONTEXT_COMPACTION_COMPLETED = "CONTEXT_COMPACTION_COMPLETED"
    CONTEXT_COMPACTION_FAILED = "CONTEXT_COMPACTION_FAILED"

    SUBAGENT_RUN_STARTED = "SUBAGENT_RUN_STARTED"
    SUBAGENT_MESSAGE_SENT = "SUBAGENT_MESSAGE_SENT"
    SUBAGENT_RUN_SUSPENDED = "SUBAGENT_RUN_SUSPENDED"
    SUBAGENT_RUN_COMPLETED = "SUBAGENT_RUN_COMPLETED"
    SUBAGENT_RUN_FAILED = "SUBAGENT_RUN_FAILED"
    SUBAGENT_RUN_CANCELLED = "SUBAGENT_RUN_CANCELLED"
    SUBAGENT_EDGE_RECORDED = "SUBAGENT_EDGE_RECORDED"
    SUBAGENT_RESULT_DELIVERED = "SUBAGENT_RESULT_DELIVERED"
    SUBAGENT_TASK_CREATED = "SUBAGENT_TASK_CREATED"
    SUBAGENT_TASK_SCHEDULED = "SUBAGENT_TASK_SCHEDULED"
    SUBAGENT_TASK_STARTED = "SUBAGENT_TASK_STARTED"
    SUBAGENT_TASK_BLOCKED = "SUBAGENT_TASK_BLOCKED"
    SUBAGENT_TASK_COMPLETED = "SUBAGENT_TASK_COMPLETED"
    SUBAGENT_TASK_FAILED = "SUBAGENT_TASK_FAILED"
    SUBAGENT_TASK_CANCELLED = "SUBAGENT_TASK_CANCELLED"
    SUBAGENT_PHASE_REPORTED = "SUBAGENT_PHASE_REPORTED"
    SUBAGENT_RESULT_SUBMITTED = "SUBAGENT_RESULT_SUBMITTED"
    SUBAGENT_RESULT_CONSUMED = "SUBAGENT_RESULT_CONSUMED"

    CUSTOM = "CUSTOM"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class EventContext:
    run_id: str
    turn_id: str
    reply_id: str

    def event_fields(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "reply_id": self.reply_id,
        }


class EventBase(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=utc_now)
    run_id: str
    turn_id: str
    reply_id: str
    sequence: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunStartEvent(EventBase):
    type: Literal[EventType.RUN_START] = EventType.RUN_START
    user_input_chars: int


class RunEndEvent(EventBase):
    type: Literal[EventType.RUN_END] = EventType.RUN_END
    status: str
    stop_reason: str | None = None
    abort_kind: str | None = None
    error_message: str | None = None


class ReplyStartEvent(EventBase):
    type: Literal[EventType.REPLY_START] = EventType.REPLY_START
    name: str
    role: Literal["assistant"] = "assistant"


class ReplyEndEvent(EventBase):
    type: Literal[EventType.REPLY_END] = EventType.REPLY_END


class RunErrorEvent(EventBase):
    type: Literal[EventType.RUN_ERROR] = EventType.RUN_ERROR
    message: str
    code: str = "runtime_error"


class ExceedMaxItersEvent(EventBase):
    type: Literal[EventType.EXCEED_MAX_ITERS] = EventType.EXCEED_MAX_ITERS
    name: str
    max_iters: int


class ModelCallStartEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_START] = EventType.MODEL_CALL_START
    model_name: str
    model_role: str
    provider: str
    context_id: str | None = None
    model_call_index: int | None = None


class ContextCompiledEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPILED] = EventType.CONTEXT_COMPILED
    status: Literal["compiled", "pressure", "failed"] = "compiled"
    context_id: str
    model_role: str
    model_call_index: int
    compile_attempt_index: int | None = None
    context_retry_index: int | None = None
    estimated_tokens: int
    context_window_tokens: int
    reserved_output_tokens: int
    tools_estimated_tokens: int
    sections: list[dict[str, Any]] = Field(default_factory=list)
    tool_specs: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    lifecycle_decisions: list[dict[str, Any]] = Field(default_factory=list)
    tool_result_render_decisions: list[dict[str, Any]] = Field(default_factory=list)
    tool_result_budget_report: dict[str, Any] = Field(default_factory=dict)


class CapabilityGateDecisionEvent(EventBase):
    type: Literal[EventType.CAPABILITY_GATE_DECISION] = EventType.CAPABILITY_GATE_DECISION
    tool_call_id: str
    tool_name: str
    descriptor_id: str | None = None
    decision: Literal["allow", "deny", "wait_for_user"]
    reason_code: str | None = None
    reason_message: str | None = None
    suggested_rules: list[dict[str, Any]] = Field(default_factory=list)
    result_state: ToolResultState | None = None
    policy_mode: str | None = None
    permission_policy: dict[str, Any] = Field(default_factory=dict)
    exposure_generation: int | None = None
    availability: str | None = None
    permission_category: str | None = None
    effective_permission_category: str | None = None
    effective_read_only: bool | None = None
    capability_context: dict[str, Any] = Field(default_factory=dict)


class ModelCallEndEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_END] = EventType.MODEL_CALL_END
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class TextBlockStartEvent(EventBase):
    type: Literal[EventType.TEXT_BLOCK_START] = EventType.TEXT_BLOCK_START
    block_id: str


class TextBlockDeltaEvent(EventBase):
    type: Literal[EventType.TEXT_BLOCK_DELTA] = EventType.TEXT_BLOCK_DELTA
    block_id: str
    delta: str


class TextBlockEndEvent(EventBase):
    type: Literal[EventType.TEXT_BLOCK_END] = EventType.TEXT_BLOCK_END
    block_id: str


class DataBlockStartEvent(EventBase):
    type: Literal[EventType.DATA_BLOCK_START] = EventType.DATA_BLOCK_START
    block_id: str
    media_type: str


class DataBlockDeltaEvent(EventBase):
    type: Literal[EventType.DATA_BLOCK_DELTA] = EventType.DATA_BLOCK_DELTA
    block_id: str
    data: str
    media_type: str


class DataBlockEndEvent(EventBase):
    type: Literal[EventType.DATA_BLOCK_END] = EventType.DATA_BLOCK_END
    block_id: str


class ThinkingBlockStartEvent(EventBase):
    type: Literal[EventType.THINKING_BLOCK_START] = EventType.THINKING_BLOCK_START
    block_id: str


class ThinkingBlockDeltaEvent(EventBase):
    type: Literal[EventType.THINKING_BLOCK_DELTA] = EventType.THINKING_BLOCK_DELTA
    block_id: str
    delta: str


class ThinkingBlockEndEvent(EventBase):
    type: Literal[EventType.THINKING_BLOCK_END] = EventType.THINKING_BLOCK_END
    block_id: str


class HintBlockEvent(EventBase):
    type: Literal[EventType.HINT_BLOCK] = EventType.HINT_BLOCK
    block_id: str
    hint: str
    source: str | None = None


class ToolCallStartEvent(EventBase):
    type: Literal[EventType.TOOL_CALL_START] = EventType.TOOL_CALL_START
    tool_call_id: str
    tool_call_name: str


class ToolCallDeltaEvent(EventBase):
    type: Literal[EventType.TOOL_CALL_DELTA] = EventType.TOOL_CALL_DELTA
    tool_call_id: str
    delta: str


class ToolCallEndEvent(EventBase):
    type: Literal[EventType.TOOL_CALL_END] = EventType.TOOL_CALL_END
    tool_call_id: str


class ToolResultStartEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_START] = EventType.TOOL_RESULT_START
    tool_call_id: str
    tool_call_name: str


class ToolResultTextDeltaEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_TEXT_DELTA] = EventType.TOOL_RESULT_TEXT_DELTA
    tool_call_id: str
    delta: str


class ToolResultDataDeltaEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_DATA_DELTA] = EventType.TOOL_RESULT_DATA_DELTA
    tool_call_id: str
    block_id: str = Field(default_factory=lambda: uuid4().hex)
    media_type: str
    data: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "ToolResultDataDeltaEvent":
        if self.data is None and self.url is None:
            raise ValueError("ToolResultDataDeltaEvent needs data or url")
        if self.data is not None and self.url is not None:
            raise ValueError("ToolResultDataDeltaEvent data and url are mutually exclusive")
        return self


class ToolResultEndEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_END] = EventType.TOOL_RESULT_END
    tool_call_id: str
    state: ToolResultState
    artifacts: list[ToolResultArtifactRef] = Field(default_factory=list)


class RequireUserConfirmEvent(EventBase):
    type: Literal[EventType.REQUIRE_USER_CONFIRM] = EventType.REQUIRE_USER_CONFIRM
    tool_calls: list[ToolCallBlock]


class ConfirmResult(BaseModel):
    confirmed: bool
    tool_call: ToolCallBlock
    rules: list[dict] | None = None


class UserConfirmResultEvent(EventBase):
    type: Literal[EventType.USER_CONFIRM_RESULT] = EventType.USER_CONFIRM_RESULT
    confirm_results: list[ConfirmResult]


class RequireExternalExecutionEvent(EventBase):
    type: Literal[EventType.REQUIRE_EXTERNAL_EXECUTION] = EventType.REQUIRE_EXTERNAL_EXECUTION
    tool_calls: list[ToolCallBlock]


class ExternalExecutionResultEvent(EventBase):
    type: Literal[EventType.EXTERNAL_EXECUTION_RESULT] = EventType.EXTERNAL_EXECUTION_RESULT
    execution_results: list[ToolResultBlock]


class TerminalProcessCompletedEvent(EventBase):
    type: Literal[EventType.TERMINAL_PROCESS_COMPLETED] = EventType.TERMINAL_PROCESS_COMPLETED
    process_id: str
    terminal_session_id: str
    command: str
    status: str
    exit_code: int
    cwd: str
    timed_out: bool = False
    duration_seconds: float
    output_preview: str = ""
    output_truncated: bool = False
    backend_type: str = "local"
    io_mode: str = "pipe"
    tool_call_id: str | None = None
    completion_reason: str | None = None


class PlanModeEnteredEvent(EventBase):
    type: Literal[EventType.PLAN_MODE_ENTERED] = EventType.PLAN_MODE_ENTERED
    source: Literal["user", "agent"]
    previous_permission_mode: str | None = None
    previous_permission_policy: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class PlanQuestionOption(BaseModel):
    label: str
    description: str = ""
    recommended: bool = False

    @field_validator("label")
    @classmethod
    def _label_must_not_be_empty(cls, value: str) -> str:
        label = value.strip()
        if not label:
            raise ValueError("plan question option label must not be empty")
        return label


class PlanQuestionAskedEvent(EventBase):
    type: Literal[EventType.PLAN_QUESTION_ASKED] = EventType.PLAN_QUESTION_ASKED
    question_id: str
    tool_call_id: str
    question: str
    options: list[PlanQuestionOption] = Field(default_factory=list)
    allow_free_text: bool = True
    reason: str = ""

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_options(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, str):
                normalized.append({"label": item})
            else:
                normalized.append(item)
        return normalized


class PlanQuestionAnsweredEvent(EventBase):
    type: Literal[EventType.PLAN_QUESTION_ANSWERED] = EventType.PLAN_QUESTION_ANSWERED
    question_id: str
    answer_text: str
    selected_option: str | None = None


class PlanExitRequestedEvent(EventBase):
    type: Literal[EventType.PLAN_EXIT_REQUESTED] = EventType.PLAN_EXIT_REQUESTED
    exit_request_id: str
    tool_call_id: str
    plan_text: str = ""
    plan_artifact_id: str | None = None
    summary: str = ""


class PlanExitResolvedEvent(EventBase):
    type: Literal[EventType.PLAN_EXIT_RESOLVED] = EventType.PLAN_EXIT_RESOLVED
    exit_request_id: str
    tool_call_id: str
    decision: Literal["approve", "revise", "cancel"]
    user_feedback: str = ""


class PlanModeExitedEvent(EventBase):
    type: Literal[EventType.PLAN_MODE_EXITED] = EventType.PLAN_MODE_EXITED
    source: Literal["approved_exit_plan", "user_cancel", "user_force_exit"]
    exit_request_id: str | None = None
    restored_permission_mode: str | None = None
    restored_permission_policy: dict[str, Any] = Field(default_factory=dict)
    accepted_plan_summary: str = ""
    accepted_plan_artifact_id: str | None = None


class MemoryEventBase(EventBase):
    scope: str
    memory_type: str
    statement: str | None = None
    summary: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    source_authority: memory.SourceAuthority | None = None
    verification_status: memory.VerificationStatus | None = None
    gate_reason: str | None = None


class MemoryCandidateProposedEvent(EventBase):
    type: Literal[EventType.MEMORY_CANDIDATE_PROPOSED] = EventType.MEMORY_CANDIDATE_PROPOSED
    candidate: MemoryCandidate


class MemoryWriteResultEvent(EventBase):
    type: Literal[EventType.MEMORY_WRITE_RESULT] = EventType.MEMORY_WRITE_RESULT
    candidate_id: str
    memory_id: str
    memory_type: str
    status: memory.NodeStatus
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    gate_reason: str


class MemoryWriteFailedEvent(EventBase):
    type: Literal[EventType.MEMORY_WRITE_FAILED] = EventType.MEMORY_WRITE_FAILED
    candidate_id: str | None = None
    memory_type: str | None = None
    error_type: str
    message: str


class MemoryReflectionCompletedEvent(EventBase):
    type: Literal[EventType.MEMORY_REFLECTION_COMPLETED] = EventType.MEMORY_REFLECTION_COMPLETED
    reflection_id: str
    trigger_reason: str
    trigger_reasons: list[str] = Field(default_factory=list)
    safe_point: str = ""
    should_reflect: bool = True
    decision_reason: str = ""
    quoted_evidence: list[str] = Field(default_factory=list)
    candidate_kinds: list[str] = Field(default_factory=list)
    proposed_count: int
    skipped_count: int
    written_count: int
    failed_count: int
    summary: str = ""


class MemoryReflectionFailedEvent(EventBase):
    type: Literal[EventType.MEMORY_REFLECTION_FAILED] = EventType.MEMORY_REFLECTION_FAILED
    reflection_id: str
    trigger_reason: str
    trigger_reasons: list[str] = Field(default_factory=list)
    safe_point: str = ""
    error_type: str
    message: str


class MemorySupersededEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_SUPERSEDED] = EventType.MEMORY_SUPERSEDED
    memory_id: str
    superseded_by: str


class MemoryContradictionLinkedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_CONTRADICTION_LINKED] = EventType.MEMORY_CONTRADICTION_LINKED
    memory_id: str
    contradicts: str


class MemoryMarkedStaleEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MARKED_STALE] = EventType.MEMORY_MARKED_STALE
    memory_id: str


class MemoryMaintenanceProposedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_PROPOSED] = EventType.MEMORY_MAINTENANCE_PROPOSED
    proposal_id: str
    target_memory_id: str
    action: str


class MemoryMaintenanceAppliedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_APPLIED] = EventType.MEMORY_MAINTENANCE_APPLIED
    proposal_id: str
    target_memory_id: str
    action: str


class MemoryMaintenanceRejectedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_REJECTED] = EventType.MEMORY_MAINTENANCE_REJECTED
    proposal_id: str
    target_memory_id: str
    action: str


class ProjectionEventBase(EventBase):
    projection_id: str
    role: str
    scope: str
    token_budget: int | None = None


class ProjectionRequestedEvent(ProjectionEventBase):
    type: Literal[EventType.PROJECTION_REQUESTED] = EventType.PROJECTION_REQUESTED


class ProjectionReadyEvent(ProjectionEventBase):
    type: Literal[EventType.PROJECTION_READY] = EventType.PROJECTION_READY
    included_memory_ids: list[str] = Field(default_factory=list)
    filtered_memory_ids: list[str] = Field(default_factory=list)
    summary: str


class ProjectionFailedEvent(ProjectionEventBase):
    type: Literal[EventType.PROJECTION_FAILED] = EventType.PROJECTION_FAILED
    error: str


class ContextCompactionStartedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_STARTED] = EventType.CONTEXT_COMPACTION_STARTED
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    estimated_tokens_before: int
    threshold_tokens: int
    context_window_tokens: int
    through_sequence: int
    keep_after_sequence: int
    force: bool = False


class ContextCompactionCompletedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_COMPLETED] = EventType.CONTEXT_COMPACTION_COMPLETED
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    summary_artifact_id: str
    summary_chars: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    threshold_tokens: int
    context_window_tokens: int
    through_sequence: int
    keep_after_sequence: int
    included_run_ids: list[str] = Field(default_factory=list)
    included_artifact_ids: list[str] = Field(default_factory=list)


class ContextCompactionFailedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_FAILED] = EventType.CONTEXT_COMPACTION_FAILED
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    estimated_tokens_before: int
    threshold_tokens: int
    context_window_tokens: int
    through_sequence: int | None = None
    keep_after_sequence: int | None = None
    error_type: str
    message: str


class SubagentRunStartedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_STARTED] = EventType.SUBAGENT_RUN_STARTED
    subagent_run_id: str
    task_id: str | None = None
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    run_index: int | None = None
    edge_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None = None
    parent_reply_id: str | None = None
    parent_context_id: str | None = None
    parent_model_call_index: int | None = None
    spawning_tool_call_id: str | None = None
    spawning_tool_name: str | None = None
    spawn_initiator_kind: Literal["tool_call", "scheduler", "dependency_satisfied"] | None = None
    spawn_initiator_id: str | None = None
    child_runtime_session_id: str
    label: str | None = None
    role: str
    profile_id: str | None = None
    task_preview: str
    context_policy: dict[str, Any] = Field(default_factory=dict)
    capability_profile: dict[str, Any] = Field(default_factory=dict)


class SubagentMessageSentEvent(EventBase):
    type: Literal[EventType.SUBAGENT_MESSAGE_SENT] = EventType.SUBAGENT_MESSAGE_SENT
    edge_id: str
    subagent_run_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    child_runtime_session_id: str
    message_artifact_id: str | None = None
    message_preview: str
    delivery_kind: Literal["spawn_task", "send", "followup"]


class SubagentRunSuspendedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_SUSPENDED] = EventType.SUBAGENT_RUN_SUSPENDED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str
    pending_kind: str
    reason_code: str
    reason_message: str | None = None
    resumable: bool = False


class SubagentRunCompletedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_COMPLETED] = EventType.SUBAGENT_RUN_COMPLETED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str
    child_run_id: str | None = None
    result_id: str
    summary: str
    result_artifact_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    token_usage: dict[str, Any] | None = None
    tool_call_count: int | None = None


class SubagentRunFailedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_FAILED] = EventType.SUBAGENT_RUN_FAILED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class SubagentRunCancelledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_CANCELLED] = EventType.SUBAGENT_RUN_CANCELLED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    cancelled_by: Literal["user", "parent_agent", "runtime", "host_shutdown"]


class SubagentEdgeRecordedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_EDGE_RECORDED] = EventType.SUBAGENT_EDGE_RECORDED
    edge_id: str
    edge_kind: Literal["spawn", "send", "followup", "wait", "cancel", "result", "suspend", "resume"]
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None = None
    parent_reply_id: str | None = None
    subagent_run_id: str
    child_runtime_session_id: str
    child_run_id: str | None = None
    source_context_id: str | None = None
    source_model_call_index: int | None = None
    source_tool_call_id: str | None = None
    source_tool_name: str | None = None
    target_context_id: str | None = None
    payload_artifact_id: str | None = None
    result_id: str | None = None
    result_artifact_id: str | None = None
    returned_to_tool_call_id: str | None = None


class SubagentResultDeliveredEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RESULT_DELIVERED] = EventType.SUBAGENT_RESULT_DELIVERED
    subagent_run_id: str
    parent_runtime_session_id: str
    parent_run_id: str | None = None
    parent_turn_id: str | None = None
    parent_reply_id: str | None = None
    context_id: str | None = None
    model_call_index: int | None = None
    section_id: str | None = None
    delivery_kind: Literal["internal_section"] = "internal_section"
    result_id: str
    result_artifact_id: str | None = None
    summary: str


class SubagentTaskCreatedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_CREATED] = EventType.SUBAGENT_TASK_CREATED
    task_id: str
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    task_key: str | None = None
    label: str | None = None
    profile_id: str
    display_role: str | None = None
    objective_preview: str
    objective_artifact_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class SubagentTaskScheduledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_SCHEDULED] = EventType.SUBAGENT_TASK_SCHEDULED
    task_id: str
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    schedule_reason: Literal["immediate", "dependency_satisfied", "manual"] = "immediate"


class SubagentTaskStartedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_STARTED] = EventType.SUBAGENT_TASK_STARTED
    task_id: str
    subagent_run_id: str
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    run_index: int = 1
    spawn_initiator_kind: Literal["tool_call", "scheduler", "dependency_satisfied"]
    spawn_initiator_id: str


class SubagentTaskBlockedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_BLOCKED] = EventType.SUBAGENT_TASK_BLOCKED
    task_id: str
    status: Literal["waiting_dependency", "blocked_dependency_failed"]
    blocked_reason: Literal["waiting_dependency", "dependency_failed"]
    blocked_by_task_ids: list[str] = Field(default_factory=list)
    dependency_status_snapshot: dict[str, str] = Field(default_factory=dict)
    dependency_terminal_event_ids: dict[str, str] = Field(default_factory=dict)
    dependency_generation: int | None = None


class SubagentTaskCompletedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_COMPLETED] = EventType.SUBAGENT_TASK_COMPLETED
    task_id: str
    subagent_run_id: str | None = None
    result_id: str | None = None
    primary_result_artifact_id: str | None = None
    result_source: Literal["explicit", "inferred"] = "inferred"


class SubagentTaskFailedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_FAILED] = EventType.SUBAGENT_TASK_FAILED
    task_id: str
    subagent_run_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class SubagentTaskCancelledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_CANCELLED] = EventType.SUBAGENT_TASK_CANCELLED
    task_id: str
    subagent_run_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    cancelled_by: Literal["user", "parent_agent", "runtime", "host_shutdown"]


class SubagentPhaseReportedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_PHASE_REPORTED] = EventType.SUBAGENT_PHASE_REPORTED
    subagent_run_id: str
    task_id: str | None = None
    phase: str
    message: str | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    source_tool_call_id: str | None = None


class SubagentResultSubmittedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RESULT_SUBMITTED] = EventType.SUBAGENT_RESULT_SUBMITTED
    subagent_run_id: str
    task_id: str | None = None
    result_id: str
    summary: str
    output_preview: str | None = None
    result_artifact_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    result_source: Literal["explicit"] = "explicit"
    source_tool_call_id: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class SubagentResultConsumedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RESULT_CONSUMED] = EventType.SUBAGENT_RESULT_CONSUMED
    consumption_id: str
    consumer_tool_call_id: str
    kind: Literal["wait_run", "wait_task"]
    task_id: str | None = None
    subagent_run_id: str | None = None
    result_id: str | None = None
    consumed_status: Literal["completed", "failed", "cancelled", "blocked_dependency_failed"]
    terminal_event_id: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class CustomEvent(EventBase):
    type: Literal[EventType.CUSTOM] = EventType.CUSTOM
    name: str
    value: dict[str, Any] = Field(default_factory=dict)


AgentEvent: TypeAlias = (
    RunStartEvent
    | RunEndEvent
    | ReplyStartEvent
    | ReplyEndEvent
    | RunErrorEvent
    | ExceedMaxItersEvent
    | ContextCompiledEvent
    | CapabilityGateDecisionEvent
    | ModelCallStartEvent
    | ModelCallEndEvent
    | TextBlockStartEvent
    | TextBlockDeltaEvent
    | TextBlockEndEvent
    | DataBlockStartEvent
    | DataBlockDeltaEvent
    | DataBlockEndEvent
    | ThinkingBlockStartEvent
    | ThinkingBlockDeltaEvent
    | ThinkingBlockEndEvent
    | HintBlockEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | ToolResultStartEvent
    | ToolResultTextDeltaEvent
    | ToolResultDataDeltaEvent
    | ToolResultEndEvent
    | RequireUserConfirmEvent
    | UserConfirmResultEvent
    | RequireExternalExecutionEvent
    | ExternalExecutionResultEvent
    | TerminalProcessCompletedEvent
    | PlanModeEnteredEvent
    | PlanQuestionAskedEvent
    | PlanQuestionAnsweredEvent
    | PlanExitRequestedEvent
    | PlanExitResolvedEvent
    | PlanModeExitedEvent
    | MemoryCandidateProposedEvent
    | MemoryWriteResultEvent
    | MemoryWriteFailedEvent
    | MemoryReflectionCompletedEvent
    | MemoryReflectionFailedEvent
    | MemorySupersededEvent
    | MemoryContradictionLinkedEvent
    | MemoryMarkedStaleEvent
    | MemoryMaintenanceProposedEvent
    | MemoryMaintenanceAppliedEvent
    | MemoryMaintenanceRejectedEvent
    | ProjectionRequestedEvent
    | ProjectionReadyEvent
    | ProjectionFailedEvent
    | ContextCompactionStartedEvent
    | ContextCompactionCompletedEvent
    | ContextCompactionFailedEvent
    | SubagentRunStartedEvent
    | SubagentMessageSentEvent
    | SubagentRunSuspendedEvent
    | SubagentRunCompletedEvent
    | SubagentRunFailedEvent
    | SubagentRunCancelledEvent
    | SubagentEdgeRecordedEvent
    | SubagentResultDeliveredEvent
    | SubagentTaskCreatedEvent
    | SubagentTaskScheduledEvent
    | SubagentTaskStartedEvent
    | SubagentTaskBlockedEvent
    | SubagentTaskCompletedEvent
    | SubagentTaskFailedEvent
    | SubagentTaskCancelledEvent
    | SubagentPhaseReportedEvent
    | SubagentResultSubmittedEvent
    | SubagentResultConsumedEvent
    | CustomEvent
)
