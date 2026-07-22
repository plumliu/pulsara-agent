"""Process-local committed run-entry carriers shared by Host and child drivers."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pulsara_agent.event import RunStartEvent
from pulsara_agent.capability.runtime import FrozenCapabilityExecutionSurface
from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.event.events import utc_now
from pulsara_agent.llm.resolution import ResolvedModelTarget
from pulsara_agent.message import Msg, UserMsg
from pulsara_agent.primitives.capability import (
    CapabilityExposureSnapshotFact,
    CapabilityResolveBasisFact,
)
from pulsara_agent.primitives.run_boundary import (
    InteractionResumeBoundaryFact,
    NewRunBoundaryFact,
    PlanWorkflowStateFact,
    RunExecutionActivationFact,
)
from pulsara_agent.primitives.run_entry import (
    CurrentUserMessageFact,
    RunEntryKind,
    SubagentRunEntryFact,
)
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.host_ingress import (
    HostIngressAdmissionProofFact,
    HostRunIngressFact,
)
from pulsara_agent.primitives.long_horizon import (
    ChildRolloutSubaccountFact,
    RunLongHorizonContractFact,
)
from pulsara_agent.primitives.mcp import McpInstallationReferenceFact
from pulsara_agent.primitives.transcript_projection import (
    RunTranscriptSeedReferenceFact,
    RunTranscriptSeedSemanticFact,
)
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.permission_snapshot import RunPermissionSnapshot
from pulsara_agent.runtime.long_horizon.run_contract import (
    PreparedLongHorizonRunFacts,
)

if TYPE_CHECKING:
    from pulsara_agent.llm.control import RunModelCallControlOwner
    from pulsara_agent.runtime.agent import AgentRuntime


@dataclass(frozen=True, slots=True)
class CapabilityResolveBasis:
    fact: CapabilityResolveBasisFact
    user_input: str
    prior_messages: tuple[Msg, ...]
    active_skill_names: frozenset[str]
    workspace_root: Path
    memory_domain_id: str


@dataclass(slots=True)
class RunWorkingSet:
    """Process-local owner of one committed run's rebound execution inputs."""

    run_start_event_id: str
    run_start_sequence: int
    run_model_target: ResolvedModelTarget
    long_horizon_contract: RunLongHorizonContractFact
    run_transcript_seed_semantic: RunTranscriptSeedSemanticFact
    run_transcript_seed_reference: RunTranscriptSeedReferenceFact
    permission_snapshot: RunPermissionSnapshot
    plan_snapshot: PlanWorkflowStateFact
    capability_resolve_basis: CapabilityResolveBasis
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    original_exposure_plan: CapabilityExposurePlan | None
    original_exposure_fact: CapabilityExposureSnapshotFact | None
    original_exposure_event_ref: ContextEventReferenceFact | None
    effective_exposure_plan: CapabilityExposurePlan | None
    effective_exposure_fact: CapabilityExposureSnapshotFact | None
    effective_exposure_event_ref: ContextEventReferenceFact | None
    latest_committed_resume_boundary: InteractionResumeBoundaryFact | None
    latest_committed_resume_boundary_ref: ContextEventReferenceFact | None
    latest_validated_suspended_state_token_fingerprint: str | None = None
    run_execution_activation: RunExecutionActivationFact | None = None
    process_segment_id: str | None = None
    model_call_control_owner: RunModelCallControlOwner | None = None

    def install_initial_exposure(
        self,
        *,
        plan: CapabilityExposurePlan,
        fact: CapabilityExposureSnapshotFact,
        event_ref: ContextEventReferenceFact,
    ) -> None:
        if self.original_exposure_fact is not None:
            if (
                self.original_exposure_fact != fact
                or self.original_exposure_event_ref != event_ref
            ):
                raise RuntimeError("initial capability exposure already differs")
            return
        self.original_exposure_plan = plan
        self.original_exposure_fact = fact
        self.original_exposure_event_ref = event_ref
        self.effective_exposure_plan = plan
        self.effective_exposure_fact = fact
        self.effective_exposure_event_ref = event_ref

    def install_continuation(
        self,
        *,
        run_model_target: ResolvedModelTarget,
        permission_snapshot: RunPermissionSnapshot,
        plan: CapabilityExposurePlan,
        fact: CapabilityExposureSnapshotFact,
        event_ref: ContextEventReferenceFact,
        boundary: InteractionResumeBoundaryFact,
        boundary_ref: ContextEventReferenceFact,
        frozen_execution_surface: FrozenCapabilityExecutionSurface,
        validated_suspended_state_token_fingerprint: str,
    ) -> None:
        if self.original_exposure_fact is None:
            raise RuntimeError("continuation requires an initial capability exposure")
        self.run_model_target = run_model_target
        self.permission_snapshot = permission_snapshot
        self.effective_exposure_plan = plan
        self.effective_exposure_fact = fact
        self.effective_exposure_event_ref = event_ref
        self.latest_committed_resume_boundary = boundary
        self.latest_committed_resume_boundary_ref = boundary_ref
        if (
            validated_suspended_state_token_fingerprint
            != boundary.suspended_state_token_fingerprint
        ):
            raise RuntimeError("validated suspended token fingerprint mismatch")
        self.latest_validated_suspended_state_token_fingerprint = (
            validated_suspended_state_token_fingerprint
        )
        self.frozen_execution_surface = frozen_execution_surface


