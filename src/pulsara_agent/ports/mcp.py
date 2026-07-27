"""Process-local MCP execution, suspension, and settlement boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal, Protocol, TypeAlias

from pulsara_agent.event import EventContext
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import (
    ToolExecutionPhysicalOwnerHandoffReceipt,
    ToolExecutionStableCandidateCommitReceipt,
    ToolExecutionStableCandidateOwnerIdentity,
    ToolResultArtifactCandidate,
)
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
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredInteractionSemanticFact,
    McpInputRequiredRequestEnvelopeFact,
    McpInputRequiredSuspensionFact,
    McpPendingLeaseReservationIdentityFact,
    PreparedMcpInputRequiredResolution,
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
class McpPreparedSuspensionCommitView:
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope: McpInputRequiredRequestEnvelopeFact
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


@dataclass(frozen=True, slots=True)
class McpToolResumeRequest:
    owner: McpInvocationOwner
    pending_handle: McpPendingExecutionHandle
    binding: McpToolBindingContract
    source_suspension_event_reference: ContextEventReferenceFact
    source_suspension: McpInputRequiredSuspensionFact
    prepared_resolution: PreparedMcpInputRequiredResolution
    timeout_ms: int
    request_fingerprint: str

    def __post_init__(self) -> None:
        identity = self.pending_handle.identity
        if identity.binding_identity != self.binding.binding_identity:
            raise ValueError("MCP resume binding identity mismatch")
        if identity.interaction_id != self.source_suspension.interaction.interaction_id:
            raise ValueError("MCP resume interaction identity mismatch")
        if (
            self.prepared_resolution.source_suspension_event_reference
            != self.source_suspension_event_reference
            or self.prepared_resolution.source_suspension_fact_fingerprint
            != self.source_suspension.suspension_fact_fingerprint
        ):
            raise ValueError("MCP resume suspension authority mismatch")
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

    async def resume(
        self, request: McpToolResumeRequest
    ) -> McpToolExecutionOutcome: ...

    def prepare_terminal_settlement(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        reason: McpPendingTerminalReason,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
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
    object.__setattr__(provisional, "timeout_ms", timeout_ms)
    object.__setattr__(provisional, "request_fingerprint", "pending")
    return McpToolResumeRequest(
        owner=owner,
        pending_handle=pending_handle,
        binding=binding,
        source_suspension_event_reference=source_suspension_event_reference,
        source_suspension=source_suspension,
        prepared_resolution=prepared_resolution,
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
        "binding_contract_fingerprint": value.binding.contract_fact_fingerprint,
        "source_suspension_event_reference": value.source_suspension_event_reference.model_dump(
            mode="json"
        ),
        "source_suspension_fingerprint": value.source_suspension.suspension_fact_fingerprint,
        "prepared_resolution_fingerprint": value.prepared_resolution.prepared_resolution_fingerprint,
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
    "McpPendingExecutionHandle",
    "McpPendingExecutionHandleIdentity",
    "McpPendingHandleState",
    "McpPendingHandleTransitionOutcome",
    "McpPendingTerminalReason",
    "McpPreparedSuspensionCommitView",
    "McpPreparedTerminalSettlement",
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
