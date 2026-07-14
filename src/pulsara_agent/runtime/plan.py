"""Plan workflow state and pending-interaction models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, TypeAlias
from uuid import uuid4

from pulsara_agent.event import AgentEvent, PlanModeEnteredEvent, PlanModeExitedEvent, PlanQuestionOption
from pulsara_agent.runtime.approval import PendingApproval
from pulsara_agent.primitives.permission import PermissionMode, parse_permission_mode
from pulsara_agent.runtime.permission import EffectivePermissionPolicy
from pulsara_agent.runtime.permission_snapshot import validate_preset_policy_payload
from pulsara_agent.runtime.state import LoopState, LoopStatus
PLAN_ENTRY_INSTRUCTION_NAME = "plan_entry_instruction"
PLAN_ACTIVE_INSTRUCTION_NAME = "plan_active_instruction"

PLAN_ENTRY_INSTRUCTION = (
    "Plan workflow is active. The host has already switched this session to read-only for planning. "
    "Do not implement changes yet. Inspect relevant context with read-only tools and keep scratch work in "
    "agent-local todo if useful. Resolve discoverable facts from the repository before asking. When a material "
    "design choice remains ambiguous and needs the user, call ask_plan_question with one concise question and "
    "2-3 mutually exclusive structured options; mark one option recommended when there is a safe default. "
    "Submit the final plan with exit_plan for user approval before doing side-effecting work."
)

PLAN_ACTIVE_INSTRUCTION = (
    "You are still in Plan workflow. Workspace/file/terminal/durable side effects are blocked by read-only permission."
    "Continue planning with read-only inspection and agent-local todo only. Ask the user with ask_plan_question only "
    "for material choices that cannot be resolved from repo evidence; provide 2-3 structured options and a recommended "
    "default when possible. When the plan is ready, call exit_plan and wait for the user's decision. If the user "
    "requests a plan revision, incorporate that feedback and call exit_plan again; do not answer with prose only."
)


def normalize_plan_question_options(raw_options: Any) -> tuple[PlanQuestionOption, ...]:
    if raw_options is None:
        return ()
    if not isinstance(raw_options, list | tuple):
        raise ValueError("options must be a list of strings or objects")
    options: list[PlanQuestionOption] = []
    for item in raw_options:
        if isinstance(item, PlanQuestionOption):
            options.append(item)
        elif isinstance(item, str):
            options.append(PlanQuestionOption(label=item))
        elif isinstance(item, dict):
            options.append(PlanQuestionOption.model_validate(item))
        else:
            raise ValueError("options must contain strings or objects")
    return tuple(options)


@dataclass(slots=True)
class PlanWorkflowState:
    active: bool = False
    entered_by: Literal["user", "agent"] | None = None
    entered_at: float | None = None
    pre_plan_permission_mode: str | None = None
    pre_plan_permission_policy: dict[str, object] | None = None
    pending_entry_audit: bool = False
    entry_reason: str = ""
    latest_accepted_plan_summary: str = ""
    latest_accepted_plan_artifact_id: str | None = None
    revision: int = 0
    entered_event_id: str | None = None
    entered_event_sequence: int | None = None
    entry_run_id: str | None = None
    entry_turn_id: str | None = None
    entry_reply_id: str | None = None

    def begin(
        self,
        *,
        source: Literal["user", "agent"],
        previous_mode: PermissionMode | str,
        previous_policy: EffectivePermissionPolicy,
        reason: str = "",
        pending_entry_audit: bool = False,
    ) -> None:
        self.active = True
        self.entered_by = source
        self.entered_at = time.monotonic()
        parsed_mode = parse_permission_mode(previous_mode)
        validate_preset_policy_payload(
            parsed_mode,
            previous_policy.to_dict(),
            context="PlanWorkflowState.begin",
        )
        self.pre_plan_permission_mode = parsed_mode.value
        self.pre_plan_permission_policy = previous_policy.to_dict()
        self.pending_entry_audit = pending_entry_audit
        self.entry_reason = reason

    def finish(
        self,
        *,
        accepted_plan_summary: str = "",
        accepted_plan_artifact_id: str | None = None,
    ) -> None:
        self.active = False
        self.entered_by = None
        self.entered_at = None
        self.pre_plan_permission_mode = None
        self.pre_plan_permission_policy = None
        self.pending_entry_audit = False
        self.entry_reason = ""
        self.entered_event_id = None
        self.entered_event_sequence = None
        self.entry_run_id = None
        self.entry_turn_id = None
        self.entry_reply_id = None
        self.latest_accepted_plan_summary = accepted_plan_summary
        self.latest_accepted_plan_artifact_id = accepted_plan_artifact_id

    def apply_durable_event(self, event: AgentEvent) -> None:
        if event.sequence is None:
            raise ValueError("plan workflow projection requires a committed event")
        self._apply_projection_event(event)

    def _apply_projection_event(self, event: AgentEvent) -> None:
        if isinstance(event, PlanModeEnteredEvent):
            self.active = True
            self.entered_by = event.source
            self.entered_at = None
            self.pre_plan_permission_mode = event.previous_permission_mode
            self.pre_plan_permission_policy = dict(event.previous_permission_policy)
            self.pending_entry_audit = False
            self.entry_reason = event.reason
            self.entered_event_id = event.id
            self.entered_event_sequence = event.sequence
            self.entry_run_id = event.run_id
            self.entry_turn_id = event.turn_id
            self.entry_reply_id = event.reply_id
            self.revision += 1
        elif isinstance(event, PlanModeExitedEvent):
            self.finish(
                accepted_plan_summary=(
                    event.accepted_plan_summary
                    if event.source == "approved_exit_plan"
                    else ""
                ),
                accepted_plan_artifact_id=(
                    event.accepted_plan_artifact_id
                    if event.source == "approved_exit_plan"
                    else None
                ),
            )
            self.revision += 1

    def to_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "entered_by": self.entered_by,
            "entered_at": self.entered_at,
            "pre_plan_permission_mode": self.pre_plan_permission_mode,
            "pre_plan_permission_policy": self.pre_plan_permission_policy,
            "pending_entry_audit": self.pending_entry_audit,
            "entry_reason": self.entry_reason,
            "latest_accepted_plan_summary": self.latest_accepted_plan_summary,
            "latest_accepted_plan_artifact_id": self.latest_accepted_plan_artifact_id,
            "revision": self.revision,
            "entered_event_id": self.entered_event_id,
            "entered_event_sequence": self.entered_event_sequence,
            "entry_run_id": self.entry_run_id,
            "entry_turn_id": self.entry_turn_id,
            "entry_reply_id": self.entry_reply_id,
        }


@dataclass(frozen=True, slots=True)
class PlanQuestionResolution:
    interaction_id: str
    answer_text: str
    selected_option: str | None = None


@dataclass(frozen=True, slots=True)
class PlanExitResolution:
    interaction_id: str
    decision: Literal["approve", "revise", "cancel"]
    user_feedback: str = ""


PlanInteractionResolution: TypeAlias = PlanQuestionResolution | PlanExitResolution


@dataclass(frozen=True, slots=True)
class McpInputRequiredInteractionResolution:
    interaction_id: str
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancelled: bool = False


@dataclass(slots=True)
class PendingMcpInputRequired:
    interaction_id: str
    kind: Literal["mcp_input_required"]
    host_session_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_call_id: str
    tool_name: str
    server_id: str
    protocol_version: str | None
    request_state: str | None
    input_requests: tuple[dict[str, Any], ...]
    original_request: dict[str, Any]
    tool_observation_timing_seed: dict[str, Any] = field(default_factory=dict)
    round_count: int = 1
    deadline_monotonic: float | None = None
    created_at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "interaction_id": self.interaction_id,
            "kind": self.kind,
            "host_session_id": self.host_session_id,
            "runtime_session_id": self.runtime_session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "reply_id": self.reply_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "server_id": self.server_id,
            "protocol_version": self.protocol_version,
            "request_state": self.request_state,
            "input_requests": [dict(item) for item in self.input_requests],
            "original_request": dict(self.original_request),
            "tool_observation_timing_seed": dict(self.tool_observation_timing_seed),
            "round_count": self.round_count,
            "created_at": self.created_at,
        }
        if self.deadline_monotonic is not None:
            payload["deadline_monotonic"] = self.deadline_monotonic
        return payload


@dataclass(slots=True)
class PendingPlanInteraction:
    interaction_id: str
    kind: Literal["question", "exit"]
    host_session_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_call_id: str
    question_id: str | None = None
    question: str = ""
    options: tuple[PlanQuestionOption, ...] = ()
    allow_free_text: bool = True
    exit_request_id: str | None = None
    plan_text: str = ""
    plan_artifact_id: str | None = None
    summary: str = ""
    created_at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, object]:
        return {
            "interaction_id": self.interaction_id,
            "kind": self.kind,
            "host_session_id": self.host_session_id,
            "runtime_session_id": self.runtime_session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "reply_id": self.reply_id,
            "tool_call_id": self.tool_call_id,
            "question_id": self.question_id,
            "question": self.question,
            "options": [option.model_dump() for option in self.options],
            "allow_free_text": self.allow_free_text,
            "exit_request_id": self.exit_request_id,
            "plan_text": self.plan_text,
            "plan_artifact_id": self.plan_artifact_id,
            "summary": self.summary,
            "created_at": self.created_at,
        }


PendingInteraction: TypeAlias = PendingApproval | PendingPlanInteraction | PendingMcpInputRequired


def pending_plan_interaction_from_state(state: LoopState, host_session_id: str) -> PendingPlanInteraction:
    if state.status is not LoopStatus.WAITING_USER:
        raise ValueError("cannot create pending plan interaction from a non-waiting state")
    if state.pending_interaction_kind != "plan":
        raise ValueError("waiting state does not contain a plan interaction")
    payload = dict(state.pending_interaction_payload)
    kind = payload.get("kind")
    if kind not in {"question", "exit"}:
        raise ValueError("pending plan interaction has invalid kind")
    return PendingPlanInteraction(
        interaction_id=str(payload.get("interaction_id") or f"plan_interaction:{uuid4().hex}"),
        kind=kind,  # type: ignore[arg-type]
        host_session_id=host_session_id,
        runtime_session_id=state.session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        tool_call_id=str(payload["tool_call_id"]),
        question_id=payload.get("question_id"),
        question=str(payload.get("question") or ""),
        options=normalize_plan_question_options(payload.get("options") or ()),
        allow_free_text=bool(payload.get("allow_free_text", True)),
        exit_request_id=payload.get("exit_request_id"),
        plan_text=str(payload.get("plan_text") or ""),
        plan_artifact_id=payload.get("plan_artifact_id"),
        summary=str(payload.get("summary") or ""),
    )


def pending_mcp_input_required_from_state(state: LoopState, host_session_id: str) -> PendingMcpInputRequired:
    if state.status is not LoopStatus.WAITING_USER:
        raise ValueError("cannot create pending MCP input-required from a non-waiting state")
    if state.pending_interaction_kind != "mcp_input_required":
        raise ValueError("waiting state does not contain an MCP input-required interaction")
    payload = dict(state.pending_interaction_payload)
    return PendingMcpInputRequired(
        interaction_id=str(payload["interaction_id"]),
        kind="mcp_input_required",
        host_session_id=host_session_id,
        runtime_session_id=state.session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        tool_call_id=str(payload["tool_call_id"]),
        tool_name=str(payload["tool_name"]),
        server_id=str(payload["server_id"]),
        protocol_version=(
            str(payload["protocol_version"]) if payload.get("protocol_version") is not None else None
        ),
        request_state=str(payload["request_state"]) if payload.get("request_state") is not None else None,
        input_requests=tuple(dict(item) for item in payload.get("input_requests") or ()),
        original_request=dict(payload.get("original_request") or {}),
        tool_observation_timing_seed=dict(payload.get("tool_observation_timing_seed") or {}),
        round_count=int(payload.get("round_count") or 1),
        deadline_monotonic=(
            float(payload["deadline_monotonic"]) if payload.get("deadline_monotonic") is not None else None
        ),
    )


def reduce_plan_workflow_state(events: Iterable[AgentEvent]) -> PlanWorkflowState:
    state = PlanWorkflowState()
    ordered = sorted(
        list(events),
        key=lambda event: event.sequence if event.sequence is not None else 0,
    )
    for event in ordered:
        if isinstance(event, (PlanModeEnteredEvent, PlanModeExitedEvent)):
            # Recovery projections also accept caller-owned event sequences;
            # only the live process projection requires committed envelopes.
            state._apply_projection_event(event)
    return state
