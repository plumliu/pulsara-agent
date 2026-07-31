"""Single-owner process-local state for one committed run."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pulsara_agent.event import AgentEvent, RunEndEvent
from pulsara_agent.ports.run_authority import RunAuthorityHead, RunGenesisAuthority
from pulsara_agent.ports.run_execution import (
    ActivationPhase,
    RunActivationIdentity,
    RunLifecycle,
    RunOwnerIdentity,
    RunTerminationIntent,
)
from pulsara_agent.runtime.execution_handles import (
    RunExecutionHandleBorrow,
    RunExecutionHandleSet,
)
from pulsara_agent.runtime.run_entry import CommittedRunEntry

if TYPE_CHECKING:
    from pulsara_agent.primitives.context import ContextEventReferenceFact
    from pulsara_agent.ports.interaction_transition import InteractionResumeLinkReceipt
    from pulsara_agent.ports.run_execution import (
        PendingInteractionIdentity,
        PendingInteractionAuthority,
        ReconciliationResolutionReceipt,
        RunActivationOutcome,
        RunReconciliationSnapshot,
        RunTerminalOutcome,
    )
    from pulsara_agent.runtime.plan import PendingInteraction
    from pulsara_agent.runtime.run_execution.prepared import RunActivationStateCarrier
    from pulsara_agent.runtime.run_execution.model_step import ModelStepAttempt
    from pulsara_agent.runtime.run_execution.tool_batch import ToolBatchAttempt


@dataclass
class RunUsageAccumulator:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    settled_model_call_count: int = 0
    source_usage_accumulator: str = "sha256:empty"


@dataclass
class RunProgressState:
    owner_identity: RunOwnerIdentity
    progress_generation: int = 0
    turn_index: int = 0
    reply_index: int = 0
    model_call_index: int = 0
    accumulated_usage: RunUsageAccumulator = field(default_factory=RunUsageAccumulator)
    latest_context_reference: object | None = None


@dataclass(frozen=True)
class UnboundRunResources:
    slot_kind: Literal["unbound"] = "unbound"
    reason: Literal[
        "reopen_initial_rebind_pending",
        "reopen_continuation_rebind_pending",
        "terminal_only_recovery",
    ] = "terminal_only_recovery"


@dataclass(frozen=True)
class BoundRunResources:
    handle_set: RunExecutionHandleSet
    slot_kind: Literal["bound"] = "bound"


@dataclass(frozen=True)
class RetiringRunResources:
    handle_set: RunExecutionHandleSet
    slot_kind: Literal["retiring"] = "retiring"


@dataclass(frozen=True)
class ClosedNeverBoundRunResources:
    slot_kind: Literal["closed_never_bound"] = "closed_never_bound"


@dataclass(frozen=True)
class ClosedBoundRunResources:
    closed_handle_id: str
    closed_handle_generation: int
    slot_kind: Literal["closed_bound"] = "closed_bound"


RunResourceSlot: TypeAlias = (
    UnboundRunResources
    | BoundRunResources
    | RetiringRunResources
    | ClosedNeverBoundRunResources
    | ClosedBoundRunResources
)


@dataclass
class RunRetiringResourceSet:
    owner_identity: RunOwnerIdentity
    handles_by_id: dict[str, RunExecutionHandleSet] = field(default_factory=dict)
    set_generation: int = 0


@dataclass(frozen=True)
class NoActiveActivation:
    slot_kind: Literal["none"] = "none"


@dataclass
class RunActivationCoordinator:
    segment_id: str
    segment_generation: int
    segment_state: Literal["reserved", "initializing", "active", "completed"]
    activation_kind: Literal["initial", "interaction_resume"]
    activation_owner_kind: Literal[
        "host_run_boundary", "host_resume_boundary", "subagent_run_start"
    ]
    activation_owner_id: str
    driver_task: asyncio.Task[object] | None
    completion: asyncio.Future["RunActivationCoordinatorResult"]
    observer: "StreamObserverHandle | None"
    activation_identity: RunActivationIdentity | None = None
    phase: ActivationPhase = "safe_point"
    execution_handle_borrow: RunExecutionHandleBorrow | None = None
    active_attempt: "ModelStepAttempt | ToolBatchAttempt | None" = None
    state_carrier: "RunActivationStateCarrier | None" = field(
        default=None,
        repr=False,
    )
    state_owner_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ActiveRunActivation:
    coordinator: RunActivationCoordinator
    slot_kind: Literal["active"] = "active"


RunActivationSlot: TypeAlias = NoActiveActivation | ActiveRunActivation


@dataclass(frozen=True)
class NoActiveSuspension:
    slot_kind: Literal["none"] = "none"


@dataclass
class ActiveRunSuspension:
    authority: "PendingInteractionAuthority"
    resources: "RunSuspensionResources"
    slot_kind: Literal["active"] = "active"


RunSuspensionSlot: TypeAlias = NoActiveSuspension | ActiveRunSuspension


@dataclass(frozen=True, slots=True)
class ApprovalSuspensionResources:
    resource_kind: Literal["approval"]
    resource_generation: int
    pending_interaction_fingerprint: str
    resource_identity_fingerprint: str
    public_view: "PendingInteraction" = field(repr=False, compare=False)
    state_carrier: "RunActivationStateCarrier" = field(repr=False, compare=False)
    state_owner_token: str = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PlanSuspensionResources:
    resource_kind: Literal["plan_question", "plan_exit"]
    resource_generation: int
    pending_interaction_fingerprint: str
    resource_identity_fingerprint: str
    public_view: "PendingInteraction" = field(repr=False, compare=False)
    activation_payload: Mapping[str, object] = field(repr=False, compare=False)
    state_carrier: "RunActivationStateCarrier" = field(repr=False, compare=False)
    state_owner_token: str = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class McpSuspensionResources:
    resource_kind: Literal["mcp_input_required"]
    resource_generation: int
    pending_interaction_fingerprint: str
    resource_identity_fingerprint: str
    public_view: "PendingInteraction" = field(repr=False, compare=False)
    pending_handle: object = field(repr=False, compare=False)
    activation_payload: Mapping[str, object] = field(repr=False, compare=False)
    state_carrier: "RunActivationStateCarrier" = field(repr=False, compare=False)
    state_owner_token: str = field(repr=False, compare=False)


RunSuspensionResources: TypeAlias = (
    ApprovalSuspensionResources | PlanSuspensionResources | McpSuspensionResources
)


@dataclass
class RunFinalizationSlot:
    state: Literal[
        "empty",
        "active",
        "run_end_full_pending_output",
        "completed",
        "reconciliation_required",
    ] = "empty"
    owner: object | None = None
    receipt: object | None = None


@dataclass
class RunFinalizationOwner:
    owner_identity: RunOwnerIdentity
    terminal_event_id: str
    state: Literal[
        "idle",
        "candidate_frozen",
        "committing",
        "retry_wait",
        "full_output_pending",
        "completed",
        "reconciliation_required",
    ] = "idle"
    commit_state: Literal[
        "open",
        "candidate_frozen",
        "committing",
        "confirmed",
        "commit_outcome_unknown",
        "ledger_latched",
    ] = "open"
    candidate_generation: int = 0
    terminal_candidates: tuple[AgentEvent, ...] = ()
    run_end_candidate: RunEndEvent | None = None
    publication_latched_termination: object | None = None
    publication_maintenance_lease: object | None = None
    publication_deadline_budget: object | None = None
    finalization_hook_done: bool = False
    long_horizon_child_drain_done: bool = False
    context_input_latch_after_terminalization: bool = False
    terminal_replan_count: int = 0
    mcp_closure_event_reference: object | None = None
    mcp_publication_closure_reason: str | None = None
    physical_task: asyncio.Task[object] | None = None
    output_materialization_task: asyncio.Task[None] | None = None
    materialization_owner: object | None = None
    materialization_attempt_generation: int = 0
    materialization_last_diagnostic_code: str | None = None
    terminal_receipt: object | None = None
    confirmed_run_end_event_reference: "ContextEventReferenceFact | None" = None
    state_carrier: "RunActivationStateCarrier | None" = field(
        default=None,
        repr=False,
    )
    state_owner_token: str | None = field(default=None, repr=False)


@dataclass
class RunReconciliationOwner:
    """One stable owner for a run-level UNKNOWN/authority repair.

    The event candidate remains with its domain owner.  This slot owns only the
    immutable pre-repair snapshot, physical confirmation generations, and the
    centrally produced resolution receipt.
    """

    snapshot: "RunReconciliationSnapshot"
    state: Literal[
        "installed",
        "confirming",
        "retry_wait",
        "resolved",
        "conflict",
        "unresolved",
    ] = "installed"
    physical_attempt_generation: int = 0
    confirmation_task: asyncio.Task[object] | None = None
    resolution_receipt: "ReconciliationResolutionReceipt | None" = None
    state_carrier: "RunActivationStateCarrier | None" = field(
        default=None,
        repr=False,
    )
    state_owner_token: str | None = field(default=None, repr=False)


@dataclass
class RunObserverRegistry:
    observers: dict[str, "StreamObserverHandle"] = field(default_factory=dict)
    next_cursor: int = 1


@dataclass(slots=True)
class StreamObserverHandle:
    observer_id: str
    queue: asyncio.Queue[Any]
    state: Literal["attached", "backpressured", "detached"]
    detached_reason: str | None
    detached: asyncio.Future[None]

    def detach(self, reason: str) -> None:
        if self.state == "detached":
            return
        self.state = "detached"
        self.detached_reason = reason
        if not self.detached.done():
            self.detached.set_result(None)


@dataclass(slots=True)
class RunActivationCoordinatorResult:
    segment_id: str
    segment_generation: int
    disposition: Literal[
        "waiting_user",
        "run_terminal",
        "terminalization_pending",
        "reconciliation_required",
    ]
    outcome: "RunActivationOutcome"


@dataclass(frozen=True, slots=True)
class ContinuationActivationCommitResult:
    authority_revision_fingerprint: str
    resource_disposition: Literal["reused_current", "swapped", "activation_blocked"]
    current_handle_id: str
    retiring_handle_id: str | None
    consumed_interaction_fingerprint: str
    termination_intent_id: str | None


@dataclass(frozen=True, slots=True)
class PendingInteractionResumeLink:
    previous_activation_identity: RunActivationIdentity
    pending_interaction_identity: "PendingInteractionIdentity"
    resume_boundary_event_reference: "ContextEventReferenceFact"
    installed_authority_revision_fingerprint: str


@dataclass
class RunOwner:
    identity: RunOwnerIdentity
    genesis: RunGenesisAuthority
    authority_head: RunAuthorityHead
    progress: RunProgressState
    lifecycle: RunLifecycle
    resource_slot: RunResourceSlot
    retiring_resources: RunRetiringResourceSet
    activation_slot: RunActivationSlot
    suspension_slot: RunSuspensionSlot
    finalization_slot: RunFinalizationSlot
    observer_registry: RunObserverRegistry
    activation_completion_history: dict[int, RunActivationCoordinatorResult]
    run_completion: asyncio.Future["RunTerminalOutcome"]
    entry: CommittedRunEntry
    termination_intent: RunTerminationIntent | None
    next_segment_generation: int
    latest_activation_owner_kind: Literal[
        "host_run_boundary", "host_resume_boundary", "subagent_run_start"
    ]
    latest_activation_owner_id: str
    interaction_resume_attempts: dict[str, int] = field(default_factory=dict)
    pending_interaction_resume_link: PendingInteractionResumeLink | None = None
    interaction_resume_links: dict[int, "InteractionResumeLinkReceipt"] = field(
        default_factory=dict
    )
    pending_activation_state: "RunActivationStateCarrier | None" = field(
        default=None,
        repr=False,
    )
    pending_activation_owner_token: str | None = field(default=None, repr=False)
    termination_revision: int = 0
    reconciliation_owner: RunReconciliationOwner | None = None
    reconciliation_resolution_history: dict[str, "ReconciliationResolutionReceipt"] = (
        field(default_factory=dict)
    )

    @property
    def execution_handles(self) -> RunExecutionHandleSet:
        slot = self.resource_slot
        if isinstance(slot, (BoundRunResources, RetiringRunResources)):
            return slot.handle_set
        raise RuntimeError("run owner has no bound execution handles")

    @execution_handles.setter
    def execution_handles(self, value: RunExecutionHandleSet) -> None:
        self.resource_slot = BoundRunResources(handle_set=value)

    @property
    def retiring_execution_handles(self) -> dict[str, RunExecutionHandleSet]:
        return self.retiring_resources.handles_by_id

    @property
    def active_segment(self) -> RunActivationCoordinator | None:
        slot = self.activation_slot
        return slot.coordinator if isinstance(slot, ActiveRunActivation) else None

    @active_segment.setter
    def active_segment(self, value: RunActivationCoordinator | None) -> None:
        self.activation_slot = (
            NoActiveActivation()
            if value is None
            else ActiveRunActivation(coordinator=value)
        )

    @property
    def finalization_owner(self) -> RunFinalizationOwner:
        finalization = self.finalization_slot.owner
        if not isinstance(finalization, RunFinalizationOwner):
            raise RuntimeError("run owner lost its finalization owner")
        return finalization


__all__ = [
    "ActiveRunActivation",
    "ActiveRunSuspension",
    "ApprovalSuspensionResources",
    "BoundRunResources",
    "ClosedBoundRunResources",
    "ClosedNeverBoundRunResources",
    "ContinuationActivationCommitResult",
    "McpSuspensionResources",
    "NoActiveActivation",
    "NoActiveSuspension",
    "PlanSuspensionResources",
    "RetiringRunResources",
    "RunActivationSlot",
    "RunActivationCoordinator",
    "RunActivationCoordinatorResult",
    "RunFinalizationSlot",
    "RunFinalizationOwner",
    "RunObserverRegistry",
    "RunOwner",
    "RunProgressState",
    "RunReconciliationOwner",
    "RunResourceSlot",
    "RunRetiringResourceSet",
    "RunSuspensionSlot",
    "RunSuspensionResources",
    "RunUsageAccumulator",
    "StreamObserverHandle",
    "UnboundRunResources",
]
