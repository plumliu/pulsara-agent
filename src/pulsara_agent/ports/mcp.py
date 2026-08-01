"""Process-local MCP execution, suspension, and settlement boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from collections.abc import Sequence
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from pulsara_agent.event import EventContext
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import (
    ToolExecutionPhysicalOwnerHandoffReceipt,
    ToolExecutionStableCandidateCommitReceipt,
    ToolExecutionStableCandidateOwnerIdentity,
    ToolResultArtifactCandidate,
)
from pulsara_agent.ports.event_write import FrozenEventWriteCandidate
from pulsara_agent.ports.tool_registry import McpToolBindingContract
from pulsara_agent.ports.tool_result_semantics import ToolResultSemanticsRuntimeInput
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.mcp import (
    McpBindingIdentityFact,
    FrozenMcpJsonDict,
    freeze_mcp_json_value,
)
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationCompanionKind,
    McpContinuationCompanionPlanFact,
    McpContinuationDispatchReservationFact,
    McpContinuationResolutionCarrierFact,
    McpInputRequiredDurableContinuationFact,
    McpInputRequiredResolutionSemanticFact,
)
from pulsara_agent.ports.mcp_secret import (
    McpFrozenRoundInputResponses,
    McpReplayReadyCarrierPlaintext,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredInteractionSemanticFact,
    McpInputRequiredRequestEnvelopeFact,
    McpInputRequiredSuspensionFact,
    McpPendingLeaseReservationIdentityFact,
)


class McpToolRejectCode(StrEnum):
    BINDING_UNAVAILABLE = "binding_unavailable"
    BINDING_IDENTITY_MISMATCH = "binding_identity_mismatch"
    LEASE_ACQUIRE_FAILED = "lease_acquire_failed"
    PENDING_LEASE_BORROW_FAILED = "pending_lease_borrow_failed"
    RESOLUTION_IDENTITY_MISMATCH = "resolution_identity_mismatch"
    REQUEST_TIMEOUT = "request_timeout"
    PROTOCOL_ERROR = "protocol_error"
    RESULT_LOWERING_FAILED = "result_lowering_failed"
    ADAPTER_ERROR = "adapter_error"


@dataclass(frozen=True, slots=True)
class McpPreparedCompanionIdentity:
    companion_id: str
    companion_kind: McpContinuationCompanionKind
    plan_fingerprint: str
    issuer_id: str
    issuer_generation: int
    ordered_candidate_event_ids: tuple[str, ...]
    ordered_candidate_schema_binding_fingerprints: tuple[str, ...]
    ordered_candidate_payload_fingerprints: tuple[str, ...]
    exact_ordered_batch_fingerprint: str
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if not self.companion_id or not self.issuer_id or self.issuer_generation < 1:
            raise ValueError("MCP companion identity is incomplete")
        count = len(self.ordered_candidate_event_ids)
        if (
            count == 0
            or len(self.ordered_candidate_schema_binding_fingerprints) != count
            or len(self.ordered_candidate_payload_fingerprints) != count
            or len(set(self.ordered_candidate_event_ids)) != count
        ):
            raise ValueError("MCP companion candidate identity is malformed")
        _validate_fingerprint(
            self,
            "identity_fingerprint",
            "mcp-prepared-companion-identity:v1",
        )


@runtime_checkable
class McpContinuationTransactionAuthority(Protocol):
    @property
    def companion_kind(self) -> McpContinuationCompanionKind: ...

    @property
    def charged_payload_bytes(self) -> int: ...

    @property
    def charge_contract_fingerprint(self) -> str: ...


@runtime_checkable
class McpPreparedContinuationCompanion(
    McpContinuationTransactionAuthority,
    Protocol,
):
    @property
    def identity(self) -> McpPreparedCompanionIdentity: ...

    @property
    def plan(self) -> McpContinuationCompanionPlanFact: ...


@runtime_checkable
class McpContinuationTransactionIntent(
    McpContinuationTransactionAuthority,
    Protocol,
):
    """Stable storage mutation awaiting the writer's exact complete batch."""

    def bind_candidate_batch(
        self,
        candidates: Sequence[FrozenEventWriteCandidate],
    ) -> McpPreparedContinuationCompanion: ...

    @property
    def storage_mutation_plan_fingerprint(self) -> str: ...


