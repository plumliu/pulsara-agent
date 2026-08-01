"""Closed renderer-neutral terminal application request/outcome contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.presentation_view import (
    PresentationHistoryViewportSnapshotFact,
)
from pulsara_agent.primitives.prompt_queue import (
    PromptQueueContentRetentionState,
    PromptQueueDeliveryMode,
    PromptQueueDeliveryState,
    PromptQueueResolvedDeliveryMode,
)

CommandOutcomeStatus = Literal[
    "succeeded",
    "rejected",
    "pending_confirmation",
    "reconciliation_required",
    "superseded_by_compatible_winner",
]


@dataclass(frozen=True, slots=True)
class TerminalCommandBinding:
    client_instance_id: str
    attachment_id: str
    attachment_generation: int
    command_id: str
    runtime_session_id: str
    expected_target_id: str
    expected_target_generation: int
    expected_controller_generation: int
    request_semantic_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not all(
                (
                    self.client_instance_id,
                    self.attachment_id,
                    self.command_id,
                    self.runtime_session_id,
                    self.expected_target_id,
                    self.request_semantic_fingerprint,
                )
            )
            or min(
                self.attachment_generation,
                self.expected_target_generation,
                self.expected_controller_generation,
            )
            < 1
        ):
            raise ValueError("terminal command binding is malformed")


@dataclass(frozen=True, slots=True)
class SubmitPromptRequest:
    command_kind: Literal["submit_prompt"]
    binding: TerminalCommandBinding
    client_submission_id: str
    text: str
    requested_delivery_mode: Literal["auto", "steer", "follow_up"]
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class StopRunRequest:
    command_kind: Literal["stop_run"]
    binding: TerminalCommandBinding
    reason: Literal["user_stop"]
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResolveApprovalRequest:
    command_kind: Literal["resolve_approval"]
    binding: TerminalCommandBinding
    approval_id: str
    decisions: tuple[tuple[str, bool], ...]
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResolvePlanQuestionRequest:
    command_kind: Literal["resolve_plan_question"]
    binding: TerminalCommandBinding
    interaction_id: str
    answer_text: str
    selected_option: str | None
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResolvePlanExitRequest:
    command_kind: Literal["resolve_plan_exit"]
    binding: TerminalCommandBinding
    interaction_id: str
    decision: Literal["approve", "revise", "cancel"]
    user_feedback: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResolveMcpInteractionRequest:
    command_kind: Literal["resolve_mcp_interaction"]
    binding: TerminalCommandBinding
    interaction_id: str
    sealed_response_handle_id: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class CancelMcpInteractionRequest:
    command_kind: Literal["cancel_mcp_interaction"]
    binding: TerminalCommandBinding
    interaction_id: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class QueueCancelRequest:
    command_kind: Literal["queue_cancel"]
    binding: TerminalCommandBinding
    queue_item_id: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class DetachSessionRequest:
    command_kind: Literal["detach_session"]
    binding: TerminalCommandBinding
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class CloseSessionRequest:
    command_kind: Literal["close_session"]
    binding: TerminalCommandBinding
    close_conversation: bool
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class StartSuccessorSessionRequest:
    command_kind: Literal["start_successor_session"]
    binding: TerminalCommandBinding
    source_capacity_state_fingerprint: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ControllerTakeoverRequest:
    command_kind: Literal["controller_takeover"]
    binding: TerminalCommandBinding
    expected_previous_controller_generation: int
    request_fingerprint: str


TerminalMutationRequest: TypeAlias = (
    SubmitPromptRequest
    | StopRunRequest
    | ResolveApprovalRequest
    | ResolvePlanQuestionRequest
    | ResolvePlanExitRequest
    | ResolveMcpInteractionRequest
    | CancelMcpInteractionRequest
    | QueueCancelRequest
    | DetachSessionRequest
    | CloseSessionRequest
    | StartSuccessorSessionRequest
    | ControllerTakeoverRequest
)


@dataclass(frozen=True, slots=True)
class TerminalCommandOutcome:
    status: CommandOutcomeStatus
    command_id: str
    target_id: str
    target_generation: int
    public_result_code: str
    public_result_text: str
    durable_reference_ids: tuple[str, ...]
    query_token: str
    outcome_fingerprint: str

    def __post_init__(self) -> None:
        if self.status not in {
            "succeeded",
            "rejected",
            "pending_confirmation",
            "reconciliation_required",
            "superseded_by_compatible_winner",
        }:
            raise ValueError("terminal command outcome status is unknown")
        if (
            not self.command_id
            or not self.target_id
            or self.target_generation < 1
            or not self.public_result_code
            or not self.query_token
        ):
            raise ValueError("terminal command outcome identity is malformed")
        if (
            len(self.public_result_text) > 512
            or len(self.public_result_text.encode("utf-8")) > 2_048
        ):
            raise ValueError("terminal command public result exceeds its hard bound")
        if len(self.durable_reference_ids) > 64 or any(
            not item for item in self.durable_reference_ids
        ):
            raise ValueError("terminal command durable references are malformed")
        expected = terminal_command_outcome_fingerprint(
            status=self.status,
            command_id=self.command_id,
            target_id=self.target_id,
            target_generation=self.target_generation,
            public_result_code=self.public_result_code,
            public_result_text=self.public_result_text,
            durable_reference_ids=self.durable_reference_ids,
            query_token=self.query_token,
        )
        if self.outcome_fingerprint != expected:
            raise ValueError("terminal command outcome fingerprint mismatch")


def terminal_command_outcome_fingerprint(
    *,
    status: CommandOutcomeStatus,
    command_id: str,
    target_id: str,
    target_generation: int,
    public_result_code: str,
    public_result_text: str,
    durable_reference_ids: tuple[str, ...],
    query_token: str,
) -> str:
    """Return the one complete semantic identity for a public command outcome."""

    return context_fingerprint(
        "terminal-command-outcome:v1",
        {
            "status": status,
            "command_id": command_id,
            "target_id": target_id,
            "target_generation": target_generation,
            "public_result_code": public_result_code,
            "public_result_text": public_result_text,
            "durable_reference_ids": durable_reference_ids,
            "query_token": query_token,
        },
    )


@dataclass(frozen=True, slots=True)
class ApprovalRequestView:
    interaction_kind: Literal["approval"]
    interaction_id: str
    run_id: str
    tool_calls: tuple[tuple[str, str], ...]
    view_fingerprint: str


@dataclass(frozen=True, slots=True)
class PlanQuestionView:
    interaction_kind: Literal["plan_question"]
    interaction_id: str
    run_id: str
    question: str
    options: tuple[tuple[str, str], ...]
    allow_free_text: bool
    view_fingerprint: str


@dataclass(frozen=True, slots=True)
class PlanExitView:
    interaction_kind: Literal["plan_exit"]
    interaction_id: str
    run_id: str
    summary: str
    plan_artifact_id: str | None
    view_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpInputRequestPublicView:
    request_key: str
    mode: Literal["form", "url"]
    public_prompt: str
    schema_or_url_present: bool


@dataclass(frozen=True, slots=True)
class McpInteractionView:
    interaction_kind: Literal["mcp_input_required"]
    interaction_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    server_id: str
    requests: tuple[McpInputRequestPublicView, ...]
    view_fingerprint: str


TerminalInteractionRequestView: TypeAlias = (
    ApprovalRequestView | PlanQuestionView | PlanExitView | McpInteractionView
)


@dataclass(frozen=True, slots=True)
class PromptQueueItemView:
    queue_item_id: str
    accepted_ordinal: int
    delivery_state: PromptQueueDeliveryState
    content_retention_state: PromptQueueContentRetentionState
    requested_delivery_mode: PromptQueueDeliveryMode
    resolved_delivery_mode: PromptQueueResolvedDeliveryMode
    public_preview: str
    head_event_id: str
    item_revision: int
    view_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.queue_item_id
            or self.accepted_ordinal < 1
            or self.delivery_state
            not in {
                "accepted_pending",
                "steer_reserved",
                "follow_up_reserved",
                "committed_to_active_run",
                "committed_to_new_run",
                "cancelled",
                "delivery_rejected",
                "reconciliation_required",
            }
            or self.content_retention_state not in {"active", "retired"}
            or self.requested_delivery_mode not in {"auto", "steer", "follow_up"}
            or self.resolved_delivery_mode not in {"pending", "steer", "follow_up"}
            or not self.head_event_id
            or self.item_revision < 1
        ):
            raise ValueError("terminal prompt queue view is malformed")
        if (
            len(self.public_preview) > 512
            or len(self.public_preview.encode("utf-8")) > 2_048
        ):
            raise ValueError("terminal prompt queue preview exceeds its bound")
        payload = {
            "queue_item_id": self.queue_item_id,
            "accepted_ordinal": self.accepted_ordinal,
            "delivery_state": self.delivery_state,
            "content_retention_state": self.content_retention_state,
            "requested_delivery_mode": self.requested_delivery_mode,
            "resolved_delivery_mode": self.resolved_delivery_mode,
            "public_preview": self.public_preview,
            "head_event_id": self.head_event_id,
            "item_revision": self.item_revision,
        }
        if self.view_fingerprint != context_fingerprint(
            "prompt-queue-item-view:v1", payload
        ):
            raise ValueError("terminal prompt queue view fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class TerminalUiSessionSnapshot:
    host_session_id: str
    runtime_session_id: str
    lifecycle: Literal["open", "closing", "closed"]
    authority_high_water: int
    projection_revision: int
    viewport: PresentationHistoryViewportSnapshotFact
    operational_generation: int
    operational_cursor: int
    pending_interaction: TerminalInteractionRequestView | None
    queue_items: tuple[PromptQueueItemView, ...]
    queue_head_event_id: str | None
    queue_account_revision: int
    active_run_id: str | None
    suspended_run_id: str | None
    stopping_run_id: str | None
    snapshot_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.host_session_id
            or not self.runtime_session_id
            or self.lifecycle not in {"open", "closing", "closed"}
            or self.authority_high_water < 0
            or self.projection_revision < 0
            or self.operational_generation < 1
            or self.operational_cursor < 0
            or self.queue_account_revision < 0
        ):
            raise ValueError("terminal UI session snapshot is malformed")
        if (
            self.viewport.active_head.runtime_session_id != self.runtime_session_id
            or self.viewport.active_head.through_authority_sequence
            != self.authority_high_water
            or self.viewport.projection_revision != self.projection_revision
        ):
            raise ValueError("terminal UI snapshot viewport join mismatch")
        queue_ordinals = tuple(item.accepted_ordinal for item in self.queue_items)
        if queue_ordinals != tuple(sorted(queue_ordinals)) or len(
            {item.queue_item_id for item in self.queue_items}
        ) != len(self.queue_items):
            raise ValueError("terminal UI queue projection is not ordered and unique")
        payload = {
            "host_session_id": self.host_session_id,
            "runtime_session_id": self.runtime_session_id,
            "lifecycle": self.lifecycle,
            "authority_high_water": self.authority_high_water,
            "projection_revision": self.projection_revision,
            "viewport_fingerprint": self.viewport.viewport_fingerprint,
            "operational_generation": self.operational_generation,
            "operational_cursor": self.operational_cursor,
            "pending_interaction_fingerprint": (
                self.pending_interaction.view_fingerprint
                if self.pending_interaction is not None
                else None
            ),
            "queue_head_event_id": self.queue_head_event_id,
            "queue_account_revision": self.queue_account_revision,
            "queue_item_fingerprints": tuple(
                item.view_fingerprint for item in self.queue_items
            ),
            "active_run_id": self.active_run_id,
            "suspended_run_id": self.suspended_run_id,
            "stopping_run_id": self.stopping_run_id,
        }
        if self.snapshot_fingerprint != context_fingerprint(
            "terminal-ui-session-snapshot:v1", payload
        ):
            raise ValueError("terminal UI snapshot fingerprint mismatch")


__all__ = [
    "ApprovalRequestView",
    "CancelMcpInteractionRequest",
    "CloseSessionRequest",
    "CommandOutcomeStatus",
    "ControllerTakeoverRequest",
    "DetachSessionRequest",
    "McpInputRequestPublicView",
    "McpInteractionView",
    "PlanExitView",
    "PlanQuestionView",
    "PromptQueueItemView",
    "QueueCancelRequest",
    "ResolveApprovalRequest",
    "ResolveMcpInteractionRequest",
    "ResolvePlanExitRequest",
    "ResolvePlanQuestionRequest",
    "StartSuccessorSessionRequest",
    "StopRunRequest",
    "SubmitPromptRequest",
    "TerminalCommandBinding",
    "TerminalCommandOutcome",
    "terminal_command_outcome_fingerprint",
    "TerminalInteractionRequestView",
    "TerminalMutationRequest",
    "TerminalUiSessionSnapshot",
]
