"""Host-owned, process-local provider-input continuity state.

This owner is deliberately small.  It linearizes a prepared immutable input
against the currently installed prefix and returns an opaque one-shot permit.
It never reads or writes PostgreSQL and never opens a provider transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, replace
from enum import StrEnum
from threading import RLock
from uuid import uuid4

from pulsara_agent.capability.contracts import FrozenToolCapabilityExposurePlan
from pulsara_agent.conversation_kernel.contracts import (
    HostWriterAcquisitionKind,
    HostWriterGuard,
    TurnStatus,
    WriterLease,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    PreparedSubagentTaskStart,
)
from pulsara_agent.model_input.continuity import (
    AdoptedCompactionSuccessor,
    EmptyScopeColdStart,
    ExplicitModelSwitchColdStart,
    FrozenDirectSwitchAdmission,
    FrozenProviderInputAppendPlanningInput,
    FrozenProviderInputEpochView,
    InstalledEpochAppend,
    InstalledEpochRuntimeCohort,
    PreparedProviderInputAppendCandidate,
    ProcessLocalCanonicalFrontier,
    ProcessLocalProviderInputInstallPermit,
    ProviderInputAdmissionPredecessorKind,
    ProviderInputContinuityScope,
    ProviderInputDispatchAnchor,
    ProviderInputEpochTransition,
    _issue_empty_scope_bootstrap_authority,
    _issue_adopted_compaction_successor,
    _issue_explicit_model_switch_transition,
    provider_input_logical_bytes,
    provider_input_prefix_fingerprint,
)
from pulsara_agent.llm.frozen_target import FrozenEpochModelCallTarget
from pulsara_agent.model_input.contracts import FrozenModelInputSemanticProjection
from pulsara_agent.llm.provider_replay import ProviderAssistantReplayFragment
from pulsara_agent.llm.request import FrozenProviderWireInputPlan
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    PreparedProviderInputCut,
    compiled_message_placements_fingerprint,
)


class ProviderInputContinuityConflict(RuntimeError):
    pass


_INSTALL_AUTHORITY_SEAL = object()
_ASSISTANT_REPLAY_RESERVATION_SEAL = object()
_BOOTSTRAP_LEASE_SEAL = object()
_EMPTY_PREPARATION_RESERVATION_SEAL = object()
_NO_CONTINUATION_FENCE_SEAL = object()
_NO_CONTINUATION_EVIDENCE_SEAL = object()
_NO_CONTINUATION_CLOSURE_SEAL = object()
_REVOKED_PREPARATION_RESOURCES_SEAL = object()
_EMPTY_ADOPTION_PENDING_SEAL = object()
_EMPTY_ADOPTION_SETTLING_SEAL = object()
_EMPTY_ADOPTION_REVOKED_SEAL = object()
_BOOTSTRAP_LEASE_SOURCE_SEAL = object()
_REVOCATION_NOT_STARTED_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class NewSessionRootLeaseSource:
    writer_lease: WriterLease

    def __init__(self, writer_lease: WriterLease, *, _seal: object) -> None:
        if (
            _seal is not _BOOTSTRAP_LEASE_SOURCE_SEAL
            or writer_lease.acquisition_kind
            is not HostWriterAcquisitionKind.NEW_SESSION
        ):
            raise TypeError("new-session root lease source is repository-issued")
        object.__setattr__(self, "writer_lease", writer_lease)


@dataclass(frozen=True, slots=True, init=False)
class FreshHostRootLeaseSource:
    writer_lease: WriterLease

    def __init__(self, writer_lease: WriterLease, *, _seal: object) -> None:
        if (
            _seal is not _BOOTSTRAP_LEASE_SOURCE_SEAL
            or writer_lease.acquisition_kind
            is not HostWriterAcquisitionKind.HOST_TAKEOVER
        ):
            raise TypeError("fresh-Host root lease source requires takeover FULL")
        object.__setattr__(self, "writer_lease", writer_lease)


@dataclass(frozen=True, slots=True, init=False)
class NewSubagentLeaseSource:
    writer_guard: HostWriterGuard
    durable_runnable_task_fact: object

    def __init__(
        self,
        *,
        writer_guard: HostWriterGuard,
        durable_runnable_task_fact: object,
        _seal: object,
    ) -> None:
        if (
            _seal is not _BOOTSTRAP_LEASE_SOURCE_SEAL
            or not isinstance(durable_runnable_task_fact, PreparedSubagentTaskStart)
            or getattr(durable_runnable_task_fact, "session_id", None)
            != writer_guard.session_id
            or getattr(durable_runnable_task_fact, "writer_generation", None)
            != writer_guard.writer_generation
            or not getattr(durable_runnable_task_fact, "task_id", "")
            or not getattr(durable_runnable_task_fact, "event_id", "")
        ):
            raise TypeError("subagent lease source requires the durable runnable fact")
        object.__setattr__(self, "writer_guard", writer_guard)
        object.__setattr__(
            self, "durable_runnable_task_fact", durable_runnable_task_fact
        )


@dataclass(frozen=True, slots=True, init=False)
class AdoptedBaseLeaseSource:
    writer_guard: HostWriterGuard
    adoption_confirmation: object
    snapshot_id: str
    binding_revision_id: str

    def __init__(
        self,
        *,
        writer_guard: HostWriterGuard,
        adoption_confirmation: object,
        snapshot_id: str,
        binding_revision_id: str,
        _seal: object,
    ) -> None:
        if (
            _seal is not _BOOTSTRAP_LEASE_SOURCE_SEAL
            or getattr(getattr(adoption_confirmation, "kind", None), "value", None)
            != "FULL"
            or not snapshot_id
            or not binding_revision_id
        ):
            raise TypeError("adopted-base lease source requires adoption FULL")
        object.__setattr__(self, "writer_guard", writer_guard)
        object.__setattr__(self, "adoption_confirmation", adoption_confirmation)
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "binding_revision_id", binding_revision_id)


BootstrapLeaseSource = (
    NewSessionRootLeaseSource
    | FreshHostRootLeaseSource
    | NewSubagentLeaseSource
    | AdoptedBaseLeaseSource
)


def _issue_root_bootstrap_lease_source(
    writer_lease: WriterLease,
) -> NewSessionRootLeaseSource | FreshHostRootLeaseSource:
    if writer_lease.acquisition_kind is HostWriterAcquisitionKind.NEW_SESSION:
        return NewSessionRootLeaseSource(
            writer_lease,
            _seal=_BOOTSTRAP_LEASE_SOURCE_SEAL,
        )
    if writer_lease.acquisition_kind is HostWriterAcquisitionKind.HOST_TAKEOVER:
        return FreshHostRootLeaseSource(
            writer_lease,
            _seal=_BOOTSTRAP_LEASE_SOURCE_SEAL,
        )
    raise TypeError("continuity ROOT requires session genesis or takeover FULL")


def _issue_new_subagent_lease_source(
    *, writer_guard: HostWriterGuard, durable_runnable_task_fact: object
) -> NewSubagentLeaseSource:
    return NewSubagentLeaseSource(
        writer_guard=writer_guard,
        durable_runnable_task_fact=durable_runnable_task_fact,
        _seal=_BOOTSTRAP_LEASE_SOURCE_SEAL,
    )


class ProcessLocalProviderInputInstallAuthority:
    """Narrow verifier for permits issued by one Host continuity owner.

    The permit DTO remains provider-neutral, but matching public fields are
    not authority.  Only the exact object installed by this owner can be
    consumed, exactly once, immediately before provider open.
    """

    __slots__ = ("_owner",)

    def __init__(
        self,
        owner: "HostProviderInputContinuityOwner",
        *,
        _seal: object,
    ) -> None:
        if _seal is not _INSTALL_AUTHORITY_SEAL:
            raise TypeError("provider-input install authority is Host-owned")
        self._owner = owner

    def consume(
        self,
        permit: ProcessLocalProviderInputInstallPermit,
        *,
        candidate: PreparedProviderInputAppendCandidate,
        execution: object,
    ) -> None:
        self._owner._consume_install_permit(
            permit,
            candidate=candidate,
            execution=execution,
        )

    def require_registered_plan(
        self,
        *,
        candidate: PreparedProviderInputAppendCandidate,
        wire_input_plan: FrozenProviderWireInputPlan,
        tool_exposure_plan: FrozenToolCapabilityExposurePlan,
    ) -> None:
        """Prove that preflight owns the exact plan registered for this CAS."""

        self._owner._require_registered_plan(
            candidate=candidate,
            wire_input_plan=wire_input_plan,
            tool_exposure_plan=tool_exposure_plan,
        )


MAXIMUM_ROOT_SCOPES = 1
MAXIMUM_CHILD_SCOPES = 4
MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES = 64 << 20
MAXIMUM_HOST_INSTALLED_BYTES = 320 << 20
MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES = 640 << 20


@dataclass(frozen=True, slots=True, init=False)
class ProcessLocalAssistantReplayFragmentReservation:
    """Opaque Host-owned capacity claim for one completed replay fragment."""

    scope: ProviderInputContinuityScope
    epoch_nonce: str
    epoch_revision: int
    fragment: ProviderAssistantReplayFragment
    additional_resident_bytes: int
    reservation_nonce: str
    _already_bound: bool

    def __init__(
        self,
        *,
        scope: ProviderInputContinuityScope,
        epoch_nonce: str,
        epoch_revision: int,
        fragment: ProviderAssistantReplayFragment,
        additional_resident_bytes: int,
        already_bound: bool,
        reservation_nonce: str,
        _seal: object,
    ) -> None:
        if _seal is not _ASSISTANT_REPLAY_RESERVATION_SEAL:
            raise TypeError("assistant replay reservation is Host-owned")
        if epoch_revision < 1 or additional_resident_bytes < 0 or not reservation_nonce:
            raise ValueError("assistant replay reservation identity is invalid")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "epoch_nonce", epoch_nonce)
        object.__setattr__(self, "epoch_revision", epoch_revision)
        object.__setattr__(self, "fragment", fragment)
        object.__setattr__(self, "additional_resident_bytes", additional_resident_bytes)
        object.__setattr__(self, "reservation_nonce", reservation_nonce)
        object.__setattr__(self, "_already_bound", already_bound)


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedEmptyBootstrapLease:
    scope: ProviderInputContinuityScope
    lease_nonce: str
    source: BootstrapLeaseSource

    def __init__(
        self,
        *,
        scope: ProviderInputContinuityScope,
        lease_nonce: str,
        source: BootstrapLeaseSource,
        _seal: object,
    ) -> None:
        if _seal is not _BOOTSTRAP_LEASE_SEAL or not isinstance(
            source,
            (
                NewSessionRootLeaseSource,
                FreshHostRootLeaseSource,
                NewSubagentLeaseSource,
                AdoptedBaseLeaseSource,
            ),
        ):
            raise TypeError("empty bootstrap lease is continuity-owned")
        source_guard = (
            source.writer_lease.guard
            if isinstance(source, (NewSessionRootLeaseSource, FreshHostRootLeaseSource))
            else source.writer_guard
        )
        if (
            not lease_nonce
            or source_guard.session_id != scope.session_id
            or (
                isinstance(
                    source, (NewSessionRootLeaseSource, FreshHostRootLeaseSource)
                )
                and scope.scope_kind is not ModelInputScopeKind.ROOT
            )
            or (
                isinstance(source, NewSubagentLeaseSource)
                and (
                    scope.scope_kind is not ModelInputScopeKind.SUBAGENT_TASK
                    or source.durable_runnable_task_fact.task_id
                    != scope.scope_subagent_task_id
                )
            )
        ):
            raise TypeError("empty bootstrap lease is continuity-owned")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "lease_nonce", lease_nonce)
        object.__setattr__(self, "source", source)


@dataclass(frozen=True, slots=True, init=False)
class EmptyPreparationReservation:
    lease: AuthorizedEmptyBootstrapLease
    reservation_nonce: str

    def __init__(
        self,
        *,
        lease: AuthorizedEmptyBootstrapLease,
        reservation_nonce: str,
        _seal: object,
    ) -> None:
        if _seal is not _EMPTY_PREPARATION_RESERVATION_SEAL or not reservation_nonce:
            raise TypeError("empty preparation reservation is continuity-owned")
        object.__setattr__(self, "lease", lease)
        object.__setattr__(self, "reservation_nonce", reservation_nonce)


@dataclass(frozen=True, slots=True, init=False)
class NoContinuationAdmissionFence:
    """Current scope+predecessor fence installed before successor cleanup."""

    scope: ProviderInputContinuityScope
    predecessor: InstalledEpochRuntimeCohort
    turn_id: str
    attempt_token: object
    confirmation: object
    candidate: object
    cut: PreparedProviderInputCut
    destination: FrozenEpochModelCallTarget
    adopted_snapshot_id: str
    adopted_binding_revision_id: str
    fence_nonce: str

    def __init__(
        self,
        *,
        scope: ProviderInputContinuityScope,
        predecessor: InstalledEpochRuntimeCohort,
        turn_id: str,
        attempt_token: object,
        confirmation: object,
        candidate: object,
        cut: PreparedProviderInputCut,
        destination: FrozenEpochModelCallTarget,
        adopted_snapshot_id: str,
        adopted_binding_revision_id: str,
        fence_nonce: str,
        _seal: object,
    ) -> None:
        from pulsara_agent.conversation_kernel.compaction.contracts import (
            CompactionAdoptionConfirmation,
        )

        if (
            _seal is not _NO_CONTINUATION_FENCE_SEAL
            or not turn_id
            or not adopted_snapshot_id
            or not adopted_binding_revision_id
            or not fence_nonce
            or getattr(getattr(confirmation, "kind", None), "value", None) != "FULL"
            or not isinstance(confirmation, CompactionAdoptionConfirmation)
            or getattr(confirmation, "candidate", None) is not candidate
            or getattr(getattr(candidate, "scope", None), "session_id", None)
            != scope.session_id
            or getattr(getattr(candidate, "scope", None), "turn_id", None)
            != turn_id
            or getattr(getattr(candidate, "scope", None), "scope_kind", None)
            is not scope.scope_kind
            or getattr(getattr(candidate, "scope", None), "scope_subagent_task_id", None)
            != scope.scope_subagent_task_id
            or getattr(getattr(candidate, "snapshot", None), "snapshot_id", None)
            != adopted_snapshot_id
            or getattr(getattr(candidate, "binding", None), "binding_revision_id", None)
            != adopted_binding_revision_id
            or getattr(attempt_token, "session_id", None) != scope.session_id
            or getattr(attempt_token, "turn_id", None) != turn_id
            or getattr(attempt_token, "scope_kind", None) is not scope.scope_kind
            or getattr(attempt_token, "scope_subagent_task_id", None)
            != scope.scope_subagent_task_id
            or cut.session_id != scope.session_id
            or cut.turn_id != turn_id
            or cut.context_binding_revision_id
            != candidate.predecessor.binding_revision_id
            or cut.provider_input_through_sequence
            != candidate.snapshot.source_through_sequence
            or destination.session_id != scope.session_id
            or destination.turn_id != turn_id
        ):
            raise TypeError("no-continuation fence is compaction-owner issued")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "predecessor", predecessor)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "attempt_token", attempt_token)
        object.__setattr__(self, "confirmation", confirmation)
        object.__setattr__(self, "candidate", candidate)
        object.__setattr__(self, "cut", cut)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "adopted_snapshot_id", adopted_snapshot_id)
        object.__setattr__(
            self, "adopted_binding_revision_id", adopted_binding_revision_id
        )
        object.__setattr__(self, "fence_nonce", fence_nonce)


@dataclass(frozen=True, slots=True, init=False)
class IdleTargetNoContinuationEvidence:
    subject: object
    kind = "IDLE_TARGET"

    def __init__(self, subject: object, *, _seal: object) -> None:
        if (
            _seal is not _NO_CONTINUATION_EVIDENCE_SEAL
            or getattr(subject, "value", None) != "IDLE_BASE_ONLY"
        ):
            raise TypeError("idle-target evidence is compaction-owner issued")
        object.__setattr__(self, "subject", subject)


@dataclass(frozen=True, slots=True, init=False)
class DurableTurnNotRunningEvidence:
    subject: object
    kind = "DURABLE_TURN_NOT_RUNNING"

    def __init__(self, subject: object, *, _seal: object) -> None:
        if (
            _seal is not _NO_CONTINUATION_EVIDENCE_SEAL
            or not isinstance(subject, TurnStatus)
            or subject is TurnStatus.RUNNING
        ):
            raise TypeError("turn-lifecycle evidence is canonical-reader issued")
        object.__setattr__(self, "subject", subject)


@dataclass(frozen=True, slots=True, init=False)
class PostCompactBlockedEvidence:
    subject: object
    kind = "POST_COMPACT_BLOCKED"

    def __init__(self, subject: object, *, _seal: object) -> None:
        if (
            _seal is not _NO_CONTINUATION_EVIDENCE_SEAL
            or not isinstance(subject, tuple)
            or len(subject) != 2
            or subject[0] is not False
        ):
            raise TypeError("PostCompact evidence is Hook-owner issued")
        object.__setattr__(self, "subject", subject)


@dataclass(frozen=True, slots=True, init=False)
class SessionStartCompactBlockedEvidence:
    subject: object
    kind = "SESSION_START_COMPACT_BLOCKED"

    def __init__(self, subject: object, *, _seal: object) -> None:
        if (
            _seal is not _NO_CONTINUATION_EVIDENCE_SEAL
            or getattr(subject, "proceed", None) is not False
        ):
            raise TypeError("SessionStartCompact evidence is boundary-owner issued")
        object.__setattr__(self, "subject", subject)


@dataclass(frozen=True, slots=True, init=False)
class SuccessorFinalAbandonmentEvidence:
    subject: object
    kind = "SUCCESSOR_FINAL_ABANDONMENT"

    def __init__(self, subject: object, *, _seal: object) -> None:
        if _seal is not _NO_CONTINUATION_EVIDENCE_SEAL or not isinstance(
            subject, BaseException
        ):
            raise TypeError("successor abandonment evidence is settlement-owner issued")
        object.__setattr__(self, "subject", subject)


NoContinuationProductEvidence = (
    IdleTargetNoContinuationEvidence
    | DurableTurnNotRunningEvidence
    | PostCompactBlockedEvidence
    | SessionStartCompactBlockedEvidence
    | SuccessorFinalAbandonmentEvidence
)


@dataclass(frozen=True, slots=True, init=False)
class NoContinuationClosure:
    fence: NoContinuationAdmissionFence
    evidence: NoContinuationProductEvidence

    def __init__(
        self,
        *,
        fence: NoContinuationAdmissionFence,
        evidence: NoContinuationProductEvidence,
        _seal: object,
    ) -> None:
        if _seal is not _NO_CONTINUATION_CLOSURE_SEAL:
            raise TypeError("no-continuation closure is safe-point issued")
        object.__setattr__(self, "fence", fence)
        object.__setattr__(self, "evidence", evidence)


@dataclass(frozen=True, slots=True, init=False)
class RevokedProviderPreparationResources:
    fence: NoContinuationAdmissionFence

    def __init__(self, *, fence: NoContinuationAdmissionFence, _seal: object) -> None:
        if _seal is not _REVOKED_PREPARATION_RESOURCES_SEAL:
            raise TypeError("revoked preparation resources are continuity-owned")
        object.__setattr__(self, "fence", fence)


@dataclass(frozen=True, slots=True, init=False)
class RevocationNotStarted:
    exact_subject: object

    def __init__(self, exact_subject: object, *, _seal: object) -> None:
        if _seal is not _REVOCATION_NOT_STARTED_SEAL:
            raise TypeError("revocation progress is continuity-owned")
        object.__setattr__(self, "exact_subject", exact_subject)


@dataclass(frozen=True, slots=True, init=False)
class EmptyAdoptionPending:
    reservation: EmptyPreparationReservation
    preparation_basis: object
    attempt_token: object
    candidate: object
    dry_result: object

    def __init__(
        self,
        *,
        reservation: EmptyPreparationReservation,
        preparation_basis: object,
        attempt_token: object,
        candidate: object,
        dry_result: object,
        _seal: object,
    ) -> None:
        if _seal is not _EMPTY_ADOPTION_PENDING_SEAL:
            raise TypeError("empty adoption pending is continuity-owned")
        object.__setattr__(self, "reservation", reservation)
        object.__setattr__(self, "preparation_basis", preparation_basis)
        object.__setattr__(self, "attempt_token", attempt_token)
        object.__setattr__(self, "candidate", candidate)
        object.__setattr__(self, "dry_result", dry_result)


@dataclass(frozen=True, slots=True, init=False)
class EmptyAdoptionFullSettling:
    pending: EmptyAdoptionPending
    confirmation: object

    def __init__(
        self,
        *,
        pending: EmptyAdoptionPending,
        confirmation: object,
        _seal: object,
    ) -> None:
        if (
            _seal is not _EMPTY_ADOPTION_SETTLING_SEAL
            or getattr(getattr(confirmation, "kind", None), "value", None) != "FULL"
            or getattr(confirmation, "candidate", None) is not pending.candidate
        ):
            raise TypeError("empty adoption FULL settlement is owner-issued")
        object.__setattr__(self, "pending", pending)
        object.__setattr__(self, "confirmation", confirmation)


@dataclass(frozen=True, slots=True, init=False)
class EmptyAdoptionConflictSettling:
    pending: EmptyAdoptionPending
    confirmation: object

    def __init__(
        self,
        *,
        pending: EmptyAdoptionPending,
        confirmation: object,
        _seal: object,
    ) -> None:
        if (
            _seal is not _EMPTY_ADOPTION_SETTLING_SEAL
            or getattr(getattr(confirmation, "kind", None), "value", None) != "CONFLICT"
            or getattr(confirmation, "candidate", None) is not pending.candidate
        ):
            raise TypeError("empty adoption CONFLICT settlement is owner-issued")
        object.__setattr__(self, "pending", pending)
        object.__setattr__(self, "confirmation", confirmation)


@dataclass(frozen=True, slots=True, init=False)
class EmptyAdoptionRevokedProviderPreparationResources:
    settling: EmptyAdoptionFullSettling | EmptyAdoptionConflictSettling

    def __init__(
        self,
        *,
        settling: EmptyAdoptionFullSettling | EmptyAdoptionConflictSettling,
        _seal: object,
    ) -> None:
        if _seal is not _EMPTY_ADOPTION_REVOKED_SEAL:
            raise TypeError("empty adoption revoked resources are owner-issued")
        object.__setattr__(self, "settling", settling)


def _issue_idle_target_no_continuation_evidence(
    subject: object,
) -> IdleTargetNoContinuationEvidence:
    return IdleTargetNoContinuationEvidence(
        subject, _seal=_NO_CONTINUATION_EVIDENCE_SEAL
    )


def _issue_durable_turn_not_running_evidence(
    subject: object,
) -> DurableTurnNotRunningEvidence:
    return DurableTurnNotRunningEvidence(subject, _seal=_NO_CONTINUATION_EVIDENCE_SEAL)


def _issue_post_compact_blocked_evidence(
    subject: object,
) -> PostCompactBlockedEvidence:
    return PostCompactBlockedEvidence(subject, _seal=_NO_CONTINUATION_EVIDENCE_SEAL)


def _issue_session_start_compact_blocked_evidence(
    subject: object,
) -> SessionStartCompactBlockedEvidence:
    return SessionStartCompactBlockedEvidence(
        subject, _seal=_NO_CONTINUATION_EVIDENCE_SEAL
    )


def _issue_successor_final_abandonment_evidence(
    subject: object,
) -> SuccessorFinalAbandonmentEvidence:
    return SuccessorFinalAbandonmentEvidence(
        subject, _seal=_NO_CONTINUATION_EVIDENCE_SEAL
    )


def _issue_no_continuation_closure(
    *,
    fence: NoContinuationAdmissionFence,
    evidence: NoContinuationProductEvidence,
) -> NoContinuationClosure:
    return NoContinuationClosure(
        fence=fence,
        evidence=evidence,
        _seal=_NO_CONTINUATION_CLOSURE_SEAL,
    )


class _SlotState(StrEnum):
    AUTHORIZED_EMPTY = "AUTHORIZED_EMPTY"
    PREPARING_EMPTY = "PREPARING_EMPTY"
    BOUND_EMPTY = "BOUND_EMPTY"
    PREPARED = "PREPARED"
    INSTALLED = "INSTALLED"
    INSTALLED_NO_CONTINUATION_SETTLING = "INSTALLED_NO_CONTINUATION_SETTLING"
    EMPTY_ADOPTION_PENDING = "EMPTY_ADOPTION_PENDING"
    EMPTY_ADOPTION_FULL_SETTLING = "EMPTY_ADOPTION_FULL_SETTLING"
    EMPTY_ADOPTION_CONFLICT_SETTLING = "EMPTY_ADOPTION_CONFLICT_SETTLING"
    EMPTY_ADOPTION_RESOURCE_RELEASE_QUARANTINE = (
        "EMPTY_ADOPTION_RESOURCE_RELEASE_QUARANTINE"
    )
    CLOSED = "CLOSED"


@dataclass(slots=True)
class _Slot:
    state: _SlotState
    bootstrap_lease: AuthorizedEmptyBootstrapLease | None = None
    empty_reservation: EmptyPreparationReservation | None = None
    installed: InstalledEpochRuntimeCohort | None = None
    prepared: PreparedProviderInputAppendCandidate | None = None
    replay_reservation: ProcessLocalAssistantReplayFragmentReservation | None = None
    no_continuation_fence: NoContinuationAdmissionFence | None = None
    no_continuation_closure: NoContinuationClosure | None = None
    no_continuation_revocation: (
        RevocationNotStarted | RevokedProviderPreparationResources | None
    ) = None
    empty_adoption_pending: EmptyAdoptionPending | None = None
    empty_adoption_settling: (
        EmptyAdoptionFullSettling | EmptyAdoptionConflictSettling | None
    ) = None
    empty_adoption_revocation: (
        RevocationNotStarted | EmptyAdoptionRevokedProviderPreparationResources | None
    ) = None


@dataclass(frozen=True, slots=True)
class _IssuedProviderInputInstallPermit:
    permit: ProcessLocalProviderInputInstallPermit
    candidate: PreparedProviderInputAppendCandidate = dataclass_field(repr=False)
    execution: object = dataclass_field(repr=False)


class HostProviderInputContinuityOwner:
    """Own at most one ROOT and four child prefix epochs for one Host."""

    def __init__(
        self,
        *,
        root_lease_source: NewSessionRootLeaseSource | FreshHostRootLeaseSource,
        maximum_child_scopes: int = MAXIMUM_CHILD_SCOPES,
    ) -> None:
        if not isinstance(
            root_lease_source,
            (NewSessionRootLeaseSource, FreshHostRootLeaseSource),
        ):
            raise TypeError("ROOT continuity requires a sealed lifecycle source")
        writer_lease = root_lease_source.writer_lease
        session_id = writer_lease.guard.session_id
        if not session_id or maximum_child_scopes < 1:
            raise ValueError("provider-input continuity owner bound is invalid")
        self._session_id = session_id
        self._writer_guard = writer_lease.guard
        self._maximum_child_scopes = maximum_child_scopes
        self._lock = RLock()
        self._slots: dict[ProviderInputContinuityScope, _Slot] = {}
        self._issued_permits: dict[str, _IssuedProviderInputInstallPermit] = {}
        self._install_authority = ProcessLocalProviderInputInstallAuthority(
            self,
            _seal=_INSTALL_AUTHORITY_SEAL,
        )
        self._closed = False
        root_scope = ProviderInputContinuityScope(
            session_id=session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        self._slots[root_scope] = self._new_authorized_empty_slot(
            root_scope, source=root_lease_source
        )

    @property
    def install_authority(self) -> ProcessLocalProviderInputInstallAuthority:
        return self._install_authority

    def authorize_new_subagent_scope(
        self,
        scope: ProviderInputContinuityScope,
        *,
        source: NewSubagentLeaseSource,
    ) -> None:
        """Publish the one lifecycle lease created by a successful child transaction."""

        self._require_scope(scope)
        if scope.scope_kind is not ModelInputScopeKind.SUBAGENT_TASK:
            raise ProviderInputContinuityConflict(
                "only a SUBAGENT_TASK scope may use child authorization"
            )
        task_fact = source.durable_runnable_task_fact
        if (
            source.writer_guard != self._writer_guard
            or getattr(task_fact, "task_id", None) != scope.scope_subagent_task_id
        ):
            raise ProviderInputContinuityConflict(
                "subagent lease source does not match the continuity scope"
            )
        with self._lock:
            if self._closed or scope in self._slots:
                raise ProviderInputContinuityConflict(
                    "subagent continuity scope is unavailable"
                )
            self._admit_scope_capacity_locked(scope)
            self._slots[scope] = self._new_authorized_empty_slot(scope, source=source)

    def abort_planning(self, planning: FrozenProviderInputAppendPlanningInput) -> None:
        """Return the exact Empty reservation; installed planning is a no-op."""

        self._require_scope(planning.scope)
        reservation = planning._empty_preparation_reservation
        if reservation is None:
            return
        with self._lock:
            slot = self._slots.get(planning.scope)
            if (
                slot is None
                or slot.state is _SlotState.CLOSED
                or slot.empty_reservation is not reservation
            ):
                return
            if slot.state not in {
                _SlotState.PREPARING_EMPTY,
                _SlotState.BOUND_EMPTY,
            }:
                raise ProviderInputContinuityConflict(
                    "empty preparation cannot be aborted from its current state"
                )
            slot.empty_reservation = None
            slot.bootstrap_lease = reservation.lease
            slot.state = _SlotState.AUTHORIZED_EMPTY

    def issue_transition(
        self,
        *,
        planning: FrozenProviderInputAppendPlanningInput,
        preparation_basis: object,
        call_target: FrozenEpochModelCallTarget,
        seed: object | None,
        semantic_projection: FrozenModelInputSemanticProjection,
        wire_input_plan: FrozenProviderWireInputPlan,
        direct_switch_admission: FrozenDirectSwitchAdmission | None = None,
    ) -> ProviderInputEpochTransition:
        """Bind an admitted pure projection to the exact current slot state."""

        from pulsara_agent.conversation_kernel.cold_epoch import (
            AdoptedCompactionContinuationSeed,
            CanonicalColdContinuationSeed,
            SubagentInitialSeed,
        )

        self._require_scope(planning.scope)
        if (
            preparation_basis is None
            or getattr(preparation_basis, "call_target", None) is not call_target
            or getattr(preparation_basis, "canonical_read", None) is None
            or getattr(preparation_basis, "capability_dispatch_cut", None) is None
            or call_target.session_id != planning.scope.session_id
            or wire_input_plan.quote.wire_api
            != call_target.target_bundle.target_fact.wire_api
        ):
            raise ProviderInputContinuityConflict(
                "provider-input transition destination drifted"
            )
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(planning.scope)
            if slot is None:
                raise ProviderInputContinuityConflict("continuity scope is unavailable")
            reservation = planning._empty_preparation_reservation
            if planning.predecessor_view is None and reservation is not None:
                if (
                    slot.state is not _SlotState.PREPARING_EMPTY
                    or slot.empty_reservation is not reservation
                    or direct_switch_admission is not None
                    or not isinstance(
                        seed, (CanonicalColdContinuationSeed, SubagentInitialSeed)
                    )
                ):
                    raise ProviderInputContinuityConflict(
                        "empty bootstrap preparation is stale"
                    )
                authority = _issue_empty_scope_bootstrap_authority(
                    scope=planning.scope,
                    call_target=call_target,
                    authority_nonce=f"empty-bootstrap-authority:{uuid4().hex}",
                    preparation_basis=preparation_basis,
                )
                slot.state = _SlotState.BOUND_EMPTY
                return EmptyScopeColdStart(authority, seed)
            cohort = slot.installed
            if (
                cohort is None
                or slot.state is not _SlotState.INSTALLED
                or planning.predecessor_view is not cohort.view
                or reservation is not None
            ):
                raise ProviderInputContinuityConflict(
                    "installed transition predecessor is stale"
                )
            if isinstance(seed, AdoptedCompactionContinuationSeed):
                if direct_switch_admission is not None:
                    raise ProviderInputContinuityConflict(
                        "adopted successor cannot reuse direct-switch admission"
                    )
                if slot.no_continuation_fence is not None:
                    raise ProviderInputContinuityConflict(
                        "adopted successor is fenced from further admission"
                    )
                seed._consume_for(
                    predecessor=cohort,
                    destination=call_target,
                    preparation_basis=preparation_basis,
                )
                return _issue_adopted_compaction_successor(
                    predecessor=cohort,
                    destination=call_target,
                    seed=seed,
                )
            if cohort.target_bundle == call_target.target_bundle:
                if seed is not None or direct_switch_admission is not None:
                    raise ProviderInputContinuityConflict(
                        "same-target append cannot rebuild its root: "
                        f"seed={type(seed).__name__}"
                    )
                return InstalledEpochAppend(cohort)
            if not isinstance(seed, CanonicalColdContinuationSeed):
                raise ProviderInputContinuityConflict(
                    "model switch lacks an admitted destination projection: "
                    f"seed={type(seed).__name__}"
                )
            admission = direct_switch_admission
            if (
                admission is None
                or admission.destination != call_target
                or admission.semantic_projection != semantic_projection
                or admission.wire_materialization != wire_input_plan.materialization
                or admission.quote != wire_input_plan.quote
            ):
                raise ProviderInputContinuityConflict(
                    "model switch admission does not exact-join its destination"
                )
            return _issue_explicit_model_switch_transition(
                predecessor=cohort,
                admission=admission,
            )

    def freeze_planning_input(
        self,
        *,
        scope: ProviderInputContinuityScope,
        canonical_frontier: ProcessLocalCanonicalFrontier,
        dispatch_anchor: ProviderInputDispatchAnchor,
    ) -> FrozenProviderInputAppendPlanningInput:
        return self._freeze_planning_input(
            scope=scope,
            canonical_frontier=canonical_frontier,
            dispatch_anchor=dispatch_anchor,
            detached_destination_projection=False,
        )

    def freeze_destination_projection_planning_input(
        self,
        *,
        scope: ProviderInputContinuityScope,
        canonical_frontier: ProcessLocalCanonicalFrontier,
        dispatch_anchor: ProviderInputDispatchAnchor,
    ) -> FrozenProviderInputAppendPlanningInput:
        """Freeze a non-installable Tier-3 projection outside A's prefix."""

        return self._freeze_planning_input(
            scope=scope,
            canonical_frontier=canonical_frontier,
            dispatch_anchor=dispatch_anchor,
            detached_destination_projection=True,
        )

    def _freeze_planning_input(
        self,
        *,
        scope: ProviderInputContinuityScope,
        canonical_frontier: ProcessLocalCanonicalFrontier,
        dispatch_anchor: ProviderInputDispatchAnchor,
        detached_destination_projection: bool,
    ) -> FrozenProviderInputAppendPlanningInput:
        self._require_scope(scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(scope)
            if slot is None:
                raise ProviderInputContinuityConflict(
                    "continuity scope lacks a lifecycle-issued bootstrap lease"
                )
            if slot.state is _SlotState.PREPARED:
                raise ProviderInputContinuityConflict(
                    "provider-input scope already owns a prepared candidate"
                )
            if slot.replay_reservation is not None:
                raise ProviderInputContinuityConflict(
                    "provider-input scope is settling an assistant replay fragment"
                )
            if slot.state is _SlotState.CLOSED:
                raise ProviderInputContinuityConflict("provider-input scope is closed")
            reservation: EmptyPreparationReservation | None = None
            if detached_destination_projection:
                predecessor_view = None
            elif slot.state is _SlotState.AUTHORIZED_EMPTY:
                lease = slot.bootstrap_lease
                if lease is None:
                    raise ProviderInputContinuityConflict(
                        "authorized empty scope lost its bootstrap lease"
                    )
                reservation = EmptyPreparationReservation(
                    lease=lease,
                    reservation_nonce=f"empty-preparation:{uuid4().hex}",
                    _seal=_EMPTY_PREPARATION_RESERVATION_SEAL,
                )
                slot.bootstrap_lease = None
                slot.empty_reservation = reservation
                slot.state = _SlotState.PREPARING_EMPTY
                predecessor_view = None
            elif slot.state is _SlotState.INSTALLED:
                cohort = slot.installed
                if cohort is None:
                    raise ProviderInputContinuityConflict(
                        "installed continuity scope lost its cohort"
                    )
                predecessor_view = cohort.view
            else:
                raise ProviderInputContinuityConflict(
                    "continuity scope is not available for preparation: "
                    f"{slot.state.value}"
                )
            return _planning_input_from_predecessor(
                scope=scope,
                predecessor_view=predecessor_view,
                canonical_frontier=canonical_frontier,
                dispatch_anchor=dispatch_anchor,
                empty_reservation=reservation,
            )

    def freeze_planning_sibling(
        self,
        *,
        basis: FrozenProviderInputAppendPlanningInput,
        canonical_frontier: ProcessLocalCanonicalFrontier,
        dispatch_anchor: ProviderInputDispatchAnchor,
    ) -> FrozenProviderInputAppendPlanningInput:
        """Build a variant against the exact predecessor frozen by ``basis``."""

        self._require_scope(basis.scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(basis.scope)
            installed_view = (
                None if slot is None or slot.installed is None else slot.installed.view
            )
            if (
                slot is None
                or slot.state not in {_SlotState.INSTALLED, _SlotState.PREPARING_EMPTY}
                or slot.replay_reservation is not None
                or installed_view is not basis.predecessor_view
                or slot.empty_reservation is not basis._empty_preparation_reservation
            ):
                raise ProviderInputContinuityConflict(
                    "provider-input planning predecessor changed during variants: "
                    f"state={None if slot is None else slot.state.value}, "
                    f"installed_matches={installed_view is basis.predecessor_view}, "
                    "empty_reservation_matches="
                    f"{slot is not None and slot.empty_reservation is basis._empty_preparation_reservation}, "
                    f"replay_reserved={slot is not None and slot.replay_reservation is not None}"
                )
            return _planning_input_from_predecessor(
                scope=basis.scope,
                predecessor_view=basis.predecessor_view,
                canonical_frontier=canonical_frontier,
                dispatch_anchor=dispatch_anchor,
                empty_reservation=basis._empty_preparation_reservation,
            )

    def register(self, candidate: PreparedProviderInputAppendCandidate) -> None:
        self._require_scope(candidate.scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(candidate.scope)
            if slot is None or slot.state is _SlotState.CLOSED:
                raise ProviderInputContinuityConflict("continuity scope is unavailable")
            if slot.state is _SlotState.PREPARED:
                raise ProviderInputContinuityConflict(
                    "continuity scope already owns a prepared candidate"
                )
            if slot.replay_reservation is not None:
                raise ProviderInputContinuityConflict(
                    "continuity scope is settling an assistant replay fragment"
                )
            installed_cohort = slot.installed
            installed = None if installed_cohort is None else installed_cohort.view
            if (
                candidate.wire_input_plan.compiled_semantic_fingerprint
                != candidate.resulting_compiled_input.compiled_semantic_fingerprint
                or candidate.wire_input_plan.message_placements_fingerprint
                != compiled_message_placements_fingerprint(
                    candidate.resulting_compiled_input.message_placements
                )
                or candidate.wire_input_plan.context_id
                != candidate.resulting_compiled_input.context_id
                or candidate.wire_input_plan.materialization.tool_items
                != tuple(
                    item.wire_tool
                    for item in candidate.direct_native_projection_set.projections
                )
            ):
                raise ProviderInputContinuityConflict(
                    "provider wire plan does not exact-join compiled input"
                )
            candidate_bytes = provider_input_logical_bytes(
                system_prompt=candidate.resulting_compiled_input.system_prompt,
                tools=candidate.resulting_compiled_input.tools,
                messages=candidate.resulting_compiled_input.messages,
            )
            if candidate_bytes > MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES:
                raise ProviderInputContinuityConflict(
                    "provider-input epoch exceeds its logical bound"
                )
            candidate_bytes = max(
                candidate_bytes,
                candidate.wire_input_plan.quote.final_wire_utf8_bytes,
            )
            installed_bytes = sum(
                _slot_installed_and_reserved_bytes(current)
                for current in self._slots.values()
            )
            current_bytes = 0 if installed is None else _view_resident_bytes(installed)
            if (
                installed_bytes - current_bytes + candidate_bytes
                > MAXIMUM_HOST_INSTALLED_BYTES
            ):
                raise ProviderInputContinuityConflict(
                    "Host provider-input resident bound is exhausted"
                )
            prepared_bytes = sum(
                max(
                    provider_input_logical_bytes(
                        system_prompt=current.prepared.resulting_compiled_input.system_prompt,
                        tools=current.prepared.resulting_compiled_input.tools,
                        messages=current.prepared.resulting_compiled_input.messages,
                    ),
                    current.prepared.wire_input_plan.quote.final_wire_utf8_bytes,
                )
                for current in self._slots.values()
                if current.prepared is not None
            )
            if (
                installed_bytes + prepared_bytes + candidate_bytes
                > MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES
            ):
                raise ProviderInputContinuityConflict(
                    "Host provider-input aggregate bound is exhausted"
                )
            transition = candidate.transition
            if candidate._preparation_basis is None:
                raise ProviderInputContinuityConflict(
                    "provider-input candidate lacks its sealed preparation basis"
                )
            if isinstance(transition, EmptyScopeColdStart):
                if (
                    slot.state is not _SlotState.BOUND_EMPTY
                    or slot.empty_reservation
                    is not candidate.planning._empty_preparation_reservation
                    or candidate.expected_epoch_revision != 0
                    or candidate.planning.predecessor_view is not None
                    or transition.bootstrap_authority.scope != candidate.scope
                    or transition.bootstrap_authority._preparation_basis
                    is not candidate._preparation_basis
                ):
                    raise ProviderInputContinuityConflict(
                        "empty cold candidate lacks its bound authority"
                    )
            elif isinstance(
                transition,
                (
                    InstalledEpochAppend,
                    ExplicitModelSwitchColdStart,
                    AdoptedCompactionSuccessor,
                ),
            ):
                if (
                    slot.state is not _SlotState.INSTALLED
                    or installed_cohort is None
                    or transition.predecessor is not installed_cohort
                    or candidate.expected_epoch_revision != installed.epoch_revision
                    or candidate.planning.predecessor_view is not installed
                ):
                    raise ProviderInputContinuityConflict(
                        "successor append candidate is stale"
                    )
                if isinstance(transition, InstalledEpochAppend):
                    if candidate.epoch_nonce != installed.epoch_nonce:
                        raise ProviderInputContinuityConflict(
                            "installed append changed epoch identity"
                        )
                    installed.canonical_frontier.require_prefix_of(
                        candidate.resulting_canonical_frontier
                    )
                    compiled = candidate.resulting_compiled_input
                    if compiled.system_prompt != installed.system_prompt:
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed system root"
                        )
                    if compiled.tools != installed.tools:
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed tool surface"
                        )
                    if (
                        candidate.direct_native_projection_set
                        != installed.direct_native_projection_set
                    ):
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed native tool projection"
                        )
                    if (
                        compiled.messages[: len(installed.messages)]
                        != installed.messages
                    ):
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed message prefix"
                        )
                    old_wire = installed.wire_input_plan.materialization
                    new_wire = candidate.wire_input_plan.materialization
                    if (
                        old_wire.root_policy_value != new_wire.root_policy_value
                        or old_wire.tool_items != new_wire.tool_items
                        or new_wire.ordered_input_items[
                            : len(old_wire.ordered_input_items)
                        ]
                        != old_wire.ordered_input_items
                    ):
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed provider wire prefix"
                        )
                else:
                    if isinstance(transition, ExplicitModelSwitchColdStart) and (
                        transition.admission.destination != candidate.call_target
                        or transition.admission.semantic_projection
                        != FrozenModelInputSemanticProjection(
                            canonical_input_identity=(
                                candidate.resulting_compiled_input.canonical_input_identity
                            ),
                            system_prompt=candidate.resulting_compiled_input.system_prompt,
                            messages=candidate.resulting_compiled_input.messages,
                            message_placements=(
                                candidate.resulting_compiled_input.message_placements
                            ),
                            tools=candidate.resulting_compiled_input.tools,
                            final_estimate=(
                                candidate.resulting_compiled_input.final_estimate
                            ),
                            source_decisions=(
                                candidate.resulting_compiled_input.source_decisions
                            ),
                            tool_result_decisions=(
                                candidate.resulting_compiled_input.tool_result_decisions
                            ),
                            diagnostic_codes=(
                                candidate.resulting_compiled_input.diagnostic_codes
                            ),
                            source_collection_fingerprint=(
                                candidate.resulting_compiled_input.source_collection_fingerprint
                            ),
                            compile_binding_fingerprint=(
                                candidate.resulting_compiled_input.compile_binding_fingerprint
                            ),
                        )
                        or transition.admission.wire_materialization
                        != candidate.wire_input_plan.materialization
                        or transition.admission.quote != candidate.wire_input_plan.quote
                    ):
                        raise ProviderInputContinuityConflict(
                            "direct-switch candidate changed its frozen admission"
                        )
                    if candidate.epoch_nonce == installed.epoch_nonce:
                        raise ProviderInputContinuityConflict(
                            "new epoch successor reused the installed epoch identity"
                        )
                    if (
                        installed.canonical_frontier.context_base_semantic_identity
                        == candidate.resulting_canonical_frontier.context_base_semantic_identity
                    ):
                        installed.canonical_frontier.require_prefix_of(
                            candidate.resulting_canonical_frontier
                        )
            else:
                raise ProviderInputContinuityConflict(
                    "provider-input transition union is not closed"
                )
            slot.prepared = candidate
            slot.state = _SlotState.PREPARED

    def install(
        self,
        *,
        candidate: PreparedProviderInputAppendCandidate,
        execution: object,
    ) -> ProcessLocalProviderInputInstallPermit:
        self._require_scope(candidate.scope)
        with self._lock:
            slot = self._slots.get(candidate.scope)
            if slot is None or slot.prepared is not candidate:
                raise ProviderInputContinuityConflict(
                    "prepared append candidate does not exact-join its scope"
                )
            scope = candidate.scope
            compiled = candidate.resulting_compiled_input
            revision = candidate.expected_epoch_revision + 1
            view = FrozenProviderInputEpochView(
                scope=scope,
                epoch_nonce=candidate.epoch_nonce,
                epoch_revision=revision,
                system_prompt=compiled.system_prompt,
                tools=compiled.tools,
                messages=compiled.messages,
                message_placements=compiled.message_placements,
                wire_input_plan=candidate.wire_input_plan,
                tool_exposure_plan=candidate.tool_exposure_plan,
                canonical_frontier=candidate.resulting_canonical_frontier,
                source_heads=candidate.resulting_source_heads,
                final_estimate=compiled.final_estimate,
                logical_bytes=provider_input_logical_bytes(
                    system_prompt=compiled.system_prompt,
                    tools=compiled.tools,
                    messages=compiled.messages,
                ),
                semantic_prefix_fingerprint=provider_input_prefix_fingerprint(
                    system_prompt=compiled.system_prompt,
                    tools=compiled.tools,
                    messages=compiled.messages,
                ),
                assistant_replay_fragments=(
                    ()
                    if slot.installed is None
                    or candidate.epoch_nonce != slot.installed.view.epoch_nonce
                    else slot.installed.view.assistant_replay_fragments
                ),
                tool_result_decisions=compiled.tool_result_decisions,
            )
            slot.installed = InstalledEpochRuntimeCohort(
                view=view,
                target_bundle=candidate.call_target.target_bundle,
            )
            slot.bootstrap_lease = None
            slot.empty_reservation = None
            slot.prepared = None
            slot.state = _SlotState.INSTALLED
            permit = ProcessLocalProviderInputInstallPermit(
                scope=scope,
                epoch_nonce=view.epoch_nonce,
                epoch_revision=view.epoch_revision,
                permit_nonce=f"provider-input-permit:{uuid4().hex}",
            )
            self._issued_permits[permit.permit_nonce] = (
                _IssuedProviderInputInstallPermit(
                    permit=permit,
                    candidate=candidate,
                    execution=execution,
                )
            )
            return permit

    def reserve_assistant_replay_fragment(
        self,
        *,
        scope: ProviderInputContinuityScope,
        epoch_nonce: str,
        epoch_revision: int,
        fragment: ProviderAssistantReplayFragment,
    ) -> ProcessLocalAssistantReplayFragmentReservation:
        """Reserve exact epoch/Host capacity before canonical assistant mutation."""

        self._require_scope(scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(scope)
            view = (
                None if slot is None or slot.installed is None else slot.installed.view
            )
            if (
                slot is None
                or slot.state is not _SlotState.INSTALLED
                or view is None
                or view.epoch_nonce != epoch_nonce
                or view.epoch_revision != epoch_revision
            ):
                raise ProviderInputContinuityConflict(
                    "assistant replay fragment targets a stale epoch"
                )
            if slot.replay_reservation is not None:
                raise ProviderInputContinuityConflict(
                    "assistant replay fragment reservation is already active"
                )
            existing = tuple(
                item
                for item in view.assistant_replay_fragments
                if item.assistant_entry_id == fragment.assistant_entry_id
            )
            if existing:
                if existing != (fragment,):
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment identity conflicts"
                    )
                already_bound = True
                additional_bytes = 0
            else:
                already_bound = False
                current_view_bytes = _view_resident_bytes(view)
                new_view_bytes = _view_resident_bytes(
                    view,
                    additional_fragment=fragment,
                )
                if new_view_bytes > MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES:
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment exhausts the epoch byte bound"
                    )
                additional_bytes = new_view_bytes - current_view_bytes
                host_bytes = sum(
                    _slot_installed_and_reserved_bytes(current)
                    for current in self._slots.values()
                )
                if host_bytes + additional_bytes > MAXIMUM_HOST_INSTALLED_BYTES:
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment exhausts the Host byte bound"
                    )
                prepared_bytes = sum(
                    max(
                        provider_input_logical_bytes(
                            system_prompt=(
                                current.prepared.resulting_compiled_input.system_prompt
                            ),
                            tools=current.prepared.resulting_compiled_input.tools,
                            messages=current.prepared.resulting_compiled_input.messages,
                        ),
                        current.prepared.wire_input_plan.quote.final_wire_utf8_bytes,
                    )
                    for current in self._slots.values()
                    if current.prepared is not None
                )
                if host_bytes + additional_bytes + prepared_bytes > (
                    MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES
                ):
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment exhausts the Host aggregate bound"
                    )
            reservation = ProcessLocalAssistantReplayFragmentReservation(
                scope=scope,
                epoch_nonce=epoch_nonce,
                epoch_revision=epoch_revision,
                fragment=fragment,
                additional_resident_bytes=additional_bytes,
                already_bound=already_bound,
                reservation_nonce=f"assistant-replay-reservation:{uuid4().hex}",
                _seal=_ASSISTANT_REPLAY_RESERVATION_SEAL,
            )
            slot.replay_reservation = reservation
            return reservation

    def promote_assistant_replay_fragment(
        self,
        reservation: ProcessLocalAssistantReplayFragmentReservation,
    ) -> None:
        """Promote a pre-admitted fragment after the exact assistant is FULL."""

        self._require_scope(reservation.scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(reservation.scope)
            view = (
                None if slot is None or slot.installed is None else slot.installed.view
            )
            if (
                slot is None
                or slot.replay_reservation is not reservation
                or slot.state is not _SlotState.INSTALLED
                or view is None
                or view.epoch_nonce != reservation.epoch_nonce
                or view.epoch_revision != reservation.epoch_revision
            ):
                raise ProviderInputContinuityConflict(
                    "assistant replay reservation targets a stale epoch"
                )
            fragment = reservation.fragment
            existing = tuple(
                item
                for item in view.assistant_replay_fragments
                if item.assistant_entry_id == fragment.assistant_entry_id
            )
            if reservation._already_bound:
                if existing != (fragment,):
                    raise ProviderInputContinuityConflict(
                        "bound assistant replay fragment identity drifted"
                    )
            else:
                if existing:
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment appeared after reservation"
                    )
                # Capacity was charged while the canonical mutation was still
                # impossible.  Promotion contains no fallible allocator gate.
                slot.installed = replace(
                    slot.installed,
                    view=replace(
                        view,
                        assistant_replay_fragments=(
                            *view.assistant_replay_fragments,
                            fragment,
                        ),
                    ),
                )
            slot.replay_reservation = None

    def release_assistant_replay_fragment_reservation(
        self,
        reservation: ProcessLocalAssistantReplayFragmentReservation,
    ) -> None:
        """Release a NONE/CONFLICT/failed pre-commit capacity claim."""

        self._require_scope(reservation.scope)
        with self._lock:
            slot = self._slots.get(reservation.scope)
            if slot is None or slot.state is _SlotState.CLOSED:
                return
            if slot.replay_reservation is reservation:
                slot.replay_reservation = None
                return
            if slot.replay_reservation is not None:
                raise ProviderInputContinuityConflict(
                    "assistant replay reservation identity conflicts"
                )

    def _consume_install_permit(
        self,
        permit: ProcessLocalProviderInputInstallPermit,
        *,
        candidate: PreparedProviderInputAppendCandidate,
        execution: object,
    ) -> None:
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            issued = self._issued_permits.get(permit.permit_nonce)
            if (
                issued is None
                or issued.permit is not permit
                or issued.candidate is not candidate
                or issued.execution is not execution
            ):
                raise ProviderInputContinuityConflict(
                    "provider-input install permit was not issued for this execution"
                )
            del self._issued_permits[permit.permit_nonce]

    def _require_registered_plan(
        self,
        *,
        candidate: PreparedProviderInputAppendCandidate,
        wire_input_plan: FrozenProviderWireInputPlan,
        tool_exposure_plan: FrozenToolCapabilityExposurePlan,
    ) -> None:
        self._require_scope(candidate.scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(candidate.scope)
            if (
                slot is None
                or slot.prepared is not candidate
                or candidate.wire_input_plan is not wire_input_plan
                or candidate.tool_exposure_plan is not tool_exposure_plan
            ):
                raise ProviderInputContinuityConflict(
                    "provider wire plan was not registered for this candidate"
                )

    def discard(self, candidate: PreparedProviderInputAppendCandidate) -> None:
        self._require_scope(candidate.scope)
        with self._lock:
            slot = self._slots.get(candidate.scope)
            if slot is None or slot.prepared is not candidate:
                raise ProviderInputContinuityConflict(
                    "prepared append discard does not exact-join"
                )
            slot.prepared = None
            if isinstance(candidate.transition, EmptyScopeColdStart):
                reservation = slot.empty_reservation
                if reservation is None:
                    raise ProviderInputContinuityConflict(
                        "empty candidate lost its preparation reservation"
                    )
                slot.empty_reservation = None
                slot.bootstrap_lease = reservation.lease
                slot.state = _SlotState.AUTHORIZED_EMPTY
            else:
                slot.state = _SlotState.INSTALLED

    def current_view(
        self, scope: ProviderInputContinuityScope
    ) -> FrozenProviderInputEpochView | None:
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            return (
                None if slot is None or slot.installed is None else slot.installed.view
            )

    def current_cohort(
        self, scope: ProviderInputContinuityScope
    ) -> InstalledEpochRuntimeCohort | None:
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            return None if slot is None else slot.installed

    def retire_terminal_subagent_scope(
        self, scope: ProviderInputContinuityScope
    ) -> None:
        self._require_scope(scope)
        if scope.scope_kind is not ModelInputScopeKind.SUBAGENT_TASK:
            raise ProviderInputContinuityConflict(
                "only a terminal child scope may be retired"
            )
        with self._lock:
            slot = self._slots.pop(scope, None)
            if slot is None:
                raise ProviderInputContinuityConflict(
                    "terminal child continuity scope is absent"
                )
            slot.installed = None
            slot.bootstrap_lease = None
            slot.empty_reservation = None
            slot.prepared = None
            slot.replay_reservation = None
            slot.no_continuation_fence = None
            slot.no_continuation_closure = None
            slot.no_continuation_revocation = None
            slot.empty_adoption_pending = None
            slot.empty_adoption_settling = None
            slot.empty_adoption_revocation = None
            slot.state = _SlotState.CLOSED
            self._issued_permits = {
                nonce: issued
                for nonce, issued in self._issued_permits.items()
                if issued.permit.scope != scope
            }

    def install_no_continuation_admission_fence(
        self,
        *,
        scope: ProviderInputContinuityScope,
        predecessor: InstalledEpochRuntimeCohort,
        turn_id: str,
        attempt_token: object,
        confirmation: object,
        candidate: object,
        cut: PreparedProviderInputCut,
        destination: FrozenEpochModelCallTarget,
        adopted_snapshot_id: str,
        adopted_binding_revision_id: str,
    ) -> NoContinuationAdmissionFence:
        """Block every adopted-successor retry from one exact predecessor."""

        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            if (
                self._closed
                or slot is None
                or slot.state is not _SlotState.INSTALLED
                or slot.installed is not predecessor
                or slot.no_continuation_fence is not None
                or slot.prepared is not None
                or slot.replay_reservation is not None
            ):
                raise ProviderInputContinuityConflict(
                    "no-continuation fence does not exact-join installed predecessor"
                )
            fence = NoContinuationAdmissionFence(
                scope=scope,
                predecessor=predecessor,
                turn_id=turn_id,
                attempt_token=attempt_token,
                confirmation=confirmation,
                candidate=candidate,
                cut=cut,
                destination=destination,
                adopted_snapshot_id=adopted_snapshot_id,
                adopted_binding_revision_id=adopted_binding_revision_id,
                fence_nonce=f"no-continuation-fence:{uuid4().hex}",
                _seal=_NO_CONTINUATION_FENCE_SEAL,
            )
            slot.no_continuation_fence = fence
            return fence

    def begin_empty_adoption(
        self,
        *,
        scope: ProviderInputContinuityScope,
        reservation: EmptyPreparationReservation,
        preparation_basis: object,
        attempt_token: object,
        candidate: object,
        dry_result: object,
    ) -> EmptyAdoptionPending:
        """Consume one pre-install Empty preparation into adoption-pending."""

        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            dry_basis = getattr(dry_result, "basis", None)
            dry_source = getattr(dry_basis, "source", None)
            canonical_read = getattr(dry_basis, "canonical_read", None)
            identity = getattr(
                getattr(getattr(canonical_read, "compile_snapshot", None), "canonical_input", None),
                "identity",
                None,
            )
            binding = getattr(
                getattr(canonical_read, "compile_snapshot", None),
                "context_binding_fact",
                None,
            )
            adoption_scope = getattr(candidate, "scope", None)
            if (
                self._closed
                or slot is None
                or slot.state is not _SlotState.PREPARING_EMPTY
                or slot.empty_reservation is not reservation
                or getattr(dry_source, "reservation", None) is not reservation
                or getattr(preparation_basis, "call_target", None)
                is not getattr(dry_basis, "destination", None)
                or getattr(attempt_token, "session_id", None) != scope.session_id
                or getattr(attempt_token, "turn_id", None)
                != getattr(adoption_scope, "turn_id", None)
                or getattr(attempt_token, "scope_kind", None) is not scope.scope_kind
                or getattr(attempt_token, "scope_subagent_task_id", None)
                != scope.scope_subagent_task_id
                or getattr(adoption_scope, "session_id", None) != scope.session_id
                or getattr(adoption_scope, "scope_kind", None) is not scope.scope_kind
                or getattr(adoption_scope, "scope_subagent_task_id", None)
                != scope.scope_subagent_task_id
                or getattr(adoption_scope, "turn_id", None)
                != getattr(identity, "turn_id", None)
                or getattr(candidate, "snapshot", None) is None
                or candidate.snapshot.snapshot_id
                != getattr(binding, "context_snapshot_id", None)
                or candidate.binding.binding_revision_id
                != getattr(binding, "binding_revision_id", None)
                or getattr(dry_basis.destination, "session_id", None)
                != scope.session_id
                or getattr(dry_basis.destination, "turn_id", None)
                != getattr(adoption_scope, "turn_id", None)
                or slot.prepared is not None
                or slot.replay_reservation is not None
            ):
                raise ProviderInputContinuityConflict(
                    "empty adoption does not exact-join current preparation"
                )
            pending = EmptyAdoptionPending(
                reservation=reservation,
                preparation_basis=preparation_basis,
                attempt_token=attempt_token,
                candidate=candidate,
                dry_result=dry_result,
                _seal=_EMPTY_ADOPTION_PENDING_SEAL,
            )
            slot.empty_adoption_pending = pending
            slot.state = _SlotState.EMPTY_ADOPTION_PENDING
            return pending

    def begin_empty_full_settlement(
        self,
        *,
        pending: EmptyAdoptionPending,
        confirmation: object,
    ) -> EmptyAdoptionFullSettling:
        scope = pending.reservation.lease.scope
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            if (
                slot is None
                or slot.state is not _SlotState.EMPTY_ADOPTION_PENDING
                or slot.empty_adoption_pending is not pending
                or slot.empty_reservation is not pending.reservation
                or slot.empty_adoption_revocation is not None
                or getattr(confirmation, "candidate", None) is not pending.candidate
            ):
                raise ProviderInputContinuityConflict(
                    "empty adoption FULL confirmation is stale"
                )
            settling = EmptyAdoptionFullSettling(
                pending=pending,
                confirmation=confirmation,
                _seal=_EMPTY_ADOPTION_SETTLING_SEAL,
            )
            slot.empty_adoption_settling = settling
            slot.empty_adoption_revocation = RevocationNotStarted(
                settling,
                _seal=_REVOCATION_NOT_STARTED_SEAL,
            )
            slot.state = _SlotState.EMPTY_ADOPTION_FULL_SETTLING
            return settling

    def restore_empty_adoption_none(
        self, pending: EmptyAdoptionPending, confirmation: object
    ) -> None:
        """Restore the exact pre-adoption Empty preparation after a NONE result."""

        scope = pending.reservation.lease.scope
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            if (
                slot is None
                or slot.state is not _SlotState.EMPTY_ADOPTION_PENDING
                or slot.empty_adoption_pending is not pending
                or slot.empty_reservation is not pending.reservation
                or slot.empty_adoption_revocation is not None
                or getattr(confirmation, "candidate", None) is not pending.candidate
                or getattr(getattr(confirmation, "kind", None), "value", None)
                != "NONE"
            ):
                raise ProviderInputContinuityConflict(
                    "empty adoption NONE settlement is stale"
                )
            slot.empty_adoption_pending = None
            slot.state = _SlotState.PREPARING_EMPTY

    def begin_empty_conflict_settlement(
        self,
        *,
        pending: EmptyAdoptionPending,
        confirmation: object,
    ) -> EmptyAdoptionConflictSettling:
        scope = pending.reservation.lease.scope
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            if (
                slot is None
                or slot.state is not _SlotState.EMPTY_ADOPTION_PENDING
                or slot.empty_adoption_pending is not pending
                or slot.empty_reservation is not pending.reservation
                or slot.empty_adoption_revocation is not None
                or getattr(confirmation, "candidate", None) is not pending.candidate
            ):
                raise ProviderInputContinuityConflict(
                    "empty adoption CONFLICT confirmation is stale"
                )
            settling = EmptyAdoptionConflictSettling(
                pending=pending,
                confirmation=confirmation,
                _seal=_EMPTY_ADOPTION_SETTLING_SEAL,
            )
            slot.empty_adoption_settling = settling
            slot.empty_adoption_revocation = RevocationNotStarted(
                settling,
                _seal=_REVOCATION_NOT_STARTED_SEAL,
            )
            slot.state = _SlotState.EMPTY_ADOPTION_CONFLICT_SETTLING
            return settling

    def revoke_empty_adoption_preparation_resources(
        self,
        settling: EmptyAdoptionFullSettling | EmptyAdoptionConflictSettling,
    ) -> EmptyAdoptionRevokedProviderPreparationResources:
        scope = settling.pending.reservation.lease.scope
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            if (
                slot is None
                or slot.state
                not in {
                    _SlotState.EMPTY_ADOPTION_FULL_SETTLING,
                    _SlotState.EMPTY_ADOPTION_CONFLICT_SETTLING,
                }
                or slot.empty_adoption_settling is not settling
                or not isinstance(slot.empty_adoption_revocation, RevocationNotStarted)
                or slot.empty_adoption_revocation.exact_subject is not settling
            ):
                raise ProviderInputContinuityConflict(
                    "empty adoption resource revocation is stale"
                )
            revoked = EmptyAdoptionRevokedProviderPreparationResources(
                settling=settling,
                _seal=_EMPTY_ADOPTION_REVOKED_SEAL,
            )
            slot.empty_adoption_revocation = revoked
            slot.empty_reservation = None
            slot.prepared = None
            return revoked

    def publish_empty_after_full_adoption_release(
        self,
        *,
        settling: EmptyAdoptionFullSettling,
        revoked: EmptyAdoptionRevokedProviderPreparationResources,
        release_full: object,
        safe_point_owner: object,
    ) -> None:
        pending = settling.pending
        scope = pending.reservation.lease.scope
        self._require_scope(scope)
        from pulsara_agent.conversation_kernel.safe_point import (
            ProviderPreparationReleaseFull,
        )

        if not isinstance(release_full, ProviderPreparationReleaseFull):
            raise ProviderInputContinuityConflict(
                "empty adoption lacks release-FULL authority"
            )
        release_full._consume(
            exact_subject=settling,
            owner=safe_point_owner,
        )
        with self._lock:
            slot = self._slots.get(scope)
            candidate = pending.candidate
            snapshot_id = getattr(
                getattr(candidate, "snapshot", None), "snapshot_id", ""
            )
            binding_id = getattr(
                getattr(candidate, "binding", None), "binding_revision_id", ""
            )
            if (
                slot is None
                or slot.state is not _SlotState.EMPTY_ADOPTION_FULL_SETTLING
                or slot.empty_adoption_settling is not settling
                or slot.empty_adoption_revocation is not revoked
                or revoked.settling is not settling
                or not snapshot_id
                or not binding_id
            ):
                raise ProviderInputContinuityConflict(
                    "empty adoption lease publication is stale"
                )
            slot.bootstrap_lease = AuthorizedEmptyBootstrapLease(
                scope=scope,
                lease_nonce=f"empty-bootstrap-lease:{uuid4().hex}",
                source=AdoptedBaseLeaseSource(
                    writer_guard=self._writer_guard,
                    adoption_confirmation=settling.confirmation,
                    snapshot_id=snapshot_id,
                    binding_revision_id=binding_id,
                    _seal=_BOOTSTRAP_LEASE_SOURCE_SEAL,
                ),
                _seal=_BOOTSTRAP_LEASE_SEAL,
            )
            slot.empty_adoption_pending = None
            slot.empty_adoption_settling = None
            slot.empty_adoption_revocation = None
            slot.state = _SlotState.AUTHORIZED_EMPTY

    def quarantine_empty_adoption_release(
        self,
        settling: EmptyAdoptionFullSettling | EmptyAdoptionConflictSettling,
    ) -> None:
        scope = settling.pending.reservation.lease.scope
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            if (
                slot is None
                or slot.empty_adoption_settling is not settling
                or not isinstance(
                    slot.empty_adoption_revocation,
                    EmptyAdoptionRevokedProviderPreparationResources,
                )
            ):
                raise ProviderInputContinuityConflict(
                    "empty adoption quarantine is stale"
                )
            slot.state = _SlotState.EMPTY_ADOPTION_RESOURCE_RELEASE_QUARANTINE

    def begin_no_continuation_settlement(
        self,
        *,
        fence: NoContinuationAdmissionFence,
        closure: NoContinuationClosure,
    ) -> None:
        """Consume Installed into a non-authorizing settling state."""

        self._require_scope(fence.scope)
        with self._lock:
            slot = self._slots.get(fence.scope)
            if (
                self._closed
                or slot is None
                or slot.state is not _SlotState.INSTALLED
                or slot.installed is not fence.predecessor
                or slot.no_continuation_fence is not fence
                or closure.fence is not fence
                or slot.prepared is not None
                or slot.replay_reservation is not None
                or slot.no_continuation_revocation is not None
                or any(
                    issued.permit.scope == fence.scope
                    for issued in self._issued_permits.values()
                )
            ):
                raise ProviderInputContinuityConflict(
                    "no-continuation closure is not quiescent/current"
                )
            slot.no_continuation_closure = closure
            slot.no_continuation_revocation = RevocationNotStarted(
                closure,
                _seal=_REVOCATION_NOT_STARTED_SEAL,
            )
            slot.state = _SlotState.INSTALLED_NO_CONTINUATION_SETTLING

    def revoke_no_continuation_preparation_resources(
        self,
        *,
        fence: NoContinuationAdmissionFence,
        closure: NoContinuationClosure,
    ) -> RevokedProviderPreparationResources:
        self._require_scope(fence.scope)
        with self._lock:
            slot = self._slots.get(fence.scope)
            if (
                slot is None
                or slot.state is not _SlotState.INSTALLED_NO_CONTINUATION_SETTLING
                or slot.no_continuation_fence is not fence
                or slot.no_continuation_closure is not closure
                or not isinstance(slot.no_continuation_revocation, RevocationNotStarted)
                or slot.no_continuation_revocation.exact_subject is not closure
            ):
                raise ProviderInputContinuityConflict(
                    "no-continuation revocation is stale"
                )
            revoked = RevokedProviderPreparationResources(
                fence=fence,
                _seal=_REVOKED_PREPARATION_RESOURCES_SEAL,
            )
            slot.no_continuation_revocation = revoked
            return revoked

    def publish_empty_after_no_continuation_release(
        self,
        *,
        fence: NoContinuationAdmissionFence,
        closure: NoContinuationClosure,
        revoked: RevokedProviderPreparationResources,
        release_full: object,
        safe_point_owner: object,
    ) -> None:
        """Publish adopted-base lease only after logical/physical release is FULL."""

        from pulsara_agent.conversation_kernel.safe_point import (
            ProviderPreparationReleaseFull,
        )

        self._require_scope(fence.scope)
        if not isinstance(release_full, ProviderPreparationReleaseFull):
            raise ProviderInputContinuityConflict(
                "no-continuation publication lacks release-FULL authority"
            )
        release_full._consume(
            exact_subject=closure,
            owner=safe_point_owner,
        )
        with self._lock:
            slot = self._slots.get(fence.scope)
            if (
                slot is None
                or slot.state is not _SlotState.INSTALLED_NO_CONTINUATION_SETTLING
                or slot.installed is not fence.predecessor
                or slot.no_continuation_fence is not fence
                or slot.no_continuation_closure is not closure
                or slot.no_continuation_revocation is not revoked
                or revoked.fence is not fence
            ):
                raise ProviderInputContinuityConflict(
                    "no-continuation final publication is stale"
                )
            slot.installed = None
            slot.empty_reservation = None
            slot.bootstrap_lease = AuthorizedEmptyBootstrapLease(
                scope=fence.scope,
                lease_nonce=f"empty-bootstrap-lease:{uuid4().hex}",
                source=AdoptedBaseLeaseSource(
                    writer_guard=self._writer_guard,
                    adoption_confirmation=fence.confirmation,
                    snapshot_id=fence.adopted_snapshot_id,
                    binding_revision_id=fence.adopted_binding_revision_id,
                    _seal=_BOOTSTRAP_LEASE_SOURCE_SEAL,
                ),
                _seal=_BOOTSTRAP_LEASE_SEAL,
            )
            slot.no_continuation_fence = None
            slot.no_continuation_closure = None
            slot.no_continuation_revocation = None
            slot.state = _SlotState.AUTHORIZED_EMPTY

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for slot in self._slots.values():
                slot.installed = None
                slot.bootstrap_lease = None
                slot.empty_reservation = None
                slot.prepared = None
                slot.replay_reservation = None
                slot.no_continuation_fence = None
                slot.no_continuation_closure = None
                slot.no_continuation_revocation = None
                slot.empty_adoption_pending = None
                slot.empty_adoption_settling = None
                slot.empty_adoption_revocation = None
                slot.state = _SlotState.CLOSED
            self._slots.clear()
            self._issued_permits.clear()

    def _require_scope(self, scope: ProviderInputContinuityScope) -> None:
        if scope.session_id != self._session_id:
            raise ProviderInputContinuityConflict(
                "continuity scope belongs to another session"
            )

    def _admit_scope_capacity_locked(self, scope: ProviderInputContinuityScope) -> None:
        root_count = sum(item.scope_kind.value == "ROOT" for item in self._slots)
        child_count = len(self._slots) - root_count
        if scope.scope_kind.value == "ROOT" and root_count >= MAXIMUM_ROOT_SCOPES:
            raise ProviderInputContinuityConflict(
                "ROOT continuity scope already exists"
            )
        if (
            scope.scope_kind.value != "ROOT"
            and child_count >= self._maximum_child_scopes
        ):
            raise ProviderInputContinuityConflict(
                "child continuity scope capacity is exhausted"
            )

    @staticmethod
    def _new_authorized_empty_slot(
        scope: ProviderInputContinuityScope, *, source: BootstrapLeaseSource
    ) -> _Slot:
        lease = AuthorizedEmptyBootstrapLease(
            scope=scope,
            lease_nonce=f"empty-bootstrap-lease:{uuid4().hex}",
            source=source,
            _seal=_BOOTSTRAP_LEASE_SEAL,
        )
        return _Slot(
            state=_SlotState.AUTHORIZED_EMPTY,
            bootstrap_lease=lease,
        )


def _view_resident_bytes(
    view: FrozenProviderInputEpochView,
    *,
    additional_fragment: ProviderAssistantReplayFragment | None = None,
) -> int:
    materialized_fragments = {
        item.replay_fragment_fingerprint for item in view.wire_input_plan.replacements
    }
    fragments = view.assistant_replay_fragments + (
        () if additional_fragment is None else (additional_fragment,)
    )
    unmaterialized = sum(
        item.logical_utf8_bytes
        for item in fragments
        if item.fragment_fingerprint not in materialized_fragments
    )
    return (
        max(
            view.logical_bytes,
            view.wire_input_plan.quote.final_wire_utf8_bytes,
        )
        + unmaterialized
    )


def _planning_input_from_predecessor(
    *,
    scope: ProviderInputContinuityScope,
    predecessor_view: FrozenProviderInputEpochView | None,
    canonical_frontier: ProcessLocalCanonicalFrontier,
    dispatch_anchor: ProviderInputDispatchAnchor,
    empty_reservation: EmptyPreparationReservation | None,
) -> FrozenProviderInputAppendPlanningInput:
    if predecessor_view is None:
        predecessor = ProviderInputAdmissionPredecessorKind.EMPTY
        delta = canonical_frontier.ordered_item_fingerprints
    else:
        predecessor = ProviderInputAdmissionPredecessorKind.INSTALLED
        old = predecessor_view.canonical_frontier
        if (
            old.context_base_semantic_identity
            == canonical_frontier.context_base_semantic_identity
            and canonical_frontier.ordered_item_fingerprints[
                : len(old.ordered_item_fingerprints)
            ]
            == old.ordered_item_fingerprints
        ):
            delta = canonical_frontier.ordered_item_fingerprints[
                len(old.ordered_item_fingerprints) :
            ]
        else:
            # A legal context-base reset must be evaluated by the pure
            # compiler using the frozen compatibility fact. Planning records
            # the full rematerialization input and never repairs a rewrite.
            delta = canonical_frontier.ordered_item_fingerprints
    return FrozenProviderInputAppendPlanningInput(
        planning_nonce=f"provider-input-planning:{uuid4().hex}",
        scope=scope,
        predecessor=predecessor,
        predecessor_view=predecessor_view,
        dispatch_anchor=dispatch_anchor,
        canonical_delta_fingerprints=delta,
        _empty_preparation_reservation=empty_reservation,
    )


def _slot_installed_and_reserved_bytes(slot: _Slot) -> int:
    installed = (
        0 if slot.installed is None else _view_resident_bytes(slot.installed.view)
    )
    reserved = (
        0
        if slot.replay_reservation is None
        else slot.replay_reservation.additional_resident_bytes
    )
    return installed + reserved


__all__ = [
    "HostProviderInputContinuityOwner",
    "MAXIMUM_CHILD_SCOPES",
    "MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES",
    "MAXIMUM_HOST_INSTALLED_BYTES",
    "MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES",
    "MAXIMUM_ROOT_SCOPES",
    "ProviderInputContinuityConflict",
    "ProcessLocalAssistantReplayFragmentReservation",
    "ProcessLocalProviderInputInstallAuthority",
]