@dataclass(frozen=True, slots=True)
class PreparedMcpInputRequiredResolution:
    """Secret-safe process carrier for one exact all-item resolution batch."""

    source_suspension_event_reference: ContextEventReferenceFact
    source_suspension_fact_fingerprint: str
    interaction_id: str
    resolution_semantic: McpInputRequiredResolutionSemanticFact
    resolution_carrier: McpContinuationResolutionCarrierFact
    transaction_companion: McpContinuationTransactionIntent = field(repr=False)
    sealed_responses: McpFrozenRoundInputResponses = field(repr=False)
    batch_owner_id: str
    prepared_resolution_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.resolution_carrier.source_suspension_event_reference
            != self.source_suspension_event_reference
            or self.resolution_carrier.resolution_event_id == ""
            or self.resolution_semantic.request_set_fingerprint
            != self.resolution_carrier.request_set_fingerprint
            or self.resolution_semantic.ordered_response_keys
            != self.resolution_carrier.ordered_response_keys
            or self.resolution_semantic.keyed_current_round_responses_commitment
            != self.resolution_carrier.keyed_current_round_responses_commitment
            or self.resolution_semantic.response_attribution_fingerprint
            != self.resolution_carrier.response_attribution_fingerprint
            or self.sealed_responses.request_set_fingerprint
            != self.resolution_semantic.request_set_fingerprint
            or self.sealed_responses.ordered_request_keys
            != self.resolution_semantic.ordered_response_keys
        ):
            raise ValueError("MCP prepared resolution authority mismatch")
        expected = context_fingerprint(
            "prepared-mcp-input-required-resolution:v2",
            {
                "source_suspension_event_reference": (
                    self.source_suspension_event_reference.model_dump(mode="json")
                ),
                "source_suspension_fact_fingerprint": (
                    self.source_suspension_fact_fingerprint
                ),
                "interaction_id": self.interaction_id,
                "resolution_semantic_fingerprint": (
                    self.resolution_semantic.resolution_semantic_fingerprint
                ),
                "resolution_carrier_fact_fingerprint": (
                    self.resolution_carrier.resolution_carrier_fact_fingerprint
                ),
                "storage_mutation_plan_fingerprint": (
                    self.transaction_companion.storage_mutation_plan_fingerprint
                ),
                "batch_owner_id": self.batch_owner_id,
            },
        )
        if self.prepared_resolution_fingerprint != expected:
            raise ValueError("MCP prepared resolution fingerprint mismatch")

    def __reduce__(self):
        raise TypeError("prepared MCP resolution is process-local")


@dataclass(frozen=True, slots=True)
class McpDispatchReservationCommitGuard:
    runtime_session_id: str
    interaction_id: str
    tool_call_id: str
    physical_operation_id: str
    physical_reservation_event_reference: ContextEventReferenceFact
    physical_reservation_fingerprint: str
    guard_generation: int
    guard_fingerprint: str

    def __post_init__(self) -> None:
        payload = asdict(self)
        actual = payload.pop("guard_fingerprint")
        if self.guard_generation < 1 or actual != context_fingerprint(
            "mcp-dispatch-reservation-commit-guard:v1", payload
        ):
            raise ValueError("MCP dispatch commit guard identity mismatch")


@dataclass(frozen=True, slots=True)
class PreparedMcpContinuationDispatch:
    dispatch_event_id: str
    dispatch_reservation: McpContinuationDispatchReservationFact
    transaction_companion: McpContinuationTransactionIntent = field(repr=False)
    replay_plaintext: McpReplayReadyCarrierPlaintext = field(repr=False)
    commit_guard: McpDispatchReservationCommitGuard
    prepared_dispatch_fingerprint: str

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "prepared-mcp-continuation-dispatch:v1",
            {
                "dispatch_event_id": self.dispatch_event_id,
                "dispatch_reservation_fingerprint": (
                    self.dispatch_reservation.dispatch_reservation_fingerprint
                ),
                "storage_mutation_plan_fingerprint": (
                    self.transaction_companion.storage_mutation_plan_fingerprint
                ),
                "commit_guard_fingerprint": self.commit_guard.guard_fingerprint,
            },
        )
        if self.prepared_dispatch_fingerprint != expected:
            raise ValueError("prepared MCP dispatch fingerprint mismatch")

    def __reduce__(self):
        raise TypeError("prepared MCP dispatch is process-local")


