"""Agent runtime events for Pulsara."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from pulsara_agent.event.candidates import MemoryCandidate
from pulsara_agent.message.blocks import (
    ToolCallBlock,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultState,
)
from pulsara_agent.ontology import memory
from pulsara_agent.primitives.model_call import (
    CompactionObservedAfterMeasurementFact,
    CompactionTargetEstimateFact,
    ContextBudgetReportEvent,
    ModelCallDiagnosticFact,
    ModelCallPurpose,
    ModelTokenUsageFact,
    ResolvedModelCallFact,
    ResolvedModelTargetFact,
)
from pulsara_agent.primitives.mcp import (
    MAX_MCP_DIAGNOSTICS_PER_FACT,
    McpDiagnosticFact,
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpReconcileTriggerValue,
)

_PRESET_PERMISSION_MODES = frozenset(
    {
        "read-only",
        "ask-permissions",
        "accept-edits",
        "bypass-permissions",
    }
)
_RUN_PERMISSION_SNAPSHOT_SOURCES = frozenset(
    {"session_default", "plan_mode", "child_profile"}
)

_PRESET_PERMISSION_POLICIES: dict[str, dict[str, Any]] = {
    "read-only": {
        "profile": "read_only",
        "approval_policy": "on_request",
        "terminal_access": "off",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "read_file_scope": "host_local_text",
            "search_files_scope": "host_local_text_guarded_broad_roots",
            "write_file_scope": "workspace_only",
            "terminal": "off",
        },
    },
    "ask-permissions": {
        "profile": "trusted_host",
        "approval_policy": "on_request",
        "terminal_access": "ask",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "read_file_scope": "host_local_text",
            "search_files_scope": "host_local_text_guarded_broad_roots",
            "write_file_scope": "workspace_only",
            "terminal": "host_shell",
        },
    },
    "accept-edits": {
        "profile": "trusted_host",
        "approval_policy": "never",
        "terminal_access": "ask",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "read_file_scope": "host_local_text",
            "search_files_scope": "host_local_text_guarded_broad_roots",
            "write_file_scope": "workspace_only",
            "terminal": "host_shell",
        },
    },
    "bypass-permissions": {
        "profile": "trusted_host",
        "approval_policy": "never",
        "terminal_access": "allow",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "read_file_scope": "host_local_text",
            "search_files_scope": "host_local_text_guarded_broad_roots",
            "write_file_scope": "workspace_only",
            "terminal": "host_shell",
        },
    },
}


def _validate_model_usage(
    usage_status: Literal["reported", "missing"],
    usage: ModelTokenUsageFact | None,
) -> None:
    if usage_status == "reported" and usage is None:
        raise ValueError("reported model usage requires a usage fact")
    if usage_status == "missing" and usage is not None:
        raise ValueError("missing model usage cannot contain a usage fact")


def _validate_reported_model_id(value: str | None) -> None:
    if value is not None and (not value or value != value.strip()):
        raise ValueError("reported model id must be a non-empty trimmed string")


def _validate_preset_permission_payload(
    *,
    mode: str,
    policy: dict[str, Any],
    context: str,
) -> None:
    if mode not in _PRESET_PERMISSION_MODES:
        allowed = ", ".join(sorted(_PRESET_PERMISSION_MODES))
        raise ValueError(f"{context} permission mode must be one of: {allowed}")
    expected = _PRESET_PERMISSION_POLICIES[mode]
    if dict(policy) != expected:
        raise ValueError(f"{context} permission policy must match preset mode {mode!r}")


class EventType(StrEnum):
    RUN_START = "RUN_START"
    RUN_END = "RUN_END"
    REPLY_START = "REPLY_START"
    REPLY_END = "REPLY_END"
    RUN_ERROR = "RUN_ERROR"
    EXCEED_MAX_ITERS = "EXCEED_MAX_ITERS"

    MODEL_CALL_START = "MODEL_CALL_START"
    MODEL_CALL_END = "MODEL_CALL_END"
    MODEL_CALL_REJECTED = "MODEL_CALL_REJECTED"
    CONTEXT_COMPILED = "CONTEXT_COMPILED"
    CAPABILITY_GATE_DECISION = "CAPABILITY_GATE_DECISION"
    MCP_CAPABILITY_SNAPSHOT_INSTALLED = "MCP_CAPABILITY_SNAPSHOT_INSTALLED"

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
    CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED = (
        "CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED"
    )
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


class ToolObservationTiming(BaseModel):
    """Pulsara-owned timing facts for a completed or suspended tool observation."""

    model_config = ConfigDict(extra="forbid")

    observed_at: str
    source_started_at: str | None = None
    source_ended_at: str | None = None
    observation_duration_seconds: float | None = None
    tool_reported_duration_seconds: float | None = None
    freshness: Literal[
        "current_tool_observation",
        "background_process_observation",
        "historical_tool_observation",
        "suspended_tool_observation",
        "unknown",
    ] = "current_tool_observation"
    clock_source: Literal[
        "tool_result_events",
        "tool_runtime_metadata",
        "mixed",
    ] = "tool_result_events"
    tool_origin: Literal[
        "builtin",
        "mcp",
        "custom",
        "workflow",
        "subagent_system",
        "unknown",
    ] = "unknown"
    tool_name: str | None = None
    tool_call_id: str | None = None
    suspended_at: str | None = None
    resumed_at: str | None = None

    @field_validator(
        "observed_at",
        "source_started_at",
        "source_ended_at",
        "suspended_at",
        "resumed_at",
    )
    @classmethod
    def _validate_utc_timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = _parse_utc_timestamp(value)
        if parsed is None:
            raise ValueError("timestamp must be an ISO-8601 datetime with UTC offset")
        return _format_utc_timestamp(parsed)

    @field_validator("observation_duration_seconds", "tool_reported_duration_seconds")
    @classmethod
    def _validate_non_negative_duration(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("duration must be finite and non-negative")
        return value


def _validate_tool_observation_timing_payload(
    value: object, *, context: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} requires a tool observation timing object")
    try:
        timing = ToolObservationTiming.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"{context} is invalid") from exc
    return timing.model_dump(mode="json", exclude_none=True)


def _parse_utc_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RunStartEvent(EventBase):
    type: Literal[EventType.RUN_START] = EventType.RUN_START
    user_input_chars: int
    permission_snapshot_id: str
    permission_mode: Literal[
        "read-only", "ask-permissions", "accept-edits", "bypass-permissions"
    ]
    permission_policy: dict[str, Any]
    permission_snapshot_source: Literal["session_default", "plan_mode", "child_profile"]
    model_target: ResolvedModelTargetFact
    mcp_installation_id: str
    mcp_installation_owner_runtime_session_id: str

    @model_validator(mode="after")
    def _validate_permission_snapshot(self) -> "RunStartEvent":
        if self.permission_snapshot_source not in _RUN_PERMISSION_SNAPSHOT_SOURCES:
            raise ValueError("RunStartEvent permission_snapshot_source is invalid")
        _validate_preset_permission_payload(
            mode=self.permission_mode,
            policy=self.permission_policy,
            context="RunStartEvent",
        )
        return self


class McpCapabilitySnapshotInstalledEvent(EventBase):
    type: Literal[EventType.MCP_CAPABILITY_SNAPSHOT_INSTALLED] = (
        EventType.MCP_CAPABILITY_SNAPSHOT_INSTALLED
    )
    installation_id: str
    previous_installation_id: str | None = None
    config_epoch: int
    event_safe_config_set_fingerprint: str
    installation_triggers: tuple[McpReconcileTriggerValue, ...]
    coalesced_installation_count: int = 0
    coalesced_attempt_summaries: tuple[McpReconcileAttemptSummaryFact, ...] = ()
    coalesced_attempt_summaries_omitted: int = 0
    server_snapshots: tuple[McpInstalledServerSnapshotFact, ...]
    total_installed_tool_count: int
    added_tool_count: int
    revoked_tool_count: int
    changed_tool_names_bounded: tuple[str, ...] = ()
    changed_tool_names_omitted: int = 0
    diagnostics: tuple[McpDiagnosticFact, ...] = ()

    @model_validator(mode="after")
    def _installation_contract(self) -> "McpCapabilitySnapshotInstalledEvent":
        if self.installation_id == self.previous_installation_id:
            raise ValueError("MCP installation cannot point to itself as previous")
        counts = (
            self.config_epoch,
            self.coalesced_installation_count,
            self.coalesced_attempt_summaries_omitted,
            self.total_installed_tool_count,
            self.added_tool_count,
            self.revoked_tool_count,
            self.changed_tool_names_omitted,
        )
        if any(value < 0 for value in counts):
            raise ValueError("MCP installation counts must be non-negative")
        if len(self.changed_tool_names_bounded) > 64:
            raise ValueError("MCP changed tool names exceed bounded cap")
        if len(self.coalesced_attempt_summaries) > 64:
            raise ValueError("MCP coalesced attempt summaries exceed bounded cap")
        if len(self.server_snapshots) > 64:
            raise ValueError("MCP installed server snapshots exceed bounded cap")
        if len(self.diagnostics) > MAX_MCP_DIAGNOSTICS_PER_FACT:
            raise ValueError("MCP installation diagnostics exceed bounded cap")
        triggers = {
            snapshot.attempt.reconcile_trigger
            for snapshot in self.server_snapshots
            if snapshot.changed_in_this_installation
        }
        triggers.update(
            summary.reconcile_trigger for summary in self.coalesced_attempt_summaries
        )
        if not triggers or tuple(sorted(triggers)) != tuple(self.installation_triggers):
            raise ValueError("MCP installation triggers must match changed attempt facts")
        return self


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
    resolved_call: ResolvedModelCallFact
    context_id: str
    model_call_index: int | None = None

    @model_validator(mode="after")
    def _validate_call_context(self) -> "ModelCallStartEvent":
        if not self.context_id:
            raise ValueError("ModelCallStartEvent context_id is required")
        if (
            self.resolved_call.context_mode == "compiled"
            and self.model_call_index is None
        ):
            raise ValueError("compiled model call start requires model_call_index")
        if (
            self.resolved_call.context_mode == "direct"
            and self.model_call_index is not None
        ):
            raise ValueError("direct model call start cannot carry model_call_index")
        if self.model_call_index is not None and self.model_call_index < 0:
            raise ValueError("model_call_index must be non-negative")
        return self


class ContextCompiledEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPILED] = EventType.CONTEXT_COMPILED
    status: Literal["compiled", "pressure", "failed"] = "compiled"
    context_id: str
    model_call_index: int
    compile_attempt_index: int
    context_retry_index: int
    resolved_call: ResolvedModelCallFact
    budget: ContextBudgetReportEvent
    sections: list[dict[str, Any]] = Field(default_factory=list)
    tool_specs: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    lifecycle_decisions: list[dict[str, Any]] = Field(default_factory=list)
    tool_result_render_decisions: list[dict[str, Any]] = Field(default_factory=list)
    tool_result_budget_report: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_budget_stage(self) -> "ContextCompiledEvent":
        if (
            self.status == "compiled"
            and self.budget.measurement_stage != "final_payload"
        ):
            raise ValueError("compiled context requires a final_payload budget report")
        if self.context_id == "":
            raise ValueError("context_id is required")
        if self.resolved_call.context_mode != "compiled":
            raise ValueError("ContextCompiledEvent requires a compiled resolved call")
        if self.model_call_index < 0:
            raise ValueError("model_call_index must be non-negative")
        target = self.resolved_call.target
        if (
            self.budget.resolved_model_call_id
            != self.resolved_call.resolved_model_call_id
        ):
            raise ValueError("compiled budget resolved call identity mismatch")
        if self.budget.target_fingerprint != target.target_fingerprint:
            raise ValueError("compiled budget target fingerprint mismatch")
        if (
            self.budget.total_context_tokens != target.limits.total_context_tokens
            or self.budget.max_input_tokens != target.limits.max_input_tokens
            or self.budget.max_output_tokens != target.limits.max_output_tokens
            or self.budget.effective_output_tokens
            != target.context_budget.effective_output_tokens
            or self.budget.safety_margin_tokens
            != target.context_budget.safety_margin_tokens
            or self.budget.input_budget_tokens
            != target.context_budget.input_budget_tokens
            or self.budget.estimator != target.token_estimator
        ):
            raise ValueError("compiled budget does not match resolved model target")
        return self


class CapabilityGateDecisionEvent(EventBase):
    type: Literal[EventType.CAPABILITY_GATE_DECISION] = (
        EventType.CAPABILITY_GATE_DECISION
    )
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
    resolved_model_call_id: str
    target_fingerprint: str
    reported_model_id: str | None
    outcome: Literal["completed", "provider_error"]
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    estimated_input_tokens: int = Field(ge=0)
    diagnostics: tuple[ModelCallDiagnosticFact, ...] = ()

    @model_validator(mode="after")
    def _validate_usage(self) -> "ModelCallEndEvent":
        if self.usage_status == "reported" and self.usage is None:
            raise ValueError("reported model usage requires a usage fact")
        if self.usage_status == "missing" and self.usage is not None:
            raise ValueError("missing model usage cannot contain a usage fact")
        _validate_reported_model_id(self.reported_model_id)
        return self


class ModelCallRejectedEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_REJECTED] = EventType.MODEL_CALL_REJECTED
    resolved_call: ResolvedModelCallFact
    context_id: str
    model_call_index: int
    reason_code: Literal[
        "model_input_budget_exceeded",
        "model_input_estimate_mismatch",
        "model_context_identity_mismatch",
        "model_target_capability_mismatch",
        "model_target_binding_mismatch",
    ]
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    input_budget_tokens: int = Field(ge=1)
    diagnostics: tuple[ModelCallDiagnosticFact, ...] = ()

    @model_validator(mode="after")
    def _validate_compiled_call(self) -> "ModelCallRejectedEvent":
        if self.resolved_call.context_mode != "compiled":
            raise ValueError("ModelCallRejectedEvent only supports compiled calls")
        if not self.context_id or self.model_call_index < 0:
            raise ValueError(
                "ModelCallRejectedEvent requires valid context attribution"
            )
        if (
            self.input_budget_tokens
            != self.resolved_call.target.context_budget.input_budget_tokens
        ):
            raise ValueError("rejected input budget does not match resolved target")
        if (
            self.reason_code
            in {
                "model_input_budget_exceeded",
                "model_input_estimate_mismatch",
            }
            and self.estimated_input_tokens is None
        ):
            raise ValueError(f"{self.reason_code} requires estimated_input_tokens")
        return self


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
            raise ValueError(
                "ToolResultDataDeltaEvent data and url are mutually exclusive"
            )
        return self


class ToolResultEndEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_END] = EventType.TOOL_RESULT_END
    tool_call_id: str
    state: ToolResultState
    artifacts: list[ToolResultArtifactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_tool_observation_timing(self) -> "ToolResultEndEvent":
        timing = _validate_tool_observation_timing_payload(
            self.metadata.get("tool_observation_timing"),
            context="ToolResultEndEvent.metadata.tool_observation_timing",
        )
        embedded_tool_call_id = timing.get("tool_call_id")
        if (
            embedded_tool_call_id is not None
            and embedded_tool_call_id != self.tool_call_id
        ):
            raise ValueError("ToolResultEndEvent timing tool_call_id mismatch")
        self.metadata["tool_observation_timing"] = timing
        return self


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
    type: Literal[EventType.REQUIRE_EXTERNAL_EXECUTION] = (
        EventType.REQUIRE_EXTERNAL_EXECUTION
    )
    tool_calls: list[ToolCallBlock]


class ExternalExecutionResultEvent(EventBase):
    type: Literal[EventType.EXTERNAL_EXECUTION_RESULT] = (
        EventType.EXTERNAL_EXECUTION_RESULT
    )
    execution_results: list[ToolResultBlock]

    @model_validator(mode="after")
    def _validate_tool_observation_timing_map(self) -> "ExternalExecutionResultEvent":
        timing_by_call_id = self.metadata.get("tool_observation_timing_by_call_id")
        if not isinstance(timing_by_call_id, dict):
            raise ValueError(
                "ExternalExecutionResultEvent requires metadata.tool_observation_timing_by_call_id"
            )
        result_ids = [result.id for result in self.execution_results]
        duplicate_ids = sorted(
            {result_id for result_id in result_ids if result_ids.count(result_id) > 1}
        )
        if duplicate_ids:
            raise ValueError(
                "ExternalExecutionResultEvent execution_results contain duplicate ids: "
                + ", ".join(duplicate_ids)
            )
        result_id_set = set(result_ids)
        missing = [
            result.id
            for result in self.execution_results
            if not isinstance(timing_by_call_id.get(result.id), dict)
        ]
        if missing:
            raise ValueError(
                "ExternalExecutionResultEvent timing map is missing tool result ids: "
                + ", ".join(missing)
            )
        extra = sorted(
            str(key) for key in timing_by_call_id if key not in result_id_set
        )
        if extra:
            raise ValueError(
                "ExternalExecutionResultEvent timing map contains unknown tool result ids: "
                + ", ".join(extra)
            )
        for tool_call_id, timing_payload in list(timing_by_call_id.items()):
            timing = _validate_tool_observation_timing_payload(
                timing_payload,
                context=f"ExternalExecutionResultEvent timing for {tool_call_id!r}",
            )
            embedded_tool_call_id = timing.get("tool_call_id")
            if (
                embedded_tool_call_id is not None
                and embedded_tool_call_id != tool_call_id
            ):
                raise ValueError(
                    "ExternalExecutionResultEvent timing tool_call_id mismatch for "
                    f"{tool_call_id!r}"
                )
            timing_by_call_id[tool_call_id] = timing
        return self


class TerminalProcessCompletedEvent(EventBase):
    type: Literal[EventType.TERMINAL_PROCESS_COMPLETED] = (
        EventType.TERMINAL_PROCESS_COMPLETED
    )
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
    previous_permission_mode: Literal[
        "read-only", "ask-permissions", "accept-edits", "bypass-permissions"
    ]
    previous_permission_policy: dict[str, Any]
    reason: str = ""

    @model_validator(mode="after")
    def _validate_previous_permission(self) -> "PlanModeEnteredEvent":
        _validate_preset_permission_payload(
            mode=self.previous_permission_mode,
            policy=self.previous_permission_policy,
            context="PlanModeEnteredEvent.previous",
        )
        return self


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
    restored_permission_mode: Literal[
        "read-only", "ask-permissions", "accept-edits", "bypass-permissions"
    ]
    restored_permission_policy: dict[str, Any]
    accepted_plan_summary: str = ""
    accepted_plan_artifact_id: str | None = None

    @model_validator(mode="after")
    def _validate_restored_permission(self) -> "PlanModeExitedEvent":
        _validate_preset_permission_payload(
            mode=self.restored_permission_mode,
            policy=self.restored_permission_policy,
            context="PlanModeExitedEvent.restored",
        )
        return self


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
    type: Literal[EventType.MEMORY_CANDIDATE_PROPOSED] = (
        EventType.MEMORY_CANDIDATE_PROPOSED
    )
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
    type: Literal[EventType.MEMORY_REFLECTION_COMPLETED] = (
        EventType.MEMORY_REFLECTION_COMPLETED
    )
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
    resolved_call: ResolvedModelCallFact
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    estimated_input_tokens: int = Field(ge=0)
    reported_model_id: str | None

    @model_validator(mode="after")
    def _validate_usage(self) -> "MemoryReflectionCompletedEvent":
        _validate_model_usage(self.usage_status, self.usage)
        _validate_reported_model_id(self.reported_model_id)
        return self


class MemoryReflectionFailedEvent(EventBase):
    type: Literal[EventType.MEMORY_REFLECTION_FAILED] = (
        EventType.MEMORY_REFLECTION_FAILED
    )
    reflection_id: str
    trigger_reason: str
    trigger_reasons: list[str] = Field(default_factory=list)
    safe_point: str = ""
    error_type: str
    message: str
    failure_stage: Literal[
        "input_build",
        "target_resolution",
        "call_resolution",
        "model_validation",
        "model_stream",
        "output_parse",
        "candidate_append",
    ]
    resolved_call: ResolvedModelCallFact | None = None
    usage_status: Literal["reported", "missing"] = "missing"
    usage: ModelTokenUsageFact | None = None
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    reported_model_id: str | None = None

    @model_validator(mode="after")
    def _validate_model_fact_stage(self) -> "MemoryReflectionFailedEvent":
        _validate_model_usage(self.usage_status, self.usage)
        _validate_reported_model_id(self.reported_model_id)
        if (
            self.failure_stage
            in {
                "model_validation",
                "model_stream",
                "output_parse",
                "candidate_append",
            }
            and self.resolved_call is None
        ):
            raise ValueError("reflection failure stage requires resolved_call")
        if (
            self.failure_stage
            in {
                "model_stream",
                "output_parse",
                "candidate_append",
            }
            and self.estimated_input_tokens is None
        ):
            raise ValueError("reflection post-start failure requires input estimate")
        return self


class MemorySupersededEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_SUPERSEDED] = EventType.MEMORY_SUPERSEDED
    memory_id: str
    superseded_by: str


class MemoryContradictionLinkedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_CONTRADICTION_LINKED] = (
        EventType.MEMORY_CONTRADICTION_LINKED
    )
    memory_id: str
    contradicts: str


class MemoryMarkedStaleEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MARKED_STALE] = EventType.MEMORY_MARKED_STALE
    memory_id: str


class MemoryMaintenanceProposedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_PROPOSED] = (
        EventType.MEMORY_MAINTENANCE_PROPOSED
    )
    proposal_id: str
    target_memory_id: str
    action: str


class MemoryMaintenanceAppliedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_APPLIED] = (
        EventType.MEMORY_MAINTENANCE_APPLIED
    )
    proposal_id: str
    target_memory_id: str
    action: str


class MemoryMaintenanceRejectedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_REJECTED] = (
        EventType.MEMORY_MAINTENANCE_REJECTED
    )
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


def _validate_compaction_target_contract(
    *,
    target: ResolvedModelTargetFact,
    target_input_budget_tokens: int,
    target_estimate: CompactionTargetEstimateFact,
) -> None:
    if target_input_budget_tokens != target.context_budget.input_budget_tokens:
        raise ValueError("compaction target input budget mismatch")
    if target_estimate.target_fingerprint != target.target_fingerprint:
        raise ValueError("compaction target estimate fingerprint mismatch")


def _validate_compaction_summarizer_contract(
    *,
    call: ResolvedModelCallFact,
    context_id: str,
    estimated_input_tokens: int,
    input_budget_tokens: int,
) -> None:
    if call.purpose != ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
        raise ValueError("compaction requires a summarizer call")
    if not context_id:
        raise ValueError("summarizer context id is required")
    if input_budget_tokens != call.target.context_budget.input_budget_tokens:
        raise ValueError("summarizer input budget does not match call target")
    if estimated_input_tokens > input_budget_tokens:
        raise ValueError("started summarizer input exceeds resolved budget")


class ContextCompactionStartedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_STARTED] = (
        EventType.CONTEXT_COMPACTION_STARTED
    )
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    target_model_target: ResolvedModelTargetFact
    target_input_budget_tokens: int = Field(ge=1)
    threshold_tokens: int
    post_compaction_target_tokens: int = Field(ge=1)
    target_estimate: CompactionTargetEstimateFact
    summarizer_call: ResolvedModelCallFact
    summarizer_context_id: str
    summarizer_input_estimated_tokens: int = Field(ge=0)
    summarizer_input_budget_tokens: int = Field(ge=1)
    through_sequence: int
    keep_after_sequence: int
    force: bool = False

    @model_validator(mode="after")
    def _validate_compaction_contract(self) -> "ContextCompactionStartedEvent":
        _validate_compaction_target_contract(
            target=self.target_model_target,
            target_input_budget_tokens=self.target_input_budget_tokens,
            target_estimate=self.target_estimate,
        )
        if (
            self.target_estimate.target_fingerprint
            != self.target_model_target.target_fingerprint
        ):
            raise ValueError("compaction target estimate fingerprint mismatch")
        if self.target_estimate.summary_tokens_actual is not None:
            raise ValueError(
                "started compaction cannot contain actual summary measurements"
            )
        if self.summarizer_call.purpose != ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            raise ValueError("compaction started requires a summarizer call")
        _validate_compaction_summarizer_contract(
            call=self.summarizer_call,
            context_id=self.summarizer_context_id,
            estimated_input_tokens=self.summarizer_input_estimated_tokens,
            input_budget_tokens=self.summarizer_input_budget_tokens,
        )
        return self


class ContextCompactionCompletedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_COMPLETED] = (
        EventType.CONTEXT_COMPACTION_COMPLETED
    )
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    summary_artifact_id: str
    summary_chars: int
    target_model_target: ResolvedModelTargetFact
    target_input_budget_tokens: int = Field(ge=1)
    threshold_tokens: int
    post_compaction_target_tokens: int = Field(ge=1)
    target_estimate: CompactionTargetEstimateFact
    summarizer_call: ResolvedModelCallFact
    summarizer_context_id: str
    summarizer_input_estimated_tokens: int = Field(ge=0)
    summarizer_input_budget_tokens: int = Field(ge=1)
    summarizer_usage_status: Literal["reported", "missing"]
    summarizer_usage: ModelTokenUsageFact | None
    summarizer_estimated_input_tokens: int = Field(ge=0)
    summarizer_reported_model_id: str | None
    predicted_post_target_reached: bool | None
    through_sequence: int
    keep_after_sequence: int
    included_run_ids: list[str] = Field(default_factory=list)
    included_artifact_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_compaction_contract(self) -> "ContextCompactionCompletedEvent":
        _validate_model_usage(self.summarizer_usage_status, self.summarizer_usage)
        _validate_reported_model_id(self.summarizer_reported_model_id)
        if (
            self.target_estimate.target_fingerprint
            != self.target_model_target.target_fingerprint
        ):
            raise ValueError("compaction target estimate fingerprint mismatch")
        if self.target_estimate.summary_tokens_actual is None:
            raise ValueError(
                "completed compaction requires actual summary measurements"
            )
        if (
            self.predicted_post_target_reached
            != self.target_estimate.predicted_post_target_reached
        ):
            raise ValueError("predicted target projection must match target estimate")
        if self.target_estimate.estimate_scope == "compiled_context_baseline":
            expected_prediction = (
                self.target_estimate.estimated_tokens_after
                <= self.post_compaction_target_tokens
            )
            if self.predicted_post_target_reached is not expected_prediction:
                raise ValueError(
                    "compiled target prediction does not match the post-compaction target"
                )
        elif self.predicted_post_target_reached is not None:
            raise ValueError(
                "transcript-only completion cannot claim full target success"
            )
        if self.summarizer_call.purpose != ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            raise ValueError("compaction completed requires a summarizer call")
        _validate_compaction_target_contract(
            target=self.target_model_target,
            target_input_budget_tokens=self.target_input_budget_tokens,
            target_estimate=self.target_estimate,
        )
        _validate_compaction_summarizer_contract(
            call=self.summarizer_call,
            context_id=self.summarizer_context_id,
            estimated_input_tokens=self.summarizer_input_estimated_tokens,
            input_budget_tokens=self.summarizer_input_budget_tokens,
        )
        if (
            self.summarizer_estimated_input_tokens
            != self.summarizer_input_estimated_tokens
        ):
            raise ValueError(
                "summarizer terminal estimate does not match started estimate"
            )
        return self


class CompactionCandidateDiagnosticEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    field: str | None = None
    message: str = ""
    redacted: bool = False


class ContextCompactionMemoryCandidatesProposedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED] = (
        EventType.CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED
    )
    compaction_id: str
    source_event_id: str
    source_event_sequence: int
    summary_artifact_id: str
    candidate_entry_ids: list[str] = Field(default_factory=list)
    attempted_count: int = 0
    proposed_count: int
    skipped_count: int = 0
    duplicate_count: int = 0
    error_count: int = 0
    extractor_version: str = "compaction-memory-candidates:v1"
    diagnostics: list[CompactionCandidateDiagnosticEvent] = Field(default_factory=list)


class ContextCompactionFailedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_FAILED] = (
        EventType.CONTEXT_COMPACTION_FAILED
    )
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    target_model_target: ResolvedModelTargetFact
    target_input_budget_tokens: int = Field(ge=1)
    threshold_tokens: int
    post_compaction_target_tokens: int = Field(ge=1)
    failure_stage: Literal[
        "planning",
        "summarizer_resolution",
        "summarizer_input_build",
        "started_append",
        "model_validation",
        "model_stream",
        "summary_validation",
        "artifact_write",
        "completed_append",
    ]
    target_estimate: CompactionTargetEstimateFact | None = None
    observed_after_measurement: CompactionObservedAfterMeasurementFact | None = None
    summarizer_target: ResolvedModelTargetFact | None = None
    summarizer_call: ResolvedModelCallFact | None = None
    summarizer_context_id: str | None = None
    summarizer_input_estimated_tokens: int | None = Field(default=None, ge=0)
    summarizer_input_budget_tokens: int | None = Field(default=None, ge=1)
    summarizer_usage_status: Literal["reported", "missing"] = "missing"
    summarizer_usage: ModelTokenUsageFact | None = None
    summarizer_estimated_input_tokens: int | None = Field(default=None, ge=0)
    summarizer_reported_model_id: str | None = None
    through_sequence: int | None = None
    keep_after_sequence: int | None = None
    error_type: str
    message: str

    @model_validator(mode="after")
    def _validate_failure_stage(self) -> "ContextCompactionFailedEvent":
        _validate_model_usage(self.summarizer_usage_status, self.summarizer_usage)
        _validate_reported_model_id(self.summarizer_reported_model_id)
        if (
            self.target_input_budget_tokens
            != self.target_model_target.context_budget.input_budget_tokens
        ):
            raise ValueError("compaction target input budget mismatch")
        stages = [
            "planning",
            "summarizer_resolution",
            "summarizer_input_build",
            "started_append",
            "model_validation",
            "model_stream",
            "summary_validation",
            "artifact_write",
            "completed_append",
        ]
        stage_index = stages.index(self.failure_stage)
        if (
            stage_index >= stages.index("summarizer_resolution")
            and self.target_estimate is None
        ):
            raise ValueError(
                "post-planning compaction failure requires target estimate"
            )
        if (
            stage_index >= stages.index("summarizer_input_build")
            and self.summarizer_call is None
        ):
            raise ValueError("summarizer input failure requires resolved call")
        if stage_index >= stages.index("model_validation"):
            if (
                self.summarizer_context_id is None
                or self.summarizer_input_estimated_tokens is None
                or self.summarizer_input_budget_tokens is None
            ):
                raise ValueError(
                    "model-stage compaction failure requires summarizer context"
                )
        if (
            stage_index >= stages.index("model_stream")
            and self.summarizer_estimated_input_tokens is None
        ):
            raise ValueError("post-start compaction failure requires input estimate")
        if self.target_estimate is not None:
            _validate_compaction_target_contract(
                target=self.target_model_target,
                target_input_budget_tokens=self.target_input_budget_tokens,
                target_estimate=self.target_estimate,
            )
            has_actual_after = self.target_estimate.estimated_tokens_after is not None
            if stage_index >= stages.index("artifact_write") and not has_actual_after:
                raise ValueError(
                    "post-summary persistence failure requires actual after measurements"
                )
            if has_actual_after:
                if self.target_estimate.estimate_scope == "compiled_context_baseline":
                    expected_prediction = (
                        self.target_estimate.estimated_tokens_after
                        <= self.post_compaction_target_tokens
                    )
                    if (
                        self.target_estimate.predicted_post_target_reached
                        is not expected_prediction
                    ):
                        raise ValueError(
                            "compiled target prediction does not match the post-compaction target"
                        )
                elif self.target_estimate.predicted_post_target_reached is not None:
                    raise ValueError(
                        "transcript-only failure cannot claim full target success"
                    )
        if self.observed_after_measurement is not None:
            if self.failure_stage != "summary_validation":
                raise ValueError(
                    "observed after measurement is only valid for summary validation"
                )
            if self.target_estimate is None:
                raise ValueError(
                    "observed after measurement requires a planning estimate"
                )
            if any(
                value is not None
                for value in (
                    self.target_estimate.summary_tokens_actual,
                    self.target_estimate.transcript_tokens_after,
                    self.target_estimate.estimated_tokens_after,
                    self.target_estimate.predicted_post_target_reached,
                )
            ):
                raise ValueError(
                    "observed after measurement requires a planning-only target estimate"
                )
            observed = self.observed_after_measurement
            if (
                observed.retained_transcript_tokens
                != self.target_estimate.retained_transcript_tokens
                or observed.protected_transcript_tokens
                != self.target_estimate.protected_transcript_tokens
            ):
                raise ValueError(
                    "observed retained/protected transcript does not match planning"
                )
            if (
                observed.summary_tokens_actual
                <= self.target_estimate.summary_tokens_reserved
            ):
                raise ValueError(
                    "summary reservation violation requires observed tokens above reservation"
                )
            baseline = self.target_estimate.non_transcript_baseline_tokens
            if baseline is None:
                if observed.estimated_tokens_after != observed.transcript_tokens_after:
                    raise ValueError(
                        "transcript-only observed estimate is inconsistent"
                    )
                if observed.predicted_post_target_reached is not None:
                    raise ValueError(
                        "transcript-only observed estimate cannot claim target success"
                    )
            else:
                if observed.estimated_tokens_after != (
                    baseline + observed.transcript_tokens_after
                ):
                    raise ValueError("compiled observed estimate is inconsistent")
                if observed.predicted_post_target_reached is None:
                    raise ValueError(
                        "compiled observed estimate requires a target prediction"
                    )
                expected_prediction = (
                    observed.estimated_tokens_after
                    <= self.post_compaction_target_tokens
                )
                if observed.predicted_post_target_reached is not expected_prediction:
                    raise ValueError(
                        "compiled observed prediction does not match the post-compaction target"
                    )
        if self.summarizer_call is not None:
            if (
                self.summarizer_target is not None
                and self.summarizer_target != self.summarizer_call.target
            ):
                raise ValueError("summarizer target and call mismatch")
            if self.summarizer_input_budget_tokens is not None and (
                self.summarizer_input_budget_tokens
                != self.summarizer_call.target.context_budget.input_budget_tokens
            ):
                raise ValueError("summarizer input budget does not match call target")
            if (
                self.summarizer_context_id is not None
                and not self.summarizer_context_id
            ):
                raise ValueError("summarizer context id must be non-empty")
        return self


SubagentPermissionMode = Literal[
    "read-only",
    "ask-permissions",
    "accept-edits",
    "bypass-permissions",
]
SubagentCapabilityProfileName = Literal[
    "general_worker",
    "research_worker",
    "review_worker",
    "verification_worker",
    "synthesizer",
    "orchestrator",
]


class SubagentContextPolicySnapshotEvent(BaseModel):
    """Immutable event-visible child context policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["isolated", "fork"]
    include_parent_summary: bool
    include_parent_current_task: bool
    include_parent_memory_projection: bool
    include_parent_artifact_refs: bool
    max_parent_context_chars: int | None
    fork_source_context_id: str | None

    @model_validator(mode="after")
    def _validate_context_policy(self) -> SubagentContextPolicySnapshotEvent:
        if (
            self.max_parent_context_chars is not None
            and self.max_parent_context_chars <= 0
        ):
            raise ValueError("max_parent_context_chars must be positive when provided")
        if self.mode == "isolated" and self.fork_source_context_id is not None:
            raise ValueError(
                "isolated context policy cannot set fork_source_context_id"
            )
        if self.mode == "fork" and not self.fork_source_context_id:
            raise ValueError("fork context policy requires fork_source_context_id")
        return self


