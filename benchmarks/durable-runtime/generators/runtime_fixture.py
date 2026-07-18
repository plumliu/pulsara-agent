"""Production-shaped root-run fixture used by offline writer benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from pulsara_agent.capability.runtime import (
    CapabilityRuntime,
    FrozenCapabilityExecutionSurface,
)
from pulsara_agent.capability.types import CapabilityProjectionResolveContext
from pulsara_agent.event import (
    CapabilityExposureResolvedEvent,
    ContextWindowOpenedEvent,
    EventContext,
    EventType,
    RolloutBudgetAccountOpenedEvent,
    RunStartEvent,
)
from pulsara_agent.event_log.transcript_prefix import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
)
from pulsara_agent.primitives.capability import (
    build_capability_execution_surface_identity,
    build_capability_resolve_basis,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.model_call import (
    ResolvedModelTargetFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_boundary import (
    BoundaryTranscriptSnapshotFact,
    NewRunBoundaryFact,
    RunExecutionActivationFact,
)
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    CurrentUserMessageFact,
    HostRunBoundaryIdentityFact,
    text_sha256,
)
from pulsara_agent.runtime.authority_materialization import (
    persist_prepared_run_transcript_seed,
    prepare_authority_artifact_write_reservation,
    prepare_run_transcript_seed,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.long_horizon.reducer_contract import (
    build_default_subagent_graph_reducer_contract,
)
from pulsara_agent.runtime.long_horizon.run_contract import (
    empty_projection_state_fingerprint,
    prepare_root_long_horizon_run,
)
from pulsara_agent.runtime.permission_snapshot import snapshot_from_mode
from pulsara_agent.runtime.plan import PlanWorkflowState, plan_workflow_state_fact
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.run_entry import (
    CapabilityResolveBasis,
    RunWorkingSet,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.llm.resolution import ResolvedModelTarget


BENCHMARK_OBSERVED_AT_UTC = "2026-01-01T00:00:00.000000Z"


@dataclass(frozen=True, slots=True)
class BenchmarkContextRun:
    event_context: EventContext
    target: ResolvedModelTarget
    activation: RunExecutionActivationFact
    state: LoopState
    working_set: RunWorkingSet
    capability_runtime: CapabilityRuntime


async def bootstrap_benchmark_root_run(
    runtime_session: RuntimeSession,
    *,
    event_context: EventContext,
    model_target: ResolvedModelTargetFact,
) -> RunExecutionActivationFact:
    """Commit a real root RunStart/window/account genesis outside sample timing."""

    runtime_session.event_log.ensure_runtime_session_owner()
    graph_contract = build_default_subagent_graph_reducer_contract()
    run_start_event_id = f"run_start:{event_context.run_id}"
    prepared_long_horizon = prepare_root_long_horizon_run(
        runtime_session_id=runtime_session.runtime_session_id,
        run_id=event_context.run_id,
        run_start_event_id=run_start_event_id,
        primary_target=model_target,
        summarizer_target=model_target,
        graph_reducer_contract=graph_contract,
        source_through_sequence_at_open=0,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    prepared_seed = _persist_run_seed(
        runtime_session,
        run_id=event_context.run_id,
    )
    permission_snapshot_id = f"permission_snapshot:{event_context.run_id}"
    mcp_installation_id = "mcp_installation:empty"
    boundary_id = f"run_boundary:{event_context.run_id}"
    current_user = CurrentUserMessageFact(
        message_id=f"user-message:{event_context.run_id}",
        source_kind="host_user_input",
        text="durable runtime writer benchmark",
        observed_at_utc=BENCHMARK_OBSERVED_AT_UTC,
        content_sha256=text_sha256("durable runtime writer benchmark"),
        source_artifact_id=None,
    )
    boundary_identity = HostRunBoundaryIdentityFact(
        boundary_id=boundary_id,
        kind="pre_run",
        runtime_session_id=runtime_session.runtime_session_id,
        run_id=event_context.run_id,
        turn_id=event_context.turn_id,
        reply_id=event_context.reply_id,
        attempt_number=1,
        observed_at_utc=BENCHMARK_OBSERVED_AT_UTC,
    )
    execution_surface = build_capability_execution_surface_identity(
        surface_contract_version="durable-runtime-benchmark:v1",
        entries=(),
        mcp_installation_id=mcp_installation_id,
    )
    capability_basis = build_capability_resolve_basis(
        basis_id=f"capability_basis:{event_context.run_id}",
        basis_kind="initial",
        source_basis_id=None,
        source_basis_fingerprint=None,
        owner=CapabilityExposureOwnerFact(
            owner_kind="host_boundary",
            owner_id=boundary_id,
            host_boundary_kind="pre_run",
            runtime_session_id=runtime_session.runtime_session_id,
            run_id=event_context.run_id,
        ),
        workspace_identity_fingerprint=sha256_fingerprint(
            "durable-runtime-benchmark-workspace:v1",
            str(runtime_session.workspace_root),
        ),
        memory_domain_id="memory_domain:durable-runtime-benchmark",
        permission_snapshot_id=permission_snapshot_id,
        plan_active=False,
        active_skill_names=(),
        user_intent_fingerprint=sha256_fingerprint(
            "durable-runtime-benchmark-user-intent:v1",
            current_user.text,
        ),
        prior_transcript_fingerprint=context_fingerprint(
            "durable-runtime-benchmark-prior-transcript:v1",
            (),
        ),
        mcp_installation_id=mcp_installation_id,
        execution_surface_identity=execution_surface,
    )
    boundary = NewRunBoundaryFact(
        identity=boundary_identity,
        transcript=BoundaryTranscriptSnapshotFact(
            source_through_sequence=0,
            source_event_count=0,
            compacted_window_id=None,
            checkpoint_compaction_id=None,
            checkpoint_terminal_event_id=None,
            checkpoint_terminal_sequence=None,
            checkpoint_keep_after_sequence=None,
            preflight_compaction_id=None,
            preflight_compaction_terminal_event_id=None,
            preflight_compaction_terminal_sequence=None,
        ),
        model_target_fingerprint=model_target.target_fingerprint,
        permission_snapshot_id=permission_snapshot_id,
        mcp_installation_id=mcp_installation_id,
        capability_basis=capability_basis,
        degraded_reason_codes=(),
    )
    permission_mode = PermissionMode.BYPASS_PERMISSIONS
    run_start = RunStartEvent(
        id=run_start_event_id,
        **event_context.event_fields(),
        created_at=BENCHMARK_OBSERVED_AT_UTC,
        user_input_chars=len(current_user.text),
        permission_snapshot_id=permission_snapshot_id,
        permission_mode=permission_mode.value,
        permission_policy=preset_to_policy(permission_mode).to_dict(),
        permission_snapshot_source="session_default",
        model_target=model_target,
        subagent_graph_reducer_contract=graph_contract,
        long_horizon=prepared_long_horizon.contract,
        child_rollout_subaccount=None,
        mcp_installation_id=mcp_installation_id,
        mcp_installation_owner_runtime_session_id=(
            runtime_session.runtime_session_id
        ),
        run_entry_kind="host",
        current_user_message=current_user,
        run_transcript_seed_semantic=prepared_seed.seed_semantic,
        run_transcript_seed_reference=prepared_seed.seed_reference,
        terminal_run_end_event_id=f"run_end:{event_context.run_id}",
        new_run_boundary=boundary,
        subagent_run_entry=None,
    )
    account = prepared_long_horizon.root_account
    if account is None:
        raise RuntimeError("benchmark root run requires a rollout account")
    stored = tuple(
        await runtime_session.emit_many(
            (
                run_start,
                ContextWindowOpenedEvent(
                    id=prepared_long_horizon.contract.initial_window_open_event_id,
                    **event_context.event_fields(),
                    window=prepared_long_horizon.initial_window,
                    opening_batch_id=prepared_long_horizon.opening_batch_id,
                ),
                RolloutBudgetAccountOpenedEvent(
                    id=f"rollout_budget_account_opened:{account.account_id}",
                    **event_context.event_fields(),
                    account=account,
                ),
            )
        )
    )
    committed_start = next(
        event for event in stored if isinstance(event, RunStartEvent)
    )
    runtime_session.transcript_projection_checkpoint_service.adopt_committed_run_seed(
        committed_start
    )
    activation_payload = {
        "schema_version": "run_execution_activation.v1",
        "activation_owner_kind": "host_run_boundary",
        "activation_owner_id": boundary_id,
        "segment_generation": 1,
    }
    return RunExecutionActivationFact(
        **activation_payload,
        activation_fingerprint=sha256_fingerprint(
            "run-execution-activation:v1",
            activation_payload,
        ),
    )


async def bootstrap_benchmark_context_run(
    runtime_session: RuntimeSession,
    *,
    event_context: EventContext,
    target: ResolvedModelTarget,
) -> BenchmarkContextRun:
    """Open a production-shaped run and install its immutable context inputs."""

    activation = await bootstrap_benchmark_root_run(
        runtime_session,
        event_context=event_context,
        model_target=target.fact,
    )
    raw_start = runtime_session.event_log.read_raw_events_by_id(
        (f"run_start:{event_context.run_id}",),
        deadline_monotonic=monotonic() + 30.0,
    )
    if len(raw_start) != 1:
        raise RuntimeError("benchmark context run lacks its committed RunStart")
    from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY

    run_start = raw_start[0].decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if not isinstance(run_start, RunStartEvent) or run_start.sequence is None:
        raise RuntimeError("benchmark context RunStart is not committed")
    boundary = run_start.new_run_boundary
    if boundary is None:
        raise RuntimeError("benchmark context run requires a Host boundary")

    capability_runtime = CapabilityRuntime(providers=())
    frozen_surface = FrozenCapabilityExecutionSurface(
        identity=boundary.capability_basis.execution_surface_identity,
        descriptors=(),
        diagnostics=(),
    )
    resolved_exposure = capability_runtime.resolve_exposure_projection(
        CapabilityProjectionResolveContext(
            workspace_root=runtime_session.workspace_root,
            workspace_kind="project",
            memory_domain=None,
            user_input=run_start.current_user_message.text,
            prior_messages=(),
            active_skill_names=frozenset(),
            plan_active=False,
        ),
        frozen_surface=frozen_surface,
        archive=runtime_session.archive,
        runtime_session_id=runtime_session.runtime_session_id,
        owner=boundary.capability_basis.owner,
        resolve_basis=boundary.capability_basis,
        exposure_id=f"capability_exposure:{event_context.run_id}:1",
    )
    stored_exposure = await runtime_session.emit(
        CapabilityExposureResolvedEvent(
            id=f"capability_exposure_resolved:{event_context.run_id}:1",
            **event_context.event_fields(),
            created_at=BENCHMARK_OBSERVED_AT_UTC,
            exposure=resolved_exposure.fact,
            exposure_revision=1,
        )
    )
    return _bind_benchmark_context_run(
        runtime_session,
        event_context=event_context,
        target=target,
        activation=activation,
        run_start=run_start,
        stored_exposure=stored_exposure,
        capability_runtime=capability_runtime,
        frozen_surface=frozen_surface,
        resolved_exposure=resolved_exposure,
    )


def rebind_benchmark_context_run(
    runtime_session: RuntimeSession,
    *,
    event_context: EventContext,
    target: ResolvedModelTarget,
) -> BenchmarkContextRun:
    """Rebuild process-local context ownership from one durable active run."""

    deadline = monotonic() + 30.0
    raw = runtime_session.event_log.read_raw_events_by_types(
        (
            EventType.RUN_START.value,
            EventType.CAPABILITY_EXPOSURE_RESOLVED.value,
        ),
        run_ids=(event_context.run_id,),
        max_events=16,
        max_payload_bytes=2 * 1024 * 1024,
        deadline_monotonic=deadline,
    )
    from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY

    decoded = tuple(
        item.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY) for item in raw.events
    )
    starts = tuple(item for item in decoded if isinstance(item, RunStartEvent))
    exposures = tuple(
        item for item in decoded if isinstance(item, CapabilityExposureResolvedEvent)
    )
    if len(starts) != 1 or len(exposures) != 1:
        raise RuntimeError("benchmark context reopen lacks unique run/exposure facts")
    run_start = starts[0]
    stored_exposure = exposures[0]
    if run_start.sequence is None or stored_exposure.sequence is None:
        raise RuntimeError("benchmark context reopen facts are not committed")
    boundary = run_start.new_run_boundary
    if boundary is None:
        raise RuntimeError("benchmark context reopen requires a Host boundary")
    activation_payload = {
        "schema_version": "run_execution_activation.v1",
        "activation_owner_kind": "host_run_boundary",
        "activation_owner_id": boundary.identity.boundary_id,
        "segment_generation": 1,
    }
    activation = RunExecutionActivationFact(
        **activation_payload,
        activation_fingerprint=sha256_fingerprint(
            "run-execution-activation:v1",
            activation_payload,
        ),
    )
    capability_runtime = CapabilityRuntime(providers=())
    frozen_surface = FrozenCapabilityExecutionSurface(
        identity=boundary.capability_basis.execution_surface_identity,
        descriptors=(),
        diagnostics=(),
    )
    resolved_exposure = capability_runtime.resolve_exposure_projection(
        CapabilityProjectionResolveContext(
            workspace_root=runtime_session.workspace_root,
            workspace_kind="project",
            memory_domain=None,
            user_input=run_start.current_user_message.text,
            prior_messages=(),
            active_skill_names=frozenset(),
            plan_active=False,
        ),
        frozen_surface=frozen_surface,
        archive=runtime_session.archive,
        runtime_session_id=runtime_session.runtime_session_id,
        owner=boundary.capability_basis.owner,
        resolve_basis=boundary.capability_basis,
        exposure_id=stored_exposure.exposure.exposure_id,
        persist_artifacts=False,
    )
    if resolved_exposure.fact != stored_exposure.exposure:
        raise RuntimeError("benchmark capability exposure failed deterministic rebind")
    return _bind_benchmark_context_run(
        runtime_session,
        event_context=event_context,
        target=target,
        activation=activation,
        run_start=run_start,
        stored_exposure=stored_exposure,
        capability_runtime=capability_runtime,
        frozen_surface=frozen_surface,
        resolved_exposure=resolved_exposure,
    )


def _bind_benchmark_context_run(
    runtime_session: RuntimeSession,
    *,
    event_context: EventContext,
    target: ResolvedModelTarget,
    activation: RunExecutionActivationFact,
    run_start: RunStartEvent,
    stored_exposure: CapabilityExposureResolvedEvent,
    capability_runtime: CapabilityRuntime,
    frozen_surface: FrozenCapabilityExecutionSurface,
    resolved_exposure,
) -> BenchmarkContextRun:
    if run_start.sequence is None:
        raise RuntimeError("benchmark context RunStart is not committed")
    boundary = run_start.new_run_boundary
    if boundary is None:
        raise RuntimeError("benchmark context run requires a Host boundary")
    permission_snapshot = snapshot_from_mode(
        runtime_session_id=runtime_session.runtime_session_id,
        run_id=event_context.run_id,
        permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        permission_snapshot_source="session_default",
    )
    plan_snapshot = plan_workflow_state_fact(
        PlanWorkflowState(),
        inactive_default_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    capability_basis = CapabilityResolveBasis(
        fact=boundary.capability_basis,
        user_input=run_start.current_user_message.text,
        prior_messages=(),
        active_skill_names=frozenset(),
        workspace_root=runtime_session.workspace_root,
        memory_domain_id=boundary.capability_basis.memory_domain_id,
    )
    working_set = RunWorkingSet(
        run_start_event_id=run_start.id,
        run_start_sequence=run_start.sequence,
        run_model_target=target,
        long_horizon_contract=run_start.long_horizon,
        run_transcript_seed_semantic=run_start.run_transcript_seed_semantic,
        run_transcript_seed_reference=run_start.run_transcript_seed_reference,
        permission_snapshot=permission_snapshot,
        plan_snapshot=plan_snapshot,
        capability_resolve_basis=capability_basis,
        frozen_execution_surface=frozen_surface,
        original_exposure_plan=None,
        original_exposure_fact=None,
        original_exposure_event_ref=None,
        effective_exposure_plan=None,
        effective_exposure_fact=None,
        effective_exposure_event_ref=None,
        latest_committed_resume_boundary=None,
        latest_committed_resume_boundary_ref=None,
        run_execution_activation=activation,
        process_segment_id=f"benchmark_segment:{event_context.run_id}:1",
    )
    working_set.install_initial_exposure(
        plan=resolved_exposure.plan,
        fact=resolved_exposure.fact,
        event_ref=event_reference_from_stored(
            stored_exposure,
            runtime_session_id=runtime_session.runtime_session_id,
        ),
    )
    state = LoopState(
        session_id=runtime_session.runtime_session_id,
        run_id=event_context.run_id,
        turn_id=event_context.turn_id,
        reply_id=event_context.reply_id,
        permission_snapshot=permission_snapshot,
        run_model_target=target,
        run_working_set=working_set,
    )
    return BenchmarkContextRun(
        event_context=event_context,
        target=target,
        activation=activation,
        state=state,
        working_set=working_set,
        capability_runtime=capability_runtime,
    )


def _persist_run_seed(
    runtime_session: RuntimeSession,
    *,
    run_id: str,
):
    projection = runtime_session.transcript_projection_state_store.snapshot()
    prepared = prepare_run_transcript_seed(
        runtime_session_id=runtime_session.runtime_session_id,
        stable_state=projection.stable_semantic_state,
        stable_entries=(
            runtime_session.transcript_projection_state_store.stable_entries()
        ),
        ledger_through_sequence=projection.ledger_through_sequence,
        ledger_continuity_accumulator=(
            projection.ledger_continuity_accumulator
            or EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
        ),
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            runtime_session.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=runtime_session.transcript_projection_materialization_contracts,
    )
    deadline = monotonic() + (
        runtime_session.authority_materialization_contracts.limits.checkpoint_operation_timeout_seconds
    )
    persist_prepared_run_transcript_seed(
        prepared,
        write_reservation=prepare_authority_artifact_write_reservation(
            operation_id=f"run-seed:{run_id}",
            owner_kind="run_seed_materialization",
            artifacts=prepared.artifacts,
            limits=runtime_session.authority_materialization_contracts.limits,
            absolute_deadline_monotonic=deadline,
        ),
        limits=runtime_session.authority_materialization_contracts.limits,
        archive=runtime_session.archive,
        runtime_session_id=runtime_session.runtime_session_id,
        deadline_monotonic=deadline,
    )
    runtime_session.transcript_projection_checkpoint_service.prepare_run_seed_artifacts(
        run_id=run_id,
        artifact_ids=frozenset(item.artifact_id for item in prepared.artifacts),
    )
    return prepared


__all__ = [
    "BENCHMARK_OBSERVED_AT_UTC",
    "BenchmarkContextRun",
    "bootstrap_benchmark_context_run",
    "bootstrap_benchmark_root_run",
    "rebind_benchmark_context_run",
]
