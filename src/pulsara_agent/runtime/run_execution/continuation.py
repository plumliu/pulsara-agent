"""Process-local carriers for one committed interaction continuation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.capability.runtime import FrozenCapabilityExecutionSurface
from pulsara_agent.event import AgentEvent, RunStartEvent
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.mcp import McpInstallationReferenceFact
from pulsara_agent.primitives.run_boundary import (
    HostRunBoundaryDiagnostic,
    ResumeGatePolicy,
)
from pulsara_agent.primitives.run_entry import HostRunBoundaryIdentityFact
from pulsara_agent.ports.mcp import PreparedMcpInputRequiredResolution
from pulsara_agent.primitives.runtime_event_vocabulary import (
    RuntimeEventOperationDeadlineBudget,
)
from pulsara_agent.runtime.execution_handles import RunExecutionHandleSet
from pulsara_agent.runtime.permission_snapshot import RunPermissionSnapshot


@dataclass(frozen=True, slots=True)
class PreparedInteractionResumeBoundary:
    identity: HostRunBoundaryIdentityFact
    interaction_id: str
    interaction_kind: Literal["approval", "plan", "mcp_input_required"]
    suspended_state_token: str
    original_run_start_event: RunStartEvent
    rebound_model_target: Any
    permission_snapshot: RunPermissionSnapshot
    mcp_installation_fact: McpInstallationReferenceFact
    owned_continuation_exposure_plan: CapabilityExposurePlan
    continuation_exposure_fact: CapabilityExposureSnapshotFact
    frozen_execution_surface: FrozenCapabilityExecutionSurface
    incoming_execution_handles: RunExecutionHandleSet
    pending_mcp_audits: tuple[AgentEvent, ...]
    deadline_budget: RuntimeEventOperationDeadlineBudget
    gate_policy: ResumeGatePolicy
    diagnostics: tuple[HostRunBoundaryDiagnostic, ...]
    predecessor_authority_fingerprint: str
    expected_termination_revision: int
    expected_current_handle_id: str
    prepared_mcp_input_required_resolution: (
        PreparedMcpInputRequiredResolution | None
    ) = None
    mcp_input_required_resolution_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class CommittedInteractionResumeBoundary:
    prepared: PreparedInteractionResumeBoundary
    exposure_event_id: str
    exposure_event_sequence: int
    boundary_event_id: str
    boundary_event_sequence: int
    committed_audit_event_ids: tuple[str, ...]
    committed_through_sequence: int
    publication_status: Literal["completed", "failed_after_commit", "unavailable"]
    mcp_input_required_resolution_event_reference: ContextEventReferenceFact | None = (
        None
    )


__all__ = [
    "CommittedInteractionResumeBoundary",
    "PreparedInteractionResumeBoundary",
]