@dataclass(frozen=True, slots=True)
class McpConfirmedContinuationDispatchReceipt:
    dispatch_event_id: str
    source_resolution_event_id: str
    replay_continuation_carrier_id: str
    runtime_session_id: str
    interaction_id: str
    round_ordinal: int
    physical_operation_id: str
    resulting_control_revision: int
    sdk_client_generation_id: str
    operation_expires_at_utc: str
    receipt_fingerprint: str

    def __post_init__(self) -> None:
        payload = asdict(self)
        actual = payload.pop("receipt_fingerprint")
        if (
            self.round_ordinal < 1
            or self.resulting_control_revision < 1
            or actual
            != context_fingerprint(
                "mcp-confirmed-continuation-dispatch-receipt:v1", payload
            )
        ):
            raise ValueError("MCP dispatch receipt identity mismatch")


@dataclass(frozen=True, slots=True)
class McpInvocationOwner:
    runtime_session_id: str
    run_id: str
    tool_call_id: str
    event_context: EventContext

    def __post_init__(self) -> None:
        if self.event_context.run_id != self.run_id:
            raise ValueError("MCP invocation owner run identity mismatch")
        if not self.runtime_session_id or not self.tool_call_id:
            raise ValueError("MCP invocation owner identity is required")


@dataclass(frozen=True, slots=True)
class McpToolExecutionRequest:
    owner: McpInvocationOwner
    exposed_tool_name: str
    original_tool_name: str
    binding: McpToolBindingContract
    frozen_arguments: FrozenMcpJsonDict
    timeout_ms: int
    request_fingerprint: str

    def __post_init__(self) -> None:
        if self.binding.tool_name != self.exposed_tool_name:
            raise ValueError("MCP request exposed tool identity mismatch")
        if self.binding.original_tool_name != self.original_tool_name:
            raise ValueError("MCP request original tool identity mismatch")
        if self.owner.tool_call_id == "" or self.timeout_ms < 1:
            raise ValueError("MCP request bounds are invalid")
        if self.request_fingerprint != _execution_request_fingerprint(self):
            raise ValueError("MCP execution request fingerprint mismatch")


class McpPendingHandleState(StrEnum):
    PREPARED_SUSPENSION = "prepared_suspension"
    SUSPENSION_COMMIT_IN_FLIGHT = "suspension_commit_in_flight"
    PENDING_CONFIRMED = "pending_confirmed"
    RESOLUTION_COMMIT_IN_FLIGHT = "resolution_commit_in_flight"
    REPLAY_READY = "replay_ready"
    DISPATCH_COMMIT_IN_FLIGHT = "dispatch_commit_in_flight"
    DISPATCH_RESERVED = "dispatch_reserved"
    RESUME_IN_FLIGHT = "resume_in_flight"
    RESUME_RESULT_RECEIVED = "resume_result_received"
    SUCCESSOR_SUSPENSION_FROZEN = "successor_suspension_frozen"
    TERMINAL_RESULT_FROZEN = "terminal_result_frozen"
    TERMINAL_CANDIDATE_FROZEN = "terminal_candidate_frozen"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    ABORTED = "aborted"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class McpPendingExecutionHandleIdentity:
    handle_id: str
    interaction_id: str
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    prepared_suspension_fingerprint: str
    predecessor_handle_id: str | None
    handle_generation: int
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if self.handle_generation < 1:
            raise ValueError("MCP handle generation must be positive")
        if (
            self.pending_lease_reservation.interaction_id != self.interaction_id
            or self.pending_lease_reservation.binding_identity != self.binding_identity
        ):
            raise ValueError("MCP handle reservation identity mismatch")
        _validate_fingerprint(
            self,
            "identity_fingerprint",
            "mcp-pending-execution-handle-identity:v1",
        )


