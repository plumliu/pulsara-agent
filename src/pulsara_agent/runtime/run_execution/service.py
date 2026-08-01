"""Session-scoped activation driver shared by Host and child runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pulsara_agent.event import AgentEvent, RunEndEvent
from pulsara_agent.runtime.run_execution.handle import (
    RegistryRunHandle,
    RegistryRunObserver,
    close_observers,
    publish_observer_event,
)
from pulsara_agent.runtime.run_execution.owner import (
    ActiveRunSuspension,
    BoundRunResources,
    NoActiveSuspension,
    RunActivationCoordinator,
)
from pulsara_agent.ports.run_execution import (
    RunActivationOutcome,
    RunSegmentInstallBlocked,
    RunTerminalizationPending,
    RunTerminationIntent,
)
from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
from pulsara_agent.runtime.state import LoopStatus, RunActivationWorkingState
from pulsara_agent.llm.control import RunModelCallControlOwner
from pulsara_agent.runtime.run_execution.prepared import PreparedRunActivationOwner
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.runtime.run_execution.reconciliation import (
    RunReconciliationService,
)

if TYPE_CHECKING:
    from pulsara_agent.event_log import EventLog
    from pulsara_agent.runtime.agent import AgentRuntime
    from pulsara_agent.runtime.run_execution.interaction_transition import (
        RuntimeInteractionTransitionService,
    )
    from pulsara_agent.runtime.run_entry import AgentRunDraft, CommittedRunEntry


ActivationSettledCallback = Callable[[RunActivationOutcome], Awaitable[None] | None]
StateSettledCallback = Callable[[RunActivationWorkingState], None]


@dataclass(frozen=True, slots=True)
class RunActivationDispatch:
    run_handle: RegistryRunHandle
    activation_generation: int
    observer: RegistryRunObserver | None

    async def wait_activation(self) -> RunActivationOutcome:
        return await self.run_handle.wait_activation(self.activation_generation)


@dataclass(frozen=True, slots=True)
class ActiveRunSafePointState:
    run_id: str
    segment_id: str
    segment_generation: int
    has_pending_interaction: bool
    has_pending_tool_calls: bool
    latest_model_control_disposition_event_id: str | None
    latest_model_control_disposition_model_call_index: int | None
    run_start_event_id: str
    terminal_open: bool
    termination_intent_absent: bool


@dataclass(frozen=True, slots=True)
class HostRunControlView:
    """Closed read-only projection of the current Host run owner.

    The Host never receives the mutable owner, state carrier, driver task, or
    registry.  This view contains only admission, lifecycle, and Inspector
    fields that the Host itself must coordinate.
    """

    run_id: str
    run_start_event_id: str
    turn_id: str
    reply_id: str
    lifecycle: str
    terminal_state: str
    finalized: bool
    active_segment_id: str | None
    active_segment_generation: int | None
    active_segment_state: str | None
    active_segment_owner_kind: str | None
    active_segment_owner_id: str | None
    active_driver_running: bool
    termination_intent: RunTerminationIntent | None
    pending_interaction_view: object | None
    pending_mcp_binding_identity: object | None
    run_completion_done: bool
    run_completion_failed: bool
    terminal_event_id: str
    terminal_event_sequence: int | None
    terminal_candidate_id: str | None
    current_execution_handle_id: str | None
    retiring_execution_handle_count: int
    genesis_long_horizon: object
    genesis_model_target: object
    effective_model_target: object
    initial_boundary_identity: object | None
    latest_resume_boundary: object | None


class RunReconciliationCloseBlocked(RuntimeError):
    def __init__(self, run_ids: tuple[str, ...]) -> None:
        self.run_ids = run_ids
        super().__init__(
            "run reconciliation remains unresolved at close: " + ", ".join(run_ids)
        )


class RunActivationService:
    """The sole task/observer owner for initial and resumed activations."""

    def __init__(
        self,
        *,
        registry: RunExecutionRegistry,
        event_log: "EventLog",
        agent_runtime: "AgentRuntime",
        runtime_session_id: str,
    ) -> None:
        self._registry = registry
        self._event_log = event_log
        self._agent_runtime = agent_runtime
        self._runtime_session_id = runtime_session_id
        self._reconciliation_service = RunReconciliationService(
            registry=registry,
            event_log=event_log,
        )
        agent_runtime.bind_run_reconciliation_service(self._reconciliation_service)
        self._interaction_transition_port: (
            RuntimeInteractionTransitionService | None
        ) = None

    def current_host_run_view(self) -> HostRunControlView | None:
        owner = self._registry.current_host_owner()
        return self._host_view(owner) if owner is not None else None

    def active_host_run_view(self) -> HostRunControlView | None:
        owner = self._registry.active_host_owner()
        return self._host_view(owner) if owner is not None else None

    def suspended_host_run_view(self) -> HostRunControlView | None:
        owner = self._registry.suspended_host_owner()
        return self._host_view(owner) if owner is not None else None

    def stopping_host_run_view(self) -> HostRunControlView | None:
        owner = self._registry.stopping_host_owner()
        return self._host_view(owner) if owner is not None else None

    def run_view(self, run_id: str) -> HostRunControlView | None:
        owner = self._registry.get(run_id)
        if owner is None or owner.genesis.entry.entry_kind != "host":
            return None
        return self._host_view(owner)

    def has_run_owner(self, run_id: str) -> bool:
        return self._registry.get(run_id) is not None

    def resident_owner_count(self) -> int:
        return self._registry.owner_count

    def reserve_prepared_owner(self, **kwargs) -> None:
        self._registry.reserve_prepared(**kwargs)

    def release_prepared_owner(self, key, *, outcome: str) -> None:
        self._registry.release_prepared(key, outcome=outcome)

    def promote_committed_owner(self, **kwargs):
        committed = kwargs["committed"]
        envelopes = self._event_log.read_raw_events_by_id(
            (committed.run_start_event.id,)
        )
        if len(envelopes) != 1:
            raise RuntimeError("committed RunStart envelope is unavailable")
        kwargs["run_start_envelope"] = envelopes[0]
        owner = self._registry.promote_committed_entry(**kwargs)
        return owner.execution_handles

    async def wait_run_completion(self, run_id: str):
        owner = self._registry.require(run_id)
        return await asyncio.shield(owner.run_completion)

    def capture_run_completion(self, run_id: str) -> asyncio.Future:
        """Retain the exact completion before an operation may retire its owner."""

        return self._registry.require(run_id).run_completion

    async def wait_active_driver(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        owner = self._registry.require(run_id)
        segment = owner.active_segment
        task = segment.driver_task if segment is not None else None
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)

    async def wait_until_retired(self, run_id: str, *, timeout_seconds: float) -> None:
        await self._registry.wait_until_retired(
            run_id,
            timeout_seconds=timeout_seconds,
        )

    def retire_confirmed(self, run_id: str) -> bool:
        owner = self._registry.get(run_id)
        return owner is not None and self._registry.retire_confirmed(run_id)

    def install_termination_intent(
        self,
        run_id: str,
        intent: RunTerminationIntent,
    ):
        return self._registry.install_termination_intent(run_id, intent)

    def build_interaction_transition_service(
        self,
        *,
        runtime_session_id: str,
        commit_resume_boundary,
        classify_write_failure,
    ):
        from pulsara_agent.runtime.run_execution.interaction_transition import (
            RuntimeInteractionTransitionService,
        )

        port = RuntimeInteractionTransitionService(
            registry=self._registry,
            event_log=self._event_log,
            runtime_session_id=runtime_session_id,
            commit_resume_boundary=commit_resume_boundary,
            classify_write_failure=classify_write_failure,
        )
        self.bind_interaction_transition_port(port)
        return port

    @staticmethod
    def _host_view(owner) -> HostRunControlView:
        segment = owner.active_segment
        task = segment.driver_task if segment is not None else None
        suspension = owner.suspension_slot
        pending_view = None
        pending_binding = None
        if isinstance(suspension, ActiveRunSuspension):
            pending_view = suspension.resources.public_view
            source = getattr(suspension.authority, "suspension", None)
            pending_binding = getattr(source, "binding_identity", None)
        revision = getattr(owner.authority_head, "revision", None)
        terminal_result = (
            owner.run_completion.result()
            if owner.run_completion.done() and not owner.run_completion.cancelled()
            else None
        )
        failed = bool(
            terminal_result is not None
            and getattr(getattr(terminal_result, "output", None), "status", None)
            == "failed"
        )
        resources = owner.resource_slot
        current_handle_id = (
            resources.handle_set.handle_id
            if isinstance(resources, BoundRunResources)
            else None
        )
        return HostRunControlView(
            run_id=owner.identity.run_id,
            run_start_event_id=owner.identity.run_start_event_id,
            turn_id=owner.entry.run_start_event.turn_id,
            reply_id=owner.entry.run_start_event.reply_id,
            lifecycle=owner.lifecycle,
            terminal_state=owner.finalization_owner.commit_state,
            finalized=owner.finalization_slot.state == "completed",
            active_segment_id=(segment.segment_id if segment is not None else None),
            active_segment_generation=(
                segment.segment_generation if segment is not None else None
            ),
            active_segment_state=(
                segment.segment_state if segment is not None else None
            ),
            active_segment_owner_kind=(
                segment.activation_owner_kind if segment is not None else None
            ),
            active_segment_owner_id=(
                segment.activation_owner_id if segment is not None else None
            ),
            active_driver_running=bool(task is not None and not task.done()),
            termination_intent=owner.termination_intent,
            pending_interaction_view=pending_view,
            pending_mcp_binding_identity=pending_binding,
            run_completion_done=owner.run_completion.done(),
            run_completion_failed=failed,
            terminal_event_id=owner.finalization_owner.terminal_event_id,
            terminal_event_sequence=(
                owner.finalization_owner.confirmed_run_end_event_reference.sequence
                if owner.finalization_owner.confirmed_run_end_event_reference
                is not None
                else None
            ),
            terminal_candidate_id=(
                owner.finalization_owner.run_end_candidate.id
                if owner.finalization_owner.run_end_candidate is not None
                else None
            ),
            current_execution_handle_id=current_handle_id,
            retiring_execution_handle_count=len(owner.retiring_execution_handles),
            genesis_long_horizon=owner.genesis.long_horizon,
            genesis_model_target=owner.genesis.run_model_target,
            effective_model_target=getattr(
                revision,
                "effective_model_target",
                owner.genesis.run_model_target,
            ),
            initial_boundary_identity=getattr(
                getattr(owner.entry.run_start_event, "new_run_boundary", None),
                "identity",
                None,
            ),
            latest_resume_boundary=getattr(revision, "source_resume_boundary", None),
        )

    def prepare_boundary_activation(
        self,
        *,
        identity,
        owner_task: asyncio.Task[object],
        generation: int = 1,
    ) -> PreparedRunActivationOwner:
        state = self._agent_runtime.new_state(
            session_id=self._runtime_session_id,
            run_id=identity.run_id,
            turn_id=identity.turn_id,
            reply_id=identity.reply_id,
        )
        return PreparedRunActivationOwner(
            run_id=identity.run_id,
            boundary_id=identity.boundary_id,
            owner_task=owner_task,
            generation=generation,
            _working_state=state,
        )

    async def prepare_run_draft(
        self,
        prepared_activation: PreparedRunActivationOwner,
        **kwargs,
    ):

        state = prepared_activation.borrow_for_boundary()
        permission_snapshot = kwargs.get("permission_snapshot")
        if state.permission_snapshot is None:
            state.permission_snapshot = permission_snapshot
        elif state.permission_snapshot != permission_snapshot:
            raise RuntimeError("prepared run permission snapshot changed")
        return await self._agent_runtime.prepare_run_draft(
            state,
            **kwargs,
        )

    def initialize_committed_state(
        self,
        *,
        prepared_activation: PreparedRunActivationOwner,
        committed,
        plan_snapshot,
        capability_resolve_basis,
        frozen_execution_surface,
    ) -> None:
        from pulsara_agent.runtime.run_entry import install_run_working_set

        install_run_working_set(
            prepared_activation.borrow_for_boundary(),
            committed,
            plan_snapshot=plan_snapshot,
            capability_resolve_basis=capability_resolve_basis,
            frozen_execution_surface=frozen_execution_surface,
        )

    def configure_pending_host_plan(
        self,
        *,
        run_id: str,
        host_session_id: str,
        workflow_state: object,
        pending_entry_audit: bool,
        previous_permission_mode: object | None,
        previous_permission_policy: dict[str, object],
        entry_reason: str,
    ) -> None:
        """Install Host plan inputs without exposing the working cache to Host."""

        from pulsara_agent.runtime.state import PlanEntryAuditState

        state = self._pending_working_state(run_id)
        state.execution_resources.host_session_id = host_session_id
        state.plan_progress.workflow_state = workflow_state
        if not pending_entry_audit:
            return
        if previous_permission_mode is None:
            raise RuntimeError("pending plan entry lacks previous permission mode")
        state.plan_progress.entry_audit = PlanEntryAuditState(
            source="user",
            previous_permission_mode=previous_permission_mode,
            previous_permission_policy=dict(previous_permission_policy),
            reason=entry_reason,
            event_id=f"plan_mode_entered:{run_id}",
        )

    def prepare_pending_host_activation(
        self,
        *,
        run_id: str,
        committed,
        host_session_id: str,
        workflow_state: object,
        pending_entry_audit: bool,
        previous_permission_mode: object | None,
        previous_permission_policy: dict[str, object],
        entry_reason: str,
    ) -> None:
        state = self._pending_working_state(run_id)
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("activation requires a committed RunWorkingSet")
        if (
            working_set.run_start_event_id != committed.run_start_event.id
            or working_set.run_start_sequence != committed.run_start_sequence
        ):
            raise RuntimeError("activation run-entry identity mismatch")
        self.configure_pending_host_plan(
            run_id=run_id,
            host_session_id=host_session_id,
            workflow_state=workflow_state,
            pending_entry_audit=pending_entry_audit,
            previous_permission_mode=previous_permission_mode,
            previous_permission_policy=previous_permission_policy,
            entry_reason=entry_reason,
        )

    def install_committed_execution_handle(
        self,
        *,
        run_id: str,
        handle_id: str,
        borrow_authority: object,
    ) -> None:
        state = self._pending_working_state(run_id)
        state.execution_resources.run_execution_handle_id = handle_id
        state.execution_resources.capability_execution_borrow_authority = (
            borrow_authority
        )

    def install_recovered_mcp_continuation(self, **kwargs) -> None:
        """Install one exact restart-rebound MCP run owner.

        Recovery composition receives this closed operation instead of the
        mutable registry. The registry remains the sole owner of lifecycle,
        suspension, and pending-activation slot transitions.
        """

        self._registry.install_recovered_mcp_continuation(**kwargs)

    def pending_stop_request(self, run_id: str):
        return self._pending_working_state(run_id).stop_request

    async def abort_pending_run(self, run_id: str, *, reason):
        completion = self._registry.require(run_id).run_completion
        self._registry.transfer_resident_state_to_finalization(run_id)
        state = self._resident_working_state(run_id)
        await self._agent_runtime.abort_run(state, reason=reason)
        return await self._terminal_public_result_from_completion(completion)

    async def fail_pending_run(self, run_id: str, *, stop_reason, error_message: str):
        completion = self._registry.require(run_id).run_completion
        self._registry.transfer_resident_state_to_finalization(run_id)
        state = self._resident_working_state(run_id)
        await self._agent_runtime.fail_committed_run(
            state,
            stop_reason=stop_reason,
            error_message=error_message,
        )
        return await self._terminal_public_result_from_completion(completion)

    def pending_identity(self, run_id: str) -> tuple[str, str, str]:
        state = self._pending_working_state(run_id)
        return state.run_id, state.turn_id, state.reply_id

    def _pending_working_state(self, run_id: str) -> RunActivationWorkingState:
        owner = self._registry.require(run_id)
        carrier = owner.pending_activation_state
        token = owner.pending_activation_owner_token
        if carrier is None or token is None:
            raise RuntimeError("run has no pending activation-state owner")
        return carrier.borrow(owner_token=token)

    def _resident_working_state(self, run_id: str) -> RunActivationWorkingState:
        from pulsara_agent.runtime.run_execution.owner import ActiveRunSuspension

        owner = self._registry.require(run_id)
        segment = owner.active_segment
        if (
            segment is not None
            and segment.state_carrier is not None
            and segment.state_owner_token is not None
        ):
            return segment.state_carrier.borrow(owner_token=segment.state_owner_token)
        if (
            owner.pending_activation_state is not None
            and owner.pending_activation_owner_token is not None
        ):
            return owner.pending_activation_state.borrow(
                owner_token=owner.pending_activation_owner_token
            )
        if isinstance(owner.suspension_slot, ActiveRunSuspension):
            resources = owner.suspension_slot.resources
            return resources.state_carrier.borrow(
                owner_token=resources.state_owner_token
            )
        finalization = owner.finalization_slot.owner
        carrier = getattr(finalization, "state_carrier", None)
        token = getattr(finalization, "state_owner_token", None)
        if carrier is not None and token is not None:
            return carrier.borrow(owner_token=token)
        raise RuntimeError("run has no resident activation-state owner")

    def is_finalized(self, run_id: str) -> bool:
        owner = self._registry.get(run_id)
        return bool(
            owner is not None and owner.finalization_owner.commit_state == "confirmed"
        )

    @staticmethod
    async def _terminal_public_result_from_completion(completion):
        from pulsara_agent.runtime.agent import agent_run_result_from_terminal_outcome

        terminal = await asyncio.shield(completion)
        return agent_run_result_from_terminal_outcome(terminal)

    def request_stop(self, run_id: str, reason: object) -> None:
        from pulsara_agent.runtime.recovery import StopRequest

        self._resident_working_state(run_id).stop_request = StopRequest(reason=reason)

    async def request_active_stop(
        self, run_id: str, reason: object
    ) -> asyncio.Task | None:
        owner = self._registry.require(run_id)
        segment = owner.active_segment
        if segment is None:
            return None
        self.request_stop(run_id, reason)
        cancel_reason: Literal["user_stop", "host_teardown"] = (
            "user_stop"
            if getattr(reason, "value", str(reason)) == "user_stop"
            else "host_teardown"
        )
        active_model_handles = await self._agent_runtime.request_model_cancel(
            run_id,
            reason=cancel_reason,
        )
        task = segment.driver_task
        if active_model_handles == 0 and task is not None and not task.done():
            task.cancel()
        return task

    async def request_active_stop_and_wait(
        self,
        run_id: str,
        reason: object,
        *,
        timeout_seconds: float,
    ) -> Literal["settled", "no_active_driver", "timed_out"]:
        task = await self.request_active_stop(run_id, reason)
        if task is None or task.done():
            return "no_active_driver"
        if task is asyncio.current_task():
            return "no_active_driver"
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except asyncio.CancelledError:
            return "settled"
        except TimeoutError:
            return "timed_out"
        except Exception:
            return "settled"
        return "settled"

    async def cancel_active_driver_and_wait(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
    ) -> Literal["settled", "no_active_driver", "timed_out"]:
        owner = self._registry.get(run_id)
        segment = owner.active_segment if owner is not None else None
        task = segment.driver_task if segment is not None else None
        if task is None or task.done():
            return "no_active_driver"
        if task is asyncio.current_task():
            return "no_active_driver"
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except asyncio.CancelledError:
            return "settled"
        except TimeoutError:
            return "timed_out"
        except Exception:
            return "settled"
        return "settled"

    async def terminalize_resident_run(self, run_id: str, *, reason=None):
        owner = self._registry.require(run_id)
        completion = owner.run_completion
        if owner.lifecycle == "suspended":
            port = self._interaction_transition_port
            if port is None:
                raise RuntimeError("suspended terminalization lacks interaction owner")
            port.hydrate_suspended_working_state(run_id=run_id)
            self._registry.transfer_suspension_state_to_finalization(run_id)
        elif owner.pending_activation_state is not None:
            self._registry.transfer_pending_state_to_finalization(run_id)
        state = self._resident_working_state(run_id)
        if state.status in {
            LoopStatus.FINISHED,
            LoopStatus.FAILED,
            LoopStatus.ABORTED,
        }:
            await self._agent_runtime.retry_run_terminalization(state)
        elif reason is not None:
            await self._agent_runtime.abort_run(state, reason=reason)
        else:
            raise RuntimeError("non-terminal resident run requires an abort reason")
        return await self._terminal_public_result_from_completion(completion)

    async def fail_resident_run(
        self,
        run_id: str,
        *,
        stop_reason,
        error_message: str,
    ):
        """Move any suspended state to stable finalization, then fail once."""

        owner = self._registry.require(run_id)
        completion = owner.run_completion
        if owner.lifecycle == "suspended":
            port = self._interaction_transition_port
            if port is None:
                raise RuntimeError("suspended failure lacks interaction owner")
            port.hydrate_suspended_working_state(run_id=run_id)
            self._registry.transfer_suspension_state_to_finalization(run_id)
        elif owner.pending_activation_state is not None:
            self._registry.transfer_pending_state_to_finalization(run_id)
        state = self._resident_working_state(run_id)
        await self._agent_runtime.fail_committed_run(
            state,
            stop_reason=stop_reason,
            error_message=error_message,
        )
        return await self._terminal_public_result_from_completion(completion)

    async def drain_reconciliations(self, *, deadline_monotonic: float) -> None:
        """Confirm resident stable candidates before dependency teardown."""

        for owner in self._registry.owners():
            reconciliation = owner.reconciliation_owner
            if reconciliation is None:
                continue
            finalization = owner.finalization_slot.owner
            candidates = tuple(getattr(finalization, "terminal_candidates", ()))
            if candidates:
                receipt = await self._reconciliation_service.confirm_stable_candidate(
                    run_id=owner.identity.run_id,
                    candidates=candidates,
                    deadline_monotonic=deadline_monotonic,
                )
                if not receipt.retry_owner_retained:
                    state = self._resident_working_state(owner.identity.run_id)
                    await self._agent_runtime.retry_run_terminalization(state)
        await self._reconciliation_service.drain(deadline_monotonic=deadline_monotonic)
        unresolved = tuple(
            owner.identity.run_id
            for owner in self._registry.owners()
            if owner.reconciliation_owner is not None
        )
        if unresolved:
            raise RunReconciliationCloseBlocked(unresolved)

    def active_safe_point_state(self, run_id: str) -> ActiveRunSafePointState | None:
        owner = self._registry.get(run_id)
        segment = owner.active_segment if owner is not None else None
        if (
            owner is None
            or segment is None
            or segment.segment_state != "active"
            or segment.state_carrier is None
            or segment.state_owner_token is None
        ):
            return None
        state = segment.state_carrier.borrow(owner_token=segment.state_owner_token)
        working_set = state.run_working_set
        if working_set is None:
            return None
        progress = state.model_tool_progress
        return ActiveRunSafePointState(
            run_id=run_id,
            segment_id=segment.segment_id,
            segment_generation=segment.segment_generation,
            has_pending_interaction=state.pending_interaction_kind is not None,
            has_pending_tool_calls=bool(state.pending_tool_calls),
            latest_model_control_disposition_event_id=(
                progress.latest_model_control_disposition_event_id
            ),
            latest_model_control_disposition_model_call_index=(
                progress.latest_model_control_disposition_model_call_index
            ),
            run_start_event_id=working_set.run_start_event_id,
            terminal_open=owner.finalization_owner.commit_state == "open",
            termination_intent_absent=owner.termination_intent is None,
        )

    async def propagate_termination_intent(self, run_id: str, intent) -> None:
        from pulsara_agent.primitives.model_call import (
            RunTerminationIntentAttributionFact,
            sha256_fingerprint,
        )

        state = self._resident_working_state(run_id)
        working_set = state.run_working_set
        control_owner = (
            working_set.model_call_control_owner if working_set is not None else None
        )
        activation = (
            working_set.run_execution_activation if working_set is not None else None
        )
        if control_owner is None:
            return
        if activation is None:
            raise RuntimeError("active model control owner lacks run activation")
        payload = {
            "schema_version": "run_termination_intent_attribution.v1",
            "intent_id": intent.intent_id,
            "kind": intent.kind,
            "requested_at_utc": intent.requested_at_utc,
            "requester_id": intent.requester_id,
            "target_run_execution_activation_fingerprint": (
                activation.activation_fingerprint
            ),
        }
        attribution = RunTerminationIntentAttributionFact(
            **payload,
            attribution_fingerprint=sha256_fingerprint(
                "run-termination-intent-attribution:v1", payload
            ),
        )
        await control_owner.install_termination_intent(attribution)

    def configure_mcp_publication_closure(
        self,
        *,
        run_id: str,
        reason: str,
        deadline_budget: object,
    ) -> None:
        owner = self._registry.require(run_id)
        finalization = owner.finalization_slot.owner
        if finalization is None:
            raise RuntimeError("run lost its finalization owner")
        finalization.mcp_publication_closure_reason = reason
        finalization.publication_deadline_budget = deadline_budget

    def bind_interaction_transition_port(
        self,
        port: "RuntimeInteractionTransitionService",
    ) -> None:
        current = self._interaction_transition_port
        if current is not None and current is not port:
            raise RuntimeError("activation service interaction owner is already bound")
        self._interaction_transition_port = port

    def start_initial_result_activation(
        self,
        *,
        run_id: str,
        host_session_id: str,
        draft: "AgentRunDraft",
        committed: "CommittedRunEntry",
        active_skill_names: frozenset[str],
        on_plan_entry_audit_emitted: Callable[[], None] | None = None,
        on_activation_settled: ActivationSettledCallback | None = None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        async def result_factory(state: RunActivationWorkingState) -> object:
            if draft.state is not state:
                raise RuntimeError("initial draft is not owned by this activation")
            async for _event in self._agent_runtime.stream_committed_entry(
                draft, committed, active_skill_names=active_skill_names
            ):
                pass
            return None

        return self._start_result_with_state(
            run_id=run_id,
            host_session_id=host_session_id,
            result_factory=result_factory,
            on_plan_entry_audit_emitted=on_plan_entry_audit_emitted,
            on_activation_settled=on_activation_settled,
        )

    def start_initial_stream_activation(
        self,
        *,
        run_id: str,
        host_session_id: str,
        draft: "AgentRunDraft",
        committed: "CommittedRunEntry",
        active_skill_names: frozenset[str],
        on_plan_entry_audit_emitted: Callable[[], None] | None = None,
        on_activation_settled: ActivationSettledCallback | None = None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        def stream_factory(
            state: RunActivationWorkingState,
        ) -> AsyncIterator[AgentEvent]:
            if draft.state is not state:
                raise RuntimeError("initial draft is not owned by this activation")
            return self._agent_runtime.stream_committed_entry(
                draft,
                committed,
                active_skill_names=active_skill_names,
            )

        return self._start_stream_with_state(
            run_id=run_id,
            host_session_id=host_session_id,
            stream_factory=stream_factory,
            on_plan_entry_audit_emitted=on_plan_entry_audit_emitted,
            on_activation_settled=on_activation_settled,
        )

    def start_resume_result_activation(
        self,
        *,
        run_id: str,
        host_session_id: str,
        interaction_kind: Literal["approval", "plan", "mcp_input_required"],
        resolution: object,
        on_plan_entry_audit_emitted: Callable[[], None] | None = None,
        on_activation_settled: ActivationSettledCallback | None = None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        async def result_factory(state: RunActivationWorkingState) -> object:
            if interaction_kind == "approval":
                stream = self._agent_runtime.stream_after_approval(state, resolution)
            elif interaction_kind == "plan":
                stream = self._agent_runtime.stream_after_plan_interaction(
                    state, resolution
                )
            else:
                stream = self._agent_runtime.stream_after_mcp_input_required(
                    state, resolution
                )
            async for _event in stream:
                pass
            return None

        return self._start_result_with_state(
            run_id=run_id,
            host_session_id=host_session_id,
            result_factory=result_factory,
            on_plan_entry_audit_emitted=on_plan_entry_audit_emitted,
            on_activation_settled=on_activation_settled,
        )

    def start_resume_stream_activation(
        self,
        *,
        run_id: str,
        host_session_id: str,
        interaction_kind: Literal["approval", "plan", "mcp_input_required"],
        resolution: object,
        on_plan_entry_audit_emitted: Callable[[], None] | None = None,
        on_activation_settled: ActivationSettledCallback | None = None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        def stream_factory(
            state: RunActivationWorkingState,
        ) -> AsyncIterator[AgentEvent]:
            if interaction_kind == "approval":
                return self._agent_runtime.stream_after_approval(state, resolution)
            if interaction_kind == "plan":
                return self._agent_runtime.stream_after_plan_interaction(
                    state, resolution
                )
            return self._agent_runtime.stream_after_mcp_input_required(
                state, resolution
            )

        return self._start_stream_with_state(
            run_id=run_id,
            host_session_id=host_session_id,
            stream_factory=stream_factory,
            on_plan_entry_audit_emitted=on_plan_entry_audit_emitted,
            on_activation_settled=on_activation_settled,
        )

    def _start_result_with_state(
        self,
        *,
        run_id: str,
        host_session_id: str,
        result_factory: Callable[[RunActivationWorkingState], Awaitable[object]],
        on_plan_entry_audit_emitted: Callable[[], None] | None,
        on_activation_settled: ActivationSettledCallback | None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        async def producer(owner) -> object:
            return await result_factory(self._active_working_state(owner))

        return self._start(
            run_id=run_id,
            host_session_id=host_session_id,
            producer=producer,
            observe=False,
            on_state_settled=(
                self._plan_audit_callback(on_plan_entry_audit_emitted)
                if on_plan_entry_audit_emitted is not None
                else None
            ),
            on_activation_settled=on_activation_settled,
        )

    def _start_stream_with_state(
        self,
        *,
        run_id: str,
        host_session_id: str,
        stream_factory: Callable[
            [RunActivationWorkingState], AsyncIterator[AgentEvent]
        ],
        on_plan_entry_audit_emitted: Callable[[], None] | None,
        on_activation_settled: ActivationSettledCallback | None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        async def producer(owner) -> object:
            async for event in stream_factory(self._active_working_state(owner)):
                publish_observer_event(owner, event)
            return None

        return self._start(
            run_id=run_id,
            host_session_id=host_session_id,
            producer=producer,
            observe=True,
            on_state_settled=(
                self._plan_audit_callback(on_plan_entry_audit_emitted)
                if on_plan_entry_audit_emitted is not None
                else None
            ),
            on_activation_settled=on_activation_settled,
        )

    @staticmethod
    def _plan_audit_callback(
        callback: Callable[[], None],
    ) -> StateSettledCallback:
        def settled(state: RunActivationWorkingState) -> None:
            if state.plan_progress.entry_audit_emitted:
                callback()

        return settled

    def start_result_activation(
        self,
        *,
        run_id: str,
        host_session_id: str,
        result_factory: Callable[[], Awaitable[object]],
        on_state_settled: StateSettledCallback | None = None,
        on_activation_settled: ActivationSettledCallback | None = None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        async def producer(_owner) -> object:
            return await result_factory()

        return self._start(
            run_id=run_id,
            host_session_id=host_session_id,
            producer=producer,
            observe=False,
            on_state_settled=on_state_settled,
            on_activation_settled=on_activation_settled,
        )

    def start_stream_activation(
        self,
        *,
        run_id: str,
        host_session_id: str,
        stream_factory: Callable[[], AsyncIterator[AgentEvent]],
        on_state_settled: StateSettledCallback | None = None,
        on_activation_settled: ActivationSettledCallback | None = None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        async def producer(owner) -> object:
            async for event in stream_factory():
                publish_observer_event(owner, event)
            return None

        return self._start(
            run_id=run_id,
            host_session_id=host_session_id,
            producer=producer,
            observe=True,
            on_state_settled=on_state_settled,
            on_activation_settled=on_activation_settled,
        )

    def _start(
        self,
        *,
        run_id: str,
        host_session_id: str,
        producer: Callable[[object], Awaitable[object]],
        observe: bool,
        on_state_settled: StateSettledCallback | None,
        on_activation_settled: ActivationSettledCallback | None,
    ) -> RunActivationDispatch | RunSegmentInstallBlocked:
        owner = self._registry.require(run_id)

        async def request_stop_from_handle(reason: object) -> object:
            return await self.request_active_stop(run_id, reason)

        handle = RegistryRunHandle(
            _owner=owner,
            _stop_requester=request_stop_from_handle,
        )
        observer = handle.subscribe() if observe else None

        async def driver() -> object:
            installed = self._registry.require(run_id).active_segment
            if installed is None:
                raise RuntimeError(
                    "activation driver started before owner installation"
                )
            state = self._active_working_state(owner)
            self._install_working_set_activation(state, installed)
            result: object | None = None
            driver_error: BaseException | None = None
            try:
                result = await producer(owner)
            except asyncio.CancelledError as exc:
                if state.stop_request is None:
                    driver_error = exc
                else:
                    _clear_current_task_cancellation()
                    result = await self._agent_runtime.abort_run(
                        state, reason=state.stop_request.reason
                    )
            except BaseException as exc:
                driver_error = exc
            finally:
                try:
                    self._install_or_clear_suspension(
                        owner=owner,
                        state=state,
                        host_session_id=host_session_id,
                    )
                    if on_state_settled is not None:
                        on_state_settled(state)
                    await self._retire_model_control_owner(state)
                except BaseException as settlement_error:
                    if driver_error is None:
                        driver_error = settlement_error
                finalization = owner.finalization_slot.owner
                finalization_reconciliation = (
                    getattr(finalization, "state", None) == "reconciliation_required"
                )
                if finalization_reconciliation:
                    candidates = tuple(getattr(finalization, "terminal_candidates", ()))
                    if not candidates:
                        raise RuntimeError(
                            "terminal reconciliation lost its stable candidate batch"
                        )
                    # Rebind the durable candidate while the exact activation
                    # generation is still resident. Segment retirement follows
                    # only after this owner has accepted the repair authority.
                    self._reconciliation_service.install_event_batch(
                        run_id=run_id,
                        attempt_kind="run_end_commit",
                        candidates=candidates,
                        repair_mode="live_resident",
                        resident_owner_generation=installed.segment_generation,
                    )
                activation_outcome: RunActivationOutcome | None = None
                if (
                    driver_error is not None
                    and state.status is LoopStatus.WAITING_USER
                    and owner.active_segment is installed
                    and not isinstance(owner.suspension_slot, NoActiveSuspension)
                ):
                    # Validation may reject a resume before any continuation is
                    # committed. The suspension remains authoritative, while
                    # this activation attempt must still retire deterministically.
                    activation_outcome = self._registry.complete_waiting_segment(
                        run_id,
                        segment_id=installed.segment_id,
                        segment_generation=installed.segment_generation,
                    )
                    driver_error = None
                elif driver_error is not None:
                    if isinstance(driver_error, asyncio.CancelledError):
                        _clear_current_task_cancellation()
                    if not state.finalized and not isinstance(
                        getattr(finalization, "run_end_candidate", None),
                        RunEndEvent,
                    ):
                        self._agent_runtime.prepare_failed_run_terminalization(
                            state,
                            stop_reason=RunStopReason.RUNTIME_EXECUTION_ERROR,
                            error_message=(
                                "run activation driver failed: "
                                f"{type(driver_error).__name__}"
                            ),
                        )
                    self._transfer_state_to_finalization(owner, installed)
                    activation_outcome = self._registry.complete_terminal_segment(
                        run_id,
                        segment_id=installed.segment_id,
                        segment_generation=installed.segment_generation,
                    )
                    if isinstance(activation_outcome, RunTerminalizationPending):
                        self._agent_runtime.continue_run_terminalization(state)
                    driver_error = None
                elif state.status is LoopStatus.WAITING_USER:
                    activation_outcome = self._registry.complete_waiting_segment(
                        run_id,
                        segment_id=installed.segment_id,
                        segment_generation=installed.segment_generation,
                    )
                else:
                    if not state.finalized and not isinstance(
                        getattr(finalization, "run_end_candidate", None),
                        RunEndEvent,
                    ):
                        self._agent_runtime.prepare_failed_run_terminalization(
                            state,
                            stop_reason=RunStopReason.RUNTIME_EXECUTION_ERROR,
                            error_message=(
                                "run activation driver exited without a terminal "
                                "or suspension outcome"
                            ),
                        )
                    self._transfer_state_to_finalization(owner, installed)
                    activation_outcome = self._registry.complete_terminal_segment(
                        run_id,
                        segment_id=installed.segment_id,
                        segment_generation=installed.segment_generation,
                    )
                    if isinstance(activation_outcome, RunTerminalizationPending):
                        self._agent_runtime.continue_run_terminalization(state)
                if activation_outcome is None:
                    raise RuntimeError(
                        "activation driver exited without a closed outcome"
                    )
                if on_activation_settled is not None:
                    callback_result = on_activation_settled(activation_outcome)
                    if callback_result is not None:
                        await callback_result
                close_observers(owner, reason="activation_completed")
            if driver_error is not None:
                raise driver_error
            return result

        activation_kind: Literal["initial", "interaction_resume"] = (
            "interaction_resume"
            if owner.latest_activation_owner_kind == "host_resume_boundary"
            else "initial"
        )
        segment = self._registry.install_segment(
            run_id,
            activation_kind=activation_kind,
            activation_owner_kind=owner.latest_activation_owner_kind,
            activation_owner_id=owner.latest_activation_owner_id,
            driver_factory=driver,
            observer=(observer._handle if observer is not None else None),
            event_log=self._event_log,
        )
        if isinstance(segment, RunSegmentInstallBlocked):
            if observer is not None:
                observer._handle.detach("activation_install_blocked")
                owner.observer_registry.observers.pop(
                    observer._handle.observer_id, None
                )
            return segment
        if segment.driver_task is None:
            raise RuntimeError("activation segment did not install its driver task")
        segment.driver_task.add_done_callback(
            lambda task: self._on_driver_task_done(
                run_id=run_id,
                segment=segment,
                task=task,
            )
        )
        return RunActivationDispatch(
            run_handle=handle,
            activation_generation=segment.segment_generation,
            observer=observer,
        )

    def _on_driver_task_done(
        self,
        *,
        run_id: str,
        segment: RunActivationCoordinator,
        task: asyncio.Future[object],
    ) -> None:
        """Backstop ownership if settlement itself aborts the driver finally block."""

        _consume_future_exception(task)
        owner = self._registry.get(run_id)
        if owner is None or owner.active_segment is not segment:
            return
        state = self._active_working_state(owner)
        finalization = owner.finalization_slot.owner
        if not state.finalized and not isinstance(
            getattr(finalization, "run_end_candidate", None), RunEndEvent
        ):
            self._agent_runtime.prepare_failed_run_terminalization(
                state,
                stop_reason=RunStopReason.RUNTIME_EXECUTION_ERROR,
                error_message="run activation driver exited before settlement completed",
            )
        self._transfer_state_to_finalization(owner, segment)
        outcome = self._registry.complete_terminal_segment(
            run_id,
            segment_id=segment.segment_id,
            segment_generation=segment.segment_generation,
        )
        if isinstance(outcome, RunTerminalizationPending):
            self._agent_runtime.continue_run_terminalization(state)

    def _install_or_clear_suspension(
        self,
        *,
        owner,
        state: RunActivationWorkingState,
        host_session_id: str,
    ) -> None:
        if state.status is LoopStatus.WAITING_USER:
            port = self._interaction_transition_port
            if port is None:
                raise RuntimeError(
                    "activation service lacks its interaction transition owner"
                )
            port.install_suspension(
                run_id=state.run_id,
                host_session_id=host_session_id,
            )
            # The suspension slot is now the sole owner. The completed
            # activation keeps transcript/progress only; branch data is
            # borrowed back into a future activation after continuation FULL.
            state.pending_tool_calls = []
            state.pending_interaction_kind = None
            state.pending_interaction_payload = {}
            state.pending_interaction_source_event_reference = None
            state.pending_interaction_source_event_candidate = None
            return
        owner.suspension_slot = NoActiveSuspension()

    @staticmethod
    def _active_working_state(owner) -> RunActivationWorkingState:
        segment = owner.active_segment
        if (
            segment is None
            or segment.state_carrier is None
            or segment.state_owner_token is None
        ):
            raise RuntimeError("active activation lacks its working-state owner")
        return segment.state_carrier.borrow(owner_token=segment.state_owner_token)

    @staticmethod
    def _transfer_state_to_finalization(owner, segment) -> None:
        carrier = segment.state_carrier
        source_token = segment.state_owner_token
        finalization = owner.finalization_slot.owner
        if carrier is None or source_token is None:
            if getattr(finalization, "state_carrier", None) is not None:
                return
            raise RuntimeError("terminal activation lost its working-state owner")
        if finalization is None:
            raise RuntimeError("terminal activation lost its finalization owner")
        target_token = f"finalization:{owner.identity.owner_fingerprint}"
        carrier.transfer(
            expected_owner_token=source_token,
            new_owner_token=target_token,
        )
        finalization.state_carrier = carrier
        finalization.state_owner_token = target_token
        segment.state_carrier = None
        segment.state_owner_token = None

    @staticmethod
    async def _retire_model_control_owner(state: RunActivationWorkingState) -> None:
        working_set = state.run_working_set
        if working_set is None or working_set.model_call_control_owner is None:
            return
        await working_set.model_call_control_owner.retire()
        working_set.model_call_control_owner = None

    @staticmethod
    def _install_working_set_activation(
        state: RunActivationWorkingState,
        segment: RunActivationCoordinator,
    ) -> None:
        working_set = state.run_working_set
        if working_set is None:
            raise RuntimeError("run activation requires a committed working set")
        if segment.activation_identity is None:
            raise RuntimeError("production activation lacks durable attribution")
        activation = segment.activation_identity.durable_activation
        current = working_set.run_execution_activation
        if working_set.model_call_control_owner is not None:
            raise RuntimeError("activation already has a model-control owner")
        if current is not None and current != activation:
            if activation.segment_generation <= current.segment_generation:
                raise RuntimeError("activation generation regressed")
            if working_set.process_segment_id == segment.segment_id:
                raise RuntimeError("segment identity was reused")
        working_set.run_execution_activation = activation
        working_set.process_segment_id = segment.segment_id
        working_set.model_call_control_owner = RunModelCallControlOwner(
            run_id=state.run_id,
            activation=activation,
            segment_id=segment.segment_id,
            segment_generation=segment.segment_generation,
        )


def _clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is None:
        return
    while task.cancelling():
        task.uncancel()


def _consume_future_exception(future: asyncio.Future[object]) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except BaseException:
        pass


__all__ = [
    "ActiveRunSafePointState",
    "RunActivationDispatch",
    "RunReconciliationCloseBlocked",
    "RunActivationService",
]
