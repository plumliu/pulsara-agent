"""Process-local provider safe-point owner for one Host activation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import TYPE_CHECKING, Callable, Iterator, TypeVar

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.steer import (
    PreparedActiveRootInputAdmission,
    PreparedActiveRootInputCandidate,
    PreparedRootProviderInputAdmission,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    AcceptedSubagentCompletion,
    ConversationKernelConflict,
    ConversationKernelRepository,
    PreparedAutomaticSubagentCompletion,
)
from pulsara_agent.conversation_kernel.reader import (
    OwnerIssuedCanonicalDispatchObservation,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    OwnerIssuedCapabilityDispatchObservation,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.ports.terminal_observation import (
    PreparedInstallationTarget,
    TerminalObservationInstallationAttempt,
)
from pulsara_agent.ports.user_control_feedback import (
    UserControlFeedbackInstallationAttempt,
)
from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator


T = TypeVar("T")

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.input_continuity import (
        HostProviderInputContinuityOwner,
        EmptyAdoptionPending,
        NoContinuationAdmissionFence,
        NoContinuationProductEvidence,
    )
    from pulsara_agent.llm.frozen_target import FrozenEpochModelCallTarget
    from pulsara_agent.model_input.continuity import (
        PreparedProviderInputAppendCandidate,
        ProcessLocalProviderInputInstallPermit,
        ProviderInputEpochTransition,
    )


_PREPARATION_BASIS_SEAL = object()
_PROVIDER_PREPARATION_RELEASE_FULL_SEAL = object()


class ExternalSourceNotAtSafePoint(RuntimeError):
    """A ROOT-visible source cannot be accepted behind an active input cut."""


@dataclass(frozen=True, slots=True, init=False)
class ProviderPreparationReleaseFull:
    """One-shot proof that logical revocation and physical close both finished."""

    exact_subject: object
    _owner: "ProviderSafePointCoordinator"
    _consumed: bool

    def __init__(
        self,
        *,
        exact_subject: object,
        owner: "ProviderSafePointCoordinator",
        _seal: object,
    ) -> None:
        if _seal is not _PROVIDER_PREPARATION_RELEASE_FULL_SEAL:
            raise TypeError("provider preparation release is safe-point-issued")
        object.__setattr__(self, "exact_subject", exact_subject)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_consumed", False)

    def _consume(
        self, *, exact_subject: object, owner: "ProviderSafePointCoordinator"
    ) -> None:
        if (
            self._owner is not owner
            or self.exact_subject is not exact_subject
            or self._consumed
        ):
            raise RuntimeError("provider preparation release proof is stale")
        object.__setattr__(self, "_consumed", True)


@dataclass(slots=True)
class PreparedProviderInputHandle:
    cut: PreparedProviderInputCut
    _owner: "ProviderSafePointCoordinator"
    _generation: int
    _model_active: bool = False
    _closed: bool = False

    def begin_model_operation(self) -> None:
        self._owner._begin_model(self)

    def close(self) -> None:
        if self._closed:
            return
        self._owner._close_handle(self)


@dataclass(frozen=True, slots=True, init=False)
class SealedProviderInputPreparationBasis:
    """Exact current handle/read/capability/target join owned by safe-point."""

    handle: PreparedProviderInputHandle
    canonical_read: object
    capability_dispatch_cut: object
    call_target: "FrozenEpochModelCallTarget"
    surface_borrow: object
    _owner: "ProviderSafePointCoordinator"
    _state: str

    def __init__(
        self,
        *,
        handle: PreparedProviderInputHandle,
        canonical_read: object,
        capability_dispatch_cut: object,
        call_target: "FrozenEpochModelCallTarget",
        surface_borrow: object,
        owner: "ProviderSafePointCoordinator",
        _seal: object,
    ) -> None:
        if _seal is not _PREPARATION_BASIS_SEAL:
            raise TypeError("provider-input preparation basis is safe-point-owned")
        object.__setattr__(self, "handle", handle)
        object.__setattr__(self, "canonical_read", canonical_read)
        object.__setattr__(self, "capability_dispatch_cut", capability_dispatch_cut)
        object.__setattr__(self, "call_target", call_target)
        object.__setattr__(self, "surface_borrow", surface_borrow)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_state", "PREPARED")

    def _mark_installed(self, owner: "ProviderSafePointCoordinator") -> None:
        if self._owner is not owner or self._state != "PREPARED":
            raise RuntimeError("provider-input basis cannot be installed")
        object.__setattr__(self, "_state", "INSTALLED")

    def _revoke(self, owner: "ProviderSafePointCoordinator") -> None:
        if self._owner is not owner or self._state == "REVOKED":
            raise RuntimeError("provider-input basis is already revoked")
        object.__setattr__(self, "_state", "REVOKED")


class ProviderSafePointCoordinator:
    """Linearizes input freeze with revision/external-source acceptance.

    This owner is deliberately process-local.  Host takeover interrupts the
    old turn instead of trying to recover a prepared input or provider call.
    """

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        guard: HostWriterGuard,
    ) -> None:
        self._repository = repository
        self._guard = guard
        self._lock = RLock()
        self._generation = 0
        self._active_handle: PreparedProviderInputHandle | None = None
        self._active_basis: SealedProviderInputPreparationBasis | None = None

    def is_idle(self) -> bool:
        with self._lock:
            return self._active_handle is None

    def seal_provider_input_preparation_basis(
        self,
        *,
        handle: PreparedProviderInputHandle,
        canonical_observation: OwnerIssuedCanonicalDispatchObservation,
        capability_observation: OwnerIssuedCapabilityDispatchObservation,
        call_target: "FrozenEpochModelCallTarget",
    ) -> SealedProviderInputPreparationBasis:
        """Seal the exact owner-produced observations while the handle is current."""

        with self._lock:
            self._require_current(handle)
            if not isinstance(
                canonical_observation, OwnerIssuedCanonicalDispatchObservation
            ) or not isinstance(
                capability_observation, OwnerIssuedCapabilityDispatchObservation
            ):
                raise TypeError("provider-input observations require physical owners")
            if self._active_basis is not None:
                raise RuntimeError("provider-input preparation basis is already active")
            canonical_read = canonical_observation.canonical_read
            capability_dispatch_cut = (
                capability_observation.capability_dispatch_cut
            )
            surface_borrow = capability_observation.surface_borrow
            identity = canonical_read.compile_snapshot.canonical_input.identity
            if (
                identity.session_id != handle.cut.session_id
                or identity.turn_id != handle.cut.turn_id
                or identity.context_binding_revision_id
                != handle.cut.context_binding_revision_id
                or identity.provider_input_through_sequence
                != handle.cut.provider_input_through_sequence
                or call_target.session_id != handle.cut.session_id
                or call_target.turn_id != handle.cut.turn_id
            ):
                raise RuntimeError("provider-input preparation basis does not exact-join")
            if canonical_observation.consume() is not canonical_read:
                raise RuntimeError("canonical observation consumption drifted")
            consumed_cut, consumed_borrow = capability_observation.consume()
            if (
                consumed_cut is not capability_dispatch_cut
                or consumed_borrow is not surface_borrow
            ):
                raise RuntimeError("provider-input observation consumption drifted")
            basis = SealedProviderInputPreparationBasis(
                handle=handle,
                canonical_read=canonical_read,
                capability_dispatch_cut=capability_dispatch_cut,
                call_target=call_target,
                surface_borrow=surface_borrow,
                owner=self,
                _seal=_PREPARATION_BASIS_SEAL,
            )
            self._active_basis = basis
            return basis

    def issue_provider_input_transition(
        self,
        basis: SealedProviderInputPreparationBasis,
        continuity: "HostProviderInputContinuityOwner",
        **values: object,
    ) -> "ProviderInputEpochTransition":
        with self._lock:
            self._require_basis_current(basis)
            return continuity.issue_transition(
                preparation_basis=basis,
                call_target=basis.call_target,
                **values,
            )

    def issue_adopted_compaction_continuation_seed(
        self,
        *,
        handle: PreparedProviderInputHandle,
        continuity: "HostProviderInputContinuityOwner",
        confirmation: object,
        candidate: object,
        attempt_token: object,
        dispatch_read: object,
        destination: "FrozenEpochModelCallTarget",
        protected_tail_selection_fingerprint: str,
        successor_turn_id: str | None = None,
    ) -> object:
        """Issue the adopted seed only from FULL plus the exact current handle."""

        from pulsara_agent.conversation_kernel.cold_epoch import (
            _issue_adopted_compaction_continuation_seed,
        )
        from pulsara_agent.conversation_kernel.compaction.contracts import (
            CompactionAdoptionConfirmation,
        )
        from pulsara_agent.model_input.continuity import ProviderInputContinuityScope

        with self._lock:
            self._require_current(handle)
            if (
                not isinstance(confirmation, CompactionAdoptionConfirmation)
                or getattr(confirmation.kind, "value", None) != "FULL"
            ):
                raise RuntimeError("adopted successor requires FULL confirmation")
            identity = dispatch_read.compile_snapshot.canonical_input.identity
            binding = dispatch_read.compile_snapshot.context_binding_fact
            scope = ProviderInputContinuityScope(
                session_id=identity.session_id,
                scope_kind=identity.conversation_scope_kind,
                scope_subagent_task_id=identity.scope_subagent_task_id,
            )
            predecessor = continuity.current_cohort(scope)
            snapshot = getattr(candidate, "snapshot", None)
            candidate_binding = getattr(candidate, "binding", None)
            if (
                predecessor is None
                or getattr(confirmation, "candidate", None) is not candidate
                or handle.cut.session_id != identity.session_id
                or handle.cut.turn_id != identity.turn_id
                or handle.cut.context_binding_revision_id
                != identity.context_binding_revision_id
                or handle.cut.provider_input_through_sequence
                != identity.provider_input_through_sequence
                or getattr(attempt_token, "session_id", None) != identity.session_id
                or getattr(attempt_token, "turn_id", None) != identity.turn_id
                or getattr(attempt_token, "scope_kind", None)
                is not identity.conversation_scope_kind
                or getattr(attempt_token, "scope_subagent_task_id", None)
                != identity.scope_subagent_task_id
                or getattr(snapshot, "snapshot_id", None)
                != binding.context_snapshot_id
                or getattr(candidate_binding, "binding_revision_id", None)
                != binding.binding_revision_id
                or destination.session_id != identity.session_id
                or destination.turn_id != (successor_turn_id or identity.turn_id)
            ):
                raise RuntimeError("adopted successor authority does not exact-join")
            return _issue_adopted_compaction_continuation_seed(
                dispatch_read=dispatch_read,
                binding_rewrite_identity=binding.binding_revision_id,
                protected_tail_selection_fingerprint=(
                    protected_tail_selection_fingerprint
                ),
                predecessor=predecessor,
                destination=destination,
                confirmation=confirmation,
                attempt_token=attempt_token,
                candidate=candidate,
            )

    def revoke_provider_input_preparation_basis(
        self, basis: SealedProviderInputPreparationBasis
    ) -> None:
        """Permanently revoke one basis before its physical resources are released."""

        with self._lock:
            if basis._owner is not self:
                raise RuntimeError("provider-input basis belongs to another safe-point")
            basis._revoke(self)
            if self._active_basis is basis:
                self._active_basis = None

    def retire_full_adoption_without_continuation_and_arm_empty(
        self,
        *,
        continuity: "HostProviderInputContinuityOwner",
        fence: "NoContinuationAdmissionFence",
        evidence: "NoContinuationProductEvidence",
        dispatch: object | None,
        verify_physical_release: Callable[[], None],
    ) -> None:
        """Settle one FULL adoption only after every preparation handle is gone."""

        from pulsara_agent.conversation_kernel.input_continuity import (
            _issue_no_continuation_closure,
        )

        with self._lock:
            closure = _issue_no_continuation_closure(
                fence=fence,
                evidence=evidence,
            )
            continuity.begin_no_continuation_settlement(
                fence=fence,
                closure=closure,
            )
            revoked = continuity.revoke_no_continuation_preparation_resources(
                fence=fence,
                closure=closure,
            )
            if dispatch is not None:
                dispatch.close()
            if self._active_handle is not None or self._active_basis is not None:
                raise RuntimeError(
                    "no-continuation release left a provider-input basis active"
                )
            verify_physical_release()
            release_full = ProviderPreparationReleaseFull(
                exact_subject=closure,
                owner=self,
                _seal=_PROVIDER_PREPARATION_RELEASE_FULL_SEAL,
            )
            continuity.publish_empty_after_no_continuation_release(
                fence=fence,
                closure=closure,
                revoked=revoked,
                release_full=release_full,
                safe_point_owner=self,
            )

    def begin_empty_adoption(
        self,
        *,
        continuity: "HostProviderInputContinuityOwner",
        dry_projection: object,
        attempt_token: object,
        candidate: object,
    ) -> "EmptyAdoptionPending":
        """Move a pending Empty preparation behind a non-authorizing fence."""

        basis, handle = dry_projection._require_for_empty_adoption()
        result = dry_projection.result
        reservation = getattr(result.basis.source, "reservation", None)
        if reservation is None:
            raise RuntimeError("installed compaction dry source is not Empty")
        with self._lock:
            self._require_basis_current(basis)
            if handle is not basis.handle:
                raise RuntimeError("empty adoption handle/basis drifted")
            return continuity.begin_empty_adoption(
                scope=reservation.lease.scope,
                reservation=reservation,
                preparation_basis=basis,
                attempt_token=attempt_token,
                candidate=candidate,
                dry_result=result,
            )

    def settle_empty_adoption_full(
        self,
        *,
        continuity: "HostProviderInputContinuityOwner",
        dry_projection: object,
        pending: "EmptyAdoptionPending",
        confirmation: object,
    ) -> None:
        """Revoke old Empty capabilities before publishing adopted-base lease."""

        basis, handle = dry_projection._require_for_empty_adoption()
        with self._lock:
            self._require_basis_current(basis)
            if (
                handle is not basis.handle
                or pending.preparation_basis is not basis
                or pending.dry_result is not dry_projection.result
            ):
                raise RuntimeError("empty adoption settlement does not exact-join")
            settling = continuity.begin_empty_full_settlement(
                pending=pending,
                confirmation=confirmation,
            )
            revoked = continuity.revoke_empty_adoption_preparation_resources(
                settling
            )
            try:
                dry_projection.close()
                if self._active_handle is not None:
                    raise RuntimeError(
                        "empty adoption release left a provider-input handle active"
                    )
                release_full = ProviderPreparationReleaseFull(
                    exact_subject=settling,
                    owner=self,
                    _seal=_PROVIDER_PREPARATION_RELEASE_FULL_SEAL,
                )
                continuity.publish_empty_after_full_adoption_release(
                    settling=settling,
                    revoked=revoked,
                    release_full=release_full,
                    safe_point_owner=self,
                )
            except BaseException:
                continuity.quarantine_empty_adoption_release(settling)
                raise

    def settle_empty_adoption_none(
        self,
        *,
        continuity: "HostProviderInputContinuityOwner",
        dry_projection: object,
        pending: "EmptyAdoptionPending",
        confirmation: object,
    ) -> None:
        """Restore and retire a source-less attempt with no canonical winner."""

        basis, handle = dry_projection._require_for_empty_adoption()
        with self._lock:
            self._require_basis_current(basis)
            if (
                handle is not basis.handle
                or pending.preparation_basis is not basis
                or pending.dry_result is not dry_projection.result
            ):
                raise RuntimeError("empty adoption NONE does not exact-join")
            continuity.restore_empty_adoption_none(pending, confirmation)
            dry_projection.close_after_empty_adoption_none()

    def settle_empty_adoption_conflict(
        self,
        *,
        continuity: "HostProviderInputContinuityOwner",
        dry_projection: object,
        pending: "EmptyAdoptionPending",
        confirmation: object,
    ) -> None:
        """Revoke all old Empty capabilities and leave the Host quarantined."""

        basis, handle = dry_projection._require_for_empty_adoption()
        with self._lock:
            self._require_basis_current(basis)
            if (
                handle is not basis.handle
                or pending.preparation_basis is not basis
                or pending.dry_result is not dry_projection.result
            ):
                raise RuntimeError("empty adoption CONFLICT does not exact-join")
            settling = continuity.begin_empty_conflict_settlement(
                pending=pending,
                confirmation=confirmation,
            )
            continuity.revoke_empty_adoption_preparation_resources(settling)
            try:
                dry_projection.close()
            finally:
                continuity.quarantine_empty_adoption_release(settling)

    def register_provider_input_candidate(
        self,
        basis: SealedProviderInputPreparationBasis,
        continuity: "HostProviderInputContinuityOwner",
        candidate: "PreparedProviderInputAppendCandidate",
    ) -> None:
        with self._lock:
            self._require_basis_current(basis)
            if candidate._preparation_basis is not basis:
                raise RuntimeError("provider-input candidate belongs to another basis")
            continuity.register(candidate)

    def install_provider_input_candidate(
        self,
        basis: SealedProviderInputPreparationBasis,
        continuity: "HostProviderInputContinuityOwner",
        *,
        candidate: "PreparedProviderInputAppendCandidate",
        execution: object,
    ) -> "ProcessLocalProviderInputInstallPermit":
        with self._lock:
            self._require_basis_current(basis)
            if candidate._preparation_basis is not basis:
                raise RuntimeError("provider-input candidate belongs to another basis")
            if basis.handle._model_active:
                raise RuntimeError("model operation already started")
            basis.handle._model_active = True
            try:
                permit = continuity.install(candidate=candidate, execution=execution)
                basis._mark_installed(self)
                return permit
            except BaseException:
                basis.handle._model_active = False
                raise

    def freeze_provider_input(
        self,
        *,
        turn_id: str,
        deadline_monotonic: float,
    ) -> PreparedProviderInputHandle:
        with self._lock:
            if self._active_handle is not None:
                raise RuntimeError("provider input handle is already active")
            self._repository.require_provider_safe_turn(
                self._guard,
                turn_id=turn_id,
                deadline_monotonic=deadline_monotonic,
            )
            cut = self._repository.prepare_provider_input_cut(
                self._guard,
                turn_id=turn_id,
                deadline_monotonic=deadline_monotonic,
            )
            self._generation += 1
            handle = PreparedProviderInputHandle(cut, self, self._generation)
            self._active_handle = handle
            return handle

    def freeze_prospective_active_root_input(
        self,
        *,
        candidate: PreparedActiveRootInputCandidate,
        deadline_monotonic: float,
    ) -> PreparedProviderInputHandle:
        """Freeze an exact active suffix, including a result that closes Plan."""

        with self._lock:
            if self._active_handle is not None:
                raise RuntimeError("provider input handle is already active")
            self._repository.require_prospective_active_root_input_safe(
                self._guard,
                candidate=candidate,
                deadline_monotonic=deadline_monotonic,
            )
            self._generation += 1
            handle = PreparedProviderInputHandle(
                candidate.expected_provider_input_cut,
                self,
                self._generation,
            )
            self._active_handle = handle
            return handle

    def freeze_compaction_input(
        self,
        *,
        turn_id: str,
        allow_terminal: bool,
        deadline_monotonic: float,
    ) -> PreparedProviderInputHandle:
        """Freeze an exact active or terminal turn for compaction planning."""

        with self._lock:
            if self._active_handle is not None:
                raise RuntimeError("provider input handle is already active")
            if not allow_terminal:
                self._repository.require_provider_safe_turn(
                    self._guard,
                    turn_id=turn_id,
                    deadline_monotonic=deadline_monotonic,
                )
            cut = self._repository.prepare_compaction_input_cut(
                self._guard,
                turn_id=turn_id,
                allow_terminal=allow_terminal,
                deadline_monotonic=deadline_monotonic,
            )
            self._generation += 1
            handle = PreparedProviderInputHandle(cut, self, self._generation)
            self._active_handle = handle
            return handle

    def rotate_provider_input(
        self,
        handle: PreparedProviderInputHandle,
        *,
        turn_id: str,
        deadline_monotonic: float,
    ) -> PreparedProviderInputHandle:
        """Atomically replace one pre-consumption cut with its successor cut.

        The old handle remains the active exclusion owner until the new cut has
        been prepared.  External producers therefore never observe an empty
        safe-point window between steer consumption and final materialization.
        This is process-local ownership, not a durable input generation.
        """

        with self._lock:
            self._require_current(handle)
            if handle._model_active:
                raise RuntimeError("cannot rotate an active model operation")
            self._repository.require_provider_safe_turn(
                self._guard,
                turn_id=turn_id,
                deadline_monotonic=deadline_monotonic,
            )
            cut = self._repository.prepare_provider_input_cut(
                self._guard,
                turn_id=turn_id,
                deadline_monotonic=deadline_monotonic,
            )
            self._generation += 1
            successor = PreparedProviderInputHandle(cut, self, self._generation)
            handle._closed = True
            handle._model_active = False
            self._active_handle = successor
            return successor

    def accept_subagent_completion(
        self,
        *,
        handle: PreparedProviderInputHandle | None = None,
        provider_input_admission: (
            PreparedActiveRootInputAdmission
            | PreparedRootProviderInputAdmission
            | None
        ) = None,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        task_id: str,
        command_id: str,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedSubagentCompletion:
        """Explicitly deliver one terminal task at the same cut boundary."""

        with self._lock:
            if provider_input_admission is not None:
                if isinstance(
                    provider_input_admission, PreparedActiveRootInputAdmission
                ):
                    if handle is None:
                        raise ValueError(
                            "active completion admission lacks its handle"
                        )
                    self._require_current(handle)
                    if handle._model_active:
                        raise ExternalSourceNotAtSafePoint(
                            "provider model operation is active"
                        )
                    if (
                        provider_input_admission.candidate.expected_provider_input_cut
                        != handle.cut
                    ):
                        raise ConversationKernelConflict(
                            "manual completion admission lost its exact input cut"
                        )
                elif handle is not None:
                    raise ValueError(
                        "new ROOT completion cannot use an active handle"
                    )
            elif self._active_handle is not None:
                raise ExternalSourceNotAtSafePoint(
                    "provider input/model operation is active"
                )
            if (
                new_context_binding_revision_id is None
                and provider_input_admission is None
            ):
                self._repository.require_provider_safe_turn(
                    self._guard,
                    turn_id=turn_id,
                    deadline_monotonic=deadline_monotonic,
                )
            return self._repository.accept_subagent_completion_into_root(
                self._guard,
                turn_id=turn_id,
                new_context_binding_revision_id=new_context_binding_revision_id,
                requested_permission_mode=requested_permission_mode,
                task_id=task_id,
                command_id=command_id,
                provider_input_admission=provider_input_admission,
                occurred_at=datetime.now(timezone.utc),
                actor_id=actor_id,
                deadline_monotonic=deadline_monotonic,
            )

    def accept_queued_subagent_completions(
        self,
        handle: PreparedProviderInputHandle,
        *,
        candidates: tuple[PreparedAutomaticSubagentCompletion, ...],
        actor_id: str,
        deadline_monotonic: float,
    ) -> tuple[
        PreparedProviderInputHandle,
        tuple[AcceptedSubagentCompletion, ...],
    ]:
        """Atomically append and rotate one admitted automatic completion suffix."""

        with self._lock:
            self._require_current(handle)
            if handle._model_active:
                raise ExternalSourceNotAtSafePoint("provider model operation is active")
            if (
                not candidates
                or candidates[0].expected_provider_input_cut != handle.cut
            ):
                raise ConversationKernelConflict(
                    "automatic completion candidate lost its exact input cut"
                )
            accepted = self._repository.accept_automatic_subagent_completion_batch(
                self._guard,
                candidates=candidates,
                occurred_at=datetime.now(timezone.utc),
                actor_id=actor_id,
                deadline_monotonic=deadline_monotonic,
            )
            cut = self._repository.prepare_provider_input_cut(
                self._guard,
                turn_id=handle.cut.turn_id,
                deadline_monotonic=deadline_monotonic,
            )
            self._generation += 1
            successor = PreparedProviderInputHandle(cut, self, self._generation)
            handle._closed = True
            handle._model_active = False
            self._active_handle = successor
            return successor, accepted

    def prepare_terminal_observation_installation(
        self,
        *,
        coordinator: TerminalMonitorCoordinator,
        monitor_id: str,
        target: PreparedInstallationTarget,
        workspace_id: str,
        actor_id: str,
        deadline_monotonic: float,
    ) -> TerminalObservationInstallationAttempt | AcceptedEntry | None:
        """Freeze or exact-confirm one monitor draft before input admission.

        A process-local in-flight candidate survives an ambiguous database
        acknowledgement.  The next invocation exact-confirms that candidate
        before it can issue the same canonical write again.
        """

        with self._lock:
            if self._active_handle is not None:
                raise ExternalSourceNotAtSafePoint(
                    "provider input/model operation is active"
                )
            attempt = coordinator.current_installation_attempt(monitor_id)
            if attempt is not None:
                confirmed = self._repository.confirm_terminal_observation_winner(
                    self._guard,
                    candidate=attempt,
                    deadline_monotonic=deadline_monotonic,
                )
                if confirmed is not None:
                    coordinator.settle_installation(attempt, accepted=True)
                    return confirmed
            else:
                attempt = coordinator.freeze(
                    monitor_id=monitor_id,
                    target=target,
                    workspace_id=workspace_id,
                    writer_generation=self._guard.writer_generation,
                    actor_id=actor_id,
                )
                if attempt is None:
                    return None
            return attempt

    def publish_terminal_observation(
        self,
        handle: PreparedProviderInputHandle | None,
        *,
        coordinator: TerminalMonitorCoordinator,
        attempt: TerminalObservationInstallationAttempt,
        provider_input_admission: (
            PreparedActiveRootInputAdmission | PreparedRootProviderInputAdmission
        ),
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        """Publish one already-measured Terminal observation exactly once."""

        with self._lock:
            if handle is not None:
                self._require_current(handle)
                if handle._model_active:
                    raise ExternalSourceNotAtSafePoint(
                        "provider model operation is active"
                    )
            elif self._active_handle is not None:
                raise ExternalSourceNotAtSafePoint(
                    "provider input/model operation is active"
                )
            try:
                accepted = self._repository.accept_terminal_observation(
                    self._guard,
                    candidate=attempt,
                    provider_input_admission=provider_input_admission,
                    deadline_monotonic=deadline_monotonic,
                )
            except ConversationKernelConflict:
                coordinator.settle_installation(attempt, accepted=False)
                raise
            except BaseException:
                confirmed = self._repository.confirm_terminal_observation_winner(
                    self._guard,
                    candidate=attempt,
                    deadline_monotonic=deadline_monotonic,
                )
                if confirmed is None:
                    # UNKNOWN remains owned by the immutable process-local
                    # attempt.  A later Host scheduler pass exact-confirms it.
                    raise
                accepted = confirmed
            coordinator.settle_installation(attempt, accepted=True)
            return accepted

    def install_user_control_feedback(
        self,
        handle: PreparedProviderInputHandle,
        *,
        attempt: UserControlFeedbackInstallationAttempt,
        provider_input_admission: PreparedActiveRootInputAdmission,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        """Install or exact-confirm one immutable user-control candidate."""

        with self._lock:
            self._require_current(handle)
            if handle._model_active:
                raise ExternalSourceNotAtSafePoint("provider model operation is active")
            if (
                provider_input_admission.candidate.expected_provider_input_cut
                != handle.cut
            ):
                raise ConversationKernelConflict(
                    "user control feedback admission lost its exact input cut"
                )
            confirmed = self._repository.confirm_user_control_feedback_winner(
                self._guard,
                candidate=attempt,
                deadline_monotonic=deadline_monotonic,
            )
            if confirmed is not None:
                return confirmed
            try:
                return self._repository.accept_user_control_feedback(
                    self._guard,
                    candidate=attempt,
                    provider_input_admission=provider_input_admission,
                    deadline_monotonic=deadline_monotonic,
                )
            except ConversationKernelConflict:
                raise
            except BaseException:
                confirmed = self._repository.confirm_user_control_feedback_winner(
                    self._guard,
                    candidate=attempt,
                    deadline_monotonic=deadline_monotonic,
                )
                if confirmed is None:
                    raise
                return confirmed

    def confirm_user_control_feedback(
        self,
        *,
        attempt: UserControlFeedbackInstallationAttempt,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Read-only exact confirmation for an ambiguity-owned candidate."""

        with self._lock:
            return self._repository.confirm_user_control_feedback_winner(
                self._guard,
                candidate=attempt,
                deadline_monotonic=deadline_monotonic,
            )

    @contextmanager
    def exclusive_safe_mutation(self) -> Iterator[None]:
        with self._lock:
            if self._active_handle is not None:
                raise RuntimeError("provider input/model operation is active")
            yield

    def run_exclusive(self, operation: Callable[[], T]) -> T:
        with self.exclusive_safe_mutation():
            return operation()

    def _begin_model(self, handle: PreparedProviderInputHandle) -> None:
        with self._lock:
            self._require_current(handle)
            if handle._model_active:
                raise RuntimeError("model operation already started")
            handle._model_active = True

    def _close_handle(self, handle: PreparedProviderInputHandle) -> None:
        with self._lock:
            self._require_current(handle)
            handle._closed = True
            handle._model_active = False
            self._active_handle = None

    def _require_current(self, handle: PreparedProviderInputHandle) -> None:
        if (
            handle._closed
            or self._active_handle is not handle
            or handle._generation != self._generation
        ):
            raise RuntimeError("provider input handle is stale")

    def _require_basis_current(
        self, basis: SealedProviderInputPreparationBasis
    ) -> None:
        if basis._owner is not self:
            raise RuntimeError("provider-input basis belongs to another safe-point")
        if basis._state == "REVOKED":
            raise RuntimeError("provider-input basis is revoked")
        self._require_current(basis.handle)


__all__ = [
    "ExternalSourceNotAtSafePoint",
    "OwnerIssuedCanonicalDispatchObservation",
    "OwnerIssuedCapabilityDispatchObservation",
    "PreparedProviderInputHandle",
    "ProviderSafePointCoordinator",
    "SealedProviderInputPreparationBasis",
]