@dataclass(frozen=True, slots=True)
class McpStatelessRecoveryRebindReceipt:
    """Process-local proof that an old suspension targets the current slot.

    Slot identity is an occurrence and therefore may change across process
    generations. Snapshot identity is semantic and must remain exact.
    """

    source_binding_identity: McpBindingIdentityFact
    effective_binding_identity: McpBindingIdentityFact
    source_suspension_event_reference: ContextEventReferenceFact
    source_suspension_fact_fingerprint: str
    source_binding_contract_fingerprint: str
    effective_binding_contract_fingerprint: str
    effective_snapshot_semantic_fingerprint: str
    protocol_semantic_fingerprint: str
    endpoint_attribution_fingerprint: str
    auth_attribution_fingerprint: str
    receipt_fingerprint: str

    def __post_init__(self) -> None:
        source = self.source_binding_identity
        effective = self.effective_binding_identity
        if (
            source.server_id != effective.server_id
            or source.snapshot_id != effective.snapshot_id
            or source.discovery_generation != effective.discovery_generation
        ):
            raise ValueError("MCP recovery rebind changed semantic target")
        if self.source_suspension_event_reference.event_type != (
            "TOOL_EXECUTION_SUSPENDED"
        ):
            raise ValueError("MCP recovery rebind requires a suspension source")
        required = (
            self.source_suspension_fact_fingerprint,
            self.source_binding_contract_fingerprint,
            self.effective_binding_contract_fingerprint,
            self.effective_snapshot_semantic_fingerprint,
            self.protocol_semantic_fingerprint,
            self.endpoint_attribution_fingerprint,
            self.auth_attribution_fingerprint,
        )
        if any(not item for item in required):
            raise ValueError("MCP recovery rebind authority is incomplete")
        _validate_fingerprint(
            self,
            "receipt_fingerprint",
            "mcp-stateless-recovery-rebind-receipt:v1",
        )

    def __reduce__(self):
        raise TypeError("MCP recovery rebind receipts are process-local")


@dataclass(frozen=True, slots=True)
class McpPreparedSuspensionCommitView:
    suspension_event_id: str
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope: McpInputRequiredRequestEnvelopeFact
    durable_continuation: McpInputRequiredDurableContinuationFact
    transaction_companion: McpContinuationTransactionIntent = field(repr=False)
    deadline_monotonic: float | None
    tool_observation_timing_seed: FrozenJsonObjectFact | None
    prepared_suspension_fingerprint: str
    view_fingerprint: str


class McpPendingExecutionHandle(Protocol):
    @property
    def identity(self) -> McpPendingExecutionHandleIdentity: ...

    @property
    def state(self) -> McpPendingHandleState: ...

    @property
    def suspension_commit_view(self) -> McpPreparedSuspensionCommitView: ...

    @property
    def elicitation_batch_owner(self) -> object: ...

    @property
    def recovery_rebind_receipt(
        self,
    ) -> McpStatelessRecoveryRebindReceipt | None: ...


