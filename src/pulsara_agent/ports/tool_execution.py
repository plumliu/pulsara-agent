"""Typed tool interface for Pulsara."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from pulsara_agent.event import EventContext
from pulsara_agent.message.blocks import ToolResultState
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    FrozenJsonObjectFact,
    RunPermissionSnapshotFact,
    thaw_json,
    freeze_json,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    TerminalPayloadTimingFact,
    ToolResultExecutionSemanticsFact,
)
from pulsara_agent.primitives.terminal_observation import (
    TerminalProcessObservationReceiptFact,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.ports.run_execution import (
    RunActivationIdentity,
    RunOwnerIdentity,
)

if TYPE_CHECKING:
    from pulsara_agent.ports.tool_result_semantics import (
        ToolResultSemanticsRuntimeInput,
    )
    from pulsara_agent.event import ToolResultArtifactRef
    from pulsara_agent.ports.mcp import McpPendingExecutionHandle
    from pulsara_agent.ports.terminal import (
        PreparedTerminalProcessMonitorCancellation,
        PreparedTerminalProcessMonitorRegistration,
        PreparedTerminalNotificationReservation,
    )


class FrozenToolJsonDict(dict[str, object]):
    """JSON-serializable, recursively immutable tool carrier."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("tool JSON carrier is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> "FrozenToolJsonDict":
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> "FrozenToolJsonDict":
        return self


def freeze_tool_json_object(value: Mapping[str, object]) -> FrozenToolJsonDict:
    normalized = freeze_json(value)
    if not isinstance(normalized, FrozenJsonObjectFact):
        raise TypeError("tool JSON carrier must be an object")
    return _freeze_tool_json_mapping(thaw_json(normalized))


def thaw_tool_json_object(value: Mapping[str, object]) -> dict[str, object]:
    def thaw(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): thaw(nested) for key, nested in item.items()}
        if isinstance(item, tuple):
            return [thaw(nested) for nested in item]
        return item

    return {str(key): thaw(item) for key, item in value.items()}


def _freeze_tool_json_mapping(value: object) -> FrozenToolJsonDict:
    if not isinstance(value, Mapping):
        raise TypeError("tool JSON carrier must be an object")

    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            result = FrozenToolJsonDict()
            for key, nested in item.items():
                dict.__setitem__(result, str(key), freeze(nested))
            return result
        if isinstance(item, (list, tuple)):
            return tuple(freeze(nested) for nested in item)
        return item

    return freeze(value)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", freeze_tool_json_object(self.arguments))


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    status: ToolResultState
    output: str
    metadata: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    artifact_candidates: tuple["ToolResultArtifactCandidate", ...] = ()
    display_payload: FrozenJsonObjectFact | None = None
    semantics_input: "ToolResultSemanticsRuntimeInput | None" = None
    terminal_payload_timing: TerminalPayloadTimingFact | None = None
    semantics: ToolResultExecutionSemanticsFact | None = None
    prepared_terminal_result: "PreparedToolTerminalResult | None" = None
    terminal_process_observation_receipt: (
        TerminalProcessObservationReceiptFact | None
    ) = None
    prepared_terminal_monitor_registration: "PreparedTerminalProcessMonitorRegistration | None" = None
    prepared_terminal_notification_reservation: "PreparedTerminalNotificationReservation | None" = None
    prepared_terminal_monitor_cancellation: "PreparedTerminalProcessMonitorCancellation | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_tool_json_object(self.metadata))


@dataclass(frozen=True, slots=True)
class PreparedToolTerminalResult:
    """Event-free terminal facts produced by the physical tool executor."""

    tool_call_id: str
    state: ToolResultState
    created_at: str
    artifacts: tuple["ToolResultArtifactRef", ...]
    observation_timing: ToolObservationTimingFact
    semantics: ToolResultExecutionSemanticsFact
    terminal_process_observation_receipt: (
        TerminalProcessObservationReceiptFact | None
    ) = None
    prepared_terminal_monitor_registration: "PreparedTerminalProcessMonitorRegistration | None" = None
    prepared_terminal_notification_reservation: "PreparedTerminalNotificationReservation | None" = None
    prepared_terminal_monitor_cancellation: "PreparedTerminalProcessMonitorCancellation | None" = None


@dataclass(frozen=True, slots=True)
class ToolExecutionSuspended:
    tool_call_id: str
    tool_name: str
    interaction_kind: Literal["mcp_input_required"]
    mcp_pending_handle: "McpPendingExecutionHandle"
    tool_observation_timing_seed: FrozenJsonObjectFact | None = None


@dataclass(frozen=True, slots=True)
class ToolPermissionInvocation:
    permission_snapshot_id: str
    permission_mode: PermissionMode
    permission_policy_fingerprint: str
    terminal_access: Literal["off", "ask", "allow"]
    network_isolated: bool
    source_run_permission_snapshot_fingerprint: str


class ToolInvocationOwnerKind(StrEnum):
    HOST_MAIN_RUN = "host_main_run"
    SUBAGENT_CHILD = "subagent_child"


class ToolExecutionStableCandidateKind(StrEnum):
    SUSPENSION = "suspension"
    TERMINAL = "terminal"


class ToolExecutionNonePolicy(StrEnum):
    ABANDON_ON_NONE = "abandon_on_none"
    RETRY_SAME_CANDIDATE = "retry_same_candidate"


class ToolExecutionCandidateConfirmationKind(StrEnum):
    FULL = "full"
    NONE = "none"
    UNKNOWN = "unknown"
    PARTIAL = "partial"


