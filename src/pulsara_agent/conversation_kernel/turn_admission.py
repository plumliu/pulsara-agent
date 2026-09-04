"""Exact ROOT/child turn admission and pre-loop terminal settlement."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.contracts import TurnStatus, WriterLease
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    ConversationKernelConflict,
    ConversationKernelRepository,
    PreparedRootTurnIntent,
    PreparedSubagentTurnAdmission,
    StaleHostWriter,
    TurnAdmissionConfirmationKind,
)
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.conversation_kernel.todo_runtime import (
    PreparedTodoChildRunActivation,
    PreparedTodoRootRunActivation,
    build_child_activation,
    build_root_activation,
)


class TodoRunAdmissionFinalizer(Protocol):
    async def __call__(
        self,
        prepared: PreparedTodoRootRunActivation | PreparedTodoChildRunActivation,
        accepted: AcceptedEntry,
    ) -> None: ...


class SubagentTurnAdmissionPostCommitError(RuntimeError):
    """A child turn committed before its process-local activation failed."""

    def __init__(
        self,
        accepted: AcceptedEntry,
        activation_error: BaseException,
    ) -> None:
        super().__init__(
            "subagent turn admission committed before TODO activation failed"
        )
        self.accepted = accepted
        self.activation_error = activation_error


@dataclass(slots=True)
class _TurnAdmissionSettlementAttempt:
    candidate: PreparedSubagentTurnAdmission
    reissue_allowed: bool
    todo_activation: PreparedTodoChildRunActivation
    cancellation_requested: bool = False
    cancellation_intent: ActiveTurnCancellationIntent | None = None


class TurnAdmissionCoordinator:
    """Own one immutable admission through write, confirmation, and cancel."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        io_owner: KernelSessionIO,
        writer_lease: WriterLease,
        deadline_factory: KernelExecutionDeadlineFactory,
        todo_finalizer: TodoRunAdmissionFinalizer | None,
    ) -> None:
        self._repository = repository
        self._io = io_owner
        self._writer_lease = writer_lease
        self._deadlines = deadline_factory
        self._todo_finalizer = todo_finalizer

    def _deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    async def accept_root_intent(
        self,
        intent: PreparedRootTurnIntent,
        *,
        model_resolution_snapshot: FrozenModelResolutionSnapshot,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> AcceptedEntry:
        activation = build_root_activation(
            session_id=intent.session_id,
            admission_kind="DIRECT",
            command_id=intent.command_id,
            exact_turn_id=intent.turn_id,
            exact_initial_entry_id=intent.entry_id,
            exact_context_binding_revision_id=intent.context_binding_revision_id,
        )
        try:
            outcome = await self._io.run(
                self._repository.accept_root_turn_intent,
                self._writer_lease.guard,
                intent=intent,
                model_resolution_snapshot=model_resolution_snapshot,
                deadline_monotonic=self._deadline(),
            )
        except asyncio.CancelledError as cancellation:
            await self._settle_root_intent(
                intent=intent,
                model_resolution_snapshot=model_resolution_snapshot,
                activation=activation,
                reissue_allowed=False,
                cancellation_requested=True,
                cancellation_intent=cancellation_intent,
            )
            raise cancellation
        except BaseException:
            accepted = await self._settle_root_intent(
                intent=intent,
                model_resolution_snapshot=model_resolution_snapshot,
                activation=activation,
                reissue_allowed=True,
                cancellation_requested=False,
                cancellation_intent=cancellation_intent,
            )
            if accepted is None:
                raise ConversationKernelConflict("ROOT turn admission did not settle")
            return accepted
        try:
            await self._finalize_todo(activation, outcome.accepted)
        except asyncio.CancelledError as cancellation:
            await self.interrupt_turn(
                outcome.accepted.turn_id,
                reason=root_cancellation_terminal_reason(cancellation_intent),
            )
            raise cancellation
        return outcome.accepted

    async def _settle_root_intent(
        self,
        *,
        intent: PreparedRootTurnIntent,
        model_resolution_snapshot: FrozenModelResolutionSnapshot,
        activation: PreparedTodoRootRunActivation,
        reissue_allowed: bool,
        cancellation_requested: bool,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> AcceptedEntry | None:
        while True:
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_root_turn_intent,
                    intent=intent,
                    guard=self._writer_lease.guard,
                    deadline_monotonic=self._deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(0.05)
                continue
            if confirmation.kind is TurnAdmissionConfirmationKind.FULL:
                assert confirmation.accepted is not None
                await self._finalize_todo(activation, confirmation.accepted)
                if cancellation_requested:
                    await self.interrupt_turn(
                        intent.turn_id,
                        reason=root_cancellation_terminal_reason(cancellation_intent),
                    )
                    return None
                return confirmation.accepted
            if confirmation.kind is TurnAdmissionConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "ROOT turn admission has a conflicting winner"
                )
            if cancellation_requested or not reissue_allowed:
                return None
            try:
                outcome = await self._io.run(
                    self._repository.accept_root_turn_intent,
                    self._writer_lease.guard,
                    intent=intent,
                    model_resolution_snapshot=model_resolution_snapshot,
                    deadline_monotonic=self._deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                continue
            await self._finalize_todo(activation, outcome.accepted)
            return outcome.accepted

    async def accept_subagent(
        self,
        candidate: PreparedSubagentTurnAdmission,
        *,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> AcceptedEntry:
        return await self._accept_subagent(
            candidate=candidate,
            cancellation_intent=cancellation_intent,
            todo_activation=build_child_activation(
                session_id=candidate.session_id,
                subagent_task_id=candidate.task_id,
                exact_turn_id=candidate.turn_id,
                exact_initial_entry_id=candidate.entry_id,
                exact_context_binding_revision_id=(
                    candidate.context_binding_revision_id
                ),
            ),
        )

    async def _accept_subagent(
        self,
        *,
        candidate: PreparedSubagentTurnAdmission,
        cancellation_intent: ActiveTurnCancellationIntent,
        todo_activation: (PreparedTodoChildRunActivation),
    ) -> AcceptedEntry:
        try:
            accepted = await self._io.run(
                self._repository.accept_subagent_turn,
                self._writer_lease.guard,
                candidate=candidate,
                deadline_monotonic=self._deadline(),
            )
        except asyncio.CancelledError as cancellation:
            attempt = _TurnAdmissionSettlementAttempt(
                candidate=candidate,
                reissue_allowed=False,
                todo_activation=todo_activation,
                cancellation_requested=True,
                cancellation_intent=cancellation_intent,
            )
            settlement = asyncio.create_task(
                self._settle(attempt),
                name=f"kernel-cancelled-turn-admission:{candidate.turn_id}",
            )
            await _await_admission_settlement(settlement, attempt)
            raise cancellation
        except BaseException:
            attempt = _TurnAdmissionSettlementAttempt(
                candidate=candidate,
                reissue_allowed=True,
                todo_activation=todo_activation,
            )
            settlement = asyncio.create_task(
                self._settle(attempt),
                name=f"kernel-turn-admission-settlement:{candidate.turn_id}",
            )
            accepted, cancellation = await _await_admission_settlement(
                settlement, attempt
            )
            if cancellation is not None:
                raise cancellation
            if accepted is None:
                raise ConversationKernelConflict("turn admission did not settle")
            return accepted
        try:
            await self._finalize_todo(todo_activation, accepted)
        except asyncio.CancelledError as cancellation:
            raise cancellation
        return accepted

    async def _settle(
        self,
        attempt: _TurnAdmissionSettlementAttempt,
    ) -> AcceptedEntry | None:
        while True:
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_subagent_turn_admission,
                    candidate=attempt.candidate,
                    guard=self._writer_lease.guard,
                    deadline_monotonic=self._deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(0.05)
                continue
            if confirmation.kind is TurnAdmissionConfirmationKind.FULL:
                assert confirmation.accepted is not None
                await self._finalize_todo(
                    attempt.todo_activation, confirmation.accepted
                )
                if attempt.cancellation_requested:
                    return None
                return confirmation.accepted
            if confirmation.kind is TurnAdmissionConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "subagent turn admission has a conflicting winner"
                )
            if attempt.cancellation_requested or not attempt.reissue_allowed:
                return None
            try:
                accepted = await self._io.run(
                    self._repository.accept_subagent_turn,
                    self._writer_lease.guard,
                    candidate=attempt.candidate,
                    deadline_monotonic=self._deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                continue
            if attempt.cancellation_requested:
                return None
            await self._finalize_todo(attempt.todo_activation, accepted)
            return accepted

    async def _finalize_todo(
        self,
        prepared: PreparedTodoRootRunActivation | PreparedTodoChildRunActivation,
        accepted: AcceptedEntry,
    ) -> None:
        if self._todo_finalizer is None:
            return
        activation = asyncio.create_task(
            self._todo_finalizer(prepared, accepted),
            name=f"kernel-todo-admission-finalizer:{accepted.turn_id}",
        )
        try:
            await _await_shielded(activation)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(prepared, PreparedTodoChildRunActivation):
                # The canonical child turn already exists.  Its manager owns
                # the joint task/turn terminal winner, so retain that boundary
                # explicitly instead of terminalizing only the turn here.
                raise SubagentTurnAdmissionPostCommitError(accepted, exc) from exc
            await self.interrupt_turn(
                accepted.turn_id,
                reason="FOREGROUND_EXECUTION_INTERRUPTED",
            )
            raise

    async def interrupt_turn(self, turn_id: str, *, reason: str) -> None:
        """Join the exact terminal winner before releasing its foreground owner."""

        task = asyncio.create_task(
            self._interrupt_turn_worker(turn_id, reason),
            name=f"kernel-turn-terminalization:{turn_id}",
        )
        await _await_shielded(task)

    async def _interrupt_turn_worker(self, turn_id: str, reason: str) -> None:
        while True:
            try:
                changed = await self._io.run(
                    self._repository.interrupt_turn,
                    self._writer_lease.guard,
                    turn_id=turn_id,
                    reason=reason,
                    occurred_at=datetime.now(timezone.utc),
                    actor_id="foreground-runner",
                    deadline_monotonic=self._deadline(),
                )
                if changed:
                    return
            except StaleHostWriter:
                return
            except BaseException:
                pass
            try:
                outcome = await self._io.run(
                    self._repository.read_turn_terminal_outcome,
                    session_id=self._writer_lease.guard.session_id,
                    turn_id=turn_id,
                    deadline_monotonic=self._deadline(),
                )
            except BaseException:
                await asyncio.sleep(0.05)
                continue
            if outcome is None:
                return
            status = str(outcome["status"])
            if status in {
                TurnStatus.INTERRUPTED.value,
                TurnStatus.COMPLETED.value,
            }:
                return
            if status != TurnStatus.RUNNING.value:
                raise ConversationKernelConflict(
                    "turn terminal outcome has an invalid status"
                )
            await asyncio.sleep(0.05)


def root_cancellation_terminal_reason(
    intent: ActiveTurnCancellationIntent | None,
) -> str:
    cause = None if intent is None else intent.cause
    if cause is ForegroundCancellationCause.USER_REQUEST:
        return "USER_STOPPED"
    if cause is ForegroundCancellationCause.HOST_SESSION_CLOSE:
        return "SESSION_CLOSED"
    return "FOREGROUND_EXECUTION_INTERRUPTED"


async def _await_shielded(task: asyncio.Task[object]) -> object:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            continue
        except BaseException:
            break
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _await_admission_settlement(
    task: asyncio.Task[AcceptedEntry | None],
    attempt: _TurnAdmissionSettlementAttempt,
) -> tuple[AcceptedEntry | None, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            attempt.cancellation_requested = True
            cancellation = exc
            continue
        except BaseException:
            break
    return task.result(), cancellation


__all__ = [
    "SubagentTurnAdmissionPostCommitError",
    "TodoRunAdmissionFinalizer",
    "TurnAdmissionCoordinator",
    "root_cancellation_terminal_reason",
]