@dataclass(slots=True)
class AgentRunDraft:
    state: Any
    run_start_event: RunStartEvent
    current_user_message: CurrentUserMessageFact
    terminal_run_end_event_id: str
    capability_basis: CapabilityResolveBasisFact
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    prior_messages: tuple[Msg, ...]
    long_horizon: PreparedLongHorizonRunFacts
    run_transcript_seed: Any
    host_run_ingress: HostRunIngressFact | None
    host_ingress_admission_proof: HostIngressAdmissionProofFact | None


@dataclass(frozen=True, slots=True)
class CommittedHostRunEntry:
    run_start_event: RunStartEvent
    run_start_sequence: int
    committed_through_sequence: int
    publication_status: Literal["completed", "failed_after_commit", "unavailable"]
    boundary_id: str
    committed_audit_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommittedSubagentRunEntry:
    run_start_event: RunStartEvent
    run_start_sequence: int
    committed_through_sequence: int
    publication_status: Literal["completed", "failed_after_commit", "unavailable"]
    subagent_run_id: str


@dataclass(frozen=True, slots=True)
class PreparedSubagentRunEntry:
    entry_fact: SubagentRunEntryFact
    current_user_message: CurrentUserMessageFact
    run_model_target: ResolvedModelTarget
    permission_snapshot: RunPermissionSnapshot
    mcp_installation_fact: McpInstallationReferenceFact
    capability_basis: CapabilityResolveBasis
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    run_start_event_id: str
    terminal_run_end_event_id: str
    long_horizon: PreparedLongHorizonRunFacts
    child_rollout_subaccount: ChildRolloutSubaccountFact


CommittedRunEntry: TypeAlias = CommittedHostRunEntry | CommittedSubagentRunEntry


