"""Exact ROOT/child turn admission and pre-loop terminal settlement."""

from __future__ import annotations

from pulsara_agent.conversation_kernel.workspace import WorkspaceExecutionGate

import asyncio
from datetime import datetime, timezone
from typing import Callable, Protocol

from psycopg import InterfaceError, OperationalError

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
    AcceptedRootTurnAdmission,
    ConversationKernelConflict,
    ConversationKernelRepository,
    PreparedRootTurnIntent,
    StaleHostWriter,
    TurnAdmissionConfirmationKind,
)
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.conversation_kernel.todo_runtime import (
    PreparedTodoRootRunActivation,
    build_root_activation,
)


class TodoRunAdmissionFinalizer(Protocol):
    async def __call__(
        self,
        prepared: PreparedTodoRootRunActivation,
        accepted: AcceptedEntry,
    ) -> None: ...






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
        workspace_gate: WorkspaceExecutionGate | None = None,
    ) -> None:
        self._workspace_gate = workspace_gate
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
    ) -> AcceptedRootTurnAdmission:
        """Start the canonical request independently of provider preparation.

        The shield joins the one physical start/finalization owner after stop
        or close.  It does not grant permission to continue model execution.
        """
        if self._workspace_gate is not None:
            self._workspace_gate.require_available()
        activation = build_root_activation(
            session_id=intent.session_id,
            admission_kind="DIRECT",
            command_id=intent.command_id,
            exact_turn_id=intent.turn_id,
            exact_initial_entry_id=intent.entry_id,
            exact_context_binding_revision_id=intent.context_binding_revision_id,
        )
        cancellation_requested = asyncio.Event()
        settlement = asyncio.create_task(
            self._accept_root_intent_worker(
                intent=intent,
                model_resolution_snapshot=model_resolution_snapshot,
                activation=activation,
                cancellation_intent=cancellation_intent,
                cancellation_requested=cancellation_requested,
            ),
            name=f"kernel-root-start:{intent.turn_id}",
        )
        try:
            outcome = await _await_shielded(
                settlement, on_cancelled=cancellation_requested.set,
            )
        except asyncio.CancelledError:
            if not settlement.cancelled() and settlement.exception() is None:
                outcome = settlement.result()
                if outcome is not None:
                    await self.interrupt_turn(
                        intent.turn_id,
                        reason=root_cancellation_terminal_reason(cancellation_intent),
                    )
            raise
        if cancellation_intent.cause is not None:
            if outcome is not None:
                await self.interrupt_turn(
                    intent.turn_id,
                    reason=root_cancellation_terminal_reason(cancellation_intent),
                )
            raise asyncio.CancelledError
        if outcome is None:
            raise asyncio.CancelledError
        return outcome

    async def _accept_root_intent_worker(
        self,
        *,
        intent: PreparedRootTurnIntent,
        model_resolution_snapshot: FrozenModelResolutionSnapshot,
        activation: PreparedTodoRootRunActivation,
        cancellation_intent: ActiveTurnCancellationIntent,
        cancellation_requested: asyncio.Event,
    ) -> AcceptedRootTurnAdmission | None:
        original_error: Exception | None = None
        while True:
            if original_error is None:
                if cancellation_requested.is_set() or cancellation_intent.cause is not None:
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
                except Exception as error:
                    # Even a deterministic rejection may follow an unknown
                    # acknowledgement. Confirm the exact original identity once;
                    # only physical I/O failures permit reissuing a NONE write.
                    original_error = error
                else:
                    await self._finalize_current_start(activation, outcome.accepted, cancellation_intent)
                    return outcome
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_root_turn_intent,
                    intent=intent,
                    guard=self._writer_lease.guard,
                    deadline_monotonic=self._deadline(),
                )
            except StaleHostWriter:
                raise
            except (TimeoutError, ConnectionError, InterfaceError, OperationalError):
                await asyncio.sleep(0.05)
                continue
            if confirmation.kind is TurnAdmissionConfirmationKind.FULL:
                assert confirmation.accepted is not None
                assert confirmation.started_input_group is not None
                outcome = AcceptedRootTurnAdmission(
                    accepted=confirmation.accepted,
                    started_input_group=confirmation.started_input_group,
                )
                await self._finalize_current_start(activation, outcome.accepted, cancellation_intent)
                return outcome
            if confirmation.kind is TurnAdmissionConfirmationKind.CONFLICT:
                raise ConversationKernelConflict(
                    "ROOT turn admission has a conflicting winner"
                ) from original_error
            if cancellation_requested.is_set() or cancellation_intent.cause is not None:
                return None
            if not isinstance(
                original_error,
                (TimeoutError, ConnectionError, InterfaceError, OperationalError),
            ):
                assert original_error is not None
                raise original_error
            if self._workspace_gate is not None:
                await self._workspace_gate.wait_available()
            original_error = None

    async def _finalize_current_start(
        self,
        activation: PreparedTodoRootRunActivation,
        accepted: AcceptedEntry,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> None:
        """Publish a confirmed start only while its current writer may run it."""
        try:
            try:
                await self._io.run(
                    self._repository.require_provider_safe_turn,
                    self._writer_lease.guard,
                    turn_id=accepted.turn_id,
                    deadline_monotonic=self._deadline(),
                )
            except ConversationKernelConflict:
                status = await self._io.run(
                    self._repository.read_turn_status,
                    session_id=self._writer_lease.guard.session_id,
                    turn_id=accepted.turn_id,
                    deadline_monotonic=self._deadline(),
                )
                if status in {TurnStatus.COMPLETED, TurnStatus.INTERRUPTED}:
                    # The immutable FULL admission remains true. A terminal
                    # winner has no new TODO activation or public active target.
                    return
                raise
        except StaleHostWriter:
            # The replacement writer owns canonical takeover settlement.
            raise
        except BaseException:
            await self.interrupt_turn(
                accepted.turn_id,
                reason=root_cancellation_terminal_reason(cancellation_intent),
            )
            raise
        await self._finalize_todo(activation, accepted, cancellation_intent)

    async def _finalize_todo(
        self,
        prepared: PreparedTodoRootRunActivation,
        accepted: AcceptedEntry,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> None:
        if self._todo_finalizer is None:
            return
        activation = asyncio.create_task(
            self._todo_finalizer(prepared, accepted),
            name=f"kernel-todo-admission-finalizer:{accepted.turn_id}",
        )
        try:
            await _await_shielded(activation)
        except BaseException:
            await self.interrupt_turn(
                accepted.turn_id,
                reason=root_cancellation_terminal_reason(cancellation_intent),
            )
            raise

    async def interrupt_turn(
        self, turn_id: str, *, reason: str, public_detail: str | None = None
    ) -> None:
        """Join the exact terminal winner before releasing its foreground owner."""

        task = asyncio.create_task(
            self._interrupt_turn_worker(turn_id, reason, public_detail),
            name=f"kernel-turn-terminalization:{turn_id}",
        )
        await _await_shielded(task)

    async def _interrupt_turn_worker(
        self, turn_id: str, reason: str, public_detail: str | None
    ) -> None:
        while True:
            try:
                changed = await self._io.run(
                    self._repository.interrupt_turn,
                    self._writer_lease.guard,
                    turn_id=turn_id,
                    reason=reason,
                    public_detail=public_detail,
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


async def _await_shielded(
    task: asyncio.Task[object],
    *,
    on_cancelled: Callable[[], None] | None = None,
) -> object:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            if on_cancelled is not None:
                on_cancelled()
            continue
        except BaseException:
            break
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result




__all__ = [
    "TodoRunAdmissionFinalizer",
    "TurnAdmissionCoordinator",
    "root_cancellation_terminal_reason",
]
