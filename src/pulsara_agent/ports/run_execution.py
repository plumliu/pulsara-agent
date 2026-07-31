"""Low-level process-local run ownership contracts.

The models in this module are immutable identities and snapshots.  They do not
own event-log writes or physical tasks; concrete ownership lives under
``runtime.run_execution``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from pydantic import Field, NonNegativeInt, PositiveInt, model_validator

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.model_call import ModelTokenUsageFact
from pulsara_agent.primitives.run_boundary import (
    InteractionResumeBoundaryFact,
    NewRunBoundaryFact,
    RunExecutionActivationFact,
)
from pulsara_agent.primitives.run_entry import SubagentRunEntryFact
from pulsara_agent.primitives.run_lifecycle import RunStopReason
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredSuspensionFact,
)


Fingerprint: TypeAlias = str
RunLifecycle: TypeAlias = Literal[
    "initializing",
    "open",
    "suspended",
    "terminalizing",
    "terminal",
    "reconciliation_required",
]
ActivationPhase: TypeAlias = Literal[
    "safe_point", "model_step", "tool_batch", "suspending", "completed"
]
AttemptState: TypeAlias = Literal[
    "prepared", "committing", "full", "none", "unknown", "retired"
]
PendingInteractionKind: TypeAlias = Literal[
    "approval", "plan_question", "plan_exit", "mcp_input_required"
]


def _runtime_fingerprint(
    model: FrozenRuntimeStateBase,
    *,
    field_name: str,
    domain: str,
) -> None:
    expected = context_fingerprint(
        domain,
        model.model_dump(mode="json", exclude={field_name}),
    )
    if getattr(model, field_name) != expected:
        raise ValueError(f"{field_name} mismatch")


class PreparedRunOwnerReservationKey(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    runtime_session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    run_start_event_id: str = Field(min_length=1)
    reservation_key_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "PreparedRunOwnerReservationKey":
        _runtime_fingerprint(
            self,
            field_name="reservation_key_fingerprint",
            domain="prepared-run-owner-reservation:v1",
        )
        return self


def build_prepared_run_owner_reservation_key(
    *, runtime_session_id: str, run_id: str, run_start_event_id: str
) -> PreparedRunOwnerReservationKey:
    payload = {
        "schema_version": 1,
        "runtime_session_id": runtime_session_id,
        "run_id": run_id,
        "run_start_event_id": run_start_event_id,
    }
    return PreparedRunOwnerReservationKey(
        **payload,
        reservation_key_fingerprint=context_fingerprint(
            "prepared-run-owner-reservation:v1", payload
        ),
    )


class RunOwnerIdentity(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    runtime_session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    run_start_event_id: str = Field(min_length=1)
    run_start_sequence: PositiveInt
    owner_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "RunOwnerIdentity":
        _runtime_fingerprint(
            self,
            field_name="owner_fingerprint",
            domain="run-owner:v1",
        )
        return self


def build_run_owner_identity(
    *,
    reservation_key: PreparedRunOwnerReservationKey,
    run_start_sequence: int,
) -> RunOwnerIdentity:
    if run_start_sequence < 1:
        raise ValueError("RunOwnerIdentity requires a positive stored sequence")
    payload = {
        "schema_version": 1,
        "runtime_session_id": reservation_key.runtime_session_id,
        "run_id": reservation_key.run_id,
        "run_start_event_id": reservation_key.run_start_event_id,
        "run_start_sequence": run_start_sequence,
    }
    return RunOwnerIdentity(
        **payload,
        owner_fingerprint=context_fingerprint("run-owner:v1", payload),
    )


class HostRunBoundaryActivationSource(FrozenRuntimeStateBase):
    source_kind: Literal["host_run_boundary"] = "host_run_boundary"
    source_run_start_event_reference: ContextEventReferenceFact
    source_boundary: NewRunBoundaryFact
    source_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_source(self) -> "HostRunBoundaryActivationSource":
        if self.source_run_start_event_reference.event_id == "":
            raise ValueError("source RunStart reference is required")
        _runtime_fingerprint(
            self,
            field_name="source_fingerprint",
            domain="run-activation-source:host-run-boundary:v1",
        )
        return self


class HostResumeBoundaryActivationSource(FrozenRuntimeStateBase):
    source_kind: Literal["host_resume_boundary"] = "host_resume_boundary"
    source_resume_boundary_event_reference: ContextEventReferenceFact
    source_resume_boundary: InteractionResumeBoundaryFact
    source_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "HostResumeBoundaryActivationSource":
        _runtime_fingerprint(
            self,
            field_name="source_fingerprint",
            domain="run-activation-source:host-resume-boundary:v1",
        )
        return self


class SubagentRunStartActivationSource(FrozenRuntimeStateBase):
    source_kind: Literal["subagent_run_start"] = "subagent_run_start"
    source_run_start_event_reference: ContextEventReferenceFact
    source_entry: SubagentRunEntryFact
    source_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "SubagentRunStartActivationSource":
        _runtime_fingerprint(
            self,
            field_name="source_fingerprint",
            domain="run-activation-source:subagent-run-start:v1",
        )
        return self


RunActivationSource: TypeAlias = (
    HostRunBoundaryActivationSource
    | HostResumeBoundaryActivationSource
    | SubagentRunStartActivationSource
)


class RunActivationIdentity(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    durable_activation: RunExecutionActivationFact
    source: RunActivationSource = Field(discriminator="source_kind")
    activation_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_join(self) -> "RunActivationIdentity":
        activation = self.durable_activation
        source = self.source
        if activation.activation_owner_kind != source.source_kind:
            raise ValueError("activation source kind mismatch")
        if isinstance(source, HostRunBoundaryActivationSource):
            expected_owner = source.source_boundary.identity.boundary_id
            if (
                source.source_run_start_event_reference.event_id
                != self.owner_identity.run_start_event_id
            ):
                raise ValueError("initial activation RunStart mismatch")
        elif isinstance(source, HostResumeBoundaryActivationSource):
            expected_owner = source.source_resume_boundary_event_reference.event_id
            if source.source_resume_boundary.identity.run_id != self.owner_identity.run_id:
                raise ValueError("resume activation run mismatch")
        else:
            expected_owner = source.source_run_start_event_reference.event_id
            if expected_owner != self.owner_identity.run_start_event_id:
                raise ValueError("child activation RunStart mismatch")
        if activation.activation_owner_id != expected_owner:
            raise ValueError("activation owner ID mismatch")
        _runtime_fingerprint(
            self,
            field_name="activation_fingerprint",
            domain="run-activation:v1",
        )
        return self


@dataclass(frozen=True, slots=True)
class RunActivationInstallation:
    identity: RunActivationIdentity
    installation_reason: Literal["live_initial", "live_resume", "reopen_rebind"]


@dataclass(frozen=True, slots=True)
class RunSegmentInstallBlocked:
    reason: Literal[
        "termination_intent_present",
        "terminalization_started",
        "stale_activation_owner",
        "authority_not_ready",
        "resources_unbound",
    ]
    current_terminal_state: str
    termination_intent_id: str | None


@dataclass(frozen=True, slots=True)
class RunTerminationIntent:
    intent_id: str
    kind: Literal["user_stop", "host_teardown"]
    requested_at_utc: str
    requester_id: str
    target_segment_id: str | None
    target_segment_generation: int | None

    def __post_init__(self) -> None:
        from pulsara_agent.primitives.run_entry import canonical_utc_timestamp

        canonical_utc_timestamp(self.requested_at_utc)
        if (self.target_segment_id is None) != (
            self.target_segment_generation is None
        ):
            raise ValueError("termination target segment identity is all-or-none")
        if (
            self.target_segment_generation is not None
            and self.target_segment_generation < 1
        ):
            raise ValueError("termination target segment generation must be positive")


class PendingInteractionIdentity(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    interaction_kind: PendingInteractionKind
    interaction_id: str = Field(min_length=1)
    source_interaction_event_reference: ContextEventReferenceFact
    interaction_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "PendingInteractionIdentity":
        expected_event_type = {
            "approval": "REQUIRE_USER_CONFIRM",
            "plan_question": "PLAN_QUESTION_ASKED",
            "plan_exit": "PLAN_EXIT_REQUESTED",
            "mcp_input_required": "TOOL_EXECUTION_SUSPENDED",
        }[self.interaction_kind]
        source = self.source_interaction_event_reference
        if source.runtime_session_id != self.owner_identity.runtime_session_id:
            raise ValueError("pending interaction source ledger mismatch")
        if source.event_type != expected_event_type:
            raise ValueError("pending interaction source event type mismatch")
        _runtime_fingerprint(
            self,
            field_name="interaction_fingerprint",
            domain="pending-interaction:v1",
        )
        return self


class PendingApprovalAuthority(FrozenRuntimeStateBase):
    interaction_kind: Literal["approval"] = "approval"
    identity: PendingInteractionIdentity
    authority_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_authority(self) -> "PendingApprovalAuthority":
        if self.identity.interaction_kind != self.interaction_kind:
            raise ValueError("approval interaction identity mismatch")
        _runtime_fingerprint(
            self,
            field_name="authority_fingerprint",
            domain="pending-approval-authority:v1",
        )
        return self


class PendingPlanQuestionAuthority(FrozenRuntimeStateBase):
    interaction_kind: Literal["plan_question"] = "plan_question"
    identity: PendingInteractionIdentity
    authority_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_authority(self) -> "PendingPlanQuestionAuthority":
        if self.identity.interaction_kind != self.interaction_kind:
            raise ValueError("plan-question interaction identity mismatch")
        _runtime_fingerprint(
            self,
            field_name="authority_fingerprint",
            domain="pending-plan-question-authority:v1",
        )
        return self


class PendingPlanExitAuthority(FrozenRuntimeStateBase):
    interaction_kind: Literal["plan_exit"] = "plan_exit"
    identity: PendingInteractionIdentity
    authority_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_authority(self) -> "PendingPlanExitAuthority":
        if self.identity.interaction_kind != self.interaction_kind:
            raise ValueError("plan-exit interaction identity mismatch")
        _runtime_fingerprint(
            self,
            field_name="authority_fingerprint",
            domain="pending-plan-exit-authority:v1",
        )
        return self


class PendingMcpInputRequiredAuthority(FrozenRuntimeStateBase):
    interaction_kind: Literal["mcp_input_required"] = "mcp_input_required"
    identity: PendingInteractionIdentity
    suspension: McpInputRequiredSuspensionFact
    authority_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_authority(self) -> "PendingMcpInputRequiredAuthority":
        if self.identity.interaction_kind != self.interaction_kind:
            raise ValueError("MCP interaction identity mismatch")
        if self.suspension.interaction.interaction_id != self.identity.interaction_id:
            raise ValueError("MCP suspension interaction mismatch")
        _runtime_fingerprint(
            self,
            field_name="authority_fingerprint",
            domain="pending-mcp-input-authority:v1",
        )
        return self


PendingInteractionAuthority: TypeAlias = (
    PendingApprovalAuthority
    | PendingPlanQuestionAuthority
    | PendingPlanExitAuthority
    | PendingMcpInputRequiredAuthority
)


class UnboundRunResourceSlotIdentity(FrozenRuntimeStateBase):
    slot_kind: Literal["unbound"] = "unbound"
    reason: Literal[
        "reopen_initial_rebind_pending",
        "reopen_continuation_rebind_pending",
        "terminal_only_recovery",
    ]
    identity_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "UnboundRunResourceSlotIdentity":
        _runtime_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="run-resource-slot:unbound:v1",
        )
        return self


class HandleBackedRunResourceSlotIdentity(FrozenRuntimeStateBase):
    slot_kind: Literal["bound", "retiring", "closed_bound"]
    handle_id: str = Field(min_length=1)
    handle_generation: PositiveInt
    handle_owner_fingerprint: Fingerprint = Field(min_length=1)
    identity_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "HandleBackedRunResourceSlotIdentity":
        _runtime_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="run-resource-slot:handle-backed:v1",
        )
        return self


class ClosedNeverBoundRunResourceSlotIdentity(FrozenRuntimeStateBase):
    slot_kind: Literal["closed_never_bound"] = "closed_never_bound"
    identity_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ClosedNeverBoundRunResourceSlotIdentity":
        _runtime_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="run-resource-slot:closed-never-bound:v1",
        )
        return self


RunResourceSlotIdentity: TypeAlias = (
    UnboundRunResourceSlotIdentity
    | HandleBackedRunResourceSlotIdentity
    | ClosedNeverBoundRunResourceSlotIdentity
)


class NoRunActivationSlotIdentity(FrozenRuntimeStateBase):
    slot_kind: Literal["none"] = "none"
    identity_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "NoRunActivationSlotIdentity":
        _runtime_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="run-activation-slot:none:v1",
        )
        return self


class ActiveRunActivationSlotIdentity(FrozenRuntimeStateBase):
    slot_kind: Literal["active"] = "active"
    activation_identity: RunActivationIdentity
    activation_phase: ActivationPhase
    driver_generation: PositiveInt
    identity_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ActiveRunActivationSlotIdentity":
        _runtime_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="run-activation-slot:active:v1",
        )
        return self


RunActivationSlotIdentity: TypeAlias = (
    NoRunActivationSlotIdentity | ActiveRunActivationSlotIdentity
)


class NoRunSuspensionSlotIdentity(FrozenRuntimeStateBase):
    slot_kind: Literal["none"] = "none"
    identity_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "NoRunSuspensionSlotIdentity":
        _runtime_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="run-suspension-slot:none:v1",
        )
        return self


class ActiveRunSuspensionSlotIdentity(FrozenRuntimeStateBase):
    slot_kind: Literal["active"] = "active"
    pending_interaction_identity: PendingInteractionIdentity
    authority_fingerprint: Fingerprint = Field(min_length=1)
    resource_kind: PendingInteractionKind
    resource_generation: PositiveInt
    resource_identity_fingerprint: Fingerprint = Field(min_length=1)
    identity_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ActiveRunSuspensionSlotIdentity":
        if self.resource_kind != self.pending_interaction_identity.interaction_kind:
            raise ValueError("suspension resource kind mismatch")
        _runtime_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="run-suspension-slot:active:v1",
        )
        return self


RunSuspensionSlotIdentity: TypeAlias = (
    NoRunSuspensionSlotIdentity | ActiveRunSuspensionSlotIdentity
)


class RunFinalizationSlotIdentity(FrozenRuntimeStateBase):
    slot_state: Literal[
        "empty",
        "active",
        "run_end_full_pending_output",
        "completed",
        "reconciliation_required",
    ]
    owner_or_receipt_id: str | None
    owner_or_receipt_fingerprint: Fingerprint | None
    stable_candidate_id: str | None
    stable_candidate_fingerprint: Fingerprint | None
    identity_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_matrix(self) -> "RunFinalizationSlotIdentity":
        owner_pair = (
            self.owner_or_receipt_id is not None
            and self.owner_or_receipt_fingerprint is not None
        )
        candidate_pair = (
            self.stable_candidate_id is not None
            and self.stable_candidate_fingerprint is not None
        )
        if (self.owner_or_receipt_id is None) != (
            self.owner_or_receipt_fingerprint is None
        ) or (self.stable_candidate_id is None) != (
            self.stable_candidate_fingerprint is None
        ):
            raise ValueError("finalization identity fields are pairwise")
        expected = {
            "empty": (False, False),
            "active": (True, True),
            "run_end_full_pending_output": (True, True),
            "completed": (True, False),
            "reconciliation_required": (True, True),
        }[self.slot_state]
        if (owner_pair, candidate_pair) != expected:
            raise ValueError("finalization slot matrix mismatch")
        _runtime_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="run-finalization-slot:v1",
        )
        return self


class RunOwnerStateIdentity(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    lifecycle: RunLifecycle
    authority_head_fingerprint: Fingerprint = Field(min_length=1)
    resource_slot: RunResourceSlotIdentity
    retiring_resource_identities: tuple[HandleBackedRunResourceSlotIdentity, ...]
    retiring_resource_accumulator: Fingerprint = Field(min_length=1)
    activation_slot: RunActivationSlotIdentity
    suspension_slot: RunSuspensionSlotIdentity
    finalization_slot: RunFinalizationSlotIdentity
    progress_generation: NonNegativeInt
    termination_revision: NonNegativeInt
    state_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_state(self) -> "RunOwnerStateIdentity":
        ordered = tuple(
            sorted(
                self.retiring_resource_identities,
                key=lambda item: (item.handle_generation, item.handle_id),
            )
        )
        if self.retiring_resource_identities != ordered or any(
            item.slot_kind != "retiring" for item in ordered
        ):
            raise ValueError("retiring resource identities are not canonical")
        expected_accumulator = context_fingerprint(
            "run-retiring-resource-set:v1",
            tuple(item.identity_fingerprint for item in ordered),
        )
        if self.retiring_resource_accumulator != expected_accumulator:
            raise ValueError("retiring resource accumulator mismatch")
        active = self.activation_slot.slot_kind == "active"
        suspended = self.suspension_slot.slot_kind == "active"
        if self.lifecycle == "open" and (not active or suspended):
            raise ValueError("open run requires exactly one active activation")
        if self.lifecycle == "suspended" and (active or not suspended):
            raise ValueError("suspended run requires exactly one suspension")
        if self.lifecycle == "initializing" and (active or suspended):
            raise ValueError("initializing run cannot expose execution slots")
        if self.lifecycle == "terminal" and (active or suspended):
            raise ValueError("terminal run cannot expose execution slots")
        _runtime_fingerprint(
            self,
            field_name="state_fingerprint",
            domain="run-owner-state:v1",
        )
        return self


ReconciliationAttemptKind: TypeAlias = Literal[
    "initial_authority_commit",
    "continuation_authority_commit",
    "activation_installation",
    "suspension_commit",
    "interaction_resolution_commit",
    "run_end_commit",
    "final_output_materialization",
    "resource_rebind",
    "publication_terminal_maintenance",
]
ClosedReconciliationDiagnosticCode: TypeAlias = Literal[
    "stored_candidate_conflict",
    "stored_authority_shape_conflict",
    "resident_owner_identity_mismatch",
    "physical_operation_deadline_exceeded",
    "ledger_confirmation_unavailable",
    "resource_rebind_unavailable",
]


class LedgerHorizonFact(FrozenRuntimeStateBase):
    through_sequence: NonNegativeInt
    continuity_accumulator: Fingerprint = Field(min_length=1)
    horizon_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "LedgerHorizonFact":
        _runtime_fingerprint(
            self,
            field_name="horizon_fingerprint",
            domain="run-ledger-horizon:v1",
        )
        return self


class PreparedRunOwnerReservationReconciliationSnapshot(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    reservation_key: PreparedRunOwnerReservationKey
    boundary_attempt_generation: PositiveInt
    stable_candidate_event_ids: tuple[str, ...]
    stable_candidate_batch_fingerprint: Fingerprint = Field(min_length=1)
    boundary_handle_id: str = Field(min_length=1)
    boundary_handle_generation: PositiveInt
    expected_ledger_horizon: LedgerHorizonFact
    snapshot_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "PreparedRunOwnerReservationReconciliationSnapshot":
        if not self.stable_candidate_event_ids:
            raise ValueError("boundary reconciliation candidate batch is empty")
        _runtime_fingerprint(
            self,
            field_name="snapshot_fingerprint",
            domain="prepared-run-owner-reconciliation:v1",
        )
        return self


class RunReconciliationSnapshot(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    repair_mode: Literal["live_resident", "reopen_recovery"]
    prior_state: RunOwnerStateIdentity
    active_attempt_kind: ReconciliationAttemptKind
    stable_candidate_id: str = Field(min_length=1)
    stable_candidate_fingerprint: Fingerprint = Field(min_length=1)
    expected_ledger_horizon: LedgerHorizonFact
    resident_owner_generation: PositiveInt | None
    snapshot_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_mode(self) -> "RunReconciliationSnapshot":
        if (self.repair_mode == "live_resident") != (
            self.resident_owner_generation is not None
        ):
            raise ValueError("reconciliation resident generation mismatch")
        _runtime_fingerprint(
            self,
            field_name="snapshot_fingerprint",
            domain="run-reconciliation-snapshot:v1",
        )
        return self


class ReconciliationFullConfirmation(FrozenRuntimeStateBase):
    disposition: Literal["full"] = "full"
    stored_candidate_id: str = Field(min_length=1)
    stored_candidate_fingerprint: Fingerprint = Field(min_length=1)
    exact_event_references: tuple[ContextEventReferenceFact, ...]
    observed_ledger_horizon: LedgerHorizonFact
    confirmation_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ReconciliationFullConfirmation":
        if not self.exact_event_references:
            raise ValueError("FULL reconciliation requires exact event references")
        _runtime_fingerprint(
            self,
            field_name="confirmation_fingerprint",
            domain="run-reconciliation-confirmation:full:v1",
        )
        return self


class ReconciliationNoneConfirmation(FrozenRuntimeStateBase):
    disposition: Literal["none"] = "none"
    observed_ledger_horizon: LedgerHorizonFact
    confirmation_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ReconciliationNoneConfirmation":
        _runtime_fingerprint(
            self,
            field_name="confirmation_fingerprint",
            domain="run-reconciliation-confirmation:none:v1",
        )
        return self


class ReconciliationConflictConfirmation(FrozenRuntimeStateBase):
    disposition: Literal["conflict"] = "conflict"
    conflicting_authority_references: tuple[ContextEventReferenceFact, ...]
    diagnostic_code: ClosedReconciliationDiagnosticCode
    confirmation_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ReconciliationConflictConfirmation":
        _runtime_fingerprint(
            self,
            field_name="confirmation_fingerprint",
            domain="run-reconciliation-confirmation:conflict:v1",
        )
        return self


class ReconciliationUnresolvedConfirmation(FrozenRuntimeStateBase):
    disposition: Literal["unresolved"] = "unresolved"
    diagnostic_code: ClosedReconciliationDiagnosticCode
    confirmation_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ReconciliationUnresolvedConfirmation":
        _runtime_fingerprint(
            self,
            field_name="confirmation_fingerprint",
            domain="run-reconciliation-confirmation:unresolved:v1",
        )
        return self


ReconciliationConfirmation: TypeAlias = (
    ReconciliationFullConfirmation
    | ReconciliationNoneConfirmation
    | ReconciliationConflictConfirmation
    | ReconciliationUnresolvedConfirmation
)


class ReconciliationResolutionReceipt(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    snapshot_fingerprint: Fingerprint = Field(min_length=1)
    physical_attempt_generation: PositiveInt
    confirmation: ReconciliationConfirmation = Field(discriminator="disposition")
    resulting_state: RunOwnerStateIdentity
    retry_owner_retained: bool
    receipt_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "ReconciliationResolutionReceipt":
        _runtime_fingerprint(
            self,
            field_name="receipt_fingerprint",
            domain="run-reconciliation-resolution:v1",
        )
        return self


class RunProgressSnapshot(FrozenRuntimeStateBase):
    state_identity: RunOwnerStateIdentity
    turn_index: NonNegativeInt
    reply_index: NonNegativeInt
    model_call_index: NonNegativeInt
    accumulated_usage: ModelTokenUsageFact
    latest_context_reference: ContextEventReferenceFact | None
    snapshot_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "RunProgressSnapshot":
        _runtime_fingerprint(
            self,
            field_name="snapshot_fingerprint",
            domain="run-progress-snapshot:v1",
        )
        return self


class RunFinalOutputView(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    status: Literal["finished", "failed", "aborted"]
    stop_reason: RunStopReason
    final_text: str | None
    ordered_message_references: tuple[ContextEventReferenceFact, ...]
    usage: ModelTokenUsageFact
    tool_call_count: NonNegativeInt
    output_fingerprint: Fingerprint = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "RunFinalOutputView":
        _runtime_fingerprint(
            self,
            field_name="output_fingerprint",
            domain="run-final-output-view:v1",
        )
        return self


class RunSuspendedOutcome(FrozenRuntimeStateBase):
    outcome_kind: Literal["suspended"] = "suspended"
    owner_identity: RunOwnerIdentity
    activation_identity: RunActivationIdentity
    authority_revision_fingerprint: Fingerprint
    source_interaction_event_reference: ContextEventReferenceFact
    pending_interaction: PendingInteractionAuthority = Field(
        discriminator="interaction_kind"
    )
    progress: RunProgressSnapshot


class RunTerminalOutcome(FrozenRuntimeStateBase):
    outcome_kind: Literal["terminal"] = "terminal"
    owner_identity: RunOwnerIdentity
    run_end_event_reference: ContextEventReferenceFact
    output: RunFinalOutputView
    finalization_receipt_fingerprint: Fingerprint


class RunTerminalizationPending(FrozenRuntimeStateBase):
    outcome_kind: Literal["terminalization_pending"] = "terminalization_pending"
    owner_identity: RunOwnerIdentity
    finalization_owner_fingerprint: Fingerprint
    stable_terminal_candidate_id: str
    stable_terminal_candidate_fingerprint: Fingerprint
    attempt_state: AttemptState


class RunTerminalOutputPending(FrozenRuntimeStateBase):
    outcome_kind: Literal["terminal_output_pending"] = "terminal_output_pending"
    owner_identity: RunOwnerIdentity
    run_end_event_reference: ContextEventReferenceFact
    materialization_owner_fingerprint: Fingerprint


class RunReconciliationRequired(FrozenRuntimeStateBase):
    outcome_kind: Literal["reconciliation_required"] = "reconciliation_required"
    fault_domain: Literal[
        "boundary", "authority", "activation", "interaction", "terminalization", "output"
    ]
    owner_identity: RunOwnerIdentity
    stable_owner_fingerprint: Fingerprint
    ledger_horizon: LedgerHorizonFact
    diagnostic_code: ClosedReconciliationDiagnosticCode


RunActivationOutcome: TypeAlias = (
    RunSuspendedOutcome
    | RunTerminalOutcome
    | RunTerminalizationPending
    | RunTerminalOutputPending
    | RunReconciliationRequired
)


class RunObserver(Protocol):
    async def __anext__(self) -> object: ...

    async def aclose(self) -> None: ...


class RunHandle(Protocol):
    @property
    def identity(self) -> RunOwnerIdentity: ...

    async def wait_activation(
        self, activation_generation: int
    ) -> RunActivationOutcome: ...

    async def wait_run_completion(self) -> RunTerminalOutcome: ...

    def subscribe(self, *, from_cursor: int | None = None) -> RunObserver: ...

    async def request_stop(self, intent: object) -> object: ...


__all__ = [
    "ActivationPhase",
    "ActiveRunActivationSlotIdentity",
    "ActiveRunSuspensionSlotIdentity",
    "AttemptState",
    "ClosedNeverBoundRunResourceSlotIdentity",
    "ClosedReconciliationDiagnosticCode",
    "HandleBackedRunResourceSlotIdentity",
    "HostResumeBoundaryActivationSource",
    "HostRunBoundaryActivationSource",
    "LedgerHorizonFact",
    "NoRunActivationSlotIdentity",
    "NoRunSuspensionSlotIdentity",
    "PendingInteractionIdentity",
    "PendingInteractionAuthority",
    "PendingInteractionKind",
    "PendingApprovalAuthority",
    "PendingMcpInputRequiredAuthority",
    "PendingPlanExitAuthority",
    "PendingPlanQuestionAuthority",
    "PreparedRunOwnerReservationKey",
    "PreparedRunOwnerReservationReconciliationSnapshot",
    "ReconciliationAttemptKind",
    "ReconciliationConfirmation",
    "ReconciliationResolutionReceipt",
    "RunActivationIdentity",
    "RunActivationInstallation",
    "RunActivationOutcome",
    "RunActivationSlotIdentity",
    "RunActivationSource",
    "RunFinalOutputView",
    "RunFinalizationSlotIdentity",
    "RunHandle",
    "RunLifecycle",
    "RunObserver",
    "RunOwnerIdentity",
    "RunOwnerStateIdentity",
    "RunProgressSnapshot",
    "RunReconciliationRequired",
    "RunReconciliationSnapshot",
    "RunResourceSlotIdentity",
    "RunSegmentInstallBlocked",
    "RunSuspendedOutcome",
    "RunSuspensionSlotIdentity",
    "RunTerminalOutcome",
    "RunTerminalOutputPending",
    "RunTerminalizationPending",
    "RunTerminationIntent",
    "SubagentRunStartActivationSource",
    "UnboundRunResourceSlotIdentity",
    "build_prepared_run_owner_reservation_key",
    "build_run_owner_identity",
]
