"""Common child activation service, outside AgentRuntime and SubagentRuntime."""

from __future__ import annotations

import asyncio
from threading import RLock
from uuid import uuid4

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.profile_runtime import (
    profile_filtered_capability_runtime,
)
from pulsara_agent.capability.types import CapabilityExecutionSurfaceSnapshotContext
from pulsara_agent.event import (
    RolloutBudgetReservationCreatedEvent,
    SubagentRolloutBudgetResolvedEvent,
)
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.ports.memory_hooks import NoopMemoryHooks
from pulsara_agent.primitives.capability import build_capability_resolve_basis
from pulsara_agent.primitives.long_horizon import RolloutReservationReferenceFact
from pulsara_agent.primitives.mcp import McpInstallationReferenceFact
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.permission import parse_permission_mode
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    CurrentUserMessageFact,
    SubagentRunEntryFact,
    text_sha256,
)
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.primitives.subagent import (
    build_child_result_render_policy,
    validate_child_render_policy_against_budget,
)
from pulsara_agent.runtime.compaction.inline import NoopRuntimeContextCompactor
from pulsara_agent.runtime.execution_handles import RunExecutionHandleSet
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.run_entry import CapabilityResolveBasis, PreparedSubagentRunEntry
from pulsara_agent.runtime.run_execution.factory import RunActivationFactory
from pulsara_agent.ports.run_execution import (
    RunHandle,
    RunReconciliationRequired,
    RunSegmentInstallBlocked,
    RunSuspendedOutcome,
    RunTerminalOutcome,
    RunTerminalOutputPending,
    RunTerminalizationPending,
)
from pulsara_agent.runtime.run_execution.prepared import PreparedRunActivationOwner
from pulsara_agent.runtime.session_run_capabilities import (
    RunRuntimeIdentity,
    RuntimeSessionRunLedgerPort,
    RuntimeSessionRunLongHorizonPort,
    build_agent_runtime_session_capabilities,
)
from pulsara_agent.ports.run_execution import build_prepared_run_owner_reservation_key
from pulsara_agent.runtime.state import LoopBudget
from pulsara_agent.runtime.subagent.run_entry import SubagentRunEntryDriver
from pulsara_agent.runtime.subagent.runtime import SubagentRuntime
from pulsara_agent.runtime.long_horizon.run_contract import (
    build_child_rollout_subaccount,
    prepare_child_long_horizon_run,
)
from pulsara_agent.runtime.long_horizon.feasibility import (
    ProductionRolloutBudgetFeasibilityReport,
)