@dataclass(frozen=True, slots=True)
class McpToolResumeRequest:
    owner: McpInvocationOwner
    pending_handle: McpPendingExecutionHandle
    binding: McpToolBindingContract
    source_suspension_event_reference: ContextEventReferenceFact
    source_suspension: McpInputRequiredSuspensionFact
    prepared_resolution: PreparedMcpInputRequiredResolution
    dispatch_receipt: McpConfirmedContinuationDispatchReceipt
    timeout_ms: int
    request_fingerprint: str

    def __post_init__(self) -> None:
        identity = self.pending_handle.identity
        rebind = self.pending_handle.recovery_rebind_receipt
        if identity.binding_identity != self.binding.binding_identity:
            if (
                rebind is None
                or rebind.source_binding_identity != identity.binding_identity
                or rebind.effective_binding_identity != self.binding.binding_identity
                or rebind.source_suspension_event_reference
                != self.source_suspension_event_reference
                or rebind.source_suspension_fact_fingerprint
                != self.source_suspension.suspension_fact_fingerprint
                or rebind.source_binding_contract_fingerprint
                != self.source_suspension.durable_continuation.binding_contract_fingerprint
                or rebind.effective_binding_contract_fingerprint
                != self.binding.contract_fact_fingerprint
            ):
                raise ValueError("MCP resume binding identity mismatch")
        elif rebind is not None and (
            rebind.source_binding_identity != identity.binding_identity
            or rebind.effective_binding_identity != self.binding.binding_identity
        ):
            raise ValueError("MCP resume recovery rebind identity mismatch")
        if identity.interaction_id != self.source_suspension.interaction.interaction_id:
            raise ValueError("MCP resume interaction identity mismatch")
        if (
            self.prepared_resolution.source_suspension_event_reference
            != self.source_suspension_event_reference
            or self.prepared_resolution.source_suspension_fact_fingerprint
            != self.source_suspension.suspension_fact_fingerprint
        ):
            raise ValueError("MCP resume suspension authority mismatch")
        if (
            self.dispatch_receipt.interaction_id != identity.interaction_id
            or self.dispatch_receipt.source_resolution_event_id
            != self.prepared_resolution.resolution_carrier.resolution_event_id
            or self.dispatch_receipt.replay_continuation_carrier_id
            != self.prepared_resolution.resolution_carrier.replay_continuation_carrier_id
        ):
            raise ValueError("MCP resume dispatch authority mismatch")
        if self.timeout_ms < 1:
            raise ValueError("MCP resume timeout must be positive")
        expected = _resume_request_fingerprint(self)
        if self.request_fingerprint != expected:
            raise ValueError("MCP resume request fingerprint mismatch")


class McpPendingTerminalReason(StrEnum):
    COMPLETED_RESULT = "completed_result"
    PERMISSION_DENIED = "permission_denied"
    BINDING_CHANGED = "binding_changed"
    INTERACTION_EXPIRED = "interaction_expired"
    MAXIMUM_ROUNDS_EXCEEDED = "maximum_rounds_exceeded"
    RESUME_UNSUPPORTED = "resume_unsupported"
    HOST_ABORT = "host_abort"
    CHILD_PENDING_UNSUPPORTED = "child_pending_unsupported"
    PUBLICATION_TERMINALIZATION = "publication_terminalization"


@dataclass(frozen=True, slots=True)
class McpPreparedTerminalSettlement:
    pending_handle_identity: McpPendingExecutionHandleIdentity
    reason: McpPendingTerminalReason
    candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity
    terminal_event_id: str
    transaction_companion: McpContinuationTransactionIntent = field(repr=False)
    settlement_generation: int
    settlement_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpPendingHandleTransitionOutcome:
    resulting_state: McpPendingHandleState
    handoff_receipt: ToolExecutionPhysicalOwnerHandoffReceipt
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpToolCompletedOutcome:
    outcome_kind: Literal["completed"]
    result_state: ToolResultState
    normalized_is_error: bool
    normalized_output: str
    frozen_display_payload: FrozenJsonObjectFact | None
    normalized_metadata: FrozenMcpJsonDict
    artifact_candidates: tuple[ToolResultArtifactCandidate, ...]
    semantics_input: ToolResultSemanticsRuntimeInput
    outcome_fingerprint: str

    def __post_init__(self) -> None:
        expected_state = (
            ToolResultState.ERROR
            if self.normalized_is_error
            else ToolResultState.SUCCESS
        )
        if self.result_state is not expected_state:
            raise ValueError("MCP completed application-error matrix mismatch")


@dataclass(frozen=True, slots=True)
class McpToolSuspendedOutcome:
    outcome_kind: Literal["suspended"]
    pending_handle: McpPendingExecutionHandle
    outcome_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpToolRejectedOutcome:
    outcome_kind: Literal["rejected"]
    error_code: McpToolRejectCode
    sanitized_message: str
    retryable_in_same_live_owner: bool
    outcome_fingerprint: str


McpToolExecutionOutcome: TypeAlias = (
    McpToolCompletedOutcome | McpToolSuspendedOutcome | McpToolRejectedOutcome
)