class ToolExecutionStableCandidateOwnerState(StrEnum):
    ADMITTED = "admitted"
    SUSPENSION_CANDIDATE_FROZEN = "suspension_candidate_frozen"
    SUSPENDED = "suspended"
    TERMINAL_CANDIDATE_FROZEN = "terminal_candidate_frozen"
    RETRY_WAIT = "retry_wait"
    COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"
    DURABLE_FULL_AWAITING_PHYSICAL_HANDOFF = "durable_full_awaiting_physical_handoff"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class ToolExecutionStableCandidateOwnerIdentity:
    registry_instance_id: str
    owner_id: str
    owner_generation: int
    runtime_session_id: str
    run_id: str
    tool_call_id: str
    rollout_reservation_id: str
    rollout_reservation_fingerprint: str
    candidate_kind: ToolExecutionStableCandidateKind
    none_policy: ToolExecutionNonePolicy
    ordered_candidate_event_ids: tuple[str, ...]
    candidate_batch_fingerprint: str
    physical_owner_kind: Literal["mcp_pending"] | None
    physical_owner_identity_fingerprint: str | None
    identity_fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolExecutionStableCandidateCommitReceipt:
    owner_identity: ToolExecutionStableCandidateOwnerIdentity
    confirmation_kind: ToolExecutionCandidateConfirmationKind
    write_attempt_generation: int
    committed_event_references: tuple[ContextEventReferenceFact, ...]
    publication_summary: Literal[
        "not_applicable",
        "completed",
        "enqueued",
        "unavailable",
        "failed_after_commit",
    ]
    retry_scheduled: bool
    reconciliation_required: bool
    receipt_fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolExecutionPhysicalOwnerHandoffReceipt:
    candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity
    source_commit_receipt_fingerprint: str
    physical_owner_kind: Literal["mcp_pending"]
    physical_owner_identity_fingerprint: str
    handoff_generation: int
    physical_disposition: Literal["retained", "confirmed", "released"]
    exact_retry_required: bool
    reconciliation_required: bool
    receipt_fingerprint: str


def tool_permission_invocation_from_snapshot(
    snapshot: RunPermissionSnapshotFact,
) -> ToolPermissionInvocation:
    policy = thaw_json(snapshot.expanded_policy)
    if not isinstance(policy, dict):
        raise TypeError("run permission snapshot policy must be an object")
    terminal_access = policy.get("terminal_access")
    if terminal_access not in {"off", "ask", "allow"}:
        raise ValueError("run permission snapshot has invalid terminal access")
    network_isolated = policy.get("network_isolated")
    if not isinstance(network_isolated, bool):
        raise ValueError("run permission snapshot has invalid network isolation")
    return ToolPermissionInvocation(
        permission_snapshot_id=snapshot.snapshot_id,
        permission_mode=snapshot.mode,
        permission_policy_fingerprint=snapshot.expanded_policy_fingerprint,
        terminal_access=terminal_access,
        network_isolated=network_isolated,
        source_run_permission_snapshot_fingerprint=snapshot.fingerprint,
    )


@dataclass(frozen=True, slots=True)
class ToolRuntimeContext:
    runtime_session_id: str
    event_context: EventContext
    permission: ToolPermissionInvocation
    owner_kind: ToolInvocationOwnerKind
    context_id: str | None = None
    model_call_index: int | None = None


@dataclass(frozen=True, slots=True)
class ToolResultArtifactCandidate:
    role: str
    media_type: str
    text: str | None = None
    data: bytes | None = None
    redacted: bool = True
    stored_complete: bool = True
    loss_reason: str | None = None
    metadata: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)

    def __post_init__(self) -> None:
        if (self.text is None) == (self.data is None):
            raise ValueError(
                "ToolResultArtifactCandidate requires exactly one of text or data"
            )
        object.__setattr__(self, "metadata", freeze_tool_json_object(self.metadata))


class Tool(Protocol):
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        """Execute a tool call."""


class AsyncTool(Protocol):
    name: str

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        """Execute a tool call on the agent runtime event loop."""


@dataclass(frozen=True, slots=True)
class ToolBatchExecutionRequest:
    owner_identity: RunOwnerIdentity
    activation_identity: RunActivationIdentity
    source_model_call_reference: ContextEventReferenceFact
    ordered_tool_calls: tuple[ToolCall, ...]
    authority_revision: int
    authority_fingerprint: str
    execution_surface_fingerprint: str
    batch_fingerprint: str


class CompletedToolBatch(FrozenRuntimeStateBase):
    outcome_kind: Literal["completed"] = "completed"
    ordered_terminal_event_references: tuple[ContextEventReferenceFact, ...]
    receipt_accumulator: str


class SuspendedToolBatch(FrozenRuntimeStateBase):
    outcome_kind: Literal["suspended"] = "suspended"
    suspension_event_reference: ContextEventReferenceFact
    pending_interaction_fingerprint: str


class TerminalizationPendingToolBatch(FrozenRuntimeStateBase):
    outcome_kind: Literal["terminalization_pending"] = "terminalization_pending"
    finalization_owner_fingerprint: str


class ToolBatchReconciliationRequired(FrozenRuntimeStateBase):
    outcome_kind: Literal["reconciliation_required"] = "reconciliation_required"
    stable_owner_fingerprint: str


ToolBatchOutcome: TypeAlias = (
    CompletedToolBatch
    | SuspendedToolBatch
    | TerminalizationPendingToolBatch
    | ToolBatchReconciliationRequired
)


class ToolBatchExecutionHandle(Protocol):
    @property
    def batch_fingerprint(self) -> str: ...

    async def wait_outcome(self) -> ToolBatchOutcome: ...

    def release(self) -> None: ...


class ToolBatchExecutionPort(Protocol):
    async def dispatch(
        self,
        request: ToolBatchExecutionRequest,
        *,
        deadline_monotonic: float,
    ) -> ToolBatchExecutionHandle: ...
