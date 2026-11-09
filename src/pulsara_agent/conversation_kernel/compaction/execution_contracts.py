"""Compaction execution results, attempt identity and phase boundary facts."""

from __future__ import annotations

from pulsara_agent.identity import random_id
from dataclasses import dataclass, field as dataclass_field
from typing import Literal, Protocol
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionContinuationMode,
    CompactionDisposition,
    CompactionOutcome,
    CompactionScope,
    CompactionTrigger,
    FrozenCompactionCanonicalRead,
    ProtectedTailSelectionFact,
    ProviderPrefixCutProof,
    PreparedCompactionCanonicalAdoption,
)
from pulsara_agent.conversation_kernel.compaction.model_call import (
    PreparedCompactionSummarySemantic,
)
from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedCompactionDryProjection,
    PreparedProviderDispatch,
    PreparedProviderHeadroomAdmission,
    PreparedProspectiveActiveRootInput,
    PreparedWireMeasurementDecision,
    HandleFreeProviderWireObservation,
)
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    PreparedProviderInputCut,
)
from pulsara_agent.conversation_kernel.direct_model import PreparedKernelModelTarget
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.hooks.context import PendingHookContextReservation
from pulsara_agent.hooks.contracts import HookDispatchScopeRef


@dataclass(frozen=True, slots=True)
class CompactionExecutionResult:
    outcome: CompactionOutcome
    successor_dispatch: PreparedProviderDispatch | None = dataclass_field(
        default=None, repr=False
    )
    prospective_active_input: PreparedProspectiveActiveRootInput | None = (
        dataclass_field(default=None, repr=False)
    )
    active_continuation_blocked_reason: str | None = None
    successor_wire_decision: PreparedWireMeasurementDecision | None = dataclass_field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.active_continuation_blocked_reason is not None and (
            self.outcome.disposition is not CompactionDisposition.COMPACTED
            or self.successor_dispatch is not None
            or self.prospective_active_input is not None
        ):
            raise ValueError("compaction continuation block carrier is invalid")
        if (
            sum(
                value is not None
                for value in (
                    self.successor_dispatch,
                    self.prospective_active_input,
                )
            )
            > 1
        ):
            raise ValueError("compaction result has multiple continuation owners")


@dataclass(frozen=True, slots=True)
class _CompactionFencedRestart:
    """Stack-free restart request after one fenced attempt releases its owners."""

    maximum_retained_tool_groups: int | None
    pre_compact_dispatched: bool


@dataclass(frozen=True, slots=True)
class _PendingToolBatchSource:
    closed_source_cut: PreparedProviderInputCut
    assistant_entry_id: str
    ordered_tool_call_ids: tuple[str, ...]


@dataclass(slots=True)
class _PendingToolBatchAdoption:
    dry_dispatch: PreparedCompactionDryProjection
    candidate: PreparedCompactionCanonicalAdoption
    source_read: FrozenCompactionCanonicalRead
    source: _PendingToolBatchSource
    destination_target: PreparedKernelModelTarget
    scope: CompactionScope
    model_call_index: int
    memory_use_policy: MemoryUsePolicy
    attempt_token: CompactionAttemptToken
    hook_scope: HookDispatchScopeRef | None
    session_start_compact_port: SessionStartCompactPort | None
    session_start_boundary_port: SessionStartCompactBoundaryPort | None
    source_projected: bool


@dataclass(frozen=True, slots=True)
class _SummaryWinner:
    semantic: PreparedCompactionSummarySemantic = dataclass_field(repr=False)
    decision: PreparedWireMeasurementDecision = dataclass_field(repr=False)
    tail: ProtectedTailSelectionFact
    prefix: ProviderPrefixCutProof
    retained_entry_ids: tuple[str, ...]
    continuation: tuple[CompactionContinuationMode, str | None]
    retained_tool_group_count: int


def _already_compact_result(turn_id: str) -> CompactionExecutionResult:
    return CompactionExecutionResult(
        CompactionOutcome(
            CompactionDisposition.NOT_NEEDED,
            turn_id,
            None,
            None,
            "CONTEXT_ALREADY_COMPACT",
        )
    )


class _PostAdoptionCompactionFailure(BaseException):
    """Carry an already-FULL compaction winner until its error is re-raised."""

    def __init__(self, outcome: CompactionOutcome, error: BaseException) -> None:
        if (
            outcome.disposition is not CompactionDisposition.COMPACTED
            or outcome.snapshot_id is None
            or outcome.revision_ordinal is None
        ):
            raise ValueError("post-adoption failure lacks its canonical winner")
        super().__init__(type(error).__name__)
        self.outcome = outcome
        self.error = error


