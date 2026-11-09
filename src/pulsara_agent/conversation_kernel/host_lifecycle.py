"""ROOT task ownership and physical session-close settlement on the Host session."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Awaitable, Callable

from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.current_work_runtime import (
    FrozenCurrentWorkCloseProjection,
    PreparedRootWorkActivation,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelWatchdogOwner
from pulsara_agent.conversation_kernel.prompt_content import FrozenCanonicalPrompt
from pulsara_agent.conversation_kernel.repository import StaleHostWriter
from pulsara_agent.conversation_kernel.runner import KernelRunResult
from pulsara_agent.conversation_kernel.steer import StartedTurnInputGroup
from pulsara_agent.hooks.context import PendingHookContextReservation
from pulsara_agent.hooks.contracts import (
    HookDispatchEnvelope,
    SessionEndInput,
    SessionEndRef,
)
from pulsara_agent.hooks.matcher import event_matcher_subject
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.model_input.contracts import (
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
)
from pulsara_agent.plugins.contracts import EnabledPluginViewDisposition
from pulsara_agent.plugins.view import FrozenEnabledPluginView
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot

from .host_contracts import (
    KernelSessionSettlementUnconfirmed,
    RootChainPhase,
)
from .host_tasks import _join_close_task


class _HostSessionLifecycleOperations:
    """ROOT task ownership and physical session-close settlement on the Host session.

    Operations share the concrete Host's existing state and admission locks.
    """

    async def _run_root_turn_chain(
        self,
        canonical_prompt: FrozenCanonicalPrompt,
        *,
        command_id: str,
        requested_permission_mode: PermissionMode,
        expected_permission_snapshot: FrozenRunPermissionSnapshot,
        hook_context_reservation: PendingHookContextReservation | None,
        cancellation_intent: ActiveTurnCancellationIntent,
        model_resolution_snapshot: FrozenModelResolutionSnapshot,
    ) -> KernelRunResult:
        result = await self._runner.run_turn(
            canonical_prompt,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
            expected_permission_snapshot=expected_permission_snapshot,
            hook_context_reservation=hook_context_reservation,
            cancellation_intent=cancellation_intent,
            model_resolution_snapshot=model_resolution_snapshot,
        )
        return await self._finish_root_chain(result, cancellation_intent)

    async def _run_accepted_root_chain(
        self,
        turn_id: str,
        cancellation_intent: ActiveTurnCancellationIntent,
        *,
        started_input_group: StartedTurnInputGroup | None = None,
    ) -> KernelRunResult:
        """Run one accepted ROOT turn and every automatic successor."""

        result = await self._runner.run_accepted_turn(
            turn_id,
            cancellation_intent=cancellation_intent,
            started_input_group=started_input_group,
        )
        return await self._finish_root_chain(result, cancellation_intent)

    async def _finish_root_chain(
        self,
        result: KernelRunResult,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> KernelRunResult:
        total_model_calls = result.model_call_count
        total_tool_calls = result.tool_call_count
        while result.continuation_turn_id is not None:
            continuation_turn_id = result.continuation_turn_id
            async with self._lock:
                if self._active_task is not asyncio.current_task():
                    raise RuntimeError("ROOT continuation lost its Host task owner")
                pending = self._pending_root_successor
                if (
                    pending is None
                    or pending.owner_task is not asyncio.current_task()
                    or pending.origin_turn_id != self._active_turn_id
                    or pending.successor_turn_id != continuation_turn_id
                    or self._active_root_phase
                    is not RootChainPhase.SUCCESSOR_COMMITTED_PENDING_HANDOFF
                ):
                    raise RuntimeError(
                        "ROOT continuation lost its closed pending handoff"
                    )
                if (
                    self._closing
                    or self._plan_exit_fence
                    or asyncio.current_task().cancelling() != 0
                    or cancellation_intent.cause is not None
                ):
                    raise asyncio.CancelledError
                self._tools.current_work_owner.bind_continuation(
                    scope_kind=self._session_scope_kind,
                    turn_id=continuation_turn_id,
                )
                self._active_turn_id = continuation_turn_id
                cancellation_intent = ActiveTurnCancellationIntent(
                    continuation_turn_id, self._session_scope_kind
                )
                self._active_cancellation_intent = cancellation_intent
                self._pending_root_successor = None
                self._active_root_phase = RootChainPhase.SUCCESSOR_RUNNING
            result = await self._runner.run_accepted_turn(
                continuation_turn_id,
                cancellation_intent=cancellation_intent,
                started_input_group=pending.started_input_group,
            )
            total_model_calls += result.model_call_count
            total_tool_calls += result.tool_call_count
        return KernelRunResult(
            turn_id=result.turn_id,
            final_entry_id=result.final_entry_id,
            final_text=result.final_text,
            model_call_count=total_model_calls,
            tool_call_count=total_tool_calls,
            pending_plan_interaction_id=result.pending_plan_interaction_id,
            final_source=result.final_source,
        )

    def _install_active_root_task_locked(
        self,
        *,
        turn_id: str,
        command_id: str | None,
        name: str,
        run: Callable[[ActiveTurnCancellationIntent], Awaitable[KernelRunResult]],
        work_id: str | None = None,
        started: bool = True,
        cancellation_intent: ActiveTurnCancellationIntent | None = None,
    ) -> asyncio.Task[KernelRunResult]:
        """Install the sole Host-owned ROOT task while ``self._lock`` is held."""

        self._retire_done_active_root_locked()
        if self._active_task is not None:
            raise RuntimeError("a canonical ROOT turn is already running")
        intent = cancellation_intent or ActiveTurnCancellationIntent(
            turn_id, self._session_scope_kind
        )
        intent.require_exact(turn_id=turn_id, scope_kind=self._session_scope_kind)
        if self.root_session_id is None:
            self._ephemeral.activate(turn_id)
        task = asyncio.create_task(
            self._run_owned_root_task(run, intent, work_id),
            name=name,
        )
        self._active_task = task
        self._active_turn_id = turn_id
        self._active_turn_started = started
        self._active_work_id = work_id
        self._active_cancellation_intent = intent
        self._active_command_id = command_id
        self._active_root_phase = RootChainPhase.ORIGIN_RUNNING
        self._pending_root_successor = None
        task.add_done_callback(self._observe_active_root_task_done)
        return task

    @staticmethod
    def _observe_active_root_task_done(task: asyncio.Task[KernelRunResult]) -> None:
        """Consume a detached ROOT task exception after task-owned settlement."""

        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logging.getLogger("pulsara_agent.conversation_kernel.host").error(
                "ROOT execution failed (%s)",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _run_owned_root_task(
        self,
        run: Callable[[ActiveTurnCancellationIntent], Awaitable[KernelRunResult]],
        intent: ActiveTurnCancellationIntent,
        work_id: str | None = None,
    ) -> KernelRunResult:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("ROOT task has no asyncio owner")
        result: KernelRunResult | None = None
        try:
            result = await run(intent)
            return result
        except BaseException as error:
            reason = (
                "USER_STOPPED"
                if intent.cause is ForegroundCancellationCause.USER_REQUEST
                else "SESSION_CLOSED"
                if self._closing
                or intent.cause is ForegroundCancellationCause.HOST_SESSION_CLOSE
                else "PROVIDER_INPUT_RESOURCE_EXHAUSTED"
                if isinstance(error, StructuredModelInputCompileError)
                and _is_input_resource_exhaustion(error)
                else "FOREGROUND_EXECUTION_INTERRUPTED"
            )
            await self._drain_start_settlement(
                asyncio.create_task(
                    self._interrupt_queued_admission_until_safe(
                        turn_id=intent.turn_id, reason=reason
                    ),
                    name=f"kernel-turn-terminal:{intent.turn_id}",
                )
            )
            raise
        finally:
            await self._drain_start_settlement(
                asyncio.create_task(
                    self._finish_owned_root_task(task, intent, work_id, result),
                    name=f"kernel-turn-cleanup:{intent.turn_id}",
                )
            )

    async def _finish_owned_root_task(self, task, intent, work_id, result) -> None:
        settled = False
        try:
            await self._settle_pending_root_successor(task)
            if self.root_session_id is not None:
                # Capacity belongs to this exact task from before start until
                # its execution and resource settlement have finished.
                await self._agent_runtime.release(self.session_id)
                try:
                    await self._io.run(
                        self.repository.mark_agent_idle,
                        session_id=self.session_id,
                        idle=True,
                        at=datetime.now(timezone.utc),
                        deadline_monotonic=self._canonical_deadline(),
                    )
                except Exception:
                    logging.getLogger(
                        "pulsara_agent.conversation_kernel.host"
                    ).exception("Agent idle observation will be retried by scheduler")
            await self._settle_active_root_task(task)
            settled = True
        finally:
            self._ephemeral.completed(intent.turn_id, result if settled else None)
            await self._agent_runtime.notify_changed()

    async def _settle_pending_root_successor(self, task: asyncio.Task[object]) -> None:
        async with self._lock:
            pending = self._pending_root_successor
            if pending is None or pending.owner_task is not task:
                return
            self._pending_root_successor = None
            intent = self._active_cancellation_intent
            cause = None if intent is None else intent.cause
        # The origin chain owns this already-committed successor until the
        # exact turn is terminal.  Caller cancellation cannot detach it, and
        # no replacement process-local task is installed.
        settlement = asyncio.create_task(
            self._terminalize_pending_root_successor(
                pending,
                reason=(
                    "USER_STOPPED"
                    if cause is ForegroundCancellationCause.USER_REQUEST
                    else "SESSION_CLOSED"
                    if cause is ForegroundCancellationCause.HOST_SESSION_CLOSE
                    else "PLAN_CONTINUATION_NOT_BOUND"
                ),
            ),
            name=f"kernel-root-successor-terminalization:{pending.successor_turn_id}",
        )
        while True:
            try:
                await asyncio.shield(settlement)
                break
            except asyncio.CancelledError:
                if settlement.done():
                    settlement.result()
                    break
                # A repeated stop/close cancellation only detaches its waiter;
                # it cannot cancel or replace this exact successor settlement.
                continue

    async def _settle_active_root_task(self, task: asyncio.Task[object]) -> None:
        async with self._lock:
            if self._active_task is task:
                active_turn_id = self._active_turn_id
                if active_turn_id is not None and self._active_turn_started:
                    self._tools.offer_current_work_idle(
                        self._tools.current_work_owner.mark_root_idle(
                            exact_turn_id=active_turn_id
                        ),
                        active_turn_id,
                    )
                self._clear_active_root_locked()

    async def _finalize_root_work_activation(
        self,
        prepared: PreparedRootWorkActivation,
        accepted: object,
    ) -> None:
        async with self._lock:
            if self._active_turn_id == accepted.turn_id:
                self._active_turn_started = True
            closed = self._finalize_root_work_activation_locked(prepared, accepted)
        self._tools.offer_current_work_close(closed)

        if self.root_session_id is None:
            self._ephemeral.admitted(accepted.turn_id)

    def _finalize_root_work_activation_locked(
        self,
        prepared: PreparedRootWorkActivation,
        accepted: object,
    ) -> FrozenCurrentWorkCloseProjection | None:
        """Apply the common admission finalizer while the Host lock is held."""

        if (
            accepted.turn_id != prepared.exact_turn_id
            or accepted.entry_id != prepared.exact_initial_entry_id
        ):
            raise RuntimeError("ROOT work activation does not exact-join admission")
        return self._tools.current_work_owner.activate_root_run(
            prepared,
            allow_closing_predecessor=self._closing,
        )

    def _retire_done_active_root_locked(self) -> None:
        task = self._active_task
        if task is not None and task.done():
            self._clear_active_root_locked()

    def _clear_active_root_locked(self) -> None:
        if self._pending_root_successor is not None:
            raise RuntimeError("ROOT slot cleared with an unsettled successor handoff")
        self._active_task = None
        self._active_turn_id = None
        self._active_turn_started = False
        self._active_work_id = None
        self._active_cancellation_intent = None
        self._active_command_id = None
        self._active_root_phase = None
        self._queue_wake.set()
        self._cell_output_wake.set()
        if self._scheduled_tasks is not None:
            self._scheduled_tasks.wake()

    @property
    def physical_close_complete(self) -> bool:
        return self._physical_close_complete

    @property
    def necessary_settlement_complete(self) -> bool:
        return (
            self._physical_close_complete and self._necessary_settlement_error is None
        )

    @property
    def local_children_stopped(self) -> bool:
        return all(
            owner.local_children_stopped
            for owner in (
                self._tools,
                self._code_mode,
                self._hooks,
                self._mcp_supervisor,
            )
        )

    def _request_close_cancellation(self) -> None:
        if self._external_new_turn_cancellation_intent is not None:
            self._external_new_turn_cancellation_intent.install_cause(
                ForegroundCancellationCause.HOST_SESSION_CLOSE,
            )
        if self._active_task is not None and not self._active_task.done():
            if self._active_cancellation_intent is None:
                raise RuntimeError("active ROOT task lacks cancellation intent")
            self._active_cancellation_intent.install_cause(
                ForegroundCancellationCause.HOST_SESSION_CLOSE
            )
        self._code_mode.mark_all_stopped()
        if (
            self._active_task is not None
            and not self._active_task.done()
            and self._active_task.cancelling() == 0
        ):
            self._active_task.cancel()

    async def _stop_close_admission(self) -> None:
        async with self._lock:
            self._closing = True
            self._pending_workspace_recreated = None
            self._pending_process_controls.clear()
            self._decision_service.stop_admission()
            self._tools.stop_local_process_admission()
            self._request_close_cancellation()
            self._presentation_notices.clear()
            self._queued_prompt_hooks.clear()
            self._tools.current_work_owner.mark_closing(
                scope_kind=self._session_scope_kind,
            )
            self._queue_wake.set()
            self._cell_output_wake.set()

    async def aclose(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        async with self._close_async_lock:
            if self._closed:
                if self._necessary_settlement_error is not None:
                    raise KernelSessionSettlementUnconfirmed(
                        "Host canonical settlement is unconfirmed"
                    ) from self._necessary_settlement_error
                return
            deadline = (
                self._deadlines.deadline(KernelWatchdogOwner.HOST_SESSION_CLOSE)
                if deadline_monotonic is None
                else deadline_monotonic
            )
            self._closing = True
            self._decision_service.stop_admission()
            self._tools.stop_local_process_admission()
            self._request_close_cancellation()
            close_error: BaseException | None = None
            settlement_error: BaseException | None = None
            code_close_task = asyncio.create_task(
                self._code_mode.aclose(),
                name=f"kernel-code-mode-close:{self.host_session_id}",
            )
            decision_close_task = asyncio.create_task(
                self._decision_service.aclose(deadline_monotonic=deadline),
                name=f"kernel-decision-close:{self.host_session_id}",
            )
            terminal_stop_task = asyncio.create_task(
                self._tools.stop_terminal_physical_owners(
                    timeout_seconds=max(0.001, deadline - monotonic())
                ),
                name=f"kernel-terminal-stop:{self.host_session_id}",
            )
            screenshot_stop_task = asyncio.create_task(
                self._tools.stop_visualization_physical_owners(),
                name=f"kernel-visualization-stop:{self.host_session_id}",
            )
            self.workspace_gate.begin_close()
            # SessionEnd consumes the exact close-attempt carrier.  Freeze the
            # fixed workspace root for this attempt; the later terminal Hook
            # lane consumes the carrier without reopening any execution owner.
            session_end_cwd = self._tools.snapshot_workspace_root()
            self._hooks.begin_host_close(deadline_monotonic=deadline)
            ephemeral_close_task = asyncio.create_task(
                self._ephemeral.aclose(),
                name=f"kernel-ephemeral-close:{self.host_session_id}",
            )
            self.extensions.stop_admission()
            self._mcp_supervisor.stop_admission()
            mcp_close_task = asyncio.create_task(
                self._mcp_supervisor.aclose(),
                name=f"kernel-mcp-close:{self.host_session_id}",
            )
            await self._stop_close_admission()
            if self._delivery_task is not None:
                self._delivery_task.cancel()
            try:
                if self._delivery_task is not None and await _join_close_task(
                    self._delivery_task, deadline_monotonic=deadline
                ):
                    close_error = TimeoutError(
                        "prompt delivery settlement exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
                settlement_error = settlement_error or exc
            try:
                if self._cell_output_task is not None and await _join_close_task(
                    self._cell_output_task, deadline_monotonic=deadline
                ):
                    close_error = TimeoutError(
                        "Cell output delivery exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
                settlement_error = settlement_error or exc
            external_settlement = asyncio.create_task(
                self._external_new_turn_settled.wait(),
                name=f"kernel-external-settlement-close:{self.host_session_id}",
            )
            try:
                if await _join_close_task(
                    external_settlement, deadline_monotonic=deadline
                ):
                    close_error = close_error or TimeoutError(
                        "external-result admission settled after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            # Local process termination was requested before any database or
            # auxiliary-provider join above could block this close operation.
            try:
                if await _join_close_task(
                    terminal_stop_task, deadline_monotonic=deadline
                ):
                    close_error = close_error or TimeoutError(
                        "Terminal owner exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            async with self._lock:
                task = self._active_task
                intent = self._active_cancellation_intent
                if task is not None and not task.done():
                    if intent is None:
                        raise RuntimeError("active ROOT task lacks cancellation intent")
                    intent.install_cause(ForegroundCancellationCause.HOST_SESSION_CLOSE)
            if task is not None and not task.done():
                if task.cancelling() == 0:
                    task.cancel()
                try:
                    if await _join_close_task(task, deadline_monotonic=deadline):
                        close_error = close_error or TimeoutError(
                            "canonical foreground task exited after close deadline"
                        )
                except BaseException as exc:
                    close_error = close_error or exc
                    settlement_error = settlement_error or exc
            if task is not None:
                try:
                    await self._settle_active_root_task(task)
                except BaseException as exc:
                    close_error = close_error or exc
                    settlement_error = settlement_error or exc
            async with self._lock:
                control_tasks = tuple(
                    attempt.task
                    for attempt in self._user_control_attempts.values()
                    if attempt.task is not None and not attempt.task.done()
                )
            async with self._lock:
                ingress_tasks = tuple(
                    attempt.task
                    for attempt in self._prompt_admission_attempts.values()
                    if attempt.task is not None and not attempt.task.done()
                )
            for control_task in (*control_tasks, *ingress_tasks):
                try:
                    if await _join_close_task(
                        control_task, deadline_monotonic=deadline
                    ):
                        close_error = close_error or TimeoutError(
                            "user-control settlement exited after close deadline"
                        )
                except BaseException as exc:
                    close_error = close_error or exc
                    settlement_error = settlement_error or exc
            try:
                # The exact ROOT runner and its shielded ToolResult settlement
                # owner have already joined. A retained token now has no
                # legitimate producer that can advance it, so fail closed
                # instead of sleeping on an ownerless condition.
                todo_closed = self._tools.current_work_owner.close_run(
                    scope_kind=self._session_scope_kind,
                )
            except BaseException as exc:
                todo_closed = None
                close_error = close_error or exc
                settlement_error = settlement_error or exc
            self._tools.offer_current_work_close(todo_closed)
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except BaseException:
                pass
            for close_operation in (
                self._compaction.aclose,
                self._plan_interactions.aclose,
                self._plan_continuations.aclose,
                self._interactions.aclose,
            ):
                try:
                    await close_operation()
                except BaseException as exc:
                    close_error = close_error or exc
                    settlement_error = settlement_error or exc
            try:
                await self._assistant_settlements.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
                if not self._assistant_settlements.necessary_settlement_complete:
                    settlement_error = settlement_error or exc
            try:
                terminal_view = await self._hooks.fence_ordinary()
                try:
                    current_binding = await self._io.run(
                        self.repository.read_session_model_call_binding,
                        self._lease.guard,
                        deadline_monotonic=deadline,
                    )
                except StaleHostWriter:
                    # An expired/replaced writer cannot authorize SessionEnd;
                    # this observation does not invalidate physical close.
                    logging.getLogger("pulsara_agent.conversation_kernel.host").warning(
                        "SessionEnd skipped because the Host writer is unavailable"
                    )
                else:
                    terminal_input = SessionEndInput(
                        session_id=self.session_id,
                        cwd=str(session_end_cwd),
                        model=(
                            "unconfigured"
                            if current_binding is None
                            else self._model_identity(current_binding)
                        ),
                        reason="other",
                        session_kind="SUBAGENT"
                        if self.root_session_id is not None
                        else "ROOT",
                        input_origin=None,
                    )
                    await self._hooks.dispatch_terminal(
                        HookDispatchEnvelope(
                            terminal_view,
                            self._hook_root_scope,
                            terminal_input,
                            SessionEndRef(object()),
                            deadline,
                        ),
                        matcher_subject=event_matcher_subject(
                            terminal_input.event_type, reason=terminal_input.reason
                        ),
                    )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self._hooks.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            self._hook_context.close()
            self._input_continuity.close()
            try:
                await self._memory_embedding_maintainer.aclose(
                    deadline_monotonic=deadline
                )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self._memory_tools.aclose()
            except BaseException as exc:
                close_error = close_error or exc
            try:
                if await _join_close_task(
                    decision_close_task, deadline_monotonic=deadline
                ):
                    close_error = close_error or TimeoutError(
                        "Decision requests exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                if await _join_close_task(code_close_task, deadline_monotonic=deadline):
                    close_error = close_error or TimeoutError(
                        "Code Mode exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                if await _join_close_task(
                    screenshot_stop_task, deadline_monotonic=deadline
                ):
                    close_error = close_error or TimeoutError(
                        "Visualization owner exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self._tools.aclose(
                    timeout_seconds=max(0.001, deadline - monotonic())
                )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                if await _join_close_task(mcp_close_task, deadline_monotonic=deadline):
                    close_error = close_error or TimeoutError(
                        "MCP physical owners exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                self._plugin_view.close()
                self._plugin_view = FrozenEnabledPluginView(
                    EnabledPluginViewDisposition.COMPLETE
                )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                if await _join_close_task(
                    ephemeral_close_task, deadline_monotonic=deadline
                ):
                    close_error = close_error or TimeoutError(
                        "Ephemeral products exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            try:
                await self.extensions.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            self.live_bus.close()
            self.live_control.close()
            try:
                await self._io.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            self._physical_close_complete = all(
                owner.physical_close_complete
                for owner in (
                    self._code_mode,
                    self._tools,
                    self._mcp_supervisor,
                    self._hooks,
                    self._ephemeral,
                    self._memory_embedding_maintainer,
                    self._memory_tools,
                    self._decision_service,
                    self.extensions,
                    self._io,
                    self._compaction,
                    self._plan_continuations,
                    self._assistant_settlements,
                )
            )
            self._necessary_settlement_error = settlement_error
            self._closed = self._physical_close_complete
            if close_error is not None:
                logging.getLogger("pulsara_agent.conversation_kernel.host").warning(
                    "Host close diagnostic: %s",
                    self.host_session_id,
                    exc_info=(
                        type(close_error),
                        close_error,
                        close_error.__traceback__,
                    ),
                )
            if not self._physical_close_complete:
                raise close_error or RuntimeError("Host physical close is unconfirmed")
            if settlement_error is not None:
                raise KernelSessionSettlementUnconfirmed(
                    "Host canonical settlement is unconfirmed"
                ) from settlement_error

    async def _renew_writer(self) -> None:
        while True:
            await asyncio.sleep(self._deadlines.policy.writer_renew_interval_seconds)
            try:
                self._lease = await self._io.run(
                    self.repository.renew_host_writer,
                    self._lease.guard,
                    lease_seconds=self._deadlines.policy.writer_lease_seconds,
                    memory_domain_id=self._memory_domain_id,
                    deadline_monotonic=self._deadlines.deadline(
                        KernelWatchdogOwner.WRITER_RENEWAL
                    ),
                )
            except Exception as error:
                self._writer_renewal_failure = error
                raise


def _is_input_resource_exhaustion(
    error: StructuredModelInputCompileError,
) -> bool:
    return error.kind in {
        ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED,
        ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,
        ModelInputCompileFailureKind.STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET,
        ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.TOOL_SCHEMA_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET,
    }