class SubagentChildActivationService:
    """Own the child composition recipe without retaining the parent AgentRuntime."""

    def __init__(
        self,
        *,
        run_identity: RunRuntimeIdentity,
        run_ledger_port: RuntimeSessionRunLedgerPort,
        run_long_horizon_port: RuntimeSessionRunLongHorizonPort,
        llm_runtime: LLMRuntime,
        model_role: ModelRole,
        options: LLMOptions | None,
        budget: LoopBudget,
        system_prompt: str | None,
        capability_runtime: CapabilityRuntime,
        workspace_kind: str,
        rollout_budget_feasibility_report: ProductionRolloutBudgetFeasibilityReport | None,
        activation_factory: RunActivationFactory,
        subagent_runtime: SubagentRuntime,
    ) -> None:
        self._run_identity = run_identity
        self._run_ledger = run_ledger_port
        self._run_long_horizon = run_long_horizon_port
        self.llm_runtime = llm_runtime
        self.model_role = model_role
        self.options = options
        self.budget = budget
        self.system_prompt = system_prompt
        self.capability_runtime = capability_runtime
        self.workspace_kind = workspace_kind
        self.rollout_budget_feasibility_report = rollout_budget_feasibility_report
        self._activation_factory = activation_factory
        self._subagent_runtime = subagent_runtime
        self._lock = RLock()
        self._run_handles: dict[str, RunHandle] = {}
        self._terminalization_handoffs: set[str] = set()

    async def activate_committed_child(
        self,
        subagent_run_id: str,
        *,
        deadline_monotonic: float,
    ) -> RunHandle:
        if asyncio.get_running_loop().time() >= deadline_monotonic:
            raise TimeoutError("child activation deadline expired before admission")
        subagent_runtime = self._subagent_runtime
        run_view = await subagent_runtime.hydrate_child_activation_run(
            subagent_run_id
        )
        run = run_view.fact
        capability_profile = run.capability_profile_value
        child_session = subagent_runtime.child_runtime_session(run.subagent_run_id)
        if capability_profile.permission_mode is None:
            raise ValueError(
                "child subagent run requires a preset child_profile permission mode"
            )
        child_permission_mode = parse_permission_mode(
            capability_profile.permission_mode
        )
        child_capability_runtime = profile_filtered_capability_runtime(
            self.capability_runtime,
            capability_profile,
        )
        composition = self._activation_factory.create(
            event_log=child_session.event_log,
            runtime_session_id=child_session.runtime_session_id,
            agent_runtime_kwargs={
                **build_agent_runtime_session_capabilities(child_session),
                "llm_runtime": self.llm_runtime,
                "memory_hooks": NoopMemoryHooks(),
                "model_role": self.model_role,
                "options": self.options,
                "budget": self.budget,
                "system_prompt": self.system_prompt,
                "capability_runtime": child_capability_runtime,
                "memory_domain": None,
                "workspace_kind": self.workspace_kind,
                "permission_policy": preset_to_policy(child_permission_mode),
                "context_compactor": NoopRuntimeContextCompactor(),
                "subagent_runtime": subagent_runtime,
                "enable_subagents": False,
            },
        )
        child_agent = composition.agent_runtime
        subagent_runtime.attach_child_activation_composition(
            run.subagent_run_id,
            composition,
        )
        child_agent.rollout_budget_feasibility_report = (
            self.rollout_budget_feasibility_report
        )
        # A child profile is an execution-surface boundary, not merely a
        # model-visible projection.  Keep descriptor and binding sets exact so
        # disallowed parent tools cannot remain as unowned executable bindings.
        child_agent.tool_executor.registry = (
            child_agent.tool_executor.registry.restricted_to(
                frozenset(capability_profile.allowed_tool_names)
            )
        )
        if not run_view.task_text_complete or run_view.task_text is None:
            raise ValueError(
                "child subagent run requires a fully hydrated task artifact"
            )
        if run.task_artifact_id is None:
            raise ValueError("child subagent run requires a durable task artifact")
        child_state = child_agent.new_state()
        child_target = child_agent.resolve_run_model_target()
        child_summarizer_target = child_agent.llm_runtime.resolve_target(
            role=ModelRole.FLASH
        )
        child_agent.require_prevalidated_rollout_pair(
            execution_profile_kind="subagent_child",
            execution_profile_id=run.profile_id or "general_worker",
            primary_target=child_target,
            summarizer_target=child_summarizer_target,
        )
        parent_run_start = self._run_long_horizon.store.run_start(
            run.parent_run_id
        )
        if parent_run_start is None:
            raise RuntimeError("child rollout contract requires one parent RunStart")
        resolved_budget_event = self._run_ledger.get_event(
            f"subagent_rollout_budget_resolved:{run.subagent_run_id}"
        )
        account_state = self._run_long_horizon.store.rollout_state(
            parent_run_start.long_horizon.rollout_account_id
        )
        reservations = (
            tuple(
                item
                for item in account_state.active_reservations
                if item.owner_kind == "subagent_run"
                and item.owner_id == run.subagent_run_id
            )
            if account_state is not None
            else ()
        )
        if (
            not isinstance(resolved_budget_event, SubagentRolloutBudgetResolvedEvent)
            or len(reservations) != 1
        ):
            raise RuntimeError("child start lost its atomic rollout admission facts")
        reservation = reservations[0]
        stored_reservation = self._run_ledger.get_event(
            f"rollout_budget_reservation_created:{reservation.reservation_id}"
        )
        if not isinstance(stored_reservation, RolloutBudgetReservationCreatedEvent):
            raise RuntimeError("child start lost its rollout reservation fact")
        if (
            resolved_budget_event.budget_snapshot_event_id
            != run.provenance.created_event_id
            or stored_reservation.sequence is None
            or stored_reservation.reservation.reserved_milliunits
            != resolved_budget_event.resolved_budget.max_rollout_milliunits_per_child
            or resolved_budget_event.resolved_budget.child_primary_target_fingerprint
            != child_target.fact.target_fingerprint
            or resolved_budget_event.resolved_budget.child_summarizer_target_fingerprint
            != child_summarizer_target.fact.target_fingerprint
        ):
            raise RuntimeError("child rollout admission identity mismatch")
        reservation_reference = RolloutReservationReferenceFact(
            owner_runtime_session_id=self._run_identity.runtime_session_id,
            reservation_id=stored_reservation.reservation.reservation_id,
            reservation_event_id=stored_reservation.id,
            reservation_sequence=stored_reservation.sequence,
            reservation_fingerprint=(
                stored_reservation.reservation.semantic_fingerprint
            ),
        )
        child_permission = child_agent._capture_run_permission_snapshot(child_state)
        child_run_start_id = f"run_start:subagent:{uuid4().hex}"
        child_long_horizon = prepare_child_long_horizon_run(
            child_runtime_session_id=child_session.runtime_session_id,
            child_run_id=child_state.run_id,
            run_start_event_id=child_run_start_id,
            primary_target=child_target.fact,
            summarizer_target=child_summarizer_target.fact,
            graph_reducer_contract=(
                child_session.subagent_graph_checkpoint_service.reducer_binding.contract
            ),
            account_id=parent_run_start.long_horizon.rollout_account_id,
            account_owner_runtime_session_id=(
                parent_run_start.long_horizon.rollout_account_owner_runtime_session_id
            ),
            account_owner_run_id=(
                parent_run_start.long_horizon.rollout_account_owner_run_id
            ),
            inherited_rollout_reservation=reservation_reference,
        )
        child_rollout_subaccount = build_child_rollout_subaccount(
            child_runtime_session_id=child_session.runtime_session_id,
            child_run_id=child_state.run_id,
            resolved_budget=resolved_budget_event.resolved_budget,
            reservation_reference=reservation_reference,
            root_account_id=parent_run_start.long_horizon.rollout_account_id,
        )
        task_observed_at = run.created_at.isoformat()
        render_policy = build_child_result_render_policy(
            renderer_version="subagent-result:v1",
            max_summary_chars=run.budget_snapshot.max_result_summary_chars_per_child,
            max_artifact_refs=run.budget_snapshot.max_result_artifact_refs_per_child,
        )
        validate_child_render_policy_against_budget(render_policy, run.budget_snapshot)
        frozen_surface = child_agent.capability_runtime.freeze_execution_surface(
            CapabilityExecutionSurfaceSnapshotContext(
                workspace_root=child_session.workspace_root,
                workspace_kind=self.workspace_kind,
                available_tool_names=frozenset(
                    child_agent.tool_executor.registry.names()
                ),
                mcp_installation_id=child_session.mcp_installation_id,
            ),
            tool_registry=child_agent.tool_executor.registry,
            archive=child_session.archive,
            runtime_session_id=child_session.runtime_session_id,
            owner_id=child_run_start_id,
        )
        child_owner_reservation_key = build_prepared_run_owner_reservation_key(
            runtime_session_id=child_session.runtime_session_id,
            run_id=child_state.run_id,
            run_start_event_id=child_run_start_id,
        )
        child_execution_handles = RunExecutionHandleSet(
            handle_id=f"child_execution_handles:{uuid4().hex}",
            handle_generation=1,
            owner=child_owner_reservation_key,
            state="boundary_owned",
            mcp_installation=child_session.mcp_installation_id,
            capability_runtime=child_agent.capability_runtime,
            tool_registry=child_agent.tool_executor.registry,
            frozen_execution_surface=frozen_surface,
        )
        composition.registry.reserve_prepared(
            key=child_owner_reservation_key,
            execution_handles=child_execution_handles,
            reservation_generation=1,
        )
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("child RunStart preparation requires a task owner")
        prepared_activation = PreparedRunActivationOwner(
            run_id=child_state.run_id,
            boundary_id=child_run_start_id,
            owner_task=current_task,
            generation=1,
            _working_state=child_state,
        )
        exposure_owner = CapabilityExposureOwnerFact(
            owner_kind="subagent_run_start",
            owner_id=child_run_start_id,
            host_boundary_kind=None,
            runtime_session_id=child_session.runtime_session_id,
            run_id=child_state.run_id,
        )
        capability_basis = build_capability_resolve_basis(
            basis_id=f"capability_basis:subagent:{uuid4().hex}",
            basis_kind="initial",
            source_basis_id=None,
            source_basis_fingerprint=None,
            owner=exposure_owner,
            workspace_identity_fingerprint=sha256_fingerprint(
                "subagent-workspace-identity:v1",
                [str(child_session.workspace_root), self.workspace_kind],
            ),
            memory_domain_id="memory_domain:subagent-disabled",
            permission_snapshot_id=child_permission.snapshot_id,
            plan_active=False,
            active_skill_names=(),
            user_intent_fingerprint=sha256_fingerprint(
                "subagent-task-intent:v1", run_view.task_text
            ),
            prior_transcript_fingerprint=sha256_fingerprint(
                "subagent-prior-transcript:v1", []
            ),
            mcp_installation_id=child_session.mcp_installation_id,
            execution_surface_identity=frozen_surface.identity,
        )
        current_user = CurrentUserMessageFact(
            message_id=f"user-message:{child_state.run_id}",
            source_kind=(
                "subagent_task"
                if run.task_id is not None
                else "subagent_primitive_objective"
            ),
            text=run_view.task_text,
            observed_at_utc=task_observed_at,
            content_sha256=text_sha256(run_view.task_text),
            source_artifact_id=run.task_artifact_id,
        )
        child_entry = SubagentRunEntryFact(
            subagent_run_id=run.subagent_run_id,
            subagent_task_id=run.task_id,
            parent_runtime_session_id=run.parent_runtime_session_id,
            parent_run_id=run.parent_run_id,
            spawn_edge_id=run.edge_id,
            capability_profile_fingerprint=sha256_fingerprint(
                "subagent-capability-profile:v1",
                capability_profile.to_event_value(),
            ),
            task_artifact_id=run.task_artifact_id,
            task_observed_at_utc=task_observed_at,
            child_result_render_policy=render_policy,
            permission_snapshot_id=child_permission.snapshot_id,
            model_target_fingerprint=child_target.fact.target_fingerprint,
            mcp_installation_id=child_session.mcp_installation_id,
            mcp_installation_owner_runtime_session_id=(
                child_session.mcp_installation_owner_runtime_session_id
            ),
            capability_basis=capability_basis,
        )
        child_state.run_model_target = child_target
        child_state.permission_snapshot = child_permission
        terminal_run_end_event_id = f"run_end:subagent:{uuid4().hex}"
        child_state.terminal_run_end_event_id = terminal_run_end_event_id
        child_resources = child_state.execution_resources
        child_resources.current_user_message_fact = current_user
        child_resources.subagent_run_entry_fact = child_entry
        child_resources.capability_resolve_basis = CapabilityResolveBasis(
            fact=capability_basis,
            user_input=run_view.task_text,
            prior_messages=(),
            active_skill_names=frozenset(),
            workspace_root=child_session.workspace_root,
            memory_domain_id="memory_domain:subagent-disabled",
        )
        child_resources.frozen_capability_execution_surface = frozen_surface
        prepared_child_entry = PreparedSubagentRunEntry(
            entry_fact=child_entry,
            current_user_message=current_user,
            run_model_target=child_target,
            permission_snapshot=child_permission,
            mcp_installation_fact=McpInstallationReferenceFact(
                installation_id=child_session.mcp_installation_id,
                owner_runtime_session_id=(
                    child_session.mcp_installation_owner_runtime_session_id
                ),
                config_epoch=0,
                event_safe_config_set_fingerprint=sha256_fingerprint(
                    "subagent-mcp-installation-reference:v1",
                    [
                        child_session.mcp_installation_id,
                        child_session.mcp_installation_owner_runtime_session_id,
                    ],
                ),
                server_snapshot_semantic_fingerprints=(),
                binding_identities=(),
            ),
            capability_basis=child_resources.capability_resolve_basis,
            frozen_execution_surface=frozen_surface,
            run_start_event_id=child_run_start_id,
            terminal_run_end_event_id=terminal_run_end_event_id,
            long_horizon=child_long_horizon,
            child_rollout_subaccount=child_rollout_subaccount,
        )
        try:
            entry_bundle = await SubagentRunEntryDriver().prepare_and_commit(
                child_agent=child_agent,
                state=child_state,
                prepared=prepared_child_entry,
                prior_messages=[],
            )
        except BaseException:
            composition.registry.release_prepared(
                child_owner_reservation_key,
                outcome="none",
            )
            prepared_activation.release()
            raise
        owner = composition.registry.promote_committed_entry(
            reservation_key=child_owner_reservation_key,
            committed=entry_bundle.committed,
            run_start_envelope=child_session.event_log.read_raw_events_by_id(
                (entry_bundle.committed.run_start_event.id,)
            )[0],
            prepared_activation=prepared_activation,
        )
        child_resources.capability_execution_borrow_authority = (
            owner.execution_handles.borrow_authority
        )
        child_resources.capability_execution_borrow_kind = "child"
        dispatch = composition.service.start_initial_result_activation(
            run_id=child_state.run_id,
            host_session_id=f"subagent:{run.subagent_run_id}",
            draft=entry_bundle.draft,
            committed=entry_bundle.committed,
            active_skill_names=frozenset(),
        )
        if isinstance(dispatch, RunSegmentInstallBlocked):
            raise RuntimeError(
                f"child activation installation blocked: {dispatch.reason}"
            )
        with self._lock:
            existing = self._run_handles.get(run.subagent_run_id)
            if existing is not None and existing is not dispatch.run_handle:
                raise RuntimeError("child activation handle identity drifted")
            self._run_handles[run.subagent_run_id] = dispatch.run_handle
        outcome = await dispatch.wait_activation()
        if isinstance(
            outcome,
            (RunTerminalizationPending, RunTerminalOutputPending),
        ):
            outcome = await dispatch.run_handle.wait_run_completion()
        with self._lock:
            terminalization_handoff = (
                run.subagent_run_id in self._terminalization_handoffs
            )
        if terminalization_handoff:
            self._retire_common_run_owner(
                composition, dispatch.run_handle.identity.run_id
            )
        elif isinstance(outcome, RunTerminalOutcome) and outcome.output.status == "finished":
            child_run_id = outcome.owner_identity.run_id
            self._retire_common_run_owner(composition, child_run_id)
            submitted = subagent_runtime.submitted_result(run.subagent_run_id)
            if submitted is not None:
                await subagent_runtime.complete_submitted_result(
                    run.subagent_run_id,
                    token_usage=outcome.output.usage.model_dump(mode="json"),
                    tool_call_count=outcome.output.tool_call_count,
                    child_run_id=child_run_id,
                )
            else:
                await subagent_runtime.complete_native_result(
                    run.subagent_run_id,
                    child_run_id=child_run_id,
                )
        elif isinstance(outcome, RunSuspendedOutcome):
            await composition.service.fail_resident_run(
                child_state.run_id,
                stop_reason=RunStopReason.SUBAGENT_PENDING_UNSUPPORTED,
                error_message=(
                    "Child agent entered a pending interaction that V1 cannot route."
                ),
            )
            terminal = await dispatch.run_handle.wait_run_completion()
            self._retire_common_run_owner(
                composition, terminal.owner_identity.run_id
            )
            await subagent_runtime.fail_from_native_child_terminal(
                run.subagent_run_id,
                child_run_id=terminal.owner_identity.run_id,
                reason_code="subagent_pending_unsupported",
                reason_message="Child agent entered a pending interaction that V1 subagent runtime cannot route.",
                diagnostics=[
                    {
                        "status": "waiting_user",
                        "stop_reason": "waiting_user",
                        "pending_interaction_kind": (
                            outcome.pending_interaction.interaction_kind
                        ),
                    }
                ],
            )
        elif isinstance(outcome, RunReconciliationRequired):
            raise RuntimeError(
                "child activation requires reconciliation: "
                f"{outcome.diagnostic_code}"
            )
        elif isinstance(outcome, RunTerminalOutcome):
            child_run_id = outcome.owner_identity.run_id
            self._retire_common_run_owner(composition, child_run_id)
            status = outcome.output.status
            await subagent_runtime.fail_from_native_child_terminal(
                run.subagent_run_id,
                child_run_id=child_run_id,
                reason_code=f"subagent_{status}",
                reason_message=(
                    f"Child agent ended with status {status} without a usable result."
                ),
                diagnostics=[
                    {
                        "status": status,
                        "stop_reason": outcome.output.stop_reason,
                        "child_error_present": status == "failed",
                    }
                ],
            )
        else:
            raise TypeError("child activation returned an unknown closed outcome")
        return dispatch.run_handle

    @staticmethod
    def _retire_common_run_owner(composition, run_id: str) -> None:
        if composition.registry.get(run_id) is None:
            return
        composition.registry.retire_confirmed(run_id)
        if composition.registry.get(run_id) is not None:
            raise RuntimeError("child RunOwner retained live physical resources")

    async def terminalize_committed_child(
        self,
        subagent_run_id: str,
        *,
        termination_kind: str,
        deadline_monotonic: float,
    ):
        """Stop the common RunOwner before parent-graph terminal settlement."""

        if termination_kind not in {
            "parent_cancel",
            "host_teardown",
            "child_timeout",
        }:
            raise ValueError("unsupported child terminalization kind")
        with self._lock:
            handle = self._run_handles.get(subagent_run_id)
            if handle is None:
                raise RuntimeError("child activation has no committed RunHandle")
            self._terminalization_handoffs.add(subagent_run_id)
        reason = (
            AbortKind.HOST_TEARDOWN
            if termination_kind == "host_teardown"
            else AbortKind.USER_STOP
        )
        try:
            await handle.request_stop(reason)
        except KeyError:
            # A prior terminalization attempt may already have retired the
            # common RunOwner while the parent graph commit returned NONE.
            # The immutable handle completion remains the exact authority.
            pass
        remaining = deadline_monotonic - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("child terminalization deadline expired")
        terminal = await asyncio.wait_for(
            asyncio.shield(handle.wait_run_completion()),
            timeout=remaining,
        )
        composition = self._subagent_runtime.borrow_child_activation_composition(
            subagent_run_id
        )
        if composition is None:
            raise RuntimeError("child terminalization lost its activation composition")
        self._retire_common_run_owner(composition, terminal.owner_identity.run_id)
        return terminal

    def retire_child_activation(self, subagent_run_id: str) -> None:
        """Release process-local handle borrows after parent graph FULL."""

        with self._lock:
            self._run_handles.pop(subagent_run_id, None)
            self._terminalization_handoffs.discard(subagent_run_id)


__all__ = ["SubagentChildActivationService"]