_COMPACTION_ATTEMPT_TOKEN_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class CompactionAttemptToken:
    attempt_id: str
    session_id: str
    turn_id: str
    scope_kind: ModelInputScopeKind
    trigger: CompactionTrigger

    def __init__(
        self,
        *,
        attempt_id: str,
        session_id: str,
        turn_id: str,
        scope_kind: ModelInputScopeKind,
        trigger: CompactionTrigger,
        _seal: object,
    ) -> None:
        if _seal is not _COMPACTION_ATTEMPT_TOKEN_SEAL or not attempt_id:
            raise TypeError("compaction attempt token is coordinator-issued")
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "scope_kind", scope_kind)
        object.__setattr__(self, "trigger", trigger)


def _new_compaction_attempt_token(
    *,
    session_id: str,
    turn_id: str,
    scope_kind: ModelInputScopeKind,
    trigger: CompactionTrigger,
) -> CompactionAttemptToken:
    return CompactionAttemptToken(
        attempt_id=random_id("compact-attempt"),
        session_id=session_id,
        turn_id=turn_id,
        scope_kind=scope_kind,
        trigger=trigger,
        _seal=_COMPACTION_ATTEMPT_TOKEN_SEAL,
    )


@dataclass(frozen=True, slots=True)
class PreparedCompactSessionStartFacts:
    attempt_token: CompactionAttemptToken = dataclass_field(repr=False, compare=False)
    adopted_snapshot_id: str
    adopted_binding_revision_id: str
    scope: CompactionScope
    turn_id: str
    model_id: str
    permission_snapshot: FrozenRunPermissionSnapshot = dataclass_field(repr=False)
    deadline_monotonic: float = dataclass_field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self.scope.turn_id != self.turn_id
            or self.attempt_token.session_id != self.scope.session_id
            or (self.attempt_token.turn_id != self.turn_id)
            or (self.attempt_token.scope_kind is not self.scope.scope_kind)
            or (not self.adopted_snapshot_id)
            or (not self.adopted_binding_revision_id)
            or (not self.model_id)
            or (not self.permission_snapshot.snapshot_id)
            or (self.deadline_monotonic <= 0)
        ):
            raise ValueError("compact SessionStart facts do not exact-join")


@dataclass(frozen=True, slots=True)
class PreparedCompactSessionStart:
    proceed: bool
    reason: str | None = None
    reservation: PendingHookContextReservation | None = dataclass_field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.proceed and self.reservation is not None:
            raise ValueError("blocked compact SessionStart cannot own context")


class SessionStartCompactPort(Protocol):
    async def __call__(
        self, facts: PreparedCompactSessionStartFacts
    ) -> PreparedCompactSessionStart: ...


class SessionStartCompactBoundaryPort(Protocol):
    async def arm_compact_boundary(
        self,
        *,
        attempt_token: CompactionAttemptToken,
        adopted_snapshot_id: str,
        adopted_binding_revision_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OrdinaryPrecompileDecision:
    """Unique ordinary admission plus its handle-free exact wire observation."""

    ordinary_admission: PreparedProviderHeadroomAdmission = dataclass_field(repr=False)
    reusable_wire_observation: HandleFreeProviderWireObservation = dataclass_field(
        repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class ModelSwitchDirectPrecompileDecision:
    """Attempt direct switch; the complete final dispatch is still measured."""

    ordinary_admission: PreparedProviderHeadroomAdmission = dataclass_field(repr=False)
    switch_candidate: ModelSwitchCompactionTriggerCandidate = dataclass_field(
        repr=False
    )


@dataclass(frozen=True, slots=True)
class ModelSwitchCompactionTriggerCandidate:
    """Authority-free destination captured by the direct precheck."""

    destination_target: PreparedKernelModelTarget = dataclass_field(repr=False)
    trigger: CompactionTrigger = CompactionTrigger.MODEL_SWITCH_HANDOVER


@dataclass(frozen=True, slots=True)
class AutomaticCompactionTriggerCandidate:
    """Authority-free trigger fact; fenced execution recaptures all source truth."""

    trigger: CompactionTrigger
    reason: Literal["FINAL_WIRE_POLICY"] = "FINAL_WIRE_POLICY"

    def __post_init__(self) -> None:
        if self.trigger not in {
            CompactionTrigger.AUTO_ACTIVE_CONTEXT,
            CompactionTrigger.MID_TURN_FOLLOWUP,
        }:
            raise ValueError("automatic trigger candidate has a non-automatic trigger")


PrecompileCompactionDecision = (
    OrdinaryPrecompileDecision
    | AutomaticCompactionTriggerCandidate
    | ModelSwitchDirectPrecompileDecision
    | ModelSwitchCompactionTriggerCandidate
)