class SubagentCapabilityProfileSnapshotEvent(BaseModel):
    """Immutable event-visible child capability contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    profile_name: SubagentCapabilityProfileName
    inherited_from_parent_context_id: str | None
    permission_mode: SubagentPermissionMode
    permission_policy: dict[str, Any]
    allowed_tool_names: tuple[str, ...]
    allowed_descriptor_ids: tuple[str, ...]
    allowed_skill_names: tuple[str, ...]
    allowed_mcp_server_ids: tuple[str, ...]
    can_spawn_subagents: bool
    max_spawn_depth_from_root: int
    memory_enabled: bool
    computed_from_parent_exposure_generation: int | None
    diagnostics: tuple[dict[str, Any], ...]

    @model_validator(mode="after")
    def _validate_capability_profile(self) -> SubagentCapabilityProfileSnapshotEvent:
        _validate_preset_permission_payload(
            mode=self.permission_mode,
            policy=self.permission_policy,
            context="SubagentCapabilityProfileSnapshotEvent",
        )
        if self.can_spawn_subagents:
            raise ValueError("V1 child capability profile cannot spawn subagents")
        if self.max_spawn_depth_from_root != 0:
            raise ValueError(
                "V1 child capability profile max_spawn_depth_from_root must be 0"
            )
        if self.memory_enabled:
            raise ValueError("V1 child capability profile cannot enable memory")
        for field_name in (
            "allowed_tool_names",
            "allowed_descriptor_ids",
            "allowed_skill_names",
            "allowed_mcp_server_ids",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be unique and canonically sorted")
        return self


class SubagentBudgetSnapshotEvent(BaseModel):
    """Immutable event-visible limits for one child runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent_children_per_parent_run: int
    max_concurrent_children_per_host_session: int
    max_spawn_depth_from_root: int
    child_timeout_seconds: float | None
    max_total_child_runs_per_parent_run: int
    max_result_summary_chars_per_child: int
    max_subagent_results_per_parent_compile: int

    @model_validator(mode="after")
    def _validate_budget(self) -> SubagentBudgetSnapshotEvent:
        positive_fields = (
            "max_concurrent_children_per_parent_run",
            "max_concurrent_children_per_host_session",
            "max_total_child_runs_per_parent_run",
            "max_subagent_results_per_parent_compile",
        )
        for field_name in positive_fields:
            if getattr(self, field_name) < 1:
                raise ValueError(f"{field_name} must be >= 1")
        if self.max_spawn_depth_from_root != 0:
            raise ValueError("V1 subagent budget max_spawn_depth_from_root must be 0")
        if self.max_result_summary_chars_per_child < 0:
            raise ValueError("max_result_summary_chars_per_child must be >= 0")
        if self.child_timeout_seconds is not None:
            if (
                not math.isfinite(self.child_timeout_seconds)
                or self.child_timeout_seconds <= 0
            ):
                raise ValueError(
                    "child_timeout_seconds must be finite and > 0 when provided"
                )
        return self


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
    spawning_tool_name: str | None = None
    spawn_initiator_kind: (
        Literal["tool_call", "scheduler", "dependency_satisfied"] | None
    ) = None
    spawn_initiator_id: str | None = None
    child_runtime_session_id: str
    label: str | None = None
    role: str
    profile_id: str | None = None
    task_preview: str
    context_policy: SubagentContextPolicySnapshotEvent
    capability_profile: SubagentCapabilityProfileSnapshotEvent
    budget_snapshot: SubagentBudgetSnapshotEvent

    @model_validator(mode="after")
    def _validate_spawn_initiator(self) -> SubagentRunStartedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        if self.task_id is None:
            if self.run_index is not None:
                raise ValueError("primitive subagent run cannot set run_index")
        elif self.run_index != 1:
            raise ValueError("V1 task-backed subagent run requires run_index=1")
        if self.spawn_initiator_kind == "tool_call":
            if not self.spawn_initiator_id:
                raise ValueError(
                    "tool_call spawn initiator requires spawn_initiator_id"
                )
        elif self.spawn_initiator_kind is not None and not self.spawn_initiator_id:
            raise ValueError("non-tool spawn initiator requires spawn_initiator_id")
        elif self.spawning_tool_name is not None:
            raise ValueError(
                "spawning_tool_name is only valid for tool_call initiators"
            )
        return self


