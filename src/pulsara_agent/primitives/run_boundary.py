"""Event-safe Host run-boundary contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives.capability import CapabilityResolveBasisFact
from pulsara_agent.primitives.model_call import canonical_json_bytes
from pulsara_agent.primitives.permission import PresetPermissionPolicyFact
from pulsara_agent.primitives.run_entry import (
    HostRunBoundaryIdentityFact,
    SubagentRunEntryFact,
)


class HostRunBoundaryPhase(StrEnum):
    INGRESS = "ingress"
    ADMISSION = "admission"
    CONTRACT_RESOLUTION = "contract_resolution"
    RECOVERY_MAINTENANCE = "recovery_maintenance"
    MCP_REQUIRED_WAIT = "mcp_required_wait"
    MCP_INSTALLATION = "mcp_installation"
    TRANSCRIPT_SNAPSHOT = "transcript_snapshot"
    PREFLIGHT_COMPACTION = "preflight_compaction"
    FINAL_FREEZE = "final_freeze"
    DURABLE_COMMIT = "durable_commit"
    ACTIVATION = "activation"
    POST_COMMIT_INITIALIZATION = "post_commit_initialization"


class HostRunBoundaryDisposition(StrEnum):
    PROCEED = "proceed"
    PROCEED_DEGRADED = "proceed_degraded"
    RETRYABLE_BLOCK = "retryable_block"
    TERMINAL_BLOCK = "terminal_block"
    SESSION_LATCHED = "session_latched"
    COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"
    COMMITTED_BUT_PUBLICATION_FAILED = "committed_but_publication_failed"
    COMMITTED_EXECUTION_FAILED = "committed_execution_failed"


class HostRunBoundaryDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    severity: Literal["info", "warning", "error"]
    phase: HostRunBoundaryPhase
    disposition: HostRunBoundaryDisposition | None
    error_type: str | None = Field(default=None, max_length=128)
    message: str = Field(max_length=1024)
    metadata: dict[str, Any]

    @model_validator(mode="after")
    def _validate_metadata(self) -> "HostRunBoundaryDiagnostic":
        try:
            payload = canonical_json_bytes(self.metadata)
        except (TypeError, ValueError) as exc:
            raise ValueError("boundary diagnostic metadata must be strict JSON") from exc
        if len(payload) > 4096:
            raise ValueError("boundary diagnostic metadata exceeds bounded cap")
        return self


class BoundaryTranscriptSnapshotFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_through_sequence: int = Field(ge=0)
    source_event_count: int = Field(ge=0)
    compacted_window_id: str | None
    preflight_compaction_id: str | None
    preflight_compaction_terminal_event_id: str | None
    preflight_compaction_terminal_sequence: int | None

    @model_validator(mode="after")
    def _validate_compaction_branch(self) -> "BoundaryTranscriptSnapshotFact":
        attempt = self.preflight_compaction_id is not None
        terminal_fields = (
            self.preflight_compaction_terminal_event_id,
            self.preflight_compaction_terminal_sequence,
        )
        if attempt:
            if any(value is None for value in terminal_fields):
                raise ValueError("preflight compaction requires terminal attribution")
            if int(self.preflight_compaction_terminal_sequence or 0) < 1:
                raise ValueError("preflight compaction terminal sequence must be positive")
        elif any(value is not None for value in (*terminal_fields, self.compacted_window_id)):
            raise ValueError("non-attempted preflight cannot carry compaction facts")
        return self


class NewRunBoundaryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: HostRunBoundaryIdentityFact
    transcript: BoundaryTranscriptSnapshotFact
    model_target_fingerprint: str = Field(min_length=1)
    permission_snapshot_id: str = Field(min_length=1)
    mcp_installation_id: str = Field(min_length=1)
    capability_basis: CapabilityResolveBasisFact
    degraded_reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_boundary(self) -> "NewRunBoundaryFact":
        if self.identity.kind.value != "pre_run":
            raise ValueError("new run boundary requires PRE_RUN identity")
        if self.degraded_reason_codes != tuple(
            sorted(set(self.degraded_reason_codes))
        ):
            raise ValueError("degraded reason codes must be sorted and unique")
        if self.capability_basis.owner.owner_id != self.identity.boundary_id:
            raise ValueError("capability basis owner does not match new-run boundary")
        if self.capability_basis.permission_snapshot_id != self.permission_snapshot_id:
            raise ValueError("capability basis permission snapshot mismatch")
        if self.capability_basis.mcp_installation_id != self.mcp_installation_id:
            raise ValueError("capability basis MCP installation mismatch")
        return self


RunEntryFact: TypeAlias = NewRunBoundaryFact | SubagentRunEntryFact


class InteractionResumeBoundaryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: HostRunBoundaryIdentityFact
    original_run_start_event_id: str = Field(min_length=1)
    original_run_start_sequence: int = Field(ge=1)
    interaction_id: str = Field(min_length=1)
    interaction_kind: Literal["approval", "plan", "mcp_input_required"]
    suspended_state_token_fingerprint: str = Field(min_length=1)
    permission_snapshot_id: str = Field(min_length=1)
    model_target_fingerprint: str = Field(min_length=1)
    mcp_installation_id: str = Field(min_length=1)
    source_exposure_id: str = Field(min_length=1)
    source_exposure_semantic_fingerprint: str = Field(min_length=1)
    source_exposure_fact_fingerprint: str = Field(min_length=1)
    effective_exposure_id: str = Field(min_length=1)
    effective_exposure_semantic_fingerprint: str = Field(min_length=1)
    effective_exposure_fact_fingerprint: str = Field(min_length=1)
    exposure_transition: Literal["reused", "narrowed"]
    committed_mcp_audit_event_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_resume(self) -> "InteractionResumeBoundaryFact":
        if self.identity.kind.value != "pre_interaction_resume":
            raise ValueError("resume boundary requires PRE_INTERACTION_RESUME identity")
        if self.committed_mcp_audit_event_ids != tuple(
            sorted(set(self.committed_mcp_audit_event_ids))
        ):
            raise ValueError("committed MCP audit ids must be sorted and unique")
        same_semantic = (
            self.source_exposure_semantic_fingerprint
            == self.effective_exposure_semantic_fingerprint
        )
        if self.exposure_transition == "reused" and not same_semantic:
            raise ValueError("reused continuation must preserve exposure semantics")
        return self


class PlanWorkflowStateFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str | None
    active: bool
    revision: int = Field(ge=0)
    entered_event_id: str | None
    entered_event_sequence: int | None
    entry_run_id: str | None
    entry_turn_id: str | None
    entry_reply_id: str | None
    stored_default_permission: PresetPermissionPolicyFact
    accepted_plan_artifact_id: str | None

    @model_validator(mode="after")
    def _validate_state(self) -> "PlanWorkflowStateFact":
        required = (
            self.workflow_id,
            self.entered_event_id,
            self.entered_event_sequence,
            self.entry_run_id,
            self.entry_turn_id,
            self.entry_reply_id,
        )
        if self.active:
            if any(value is None for value in required):
                raise ValueError("active plan workflow requires entry attribution")
            if int(self.entered_event_sequence or 0) < 1:
                raise ValueError("plan entered event sequence must be positive")
        elif any(value is not None for value in required):
            raise ValueError("inactive plan workflow cannot carry entry attribution")
        return self


class ResumeGatePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    interaction_kind: Literal["approval", "plan", "mcp_input_required"]
    recheck_capability: bool
    recheck_binding: bool
    recheck_permission: bool
    permission_wait_behavior: Literal[
        "not_applicable", "already_confirmed", "allow_wait", "fail_closed_deny"
    ]

    @model_validator(mode="after")
    def _validate_mapping(self) -> "ResumeGatePolicy":
        expected = {
            "approval": (True, True, False, "already_confirmed"),
            "plan": (True, False, False, "not_applicable"),
            "mcp_input_required": (True, True, True, "fail_closed_deny"),
        }[self.interaction_kind]
        actual = (
            self.recheck_capability,
            self.recheck_binding,
            self.recheck_permission,
            self.permission_wait_behavior,
        )
        if actual != expected:
            raise ValueError("resume gate policy does not match interaction kind")
        return self


def resume_gate_policy_for(
    interaction_kind: Literal["approval", "plan", "mcp_input_required"],
) -> ResumeGatePolicy:
    values = {
        "approval": (True, True, False, "already_confirmed"),
        "plan": (True, False, False, "not_applicable"),
        "mcp_input_required": (True, True, True, "fail_closed_deny"),
    }[interaction_kind]
    return ResumeGatePolicy(
        interaction_kind=interaction_kind,
        recheck_capability=values[0],
        recheck_binding=values[1],
        recheck_permission=values[2],
        permission_wait_behavior=values[3],
    )


class BoundaryBatchCommitStatus(StrEnum):
    NONE = "none"
    FULL = "full"
    PARTIAL = "partial"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class BoundaryBatchConfirmation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BoundaryBatchCommitStatus
    candidate_event_ids: tuple[str, ...]
    committed_event_ids: tuple[str, ...]
    committed_sequences: tuple[int, ...]
    actual_last_sequence: int | None

    @model_validator(mode="after")
    def _validate_confirmation(self) -> "BoundaryBatchConfirmation":
        candidates = self.candidate_event_ids
        committed = self.committed_event_ids
        if not candidates or len(candidates) != len(set(candidates)):
            raise ValueError("candidate event ids must be non-empty and unique")
        if len(committed) != len(set(committed)):
            raise ValueError("committed event ids must be unique")
        if len(committed) != len(self.committed_sequences):
            raise ValueError("committed ids and sequences must have equal length")
        if any(sequence < 1 for sequence in self.committed_sequences):
            raise ValueError("committed sequences must be positive")
        if self.status is BoundaryBatchCommitStatus.FULL:
            if committed != candidates:
                raise ValueError("full confirmation must contain every candidate in order")
            if self.committed_sequences and self.committed_sequences != tuple(
                range(
                    self.committed_sequences[0],
                    self.committed_sequences[0] + len(self.committed_sequences),
                )
            ):
                raise ValueError("full confirmation sequences must be contiguous")
        elif self.status is BoundaryBatchCommitStatus.NONE:
            if committed or self.committed_sequences:
                raise ValueError("none confirmation cannot contain committed events")
        elif self.status is BoundaryBatchCommitStatus.PARTIAL:
            if not committed or len(committed) >= len(candidates):
                raise ValueError("partial confirmation requires a strict candidate subset")
        return self


__all__ = [
    "BoundaryBatchCommitStatus",
    "BoundaryBatchConfirmation",
    "BoundaryTranscriptSnapshotFact",
    "HostRunBoundaryDiagnostic",
    "HostRunBoundaryDisposition",
    "HostRunBoundaryPhase",
    "InteractionResumeBoundaryFact",
    "NewRunBoundaryFact",
    "PlanWorkflowStateFact",
    "ResumeGatePolicy",
    "RunEntryFact",
    "resume_gate_policy_for",
]
