"""Loop state for the main agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pulsara_agent.message import Msg, ToolCallBlock, ToolResultBlock, Usage
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.runtime.permission_snapshot import RunPermissionSnapshot

if TYPE_CHECKING:
    from pulsara_agent.capability.execution_surface import FrozenCapabilityExecutionSurface
    from pulsara_agent.replay.provenance import RuntimeEventSpan
    from pulsara_agent.primitives.capability import CapabilityResolveBasisFact
    from pulsara_agent.primitives.context import ContextEventReferenceFact
    from pulsara_agent.primitives.run_entry import SubagentRunEntryFact
    from pulsara_agent.primitives.user_message import CurrentUserMessageFact
    from pulsara_agent.ports.event_write import FrozenEventWriteCandidate
    from pulsara_agent.runtime.plan import PlanWorkflowState
    from pulsara_agent.llm.resolution import ResolvedModelTarget
    from pulsara_agent.runtime.recovery import (
        AbortKind,
        InRunRecoveryState,
        StopRequest,
    )
    from pulsara_agent.runtime.run_entry import RunWorkingSet

class LoopStatus(StrEnum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    FINISHED = "finished"
    FAILED = "failed"
    ABORTED = "aborted"


class LoopTransition(StrEnum):
    START = "start"
    CONTINUE_AFTER_MODEL = "continue_after_model"
    CONTINUE_AFTER_TOOL = "continue_after_tool"
    CONTINUE_AFTER_RECOVERY = "continue_after_recovery"
    FINISH = "finish"
    FAIL = "fail"
    WAIT_FOR_USER = "wait_for_user"


@dataclass(frozen=True, slots=True)
class LoopBudget:
    max_consecutive_model_failures: int = 2
    max_consecutive_tool_failures: int = 8
    max_plan_interactions_per_run: int = 16
    max_plan_exit_revisions_per_run: int = 8
    projection_token_budget: int = 2_000
    recall_hard_timeout_ms: int = 1_500
    tool_result_per_tool_cap_chars: int = 32_000
    tool_result_per_message_cap_chars: int = 64_000
    tool_result_per_envelope_cap_chars: int = 1_200
    minimum_essential_envelope_chars: int = 256
    max_subagent_results_per_parent_compile: int = 8


@dataclass(slots=True)
class PlanEntryAuditState:
    source: Literal["user", "agent"]
    reason: str
    previous_permission_mode: str
    previous_permission_policy: dict[str, object]
    event_id: str


@dataclass(slots=True)
class RunPlanProgressState:
    workflow_state: PlanWorkflowState | None = None
    entry_audit: PlanEntryAuditState | None = None
    entry_audit_emitted: bool = False
    exit_revisions: int = 0
    interactions: int = 0
    revision_feedback: str = ""
    revision_required: bool = False


@dataclass(slots=True)
class RunModelToolProgressState:
    model_call_index: int = 0
    current_model_call_index: int | None = None
    current_context_id: str | None = None
    latest_model_control_disposition_event_id: str | None = None
    latest_model_control_disposition_model_call_index: int | None = None
    tool_result_event_spans: dict[str, RuntimeEventSpan] = field(default_factory=dict)
    tool_result_audit_consumed_call_ids: set[str] = field(default_factory=set)
    active_context_window_id: str | None = None


@dataclass(slots=True)
class RunExecutionResourceState:
    host_session_id: str | None = None
    run_execution_handle_id: str | None = None
    capability_execution_borrow_authority: object | None = None
    capability_execution_borrow_kind: Literal["parent", "child"] = "parent"
    capability_resolve_basis: CapabilityResolveBasisFact | None = None
    frozen_capability_execution_surface: FrozenCapabilityExecutionSurface | None = None
    subagent_run_entry_fact: SubagentRunEntryFact | None = None
    current_user_message_fact: CurrentUserMessageFact | None = None
    resume_activation_blocked: bool = False
    resume_boundary_attempts: dict[str, int] = field(default_factory=dict)
    latest_mcp_input_required_resolution_reference: (
        ContextEventReferenceFact | None
    ) = None


@dataclass(slots=True)
class RunActivationWorkingState:
    """Short-lived state for one active agent loop.

    This is the Working Context Cache. It is not a durable fact source.
    """

    session_id: str
    run_id: str = field(default_factory=lambda: f"run:{uuid4().hex}")
    turn_id: str = field(default_factory=lambda: f"turn:{uuid4().hex}")
    reply_id: str = field(default_factory=lambda: f"reply:{uuid4().hex}")
    turn_index: int = 0
    current_scope: str | None = None
    status: LoopStatus = LoopStatus.RUNNING
    last_transition: LoopTransition = LoopTransition.START
    messages: list[Msg] = field(default_factory=list)
    pending_tool_calls: list[ToolCallBlock] = field(default_factory=list)
    pending_interaction_kind: str | None = None
    pending_interaction_payload: dict[str, Any] = field(default_factory=dict)
    pending_interaction_source_event_reference: (
        ContextEventReferenceFact | None
    ) = None
    pending_interaction_source_event_candidate: FrozenEventWriteCandidate | None = None
    tool_results: list[ToolResultBlock] = field(default_factory=list)
    memory_projection: dict[str, Any] | None = None
    token_usage: Usage = field(default_factory=Usage)
    tool_call_count: int = 0
    consecutive_model_failures: int = 0
    consecutive_tool_failures: int = 0
    in_run_recovery: InRunRecoveryState | None = None
    stop_request: StopRequest | None = None
    abort_kind: AbortKind | None = None
    compacted: bool = False
    stop_reason: RunStopReason | None = None
    error_message: str | None = None
    finalized: bool = False
    plan_progress: RunPlanProgressState = field(default_factory=RunPlanProgressState)
    model_tool_progress: RunModelToolProgressState = field(
        default_factory=RunModelToolProgressState
    )
    execution_resources: RunExecutionResourceState = field(
        default_factory=RunExecutionResourceState
    )
    budget: LoopBudget = field(default_factory=LoopBudget)
    permission_snapshot: RunPermissionSnapshot | None = None
    run_model_target: ResolvedModelTarget | None = None
    run_working_set: RunWorkingSet | None = None
    terminal_run_end_event_id: str | None = None

    def begin_next_turn(self) -> None:
        self.turn_index += 1
        self.turn_id = f"turn:{uuid4().hex}"
        self.reply_id = f"reply:{uuid4().hex}"
        self.pending_tool_calls = []
        self.pending_interaction_kind = None
        self.pending_interaction_payload = {}
        self.pending_interaction_source_event_reference = None
        self.pending_interaction_source_event_candidate = None
        self.tool_results = []

    def transition(self, transition: LoopTransition) -> None:
        self.last_transition = transition
