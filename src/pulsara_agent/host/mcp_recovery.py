"""Restart rebind for durable stateless MCP continuations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.event import (
    AgentEvent,
    McpInputRequiredResolutionSubmittedEvent,
    ModelCallControlDispositionResolvedEvent,
    RunStartEvent,
    ToolExecutionSuspendedEvent,
)
from pulsara_agent.event_log.serialization import freeze_event_write_candidate
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.event_log.protocol import (
    DEFAULT_SPARSE_EVENT_READ_MAX_EVENTS,
    DEFAULT_SPARSE_EVENT_READ_MAX_PAYLOAD_BYTES,
)
from pulsara_agent.ports.mcp import McpInvocationOwner
from pulsara_agent.ports.run_authority import (
    ContinuationRunAuthorityRevision,
    InstalledRunAuthorityRevision,
)
from pulsara_agent.ports.tool_registry import McpToolBindingContract
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.permission import parse_permission_mode
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.runtime.execution_handles import RunExecutionHandleSet
from pulsara_agent.runtime.mcp.tool_execution_port import (
    RecoveredMcpContinuationOwner,
)
from pulsara_agent.runtime.permission_snapshot import RunPermissionSnapshot
from pulsara_agent.runtime.run_entry import (
    CapabilityResolveBasis,
    install_run_working_set,
)
from pulsara_agent.runtime.run_execution.activation import (
    materialize_activation_identity_from_fact,
)
from pulsara_agent.runtime.run_execution.interaction import (
    materialize_recovered_mcp_interaction,
)
from pulsara_agent.runtime.run_execution.prepared import RunActivationStateCarrier
from pulsara_agent.runtime.run_execution.recovery import materialize_dormant_run_owner
from pulsara_agent.runtime.state import LoopStatus, LoopTransition
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.host.transcript import rebuild_prior_messages_bounded

if TYPE_CHECKING:
    from pulsara_agent.host.session import HostSession


@dataclass(frozen=True, slots=True)
class RecoveredHostMcpRun:
    run_id: str
    recovery_state: str
    prepared_resolution: object | None
    run_start_event_id: str


async def recover_host_mcp_run(
    host: "HostSession",
    *,
    run_id: str,
    deadline_monotonic: float,
) -> RecoveredHostMcpRun:
    """Rebind one durable Host run after current MCP discovery is READY."""

    runtime_wiring = host.wiring.runtime_wiring
    runtime_session = runtime_wiring.runtime_session
    event_log = runtime_wiring.event_log
    raw_run_events = await runtime_session.context_input_io_service.execute(
        operation_name="host-mcp-restart-authority-read",
        operation=lambda: event_log.read_raw_run_events(
            run_id,
            max_events=DEFAULT_SPARSE_EVENT_READ_MAX_EVENTS,
            max_payload_bytes=DEFAULT_SPARSE_EVENT_READ_MAX_PAYLOAD_BYTES,
            deadline_monotonic=deadline_monotonic,
        ),
        deadline_monotonic=deadline_monotonic,
    )
    run_events = tuple(
        envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        for envelope in raw_run_events
    )
    starts = tuple(event for event in run_events if isinstance(event, RunStartEvent))
    if len(starts) != 1 or starts[0].child_rollout_subaccount is not None:
        raise RuntimeError("MCP restart rebind requires one Host RunStart")
    run_start = starts[0]
    envelopes = tuple(
        envelope for envelope in raw_run_events if envelope.event_id == run_start.id
    )
    if len(envelopes) != 1:
        raise RuntimeError("MCP restart rebind lost its RunStart envelope")
    owner = materialize_dormant_run_owner(
        events=run_events,
        run_start_envelope=envelopes[0],
    )
    if not isinstance(owner.authority_head, InstalledRunAuthorityRevision):
        raise RuntimeError("MCP restart rebind lacks committed run authority")

    lifecycle_records = (
        runtime_session.mcp_input_required_lifecycle_store.active_for_run(run_id)
    )
    if len(lifecycle_records) != 1:
        raise RuntimeError("MCP restart rebind requires one active lifecycle")
    lifecycle = lifecycle_records[0]
    if lifecycle.status not in {"suspended", "resolution_submitted"}:
        raise RuntimeError("MCP restart state is not safely replayable")
    source_reference = lifecycle.source_suspension_event_reference
    source = _event_by_id(
        run_events,
        source_reference.event_id,
    )
    if (
        not isinstance(source, ToolExecutionSuspendedEvent)
        or source.sequence != source_reference.sequence
        or event_reference_from_stored(
            source, runtime_session_id=runtime_session.runtime_session_id
        )
        != source_reference
    ):
        raise RuntimeError("MCP restart source suspension is not exact")

    resolution_reference = lifecycle.latest_resolution_submitted_event_reference
    resolution = None
    if lifecycle.status == "resolution_submitted":
        if resolution_reference is None:
            raise RuntimeError("MCP replay-ready lifecycle lost its resolution")
        resolution = _event_by_id(
            run_events,
            resolution_reference.event_id,
        )
        if (
            not isinstance(resolution, McpInputRequiredResolutionSubmittedEvent)
            or resolution.sequence != resolution_reference.sequence
            or resolution.source.source_suspension_event_reference != source_reference
        ):
            raise RuntimeError("MCP replay-ready resolution is not exact")
    elif resolution_reference is not None:
        raise RuntimeError("awaiting MCP lifecycle unexpectedly has a resolution")

    registry = host.wiring.agent_runtime.tool_executor.registry
    binding = registry.binding_contract(source.suspension.interaction.tool_name)
    if not isinstance(binding, McpToolBindingContract):
        raise RuntimeError("MCP restart tool binding is unavailable")
    recovered: RecoveredMcpContinuationOwner | None = None
    mcp_port = runtime_session.mcp_tool_execution_port
    if mcp_port is None:
        raise RuntimeError("MCP restart lacks its execution-port owner")
    try:
        recovered = await runtime_session.context_input_io_service.execute(
            operation_name="host-mcp-restart-secret-rebind",
            operation=lambda: mcp_port.recover_committed_continuation(
                owner=McpInvocationOwner(
                    runtime_session_id=runtime_session.runtime_session_id,
                    run_id=run_id,
                    tool_call_id=source.suspension.interaction.tool_call_id,
                    event_context=_event_context(source),
                ),
                binding=binding,
                source_suspension_event_reference=source_reference,
                source_suspension=source.suspension,
                source_resolution_event_reference=resolution_reference,
                resolution_semantic=(
                    resolution.resolution if resolution is not None else None
                ),
                resolution_carrier=(
                    resolution.continuation if resolution is not None else None
                ),
            ),
            deadline_monotonic=deadline_monotonic,
        )
        source_owner = materialize_dormant_run_owner(
            events=tuple(
                event
                for event in run_events
                if event.sequence is not None and event.sequence <= source.sequence
            ),
            run_start_envelope=envelopes[0],
        )
        if not isinstance(source_owner.authority_head, InstalledRunAuthorityRevision):
            raise RuntimeError("MCP suspension lacks predecessor run authority")
        previous_activation_fact = _latest_activation_fact(
            run_events,
            through_sequence=source.sequence,
        )
        previous_activation_identity = materialize_activation_identity_from_fact(
            owner=source_owner,
            durable_activation=previous_activation_fact,
            event_log=event_log,
            stored_source_event=_activation_source_event(
                events=run_events,
                owner=source_owner,
                activation=previous_activation_fact,
            ),
        )

        transcript = await runtime_session.context_input_io_service.execute(
            operation_name="host-mcp-restart-transcript-rebind",
            operation=lambda: rebuild_prior_messages_bounded(
                event_log,
                archive=runtime_wiring.archive,
                session_id=runtime_session.runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )
        current_revision = owner.authority_head.revision
        permission = _permission_snapshot(current_revision.effective_permission)
        target = host.wiring.agent_runtime.rebind_run_model_target(
            current_revision.effective_model_target
        )
        basis = CapabilityResolveBasis(
            fact=current_revision.source_exposure.resolve_basis,
            user_input=owner.genesis.current_user_message.text,
            prior_messages=tuple(
                message.model_copy(deep=True) for message in transcript.messages
            ),
            active_skill_names=frozenset(
                current_revision.source_exposure.resolve_basis.active_skill_names
            ),
            workspace_root=host.workspace.workspace_root,
            memory_domain_id=host.workspace.memory_domain.memory_domain_id,
        )
        frozen_surface, exposure_plan = (
            await runtime_session.context_input_io_service.execute(
                operation_name="host-mcp-restart-capability-rebind",
                operation=lambda: _rebind_capability_surface(
                    host=host,
                    registry=registry,
                    basis=basis,
                    source=current_revision.source_exposure,
                    deadline_monotonic=deadline_monotonic,
                ),
                deadline_monotonic=deadline_monotonic,
            )
        )

        state = host.wiring.agent_runtime.new_state(
            session_id=runtime_session.runtime_session_id,
            run_id=run_id,
            turn_id=source.turn_id,
            reply_id=source.reply_id,
        )
        state.messages = [
            message.model_copy(deep=True) for message in transcript.messages
        ]
        state.run_model_target = target
        state.permission_snapshot = permission
        state.terminal_run_end_event_id = owner.genesis.terminal_run_end_event_id
        state.execution_resources.host_session_id = host.host_session_id
        state.execution_resources.capability_resolve_basis = basis.fact
        state.execution_resources.frozen_capability_execution_surface = frozen_surface
        state.execution_resources.current_user_message_fact = (
            owner.genesis.current_user_message
        )
        state.plan_progress.workflow_state = host.plan_state
        state.model_tool_progress.model_call_index = _latest_model_call_index(
            run_events
        )
        _restore_latest_model_control(state, run_events)
        rollout_state = runtime_session.long_horizon_state_store.rollout_state(
            owner.genesis.long_horizon.rollout_account_id
        )
        if rollout_state is None:
            raise RuntimeError("MCP restart lost its rollout account state")
        state.tool_call_count = rollout_state.tool_call_count

        working_set = install_run_working_set(
            state,
            owner.entry,
            plan_snapshot=host._plan_workflow_state_fact(),
            capability_resolve_basis=basis,
            frozen_execution_surface=frozen_surface,
        )
        working_set.install_initial_exposure(
            plan=exposure_plan,
            fact=current_revision.source_exposure,
            event_ref=current_revision.source_exposure_event_reference,
        )
        working_set.run_execution_activation = previous_activation_fact
        if isinstance(current_revision, ContinuationRunAuthorityRevision):
            working_set.latest_committed_resume_boundary = (
                current_revision.source_resume_boundary
            )
            working_set.latest_committed_resume_boundary_ref = (
                current_revision.source_resume_boundary_event_reference
            )
            working_set.latest_validated_suspended_state_token_fingerprint = current_revision.source_resume_boundary.suspended_state_token_fingerprint
        if resolution_reference is not None:
            working_set.latest_mcp_input_required_resolution_ref = resolution_reference
            state.execution_resources.latest_mcp_input_required_resolution_reference = (
                resolution_reference
            )

        view = recovered.pending_handle.suspension_commit_view
        state.pending_tool_calls = []
        state.pending_interaction_kind = "mcp_input_required"
        state.pending_interaction_payload = {
            "interaction_id": view.interaction.interaction_id,
            "kind": "mcp_input_required",
            "tool_call_id": view.interaction.tool_call_id,
            "tool_name": view.interaction.tool_name,
            "server_id": view.interaction.server_id,
            "round_count": view.interaction.round_count,
            "mcp_pending_handle": recovered.pending_handle,
            "suspension_fact": source.suspension,
            "source_suspension_event_reference": source_reference,
            "deadline_monotonic": view.deadline_monotonic,
            "tool_observation_timing_seed": {},
            "rollout_reservation_id": source.suspension.rollout_reservation_id,
            "rollout_reservation_fingerprint": (
                source.suspension.rollout_reservation_fingerprint
            ),
        }
        state.pending_interaction_source_event_reference = source_reference
        state.pending_interaction_source_event_candidate = freeze_event_write_candidate(
            source.model_copy(update={"sequence": None})
        )
        state.status = LoopStatus.WAITING_USER
        state.stop_reason = RunStopReason.WAITING_USER
        state.last_transition = LoopTransition.WAIT_FOR_USER

        state_owner_token = (
            f"mcp-recovery:{owner.identity.owner_fingerprint}:"
            f"{view.interaction.interaction_id}"
        )
        carrier = RunActivationStateCarrier(
            run_id=run_id,
            generation=previous_activation_fact.segment_generation,
            owner_token=state_owner_token,
            _working_state=state,
        )
        authority, resources = materialize_recovered_mcp_interaction(
            owner=owner,
            host_session_id=host.host_session_id,
            state_carrier=carrier,
            state_owner_token=state_owner_token,
            resource_generation=previous_activation_fact.segment_generation,
        )
        handles = RunExecutionHandleSet(
            handle_id=f"run_execution_handles:recovered:{uuid4().hex}",
            handle_generation=1,
            owner=owner.identity,
            state="run_owned",
            mcp_installation=runtime_wiring.mcp_installation,
            capability_runtime=host.wiring.agent_runtime.capability_runtime,
            tool_registry=registry,
            frozen_execution_surface=frozen_surface,
        )
        state.execution_resources.run_execution_handle_id = handles.handle_id
        state.execution_resources.capability_execution_borrow_authority = (
            handles.borrow_authority
        )
        replay_ready = recovered.recovery_state == "replay_ready"
        resume_reference = (
            current_revision.source_resume_boundary_event_reference
            if replay_ready
            and isinstance(current_revision, ContinuationRunAuthorityRevision)
            else None
        )
        if replay_ready and resume_reference is None:
            raise RuntimeError("MCP replay-ready recovery lacks its resume boundary")
        prepared_resolution = recovered.prepared_resolution
        host._run_activation_service.install_recovered_mcp_continuation(
            owner=owner,
            execution_handles=handles,
            authority=authority,
            resources=resources,
            previous_activation_identity=previous_activation_identity,
            suspended_authority_revision_fingerprint=(
                source_owner.authority_head.revision.authority_fingerprint
            ),
            replay_ready=replay_ready,
            resume_boundary_event_reference=resume_reference,
        )
        recovered = None
        return RecoveredHostMcpRun(
            run_id=run_id,
            recovery_state=(
                "replay_ready" if replay_ready else "awaiting_client_input"
            ),
            prepared_resolution=(prepared_resolution if replay_ready else None),
            run_start_event_id=run_start.id,
        )
    except BaseException:
        if recovered is not None:
            mcp_port.discard_recovered_continuation(recovered)
        raise


def _event_context(event: AgentEvent):
    from pulsara_agent.event import EventContext

    return EventContext(
        run_id=event.run_id, turn_id=event.turn_id, reply_id=event.reply_id
    )


def _event_by_id(events: tuple[AgentEvent, ...], event_id: str) -> AgentEvent | None:
    matches = tuple(event for event in events if event.id == event_id)
    if len(matches) > 1:
        raise RuntimeError("MCP restart ledger contains duplicate event IDs")
    return matches[0] if matches else None


def _rebind_capability_surface(
    *,
    host: "HostSession",
    registry,
    basis: CapabilityResolveBasis,
    source,
    deadline_monotonic: float,
):
    runtime_wiring = host.wiring.runtime_wiring
    frozen_surface = (
        host.wiring.agent_runtime.capability_runtime.freeze_execution_surface(
            CapabilityExecutionSurfaceSnapshotContext(
                workspace_root=host.workspace.workspace_root,
                workspace_kind=host.workspace.workspace_kind,
                available_tool_names=frozenset(registry.names()),
                mcp_installation_id=runtime_wiring.mcp_installation.installation_id,
            ),
            tool_registry=registry,
            archive=runtime_wiring.archive,
            runtime_session_id=runtime_wiring.runtime_session.runtime_session_id,
            owner_id=source.owner.owner_id,
        )
    )
    return frozen_surface, _hydrate_exposure_plan(
        host=host,
        basis=basis,
        frozen_surface=frozen_surface,
        source=source,
        deadline_monotonic=deadline_monotonic,
    )


def _latest_activation_fact(
    events: tuple[AgentEvent, ...],
    *,
    through_sequence: int,
):
    matches = tuple(
        (event.sequence, activation)
        for event in events
        if event.sequence is not None
        and event.sequence <= through_sequence
        and (activation := getattr(event, "run_execution_activation", None)) is not None
    )
    if not matches:
        raise RuntimeError("MCP suspension lacks durable activation attribution")
    return max(matches, key=lambda item: item[0])[1]


def _activation_source_event(*, events, owner, activation):
    event_id = (
        activation.activation_owner_id
        if activation.activation_owner_kind == "host_resume_boundary"
        else owner.identity.run_start_event_id
    )
    source = _event_by_id(tuple(events), event_id)
    if source is None:
        raise RuntimeError("MCP restart activation source is missing")
    return source


def _permission_snapshot(fact) -> RunPermissionSnapshot:
    policy = thaw_json(fact.expanded_policy)
    if not isinstance(policy, dict):
        raise RuntimeError("run permission authority is not an object")
    snapshot = RunPermissionSnapshot(
        snapshot_id=fact.snapshot_id,
        runtime_session_id=fact.runtime_session_id,
        run_id=fact.run_id,
        permission_mode=parse_permission_mode(fact.mode),
        permission_policy=policy,
        permission_snapshot_source=fact.source,
    )
    if snapshot.to_context_fact() != fact:
        raise RuntimeError("run permission authority failed exact rebind")
    return snapshot


def _hydrate_exposure_plan(
    *,
    host: "HostSession",
    basis: CapabilityResolveBasis,
    frozen_surface,
    source,
    deadline_monotonic: float,
) -> CapabilityExposurePlan:
    runtime_wiring = host.wiring.runtime_wiring
    candidate = (
        host.wiring.agent_runtime.capability_runtime.resolve_exposure_projection(
            CapabilityProjectionResolveContext(
                workspace_root=host.workspace.workspace_root,
                workspace_kind=host.workspace.workspace_kind,
                memory_domain=host.workspace.memory_domain,
                user_input=basis.user_input,
                prior_messages=basis.prior_messages,
                active_skill_names=basis.active_skill_names,
                plan_active=source.resolve_basis.plan_active,
            ),
            frozen_surface=frozen_surface,
            archive=runtime_wiring.archive,
            runtime_session_id=runtime_wiring.runtime_session.runtime_session_id,
            owner=source.owner,
            resolve_basis=source.resolve_basis,
            exposure_id=f"recovery-candidate:{source.exposure_id}",
            resolution_kind="initial",
            persist_artifacts=False,
        )
    )
    durable_surface = {
        item.capability_name: item for item in source.semantic.execution_surface.entries
    }
    current_surface = {
        item.capability_name: item for item in frozen_surface.identity.entries
    }
    for authorization in source.authorization_entries:
        durable = durable_surface.get(authorization.capability_name)
        current = current_surface.get(authorization.capability_name)
        if (
            durable is None
            or current is None
            or _binding_identity(durable) != _binding_identity(current)
        ):
            raise RuntimeError(
                "recovered capability binding changed: " + authorization.capability_name
            )
        if (
            authorization.disposition == "direct"
            and authorization.capability_name not in candidate.plan.direct_names
        ):
            raise RuntimeError(
                "recovered direct capability is no longer callable: "
                + authorization.capability_name
            )
    _validate_projection(
        source.semantic.catalog_projection, candidate.fact.semantic.catalog_projection
    )
    _validate_projection(
        source.semantic.active_skill_projection,
        candidate.fact.semantic.active_skill_projection,
    )
    catalog_names = {
        item.stable_name
        for item in source.semantic.catalog_projection.visible_source_entries
    }
    active_names = {
        item.stable_name
        for item in source.semantic.active_skill_projection.visible_source_entries
    }
    direct_names = frozenset(source.direct_names)
    return CapabilityExposurePlan(
        registry_generation=candidate.plan.registry_generation,
        direct_tool_specs=tuple(
            item
            for item in candidate.plan.direct_tool_specs
            if item.name in direct_names
        ),
        direct_names=direct_names,
        deferred_names=frozenset(source.deferred_names),
        hidden_names=frozenset(source.hidden_names),
        callable_names=frozenset(source.callable_names),
        descriptors_by_name=MappingProxyType(
            {
                name: candidate.plan.descriptors_by_name[name]
                for name in durable_surface
                if name in candidate.plan.descriptors_by_name
            }
        ),
        catalog_entries=tuple(
            item
            for item in candidate.plan.catalog_entries
            if item.name in catalog_names
        ),
        active_injections=tuple(
            item
            for item in candidate.plan.active_injections
            if item.name in active_names
        ),
        catalog_prompt=_hydrate_prompt(
            runtime_wiring.archive,
            runtime_wiring.runtime_session.runtime_session_id,
            source.semantic.catalog_projection,
            deadline_monotonic,
        ),
        active_skill_prompt=_hydrate_prompt(
            runtime_wiring.archive,
            runtime_wiring.runtime_session.runtime_session_id,
            source.semantic.active_skill_projection,
            deadline_monotonic,
        ),
        diagnostics=candidate.plan.diagnostics,
    )


def _binding_identity(value) -> tuple[object, ...]:
    return (
        value.provider_id,
        value.descriptor_id,
        value.descriptor_fingerprint,
        value.binding_fingerprint,
        value.binding_contract_id,
        value.binding_contract_version,
    )


def _validate_projection(durable, current) -> None:
    current_entries = {
        item.projection_entry_id: item for item in current.visible_source_entries
    }
    for item in durable.visible_source_entries:
        rebound = current_entries.get(item.projection_entry_id)
        if rebound is None or rebound.content_fingerprint != item.content_fingerprint:
            raise RuntimeError(
                "recovered capability projection content changed: " + item.stable_name
            )


def _hydrate_prompt(
    archive, session_id: str, projection, deadline: float
) -> str | None:
    artifact_id = projection.rendered_prompt_artifact_id
    if artifact_id is None:
        return None
    text = archive.get_text(
        artifact_id,
        session_id=session_id,
        deadline_monotonic=deadline,
    )
    fingerprint = f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    if (
        fingerprint != projection.rendered_prompt_fingerprint
        or len(text) != projection.rendered_prompt_chars
    ):
        raise RuntimeError("recovered capability prompt artifact drifted")
    return text


def _latest_model_call_index(events: tuple[AgentEvent, ...]) -> int:
    return max(
        (
            int(value)
            for event in events
            if (value := getattr(event, "model_call_index", None)) is not None
        ),
        default=0,
    )


def _restore_latest_model_control(state, events: tuple[AgentEvent, ...]) -> None:
    matches = tuple(
        event
        for event in events
        if isinstance(event, ModelCallControlDispositionResolvedEvent)
        and event.sequence is not None
    )
    if not matches:
        return
    latest = max(matches, key=lambda item: item.sequence or 0)
    state.model_tool_progress.latest_model_control_disposition_event_id = latest.id
    state.model_tool_progress.latest_model_control_disposition_model_call_index = (
        latest.model_call_index
    )


__all__ = ["RecoveredHostMcpRun", "recover_host_mcp_run"]