def install_run_working_set(
    state: LoopState,
    committed: CommittedRunEntry,
    *,
    plan_snapshot: PlanWorkflowStateFact,
    capability_resolve_basis: CapabilityResolveBasis,
    frozen_execution_surface: FrozenCapabilityExecutionSurface,
) -> RunWorkingSet:
    if state.run_model_target is None or state.permission_snapshot is None:
        raise RuntimeError("committed run lost target or permission snapshot")
    if committed.run_start_event.run_id != state.run_id:
        raise RuntimeError("committed run entry/state identity mismatch")
    working_set = RunWorkingSet(
        run_start_event_id=committed.run_start_event.id,
        run_start_sequence=committed.run_start_sequence,
        run_model_target=state.run_model_target,
        long_horizon_contract=committed.run_start_event.long_horizon,
        run_transcript_seed_semantic=(
            committed.run_start_event.run_transcript_seed_semantic
        ),
        run_transcript_seed_reference=(
            committed.run_start_event.run_transcript_seed_reference
        ),
        permission_snapshot=state.permission_snapshot,
        plan_snapshot=plan_snapshot,
        capability_resolve_basis=capability_resolve_basis,
        frozen_execution_surface=frozen_execution_surface,
        original_exposure_plan=None,
        original_exposure_fact=None,
        original_exposure_event_ref=None,
        effective_exposure_plan=None,
        effective_exposure_fact=None,
        effective_exposure_event_ref=None,
        latest_committed_resume_boundary=None,
        latest_committed_resume_boundary_ref=None,
    )
    state.run_working_set = working_set
    return working_set


