"""Process-local provider safe-point owner for one Host activation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, Iterator, TypeVar

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    AcceptedSubagentCompletion,
    ConversationKernelConflict,
    ConversationKernelRepository,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.ports.terminal_observation import PreparedInstallationTarget
from pulsara_agent.ports.user_control_feedback import (
    UserControlFeedbackInstallationAttempt,
)
from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator


T = TypeVar("T")


class ExternalSourceNotAtSafePoint(RuntimeError):
    """A ROOT-visible source cannot be accepted behind an active input cut."""


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
            if self._active_handle is not None:
                raise ExternalSourceNotAtSafePoint(
                    "provider input/model operation is active"
                )
            if new_context_binding_revision_id is None:
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
                occurred_at=datetime.now(timezone.utc),
                actor_id=actor_id,
                deadline_monotonic=deadline_monotonic,
            )

    def accept_queued_subagent_completion(
        self,
        handle: PreparedProviderInputHandle,
        *,
        task_id: str,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedSubagentCompletion:
        """Append one automatic completion behind the handle's exact base cut."""

        with self._lock:
            self._require_current(handle)
            if handle._model_active:
                raise ExternalSourceNotAtSafePoint(
                    "provider model operation is active"
                )
            return self._repository.accept_subagent_completion_into_root(
                self._guard,
                task_id=task_id,
                turn_id=handle.cut.turn_id,
                expected_provider_input_cut=handle.cut,
                occurred_at=datetime.now(timezone.utc),
                actor_id=actor_id,
                deadline_monotonic=deadline_monotonic,
            )

    def install_terminal_observation(
        self,
        *,
        coordinator: TerminalMonitorCoordinator,
        monitor_id: str,
        target: PreparedInstallationTarget,
        workspace_id: str,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Freeze and install one monitor draft under the provider-safe lock.

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
            try:
                accepted = self._repository.accept_terminal_observation(
                    self._guard,
                    candidate=attempt,
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
        *,
        attempt: UserControlFeedbackInstallationAttempt,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        """Install or exact-confirm one immutable user-control candidate."""

        with self._lock:
            if self._active_handle is not None:
                raise ExternalSourceNotAtSafePoint(
                    "provider input/model operation is active"
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


__all__ = [
    "ExternalSourceNotAtSafePoint",
    "PreparedProviderInputHandle",
    "ProviderSafePointCoordinator",
]
