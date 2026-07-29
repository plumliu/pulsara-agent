"""Session-scoped owner for interaction resume transition attempts.

The Host routes public approval/plan/MCP resolutions through this service, but
does not own the durable continuation candidate or its physical write task.
The service freezes one attempt per exact pending-authority/resolution pair,
shields the physical transition from waiter cancellation, and retains the
closed outcome for duplicate confirmation.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from typing import Literal

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    McpInputRequiredResolutionSubmittedEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
    utc_now,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.runtime.run_execution.continuation import (
    CommittedInteractionResumeBoundary,
    PreparedInteractionResumeBoundary,
)
from pulsara_agent.ports.interaction_transition import (
    InteractionResumeOutcome,
    InteractionResumeRequest,
    InteractionSuspensionOutcome,
    InteractionSuspensionRequest,
    InteractionTransitionFull,
    InteractionTransitionNone,
    InteractionTransitionUntrusted,
)
from pulsara_agent.ports.run_authority import AwaitingInitialRevision
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.run_entry import HostRunBoundaryIdentityFact
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.run_execution.owner import ActiveRunSuspension
from pulsara_agent.runtime.run_execution.owner import RunSuspensionResources
from pulsara_agent.runtime.run_execution.interaction import (
    materialize_pending_interaction,
)
from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
from pulsara_agent.runtime.state import LoopStatus, RunActivationWorkingState
from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.runtime.run_entry import CapabilityResolveBasis
from pulsara_agent.runtime.permission_snapshot import RunPermissionSnapshot


InteractionKind = Literal["approval", "plan", "mcp_input_required"]
ResolutionKind = Literal["approval", "plan_question", "plan_exit", "mcp_input_required"]


class InteractionTransitionNotCommitted(RuntimeError):
    """The stable transition candidate was proven NONE for this attempt."""


class InteractionTransitionReconciliationRequired(RuntimeError):
    """The transition write could not be authoritatively classified."""


@dataclass(frozen=True, slots=True)
class PreparedInteractionResumeAttempt:
    request: InteractionResumeRequest
    boundary_identity: HostRunBoundaryIdentityFact
    interaction_kind: InteractionKind
    pending_public_view: object
    suspension_resources: RunSuspensionResources
    resolution: object


@dataclass(frozen=True, slots=True)
class InteractionTransitionCommitReceipt:
    outcome: InteractionResumeOutcome
    committed_boundary: CommittedInteractionResumeBoundary | None
    committed_events: tuple[AgentEvent, ...]


@dataclass(frozen=True, slots=True)
class SuspendedRunBoundaryView:
    """Typed, immutable boundary inputs borrowed from the suspension owner."""

    run_id: str
    turn_id: str
    reply_id: str
    original_run_start_event: RunStartEvent
    capability_resolve_basis: CapabilityResolveBasis
    source_exposure_plan: CapabilityExposurePlan
    source_exposure_fact: CapabilityExposureSnapshotFact
    source_exposure_event_reference: ContextEventReferenceFact
    suspended_state_token: str
    resident_model_target_fingerprint: str | None
    resident_permission_snapshot: RunPermissionSnapshot | None
    latest_mcp_resolution_reference: ContextEventReferenceFact | None
    latest_mcp_resume_failure_reference: ContextEventReferenceFact | None
    predecessor_authority_fingerprint: str
    expected_termination_revision: int
    current_execution_handle_id: str
    next_execution_handle_generation: int


@dataclass(slots=True)
class _ResidentAttempt:
    prepared: PreparedInteractionResumeAttempt
    task: asyncio.Task[InteractionTransitionCommitReceipt] | None = None
    receipt: InteractionTransitionCommitReceipt | None = None
    write_generation: int = 0


CommitResumeBoundary = Callable[
    [PreparedInteractionResumeAttempt],
    Awaitable[
        tuple[
            CommittedInteractionResumeBoundary,
            tuple[AgentEvent, ...],
        ]
    ],
]
WriteFailureClassifier = Callable[[BaseException], Literal["none", "unknown", "other"]]


class RuntimeInteractionTransitionService:
    """Only process-local owner of Host interaction transition attempts."""

    def __init__(
        self,
        *,
        registry: RunExecutionRegistry,
        event_log: EventLog,
        runtime_session_id: str,
        commit_resume_boundary: CommitResumeBoundary,
        classify_write_failure: WriteFailureClassifier,
        maximum_retained_receipts: int = 256,
    ) -> None:
        if maximum_retained_receipts < 1:
            raise ValueError("interaction receipt bound must be positive")
        self._registry = registry
        self._event_log = event_log
        self._runtime_session_id = runtime_session_id
        self._commit_resume_boundary = commit_resume_boundary
        self._classify_write_failure = classify_write_failure
        self._maximum_retained_receipts = maximum_retained_receipts
        self._attempts: OrderedDict[str, _ResidentAttempt] = OrderedDict()
        self._semantic_attempts: dict[tuple[str, str], str] = {}
        self._closing = False

    def install_suspension(
        self,
        *,
        run_id: str,
        host_session_id: str,
    ) -> InteractionTransitionFull:
        """Materialize and install the sole suspension slot from durable source."""

        if self._closing:
            raise RuntimeError("interaction transition service is closing")
        owner = self._registry.require(run_id)
        authority, resources = materialize_pending_interaction(
            owner=owner,
            host_session_id=host_session_id,
        )
        request_payload = {
            "owner_identity": owner.identity,
            "activation_identity": owner.active_segment.activation_identity
            if owner.active_segment is not None
            else None,
            "authority": authority,
            "expected_termination_revision": owner.termination_revision,
        }
        if request_payload["activation_identity"] is None:
            raise RuntimeError("suspension installation lacks active activation")
        stable_candidate_fingerprint = context_fingerprint(
            "interaction-suspension-candidate:v1", request_payload
        )
        request = InteractionSuspensionRequest(
            **request_payload,
            stable_candidate_fingerprint=stable_candidate_fingerprint,
        )
        self._registry.install_suspension(
            run_id,
            authority=authority,
            resources=resources,
        )
        if isinstance(owner.authority_head, AwaitingInitialRevision):
            raise RuntimeError("suspended run lacks installed authority")
        return InteractionTransitionFull(
            stable_candidate_id=(
                "interaction_suspension:"
                + authority.identity.interaction_fingerprint.removeprefix("sha256:")
            ),
            stable_candidate_fingerprint=request.stable_candidate_fingerprint,
            source_event_references=(
                authority.identity.source_interaction_event_reference,
            ),
            resulting_authority_fingerprint=(
                owner.authority_head.revision.authority_fingerprint
            ),
            resulting_activation_identity=request.activation_identity,
        )

    async def suspend(
        self,
        request: InteractionSuspensionRequest,
        *,
        deadline_monotonic: float,
    ) -> InteractionSuspensionOutcome:
        if asyncio.get_running_loop().time() >= deadline_monotonic:
            raise TimeoutError("interaction suspension deadline expired")
        owner = self._registry.require(request.owner_identity.run_id)
        slot = owner.suspension_slot
        if not isinstance(slot, ActiveRunSuspension):
            return InteractionTransitionNone(
                stable_candidate_id=(
                    "interaction_suspension:"
                    + request.authority.identity.interaction_fingerprint.removeprefix(
                        "sha256:"
                    )
                ),
                stable_candidate_fingerprint=request.stable_candidate_fingerprint,
            )
        if slot.authority != request.authority:
            return InteractionTransitionUntrusted(
                disposition="conflict",
                stable_candidate_id=(
                    "interaction_suspension:"
                    + request.authority.identity.interaction_fingerprint.removeprefix(
                        "sha256:"
                    )
                ),
                stable_candidate_fingerprint=request.stable_candidate_fingerprint,
            )
        if isinstance(owner.authority_head, AwaitingInitialRevision):
            raise RuntimeError("suspended run lacks installed authority")
        return InteractionTransitionFull(
            stable_candidate_id=(
                "interaction_suspension:"
                + request.authority.identity.interaction_fingerprint.removeprefix(
                    "sha256:"
                )
            ),
            stable_candidate_fingerprint=request.stable_candidate_fingerprint,
            source_event_references=(
                request.authority.identity.source_interaction_event_reference,
            ),
            resulting_authority_fingerprint=(
                owner.authority_head.revision.authority_fingerprint
            ),
            resulting_activation_identity=request.activation_identity,
        )

    def prepare_resume(
        self,
        *,
        run_id: str,
        interaction_id: str,
        interaction_kind: InteractionKind,
        resolution_kind: ResolutionKind,
        resolution: object,
    ) -> PreparedInteractionResumeAttempt:
        if self._closing:
            raise RuntimeError("interaction transition service is closing")
        owner = self._registry.require(run_id)
        suspension = owner.suspension_slot
        if owner.lifecycle != "suspended" or not isinstance(
            suspension, ActiveRunSuspension
        ):
            raise ValueError("run has no active suspension authority")
        identity = suspension.authority.identity
        if identity.interaction_id != interaction_id:
            raise ValueError("interaction id does not match suspension authority")
        if not _resolution_kind_matches_authority(
            resolution_kind=resolution_kind,
            authority_kind=identity.interaction_kind,
        ):
            raise ValueError("interaction resolution kind does not match authority")
        if isinstance(owner.authority_head, AwaitingInitialRevision):
            raise RuntimeError("interaction resume lacks installed run authority")

        resolution_copy = deepcopy(resolution)
        resolution_fingerprint = _resolution_fingerprint(
            resolution_kind=resolution_kind,
            resolution=resolution_copy,
        )
        semantic_key = (identity.interaction_fingerprint, resolution_fingerprint)
        existing_id = self._semantic_attempts.get(semantic_key)
        if existing_id is not None:
            return self._attempts[existing_id].prepared

        attempt_number = (
            int(owner.interaction_resume_attempts.get(interaction_id, 0)) + 1
        )
        owner.interaction_resume_attempts[interaction_id] = attempt_number
        stable_candidate_id = "interaction_transition:" + context_fingerprint(
            "interaction-resume-candidate-id:v1",
            {
                "owner_fingerprint": owner.identity.owner_fingerprint,
                "pending_interaction_fingerprint": identity.interaction_fingerprint,
                "resolution_fingerprint": resolution_fingerprint,
                "attempt_number": attempt_number,
            },
        ).removeprefix("sha256:")
        transition_attempt_id = stable_candidate_id
        boundary_identity = HostRunBoundaryIdentityFact(
            boundary_id=f"run_boundary:resume:{stable_candidate_id.rsplit(':', 1)[-1]}",
            kind="pre_interaction_resume",
            runtime_session_id=self._runtime_session_id,
            run_id=owner.identity.run_id,
            turn_id=_required_suspension_state_identity(
                suspension.resources, "turn_id"
            ),
            reply_id=_required_suspension_state_identity(
                suspension.resources, "reply_id"
            ),
            attempt_number=attempt_number,
            observed_at_utc=utc_now(),
        )
        payload = {
            "schema_version": 1,
            "transition_attempt_id": transition_attempt_id,
            "owner_identity": owner.identity,
            "pending_interaction_identity": identity,
            "resolution_kind": resolution_kind,
            "resolution_fingerprint": resolution_fingerprint,
            "expected_authority_head_fingerprint": (
                owner.authority_head.revision.authority_fingerprint
            ),
            "expected_termination_revision": owner.termination_revision,
            "stable_candidate_id": stable_candidate_id,
        }
        request = InteractionResumeRequest(
            **payload,
            stable_candidate_fingerprint=context_fingerprint(
                "interaction-resume-request:v1", payload
            ),
        )
        prepared = PreparedInteractionResumeAttempt(
            request=request,
            boundary_identity=boundary_identity,
            interaction_kind=interaction_kind,
            pending_public_view=suspension.resources.public_view,
            suspension_resources=suspension.resources,
            resolution=resolution_copy,
        )
        self._attempts[transition_attempt_id] = _ResidentAttempt(prepared=prepared)
        self._semantic_attempts[semantic_key] = transition_attempt_id
        self._trim_completed_receipts()
        return prepared

    def suspended_boundary_view(
        self,
        prepared: PreparedInteractionResumeAttempt,
    ) -> SuspendedRunBoundaryView:
        self._require_resident(prepared.request)
        owner = self._registry.require(prepared.request.owner_identity.run_id)
        slot = owner.suspension_slot
        if owner.lifecycle != "suspended" or not isinstance(slot, ActiveRunSuspension):
            raise RuntimeError("resume boundary requires an active suspension owner")
        resources = slot.resources
        state = resources.state_carrier.borrow(owner_token=resources.state_owner_token)
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("suspended run lost its committed working set")
        started = self._event_log.get_by_id(working_set.run_start_event_id)
        if (
            not isinstance(started, RunStartEvent)
            or started.sequence is None
            or started.run_id != owner.identity.run_id
            or started.run_entry_kind.value != "host"
        ):
            raise RuntimeError("suspended run lacks its exact Host RunStart")
        source_plan = working_set.effective_exposure_plan
        source_fact = working_set.effective_exposure_fact
        source_reference = working_set.effective_exposure_event_ref
        if (
            not isinstance(source_plan, CapabilityExposurePlan)
            or not isinstance(source_fact, CapabilityExposureSnapshotFact)
            or source_reference is None
        ):
            raise RuntimeError("suspended run lost its effective exposure authority")
        if not isinstance(working_set.capability_resolve_basis, CapabilityResolveBasis):
            raise RuntimeError("suspended run lost its capability basis")
        if isinstance(owner.authority_head, AwaitingInitialRevision):
            raise RuntimeError("resume boundary lacks installed authority")
        token = slot.authority.identity.interaction_fingerprint
        return SuspendedRunBoundaryView(
            run_id=owner.identity.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
            original_run_start_event=started,
            capability_resolve_basis=working_set.capability_resolve_basis,
            source_exposure_plan=source_plan,
            source_exposure_fact=source_fact,
            source_exposure_event_reference=source_reference,
            suspended_state_token=token,
            resident_model_target_fingerprint=(
                state.run_model_target.fact.target_fingerprint
                if state.run_model_target is not None
                else None
            ),
            resident_permission_snapshot=state.permission_snapshot,
            latest_mcp_resolution_reference=(
                working_set.latest_mcp_input_required_resolution_ref
            ),
            latest_mcp_resume_failure_reference=(
                working_set.latest_mcp_resume_failure_event_ref
            ),
            predecessor_authority_fingerprint=(
                owner.authority_head.revision.authority_fingerprint
            ),
            expected_termination_revision=owner.termination_revision,
            current_execution_handle_id=owner.execution_handles.handle_id,
            next_execution_handle_generation=(
                owner.execution_handles.handle_generation + 1
            ),
        )

    def fold_committed_resume_boundary(
        self,
        *,
        prepared_attempt: PreparedInteractionResumeAttempt,
        prepared_boundary: PreparedInteractionResumeBoundary,
        stored: tuple[AgentEvent, ...],
        publication_status: Literal["completed", "failed_after_commit", "unavailable"],
    ) -> CommittedInteractionResumeBoundary:
        self._require_resident(prepared_attempt.request)
        exposure_event = next(
            (
                event
                for event in stored
                if isinstance(event, CapabilityExposureResolvedEvent)
                and event.exposure.exposure_id
                == prepared_boundary.continuation_exposure_fact.exposure_id
            ),
            None,
        )
        boundary_event = next(
            (
                event
                for event in stored
                if isinstance(event, RunInteractionResumeBoundaryEvent)
                and event.boundary.identity.boundary_id
                == prepared_boundary.identity.boundary_id
            ),
            None,
        )
        resolution_event = next(
            (
                event
                for event in stored
                if isinstance(event, McpInputRequiredResolutionSubmittedEvent)
                and event.id == prepared_boundary.mcp_input_required_resolution_event_id
            ),
            None,
        )
        if (
            exposure_event is None
            or exposure_event.sequence is None
            or boundary_event is None
            or boundary_event.sequence is None
            or (
                prepared_boundary.mcp_input_required_resolution_event_id is not None
                and (resolution_event is None or resolution_event.sequence is None)
            )
        ):
            raise RuntimeError("resume boundary batch was not fully committed")
        through_sequence = stored[-1].sequence if stored else None
        if through_sequence is None:
            raise RuntimeError("resume boundary batch ended unsequenced")
        resolution_reference = (
            event_reference_from_stored(
                resolution_event,
                runtime_session_id=self._runtime_session_id,
            )
            if resolution_event is not None
            else None
        )
        owner = self._registry.require(prepared_attempt.request.owner_identity.run_id)
        current_handles = owner.execution_handles
        incoming = prepared_boundary.incoming_execution_handles
        if current_handles.handle_id != prepared_boundary.expected_current_handle_id:
            raise RuntimeError("continuation execution handle authority became stale")
        if (
            incoming.frozen_execution_surface
            is not prepared_boundary.frozen_execution_surface
        ):
            raise RuntimeError("continuation execution surface drifted after freeze")
        reuse_current = (
            current_handles.mcp_installation is incoming.mcp_installation
            and current_handles.capability_runtime is incoming.capability_runtime
            and current_handles.tool_registry is incoming.tool_registry
            and current_handles.frozen_execution_surface.identity
            == incoming.frozen_execution_surface.identity
        )
        suspension = owner.suspension_slot
        if not isinstance(suspension, ActiveRunSuspension):
            raise RuntimeError("committed continuation lost suspension authority")
        installation = self._registry.commit_continuation_activation_full(
            run_id=owner.identity.run_id,
            stored_boundary=boundary_event,
            stored_exposure=exposure_event,
            effective_model_target=prepared_boundary.rebound_model_target.fact,
            effective_permission=(
                prepared_boundary.permission_snapshot.to_context_fact()
            ),
            expected_predecessor_fingerprint=(
                prepared_boundary.predecessor_authority_fingerprint
            ),
            expected_termination_revision=(
                prepared_boundary.expected_termination_revision
            ),
            expected_current_handle_id=current_handles.handle_id,
            incoming=incoming,
            reuse_current_handles=reuse_current,
            expected_interaction_fingerprint=(
                suspension.authority.identity.interaction_fingerprint
            ),
        )
        state = self._pending_state_after_continuation(owner.identity.run_id)
        activation_blocked = installation.resource_disposition == "activation_blocked"
        state.execution_resources.resume_activation_blocked = activation_blocked
        if installation.resource_disposition == "swapped":
            state.execution_resources.run_execution_handle_id = (
                installation.current_handle_id
            )
            state.execution_resources.capability_execution_borrow_authority = (
                incoming.borrow_authority
            )
        if not activation_blocked:
            state.run_model_target = prepared_boundary.rebound_model_target
            state.permission_snapshot = prepared_boundary.permission_snapshot
            working_set = state.run_working_set
            if working_set is None:
                raise RuntimeError("committed continuation lost RunWorkingSet")
            working_set.install_continuation(
                run_model_target=prepared_boundary.rebound_model_target,
                permission_snapshot=prepared_boundary.permission_snapshot,
                plan=prepared_boundary.owned_continuation_exposure_plan,
                fact=prepared_boundary.continuation_exposure_fact,
                event_ref=event_reference_from_stored(
                    exposure_event,
                    runtime_session_id=self._runtime_session_id,
                ),
                boundary=boundary_event.boundary,
                boundary_ref=event_reference_from_stored(
                    boundary_event,
                    runtime_session_id=self._runtime_session_id,
                ),
                frozen_execution_surface=prepared_boundary.frozen_execution_surface,
                validated_suspended_state_token_fingerprint=(
                    boundary_event.boundary.suspended_state_token_fingerprint
                ),
                mcp_input_required_resolution_ref=resolution_reference,
            )
            if resolution_reference is not None:
                state.execution_resources.latest_mcp_input_required_resolution_reference = resolution_reference
        return CommittedInteractionResumeBoundary(
            prepared=prepared_boundary,
            exposure_event_id=exposure_event.id,
            exposure_event_sequence=exposure_event.sequence,
            boundary_event_id=boundary_event.id,
            boundary_event_sequence=boundary_event.sequence,
            committed_audit_event_ids=tuple(
                event.id
                for event in stored
                if event.id
                in {audit.id for audit in prepared_boundary.pending_mcp_audits}
            ),
            committed_through_sequence=through_sequence,
            publication_status=publication_status,
            mcp_input_required_resolution_event_reference=resolution_reference,
        )

    def _pending_state_after_continuation(
        self, run_id: str
    ) -> RunActivationWorkingState:
        owner = self._registry.require(run_id)
        carrier = owner.pending_activation_state
        token = owner.pending_activation_owner_token
        if carrier is None or token is None:
            raise RuntimeError("continuation lost its pending activation-state owner")
        return carrier.borrow(owner_token=token)

    async def resume(
        self,
        request: InteractionResumeRequest,
        *,
        deadline_monotonic: float,
    ) -> InteractionResumeOutcome:
        receipt = await self.commit_resume(
            request,
            deadline_monotonic=deadline_monotonic,
        )
        return receipt.outcome

    async def commit_resume(
        self,
        request: InteractionResumeRequest,
        *,
        deadline_monotonic: float,
    ) -> InteractionTransitionCommitReceipt:
        if asyncio.get_running_loop().time() >= deadline_monotonic:
            raise TimeoutError("interaction transition deadline expired")
        resident = self._require_resident(request)
        if resident.receipt is not None:
            if not isinstance(
                resident.receipt.outcome,
                InteractionTransitionNone,
            ):
                return resident.receipt
            # NONE proves the exact candidate did not commit. Keep its frozen
            # semantic identity, but let the next waiter own a new physical
            # write generation and deadline.
            resident.receipt = None
            resident.task = None
        if resident.task is None:
            resident.write_generation += 1
            resident.task = asyncio.create_task(
                self._drive(resident),
                name=(
                    "pulsara-interaction-transition:"
                    f"{request.transition_attempt_id}:g{resident.write_generation}"
                ),
            )
        try:
            return await asyncio.shield(resident.task)
        except asyncio.CancelledError:
            # Waiter cancellation is detach-only. The resident task keeps exact
            # candidate ownership and can be joined by another waiter or close.
            raise

    def working_state_for(self, prepared: PreparedInteractionResumeAttempt) -> object:
        self._require_resident(prepared.request)
        owner = self._registry.require(prepared.request.owner_identity.run_id)
        if owner.lifecycle == "suspended":
            resources = prepared.suspension_resources
            return resources.state_carrier.borrow(
                owner_token=resources.state_owner_token
            )
        carrier = owner.pending_activation_state
        token = owner.pending_activation_owner_token
        if carrier is None or token is None:
            raise RuntimeError("interaction transition lost resident activation state")
        return carrier.borrow(owner_token=token)

    def hydrate_resume_working_state(
        self,
        prepared: PreparedInteractionResumeAttempt,
    ) -> RunActivationWorkingState:
        """Borrow slot-owned interaction data into the new activation only."""

        self._require_resident(prepared.request)
        owner = self._registry.require(prepared.request.owner_identity.run_id)
        if owner.lifecycle != "initializing":
            raise RuntimeError("resume working state requires committed continuation")
        carrier = owner.pending_activation_state
        token = owner.pending_activation_owner_token
        if carrier is None or token is None:
            raise RuntimeError("interaction transition lost pending activation state")
        state = carrier.borrow(owner_token=token)
        hydrated = self._hydrate_working_state(
            state=state,
            resources=prepared.suspension_resources,
            public=prepared.pending_public_view,
        )
        hydrated.pending_interaction_source_event_reference = prepared.request.pending_interaction_identity.source_interaction_event_reference
        return hydrated

    def prepare_resume_activation(
        self,
        prepared: PreparedInteractionResumeAttempt,
    ) -> None:
        self.hydrate_resume_working_state(prepared)

    def hydrate_suspended_working_state(
        self,
        *,
        run_id: str,
    ) -> RunActivationWorkingState:
        owner = self._registry.require(run_id)
        slot = owner.suspension_slot
        if owner.lifecycle != "suspended" or not isinstance(slot, ActiveRunSuspension):
            raise RuntimeError("run has no active suspension to borrow")
        state = slot.resources.state_carrier.borrow(
            owner_token=slot.resources.state_owner_token
        )
        hydrated = self._hydrate_working_state(
            state=state,
            resources=slot.resources,
            public=slot.resources.public_view,
        )
        hydrated.pending_interaction_source_event_reference = (
            slot.authority.identity.source_interaction_event_reference
        )
        return hydrated

    @staticmethod
    def _hydrate_working_state(
        *,
        state: RunActivationWorkingState,
        resources: RunSuspensionResources,
        public: object,
    ) -> RunActivationWorkingState:
        state.status = LoopStatus.WAITING_USER
        if resources.resource_kind == "approval":
            tool_calls = getattr(public, "tool_calls", None)
            if not isinstance(tool_calls, tuple):
                raise RuntimeError("approval suspension lost pending tool calls")
            state.pending_tool_calls = [
                call.model_copy(deep=True) for call in tool_calls
            ]
            state.pending_interaction_kind = None
            state.pending_interaction_payload = {}
        elif resources.resource_kind in {"plan_question", "plan_exit"}:
            state.pending_tool_calls = []
            state.pending_interaction_kind = "plan"
            state.pending_interaction_payload = dict(resources.activation_payload)
        else:
            payload = getattr(resources, "activation_payload", None)
            if payload is None:
                raise RuntimeError("MCP suspension lost its activation payload")
            state.pending_tool_calls = []
            state.pending_interaction_kind = "mcp_input_required"
            state.pending_interaction_payload = {
                **dict(payload),
                "mcp_pending_handle": resources.pending_handle,
            }
        return state

    async def aclose(self, *, deadline_monotonic: float) -> None:
        self._closing = True
        tasks = tuple(
            resident.task
            for resident in self._attempts.values()
            if resident.task is not None and not resident.task.done()
        )
        if not tasks:
            return
        remaining = deadline_monotonic - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("interaction transition close deadline expired")
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in done:
            task.result()
        if pending:
            raise TimeoutError("interaction transition physical owner did not drain")

    async def _drive(
        self, resident: _ResidentAttempt
    ) -> InteractionTransitionCommitReceipt:
        prepared = resident.prepared
        request = prepared.request
        try:
            committed, stored = await self._commit_resume_boundary(prepared)
        except BaseException as exc:
            classification = self._classify_write_failure(exc)
            if classification == "none":
                outcome: InteractionResumeOutcome = InteractionTransitionNone(
                    stable_candidate_id=request.stable_candidate_id,
                    stable_candidate_fingerprint=request.stable_candidate_fingerprint,
                )
                receipt = InteractionTransitionCommitReceipt(
                    outcome=outcome,
                    committed_boundary=None,
                    committed_events=(),
                )
                resident.receipt = receipt
                return receipt
            if classification == "unknown":
                owner = self._registry.require(request.owner_identity.run_id)
                owner.lifecycle = "reconciliation_required"
                outcome = InteractionTransitionUntrusted(
                    disposition="unknown",
                    stable_candidate_id=request.stable_candidate_id,
                    stable_candidate_fingerprint=request.stable_candidate_fingerprint,
                )
                receipt = InteractionTransitionCommitReceipt(
                    outcome=outcome,
                    committed_boundary=None,
                    committed_events=(),
                )
                resident.receipt = receipt
                return receipt
            raise

        owner = self._registry.require(request.owner_identity.run_id)
        if owner.identity != request.owner_identity:
            raise RuntimeError("interaction transition owner changed after commit")
        if isinstance(owner.authority_head, AwaitingInitialRevision):
            raise RuntimeError("interaction transition did not install authority")
        if (
            owner.authority_head.revision.authority_fingerprint
            == request.expected_authority_head_fingerprint
        ):
            raise RuntimeError("interaction transition did not advance authority")
        references = tuple(
            event_reference_from_stored(
                event,
                runtime_session_id=self._runtime_session_id,
            )
            for event in stored
        )
        outcome = InteractionTransitionFull(
            stable_candidate_id=request.stable_candidate_id,
            stable_candidate_fingerprint=request.stable_candidate_fingerprint,
            source_event_references=references,
            resulting_authority_fingerprint=(
                owner.authority_head.revision.authority_fingerprint
            ),
            resulting_activation_identity=None,
        )
        receipt = InteractionTransitionCommitReceipt(
            outcome=outcome,
            committed_boundary=committed,
            committed_events=stored,
        )
        resident.receipt = receipt
        return receipt

    def _require_resident(self, request: InteractionResumeRequest) -> _ResidentAttempt:
        resident = self._attempts.get(request.transition_attempt_id)
        if resident is None or resident.prepared.request != request:
            raise ValueError("interaction transition attempt identity mismatch")
        return resident

    def _trim_completed_receipts(self) -> None:
        while len(self._attempts) > self._maximum_retained_receipts:
            attempt_id, resident = next(iter(self._attempts.items()))
            if resident.receipt is None or (
                resident.task is not None and not resident.task.done()
            ):
                return
            self._attempts.pop(attempt_id)
            key = (
                resident.prepared.request.pending_interaction_identity.interaction_fingerprint,
                resident.prepared.request.resolution_fingerprint,
            )
            self._semantic_attempts.pop(key, None)


def _resolution_kind_matches_authority(
    *, resolution_kind: ResolutionKind, authority_kind: str
) -> bool:
    if resolution_kind == "approval":
        return authority_kind == "approval"
    if resolution_kind == "mcp_input_required":
        return authority_kind == "mcp_input_required"
    return resolution_kind == authority_kind


def _resolution_fingerprint(*, resolution_kind: str, resolution: object) -> str:
    if is_dataclass(resolution):
        payload = asdict(resolution)
    elif hasattr(resolution, "model_dump"):
        payload = resolution.model_dump(mode="json")
    else:
        payload = resolution
    frozen = freeze_json(payload)
    canonical = (
        frozen.model_dump(mode="json") if hasattr(frozen, "model_dump") else frozen
    )
    return context_fingerprint(
        "interaction-public-resolution:v1",
        {
            "resolution_kind": resolution_kind,
            "resolution": canonical,
        },
    )


def _required_suspension_state_identity(resources, field_name: str) -> str:
    state = resources.state_carrier.borrow(owner_token=resources.state_owner_token)
    value = getattr(state, field_name, None)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"interaction transition lost working-state {field_name}")
    return value


__all__ = [
    "InteractionTransitionCommitReceipt",
    "InteractionTransitionNotCommitted",
    "InteractionTransitionReconciliationRequired",
    "PreparedInteractionResumeAttempt",
    "RuntimeInteractionTransitionService",
]