class McpToolExecutionPort(Protocol):
    async def execute(
        self, request: McpToolExecutionRequest
    ) -> McpToolExecutionOutcome: ...

    def bind_suspension_candidate(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
    ) -> None: ...

    def confirm_suspension_commit(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> McpPendingHandleTransitionOutcome: ...

    def prepare_resolution(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        source_suspension_event_reference: ContextEventReferenceFact,
        source_suspension: McpInputRequiredSuspensionFact,
        attempt_ordinal: int,
        submitted_at_utc: str,
    ) -> PreparedMcpInputRequiredResolution: ...

    def confirm_resolution_commit(
        self,
        *,
        prepared_resolution: PreparedMcpInputRequiredResolution,
        outcome: Literal["full", "none", "unknown", "conflict"],
    ) -> None: ...

    def prepare_dispatch(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        prepared_resolution: PreparedMcpInputRequiredResolution,
        source_resolution_event_reference: ContextEventReferenceFact,
        commit_guard: McpDispatchReservationCommitGuard,
    ) -> PreparedMcpContinuationDispatch: ...

    def confirm_dispatch_commit(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        prepared_dispatch: PreparedMcpContinuationDispatch,
        outcome: Literal["full", "none", "unknown", "conflict"],
    ) -> McpConfirmedContinuationDispatchReceipt | None: ...

    async def resume(
        self, request: McpToolResumeRequest
    ) -> McpToolExecutionOutcome: ...

    def prepare_terminal_settlement(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        reason: McpPendingTerminalReason,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
        terminal_event_id: str,
    ) -> McpPreparedTerminalSettlement: ...

    def confirm_terminal_commit(
        self,
        *,
        settlement: McpPreparedTerminalSettlement,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> McpPendingHandleTransitionOutcome: ...

    def confirm_owned_candidate_commit(
        self,
        *,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> McpPendingHandleTransitionOutcome: ...

    async def stop_admission_and_drain(
        self,
        *,
        deadline_monotonic: float,
    ) -> None: ...


def freeze_mcp_json_object(value: object) -> FrozenMcpJsonDict:
    strict = freeze_json(value)
    if not isinstance(strict, FrozenJsonObjectFact):
        raise TypeError("MCP JSON arguments must be an object")
    frozen = freeze_mcp_json_value(thaw_json(strict))
    if not isinstance(frozen, FrozenMcpJsonDict):
        raise AssertionError("MCP JSON freezer did not produce an object")
    return frozen


def build_mcp_tool_execution_request(
    *,
    owner: McpInvocationOwner,
    exposed_tool_name: str,
    original_tool_name: str,
    binding: McpToolBindingContract,
    arguments: object,
    timeout_ms: int,
) -> McpToolExecutionRequest:
    frozen_arguments = freeze_mcp_json_object(arguments)
    provisional = McpToolExecutionRequest.__new__(McpToolExecutionRequest)
    object.__setattr__(provisional, "owner", owner)
    object.__setattr__(provisional, "exposed_tool_name", exposed_tool_name)
    object.__setattr__(provisional, "original_tool_name", original_tool_name)
    object.__setattr__(provisional, "binding", binding)
    object.__setattr__(provisional, "frozen_arguments", frozen_arguments)
    object.__setattr__(provisional, "timeout_ms", timeout_ms)
    object.__setattr__(provisional, "request_fingerprint", "pending")
    return McpToolExecutionRequest(
        owner=owner,
        exposed_tool_name=exposed_tool_name,
        original_tool_name=original_tool_name,
        binding=binding,
        frozen_arguments=frozen_arguments,
        timeout_ms=timeout_ms,
        request_fingerprint=_execution_request_fingerprint(provisional),
    )


def build_mcp_tool_resume_request(
    *,
    owner: McpInvocationOwner,
    pending_handle: McpPendingExecutionHandle,
    binding: McpToolBindingContract,
    source_suspension_event_reference: ContextEventReferenceFact,
    source_suspension: McpInputRequiredSuspensionFact,
    prepared_resolution: PreparedMcpInputRequiredResolution,
    dispatch_receipt: McpConfirmedContinuationDispatchReceipt,
    timeout_ms: int,
) -> McpToolResumeRequest:
    provisional = McpToolResumeRequest.__new__(McpToolResumeRequest)
    object.__setattr__(provisional, "owner", owner)
    object.__setattr__(provisional, "pending_handle", pending_handle)
    object.__setattr__(provisional, "binding", binding)
    object.__setattr__(
        provisional,
        "source_suspension_event_reference",
        source_suspension_event_reference,
    )
    object.__setattr__(provisional, "source_suspension", source_suspension)
    object.__setattr__(provisional, "prepared_resolution", prepared_resolution)
    object.__setattr__(provisional, "dispatch_receipt", dispatch_receipt)
    object.__setattr__(provisional, "timeout_ms", timeout_ms)
    object.__setattr__(provisional, "request_fingerprint", "pending")
    return McpToolResumeRequest(
        owner=owner,
        pending_handle=pending_handle,
        binding=binding,
        source_suspension_event_reference=source_suspension_event_reference,
        source_suspension=source_suspension,
        prepared_resolution=prepared_resolution,
        dispatch_receipt=dispatch_receipt,
        timeout_ms=timeout_ms,
        request_fingerprint=_resume_request_fingerprint(provisional),
    )


def _execution_request_fingerprint(value: McpToolExecutionRequest) -> str:
    payload = {
        "owner": asdict(value.owner),
        "exposed_tool_name": value.exposed_tool_name,
        "original_tool_name": value.original_tool_name,
        "binding_contract_fingerprint": value.binding.contract_fact_fingerprint,
        "frozen_arguments": value.frozen_arguments,
        "timeout_ms": value.timeout_ms,
    }
    return context_fingerprint("mcp-tool-execution-request:v1", payload)


def _resume_request_fingerprint(value: McpToolResumeRequest) -> str:
    payload = {
        "owner": asdict(value.owner),
        "pending_handle_identity": asdict(value.pending_handle.identity),
        "recovery_rebind_receipt_fingerprint": (
            value.pending_handle.recovery_rebind_receipt.receipt_fingerprint
            if value.pending_handle.recovery_rebind_receipt is not None
            else None
        ),
        "binding_contract_fingerprint": value.binding.contract_fact_fingerprint,
        "source_suspension_event_reference": value.source_suspension_event_reference.model_dump(
            mode="json"
        ),
        "source_suspension_fingerprint": value.source_suspension.suspension_fact_fingerprint,
        "prepared_resolution_fingerprint": value.prepared_resolution.prepared_resolution_fingerprint,
        "dispatch_receipt_fingerprint": value.dispatch_receipt.receipt_fingerprint,
        "timeout_ms": value.timeout_ms,
    }
    return context_fingerprint("mcp-tool-resume-request:v1", payload)


def _validate_fingerprint(value: object, field_name: str, namespace: str) -> None:
    payload = asdict(value)
    actual = payload.pop(field_name)
    if actual != context_fingerprint(namespace, payload):
        raise ValueError(f"{field_name} mismatch")


__all__ = [
    "FrozenMcpJsonDict",
    "McpInvocationOwner",
    "McpConfirmedContinuationDispatchReceipt",
    "McpDispatchReservationCommitGuard",
    "McpPendingExecutionHandle",
    "McpPendingExecutionHandleIdentity",
    "McpPendingHandleState",
    "McpPendingHandleTransitionOutcome",
    "McpPendingTerminalReason",
    "McpPreparedCompanionIdentity",
    "McpPreparedContinuationCompanion",
    "McpContinuationTransactionAuthority",
    "McpContinuationTransactionIntent",
    "McpPreparedSuspensionCommitView",
    "McpPreparedTerminalSettlement",
    "PreparedMcpInputRequiredResolution",
    "PreparedMcpContinuationDispatch",
    "McpToolCompletedOutcome",
    "McpToolExecutionOutcome",
    "McpToolExecutionPort",
    "McpToolExecutionRequest",
    "McpToolRejectCode",
    "McpToolRejectedOutcome",
    "McpToolResumeRequest",
    "McpToolSuspendedOutcome",
    "build_mcp_tool_execution_request",
    "build_mcp_tool_resume_request",
    "freeze_mcp_json_object",
]