class SubagentMessageSentEvent(EventBase):
    type: Literal[EventType.SUBAGENT_MESSAGE_SENT] = EventType.SUBAGENT_MESSAGE_SENT
    edge_id: str
    subagent_run_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    child_runtime_session_id: str
    message_artifact_id: str
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
    result_artifact_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    token_usage: dict[str, Any] | None = None
    tool_call_count: int | None = None

    @model_validator(mode="after")
    def _validate_result_artifact(self) -> SubagentRunCompletedEvent:
        if self.result_artifact_id not in self.artifact_ids:
            raise ValueError("result_artifact_id must be present in artifact_ids")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("artifact_ids must be unique")
        return self


class SubagentRunFailedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_FAILED] = EventType.SUBAGENT_RUN_FAILED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentRunFailedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        return self


class SubagentRunCancelledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_CANCELLED] = EventType.SUBAGENT_RUN_CANCELLED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    cancelled_by: Literal["user", "parent_agent", "runtime", "host_shutdown"]
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentRunCancelledEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        return self


class SubagentEdgeRecordedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_EDGE_RECORDED] = EventType.SUBAGENT_EDGE_RECORDED
    edge_id: str
    edge_kind: Literal[
        "spawn", "send", "followup", "wait", "cancel", "result", "suspend", "resume"
    ]
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
    type: Literal[EventType.SUBAGENT_RESULT_DELIVERED] = (
        EventType.SUBAGENT_RESULT_DELIVERED
    )
    subagent_run_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None = None
    parent_reply_id: str | None = None
    context_id: str
    model_call_index: int
    section_id: str
    delivery_kind: Literal["internal_section"] = "internal_section"
    result_id: str
    result_artifact_id: str
    summary: str

    @model_validator(mode="after")
    def _validate_delivery_join(self) -> SubagentResultDeliveredEvent:
        if self.model_call_index < 0:
            raise ValueError("model_call_index must be >= 0")
        if not self.context_id or not self.section_id or not self.result_artifact_id:
            raise ValueError("delivery join fields must be non-empty")
        return self


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
    objective_artifact_id: str
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_task_creation(self) -> SubagentTaskCreatedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        if self.task_id in self.depends_on:
            raise ValueError("task cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on must be unique")
        return self


class SubagentTaskScheduledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_SCHEDULED] = EventType.SUBAGENT_TASK_SCHEDULED
    task_id: str
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    schedule_reason: Literal["immediate", "dependency_satisfied", "manual"] = (
        "immediate"
    )

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentTaskScheduledEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        return self


class SubagentTaskStartedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_STARTED] = EventType.SUBAGENT_TASK_STARTED
    task_id: str
    subagent_run_id: str
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    run_index: int = 1
    spawn_initiator_kind: Literal["tool_call", "scheduler", "dependency_satisfied"]
    spawn_initiator_id: str

    @model_validator(mode="after")
    def _validate_task_start(self) -> SubagentTaskStartedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        if self.run_index != 1:
            raise ValueError("V1 task-backed run_index must be 1")
        if not self.spawn_initiator_id:
            raise ValueError("spawn_initiator_id must be non-empty")
        return self


class SubagentTaskBlockedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_BLOCKED] = EventType.SUBAGENT_TASK_BLOCKED
    task_id: str
    status: Literal["waiting_dependency", "blocked_dependency_failed"]
    blocked_reason: Literal["waiting_dependency", "dependency_failed"]
    blocked_by_task_ids: list[str] = Field(default_factory=list)
    dependency_status_snapshot: dict[str, str] = Field(default_factory=dict)
    dependency_terminal_event_ids: dict[str, str] = Field(default_factory=dict)
    dependency_generation: int | None = None

    @model_validator(mode="after")
    def _validate_dependency_facts(self) -> SubagentTaskBlockedEvent:
        blocked_ids = set(self.blocked_by_task_ids)
        if len(blocked_ids) != len(self.blocked_by_task_ids):
            raise ValueError("blocked_by_task_ids must be unique")
        if set(self.dependency_status_snapshot) != blocked_ids:
            raise ValueError(
                "dependency_status_snapshot keys must match blocked_by_task_ids"
            )
        terminal = self.status == "blocked_dependency_failed"
        if terminal:
            if self.blocked_reason != "dependency_failed" or not blocked_ids:
                raise ValueError(
                    "terminal dependency block requires failed dependency facts"
                )
            if set(self.dependency_terminal_event_ids) != blocked_ids:
                raise ValueError(
                    "dependency_terminal_event_ids keys must match blocked_by_task_ids"
                )
            if self.dependency_generation is None or self.dependency_generation < 0:
                raise ValueError(
                    "terminal dependency block requires dependency_generation"
                )
        elif (
            self.blocked_reason != "waiting_dependency"
            or self.dependency_terminal_event_ids
            or self.dependency_generation is not None
        ):
            raise ValueError(
                "waiting dependency block cannot carry terminal dependency facts"
            )
        return self


class SubagentTaskCompletedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_COMPLETED] = EventType.SUBAGENT_TASK_COMPLETED
    task_id: str
    subagent_run_id: str
    result_id: str
    primary_result_artifact_id: str
    result_source: Literal["explicit", "inferred"] = "inferred"


class SubagentTaskFailedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_FAILED] = EventType.SUBAGENT_TASK_FAILED
    task_id: str
    subagent_run_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentTaskFailedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        return self


class SubagentTaskCancelledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_CANCELLED] = EventType.SUBAGENT_TASK_CANCELLED
    task_id: str
    subagent_run_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    cancelled_by: Literal["user", "parent_agent", "runtime", "host_shutdown"]
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentTaskCancelledEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        return self


class SubagentPhaseReportedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_PHASE_REPORTED] = EventType.SUBAGENT_PHASE_REPORTED
    subagent_run_id: str
    task_id: str | None = None
    phase: str
    message: str | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    source_tool_call_id: str | None = None


class SubagentResultSubmittedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RESULT_SUBMITTED] = (
        EventType.SUBAGENT_RESULT_SUBMITTED
    )
    subagent_run_id: str
    task_id: str | None = None
    result_id: str
    summary: str
    output_preview: str | None = None
    result_artifact_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    result_source: Literal["explicit"] = "explicit"
    source_tool_call_id: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_result_artifact(self) -> SubagentResultSubmittedEvent:
        if self.result_artifact_id not in self.artifact_ids:
            raise ValueError("result_artifact_id must be present in artifact_ids")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("artifact_ids must be unique")
        return self


class SubagentResultConsumedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RESULT_CONSUMED] = (
        EventType.SUBAGENT_RESULT_CONSUMED
    )
    consumption_id: str
    consumer_tool_call_id: str
    kind: Literal["wait_run", "wait_task"]
    task_id: str | None = None
    subagent_run_id: str | None = None
    result_id: str | None = None
    consumed_status: Literal[
        "completed", "failed", "cancelled", "blocked_dependency_failed"
    ]
    terminal_event_id: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_consumption(self) -> SubagentResultConsumedEvent:
        if self.task_id is None and self.subagent_run_id is None:
            raise ValueError("consumption requires task_id or subagent_run_id")
        if self.kind == "wait_run" and self.subagent_run_id is None:
            raise ValueError("wait_run consumption requires subagent_run_id")
        if self.kind == "wait_task" and self.task_id is None:
            raise ValueError("wait_task consumption requires task_id")
        if self.result_id is None and self.terminal_event_id is None:
            raise ValueError("consumption without result_id requires terminal_event_id")
        if self.consumed_status == "completed" and self.result_id is None:
            raise ValueError("completed consumption requires result_id")
        return self


class CustomEvent(EventBase):
    type: Literal[EventType.CUSTOM] = EventType.CUSTOM
    name: str
    value: dict[str, Any] = Field(default_factory=dict)


AgentEvent: TypeAlias = (
    RunStartEvent
    | McpCapabilitySnapshotInstalledEvent
    | RunEndEvent
    | ReplyStartEvent
    | ReplyEndEvent
    | RunErrorEvent
    | ExceedMaxItersEvent
    | ContextCompiledEvent
    | CapabilityGateDecisionEvent
    | ModelCallStartEvent
    | ModelCallEndEvent
    | ModelCallRejectedEvent
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
    | ContextCompactionMemoryCandidatesProposedEvent
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
