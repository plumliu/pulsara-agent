"""Host run-boundary process-local ownership contracts.

The event-safe identities live in ``pulsara_agent.primitives``.  This module
owns only process-local attempts, live execution handles, and the stable
RunStart-to-RunEnd owner / replaceable execution-segment state machine.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from pulsara_agent.capability.runtime import FrozenCapabilityExecutionSurface
from pulsara_agent.event import AgentEvent
from pulsara_agent.message import Msg
from pulsara_agent.primitives.capability import (
    build_capability_resolve_basis,
)
from pulsara_agent.primitives.mcp import McpInstallationReferenceFact
from pulsara_agent.primitives.run_boundary import (
    BoundaryBatchConfirmation,
    BoundaryTranscriptSnapshotFact,
    HostRunBoundaryDiagnostic,
    HostRunBoundaryDisposition,
    HostRunBoundaryPhase,
    NewRunBoundaryFact,
    PlanWorkflowStateFact,
)
from pulsara_agent.primitives.run_entry import (
    CurrentUserMessageFact,
    DurableRunExistence,
    HostRunBoundaryIdentityFact,
    HostRunBoundaryKind,
    canonical_utc_timestamp,
)
from pulsara_agent.primitives.host_ingress import (
    HostIngressAdmissionProofFact,
    HostRunIngressFact,
)
from pulsara_agent.runtime.permission_snapshot import RunPermissionSnapshot
from pulsara_agent.runtime.execution_handles import (
    RunExecutionHandleSet,
    CapabilityExecutionBorrowAuthority,
    CapabilityExecutionBorrowTracker,
    CapabilityExecutionBorrowUnavailable,
)
from pulsara_agent.runtime.run_entry import (
    AgentRunDraft,
    CapabilityResolveBasis,
    CommittedHostRunEntry,
    CommittedRunEntry,
    CommittedSubagentRunEntry,
    PreparedSubagentRunEntry,
    RunWorkingSet,
)
from pulsara_agent.runtime.long_horizon.run_contract import (
    PreparedLongHorizonRunFacts,
)
from pulsara_agent.ports.run_execution import PreparedRunOwnerReservationKey
from pulsara_agent.runtime.run_execution.prepared import PreparedRunActivationOwner


@dataclass(frozen=True, slots=True)
class NewRunBoundaryInput:
    identity: HostRunBoundaryIdentityFact
    user_input: str
    active_skill_names: frozenset[str]
    host_session_id: str
    conversation_id: str
    ingress_owner: Any


@dataclass(frozen=True, slots=True)
class InteractionResumeBoundaryInput:
    identity: HostRunBoundaryIdentityFact
    interaction_id: str
    interaction_kind: Literal["approval", "plan", "mcp_input_required"]
    resolution: object
    suspended_state_token: str


def derive_continuation_basis(
    original: CapabilityResolveBasis,
    *,
    continuation_owner: Any,
    current_execution_surface: Any,
    basis_id: str,
) -> CapabilityResolveBasis:
    """Preserve the initial raw basis while replacing continuation attribution."""

    original_fact = original.fact
    fact = build_capability_resolve_basis(
        basis_id=basis_id,
        basis_kind="continuation",
        source_basis_id=original_fact.basis_id,
        source_basis_fingerprint=original_fact.basis_fingerprint,
        owner=continuation_owner,
        workspace_identity_fingerprint=(
            original_fact.workspace_identity_fingerprint
        ),
        memory_domain_id=original_fact.memory_domain_id,
        permission_snapshot_id=original_fact.permission_snapshot_id,
        plan_active=original_fact.plan_active,
        active_skill_names=original_fact.active_skill_names,
        user_intent_fingerprint=original_fact.user_intent_fingerprint,
        prior_transcript_fingerprint=original_fact.prior_transcript_fingerprint,
        mcp_installation_id=current_execution_surface.identity.mcp_installation_id,
        execution_surface_identity=current_execution_surface.identity,
    )
    return CapabilityResolveBasis(
        fact=fact,
        user_input=original.user_input,
        prior_messages=tuple(
            message.model_copy(deep=True) for message in original.prior_messages
        ),
        active_skill_names=original.active_skill_names,
        workspace_root=original.workspace_root,
        memory_domain_id=original.memory_domain_id,
    )


@dataclass(frozen=True, slots=True)
class PreparedNewRunBoundary:
    identity: HostRunBoundaryIdentityFact
    run_model_target: Any
    permission_snapshot: RunPermissionSnapshot
    plan_snapshot: PlanWorkflowStateFact
    mcp_installation_fact: McpInstallationReferenceFact
    owned_transcript_messages: tuple[Msg, ...]
    transcript_fact: BoundaryTranscriptSnapshotFact
    capability_basis: CapabilityResolveBasis
    current_user_message: CurrentUserMessageFact
    host_run_ingress: HostRunIngressFact
    host_ingress_admission_proof: HostIngressAdmissionProofFact
    ingress_owner: Any
    run_start_event_id: str
    terminal_run_end_event_id: str
    new_run_boundary: NewRunBoundaryFact
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    pending_mcp_audits: tuple[AgentEvent, ...]
    long_horizon: PreparedLongHorizonRunFacts
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class PreparedNewRunBoundaryAuthority:
    identity: HostRunBoundaryIdentityFact
    transcript: BoundaryTranscriptSnapshotFact
    mcp_installation: McpInstallationReferenceFact
    plan: PlanWorkflowStateFact
    capability_basis: CapabilityResolveBasis
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    host_run_ingress: HostRunIngressFact
    host_ingress_admission_proof: HostIngressAdmissionProofFact
    current_user_message: CurrentUserMessageFact
    terminal_run_end_event_id: str
    new_run_boundary: NewRunBoundaryFact


@dataclass(frozen=True, slots=True)
class HostRunBoundaryBlocked:
    identity: HostRunBoundaryIdentityFact
    phase: HostRunBoundaryPhase
    disposition: HostRunBoundaryDisposition
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]
    retry_after_utc: str | None

    def __post_init__(self) -> None:
        allowed = {
            HostRunBoundaryDisposition.RETRYABLE_BLOCK,
            HostRunBoundaryDisposition.TERMINAL_BLOCK,
            HostRunBoundaryDisposition.SESSION_LATCHED,
            HostRunBoundaryDisposition.COMMIT_OUTCOME_UNKNOWN,
        }
        if self.disposition not in allowed:
            raise ValueError("blocked boundary has a non-blocked disposition")
        if self.retry_after_utc is not None:
            canonical_utc_timestamp(self.retry_after_utc)


PrepareNewRunBoundaryResult: TypeAlias = PreparedNewRunBoundary | HostRunBoundaryBlocked


@dataclass(frozen=True, slots=True)
class CommittedNewRunBoundary:
    prepared: PreparedNewRunBoundary
    run_start_event_id: str
    run_start_sequence: int
    committed_audit_event_ids: tuple[str, ...]
    committed_through_sequence: int
    publication_status: Literal["completed", "failed_after_commit", "unavailable"]


@dataclass(frozen=True, slots=True)
class HostRunBoundaryAttemptOutcome:
    boundary_id: str
    disposition: HostRunBoundaryDisposition
    commit_confirmation: BoundaryBatchConfirmation | None
    durable_run_existence: DurableRunExistence
    terminal_event_id: str | None
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


@dataclass(slots=True)
class HostRunBoundaryAttempt:
    boundary_id: str
    kind: HostRunBoundaryKind
    phase: HostRunBoundaryPhase
    owner_task: asyncio.Task[object]
    draft_run_id: str
    prepared_authority: PreparedNewRunBoundaryAuthority | None
    run_owner_reservation_key: PreparedRunOwnerReservationKey | None
    execution_handles: RunExecutionHandleSet | None
    candidate_events: tuple[AgentEvent, ...]
    candidate_event_ids: tuple[str, ...]
    candidate_payload_fingerprints: tuple[str, ...]
    commit_state: Literal[
        "not_started",
        "commit_in_flight",
        "committed",
        "publication_failed",
        "commit_outcome_unknown",
        "ledger_latched",
    ]
    completion: asyncio.Future[HostRunBoundaryAttemptOutcome]
    commit_confirmation: BoundaryBatchConfirmation | None = None
    prepared_activation: PreparedRunActivationOwner | None = None
    observer: object | None = None


@dataclass(frozen=True, slots=True)
class HostBoundaryStoppedBeforeCommit:
    status: Literal["cancelled_before_run_start"]
    boundary_id: str
    draft_run_id: str
    durable_run_existence: Literal[DurableRunExistence.NONE]
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class HostBoundaryStopUncertain:
    status: Literal["commit_outcome_unknown", "ledger_latched"]
    boundary_id: str
    draft_run_id: str
    durable_run_existence: Literal[
        DurableRunExistence.UNKNOWN, DurableRunExistence.PARTIAL_UNTRUSTED
    ]
    commit_confirmation: BoundaryBatchConfirmation
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]


HostBoundaryStopResult: TypeAlias = (
    HostBoundaryStoppedBeforeCommit | HostBoundaryStopUncertain
)


__all__ = [
    "AgentRunDraft",
    "RunExecutionHandleSet",
    "CapabilityExecutionBorrowAuthority",
    "CapabilityExecutionBorrowTracker",
    "CapabilityExecutionBorrowUnavailable",
    "CapabilityResolveBasis",
    "CommittedHostRunEntry",
    "CommittedNewRunBoundary",
    "CommittedRunEntry",
    "CommittedSubagentRunEntry",
    "HostBoundaryStopResult",
    "HostBoundaryStopUncertain",
    "HostBoundaryStoppedBeforeCommit",
    "HostRunBoundaryAttempt",
    "HostRunBoundaryAttemptOutcome",
    "HostRunBoundaryBlocked",
    "InteractionResumeBoundaryInput",
    "NewRunBoundaryInput",
    "PrepareNewRunBoundaryResult",
    "PreparedNewRunBoundary",
    "PreparedNewRunBoundaryAuthority",
    "PreparedSubagentRunEntry",
    "RunWorkingSet",
    "derive_continuation_basis",
]