async def prepare_agent_run_draft(
    agent: AgentRuntime,
    state: LoopState,
    *,
    run_model_target: ResolvedModelTarget,
    permission_snapshot: RunPermissionSnapshot,
    current_user_message: CurrentUserMessageFact,
    run_start_event_id: str,
    terminal_run_end_event_id: str,
    capability_basis: CapabilityResolveBasisFact,
    frozen_execution_surface: FrozenCapabilityExecutionSurface,
    new_run_boundary: NewRunBoundaryFact | None,
    subagent_run_entry: SubagentRunEntryFact | None,
    long_horizon: PreparedLongHorizonRunFacts,
    child_rollout_subaccount: ChildRolloutSubaccountFact | None,
    host_run_ingress: HostRunIngressFact | None,
    host_ingress_admission_proof: HostIngressAdmissionProofFact | None,
    prior_messages: list[Msg] | None = None,
) -> AgentRunDraft:
    """Freeze one RunStart candidate without granting AgentRuntime commit ownership."""

    if state.run_model_target is not None:
        if (
            state.run_model_target.fact.target_fingerprint
            != run_model_target.fact.target_fingerprint
        ):
            raise ValueError("active run model target cannot be replaced")
    else:
        state.run_model_target = run_model_target
    if state.permission_snapshot != permission_snapshot:
        raise RuntimeError("prepared run permission snapshot/state mismatch")
    state.messages.extend(
        message.model_copy(deep=True) for message in (prior_messages or [])
    )
    state.messages.append(
        UserMsg(
            name="user",
            content=current_user_message.text,
            id=current_user_message.message_id,
            metadata={"run_id": state.run_id},
            created_at=current_user_message.observed_at_utc,
        )
    )
    event_context = agent._event_context(state)
    if isinstance(new_run_boundary, NewRunBoundaryFact):
        if subagent_run_entry is not None:
            raise RuntimeError("host run entry cannot carry a child entry fact")
        run_entry_kind = RunEntryKind.HOST
    elif isinstance(subagent_run_entry, SubagentRunEntryFact):
        run_entry_kind = RunEntryKind.SUBAGENT_CHILD
        new_run_boundary = None
    else:
        raise RuntimeError(
            "run entry requires exactly one host boundary or subagent entry fact"
        )
    from pulsara_agent.runtime.authority_materialization import (
        prepare_authority_artifact_write_reservation,
        persist_prepared_run_transcript_seed,
        prepare_run_transcript_seed,
    )
    from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
        TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
    )

    projection_store = agent.runtime_session.transcript_projection_state_store
    projection_snapshot = projection_store.snapshot()
    if not projection_snapshot.checkpointable:
        raise RuntimeError("RunStart transcript seed requires a stable projection safe point")
    prepared_seed = prepare_run_transcript_seed(
        runtime_session_id=agent.runtime_session.runtime_session_id,
        stable_state=projection_snapshot.stable_semantic_state,
        stable_entries=projection_store.stable_entries(),
        ledger_through_sequence=projection_snapshot.ledger_through_sequence,
        ledger_continuity_accumulator=(
            projection_snapshot.ledger_continuity_accumulator
        ),
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            agent.runtime_session.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=(
            agent.runtime_session.transcript_projection_materialization_contracts
        ),
    )
    seed_deadline = monotonic() + (
        agent.runtime_session.authority_materialization_contracts.limits.checkpoint_operation_timeout_seconds
    )
    seed_write_reservation = prepare_authority_artifact_write_reservation(
        operation_id=f"run-seed:{state.run_id}",
        owner_kind="run_seed_materialization",
        artifacts=prepared_seed.artifacts,
        limits=agent.runtime_session.authority_materialization_contracts.limits,
        absolute_deadline_monotonic=seed_deadline,
    )
    await agent.runtime_session.context_input_io_service.execute(
        operation_name="run-transcript-seed-materialization",
        operation=lambda: persist_prepared_run_transcript_seed(
            prepared_seed,
            write_reservation=seed_write_reservation,
            limits=agent.runtime_session.authority_materialization_contracts.limits,
            archive=agent.runtime_session.archive,
            runtime_session_id=agent.runtime_session.runtime_session_id,
            deadline_monotonic=seed_deadline,
        ),
        deadline_monotonic=seed_deadline,
    )
    agent.runtime_session.transcript_projection_checkpoint_service.prepare_run_seed_artifacts(
        run_id=state.run_id,
        artifact_ids=frozenset(
            item.artifact_id for item in prepared_seed.artifacts
        ),
    )
    run_start = RunStartEvent(
        id=run_start_event_id,
        **event_context.event_fields(),
        created_at=utc_now(),
        user_input_chars=len(current_user_message.text),
        **permission_snapshot.to_event_fields(),
        model_target=run_model_target.fact,
        subagent_graph_reducer_contract=(
            agent.runtime_session.subagent_graph_checkpoint_service.reducer_binding.contract
        ),
        long_horizon=long_horizon.contract,
        child_rollout_subaccount=child_rollout_subaccount,
        mcp_installation_id=agent.runtime_session.mcp_installation_id,
        mcp_installation_owner_runtime_session_id=(
            agent.runtime_session.mcp_installation_owner_runtime_session_id
        ),
        run_entry_kind=run_entry_kind,
        current_user_message=current_user_message,
        host_run_ingress=host_run_ingress,
        host_ingress_admission_proof=host_ingress_admission_proof,
        run_transcript_seed_semantic=prepared_seed.seed_semantic,
        run_transcript_seed_reference=prepared_seed.seed_reference,
        terminal_run_end_event_id=terminal_run_end_event_id,
        new_run_boundary=new_run_boundary,
        subagent_run_entry=subagent_run_entry,
    )
    expected_basis = (
        new_run_boundary.capability_basis
        if isinstance(new_run_boundary, NewRunBoundaryFact)
        else capability_basis
    )
    if expected_basis != capability_basis:
        raise RuntimeError("prepared run capability basis mismatch")
    return AgentRunDraft(
        state=state,
        run_start_event=run_start,
        current_user_message=current_user_message,
        terminal_run_end_event_id=terminal_run_end_event_id,
        capability_basis=capability_basis,
        frozen_execution_surface=frozen_execution_surface,
        prior_messages=tuple(
            message.model_copy(deep=True) for message in (prior_messages or ())
        ),
        long_horizon=long_horizon,
        run_transcript_seed=prepared_seed,
        host_run_ingress=host_run_ingress,
        host_ingress_admission_proof=host_ingress_admission_proof,
    )


__all__ = [
    "AgentRunDraft",
    "CapabilityResolveBasis",
    "CommittedHostRunEntry",
    "CommittedRunEntry",
    "CommittedSubagentRunEntry",
    "PreparedSubagentRunEntry",
    "RunWorkingSet",
    "install_run_working_set",
    "prepare_agent_run_draft",
]
