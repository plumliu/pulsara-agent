"""User-control admission, execution, and transient result retention on the Host session."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timezone

from pulsara_agent.conversation_kernel.cancellation import ForegroundCancellationCause
from pulsara_agent.conversation_kernel.contracts import TurnStatus
from pulsara_agent.conversation_kernel.runner import KernelRunResult
from pulsara_agent.conversation_kernel.steer import build_queued_root_turn_identity
from pulsara_agent.conversation_kernel.user_control import (
    CONTROL_ADMISSION_WINDOW_MS,
    CONTROL_RESULT_RETENTION_MS,
    CodeModeControlResult,
    ControlQueryStatus,
    ProcessControlResult,
    RootControlResult,
    UserControlExecution,
    UserControlOperation,
    UserControlOutcome,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.model_input.continuity import RuntimeObservationItem
from pulsara_agent.identity import is_valid_id, random_id
from pulsara_agent.terminal_process.models import TerminalProcessInfo

from .host_contracts import (
    KernelCommandOutcome,
    KernelControlQueryResult,
    SessionControlRejected,
    _ProcessControlObservation,
    _UserControlAttempt,
)
from .host_tasks import (
    _join_task_beyond_logical_deadline,
    _restore_workspace_directory,
)


@dataclass(frozen=True, slots=True)
class SessionCloseWork:
    root_turn_id: str | None
    queued_root_ids: tuple[str, ...]
    manual_compaction_id: str | None
    compaction_settling: bool
    cells: tuple[tuple[str, str], ...]
    agent_work: tuple[tuple[str, str], ...]
    has_terminal_processes: bool

    @property
    def requires_confirmation(self) -> bool:
        return bool(
            self.root_turn_id or self.queued_root_ids or self.manual_compaction_id
            or self.compaction_settling or self.cells or self.agent_work
        )


async def _join_tab_stop_operations(*operations: Awaitable[None]) -> None:
    for outcome in await asyncio.gather(*operations, return_exceptions=True):
        if isinstance(outcome, BaseException):
            raise outcome


class _HostControlOperations:
    """User-control admission, execution, and transient result retention on the Host session.

    Operations share the concrete Host's existing state and admission locks.
    """

    def _tab_close_cell_owners(self):
        return {self.host_session_id: self._code_mode,
                **self._agent_runtime.cell_owners(self.session_id)}

    async def observe_session_close_work(self) -> SessionCloseWork:
        # Re-read if the ROOT finishes during inspection. Its final tool
        # effects may have admitted work after the first database snapshot.
        original_turn_id = self._session_close_root_turn_id()
        while True:
            queued_root_ids, agent_work = await asyncio.gather(
                self._io.run(
                    self.repository.list_pending_new_turn_ids, session_id=self.session_id,
                    deadline_monotonic=self._canonical_deadline(),
                ),
                self._io.run(
                    self.repository.list_unfinished_agent_work, root_session_id=self.session_id,
                    deadline_monotonic=self._canonical_deadline(),
                ),
            )
            async with self._lock:
                self._require_open()
                root_turn_id = self._session_close_root_turn_id()
                if original_turn_id is not None and root_turn_id is None:
                    original_turn_id = None
                    continue
                manual_compaction_id = self.active_manual_compaction_command_id()
                return SessionCloseWork(
                    root_turn_id=root_turn_id,
                    queued_root_ids=queued_root_ids,
                    manual_compaction_id=manual_compaction_id,
                    compaction_settling=(root_turn_id is None and manual_compaction_id is None
                                         and self._compaction.has_active_work()),
                    cells=tuple((host_id, cell_id)
                                for host_id, owner in self._tab_close_cell_owners().items()
                                for cell_id in owner.live_cell_ids()),
                    agent_work=agent_work,
                    has_terminal_processes=self.archive_has_terminal_work(),
                )

    def _session_close_root_turn_id(self) -> str | None:
        if self._active_task is not None and not self._active_task.done():
            return self._active_turn_id
        intent = self._external_new_turn_cancellation_intent
        return None if intent is None else intent.turn_id

    async def stop_session_close_work(self, work: SessionCloseWork) -> None:
        await _join_tab_stop_operations(
            self._stop_session_close_root(work),
            self._stop_session_close_agents(work.agent_work),
        )
        if (await self.observe_session_close_work()).requires_confirmation:
            raise SessionControlRejected("SESSION_CLOSE_WORK_PENDING", "会话仍有未完成的任务。请检查任务状态后再关闭。")

    async def _stop_session_close_root(self, work: SessionCloseWork) -> None:
        try:
            # Cancel observed pending inputs before interrupting the ROOT that
            # currently holds delivery. If consumption already won, the queue
            # identity still identifies the exact turn we were asked to stop.
            turns: dict[str, bool] = {}
            if work.root_turn_id is not None:
                turns[work.root_turn_id] = False
            for queue_id in work.queued_root_ids:
                status = await self._io.run(
                    self.repository.cancel_prompt, self._lease.guard,
                    queue_item_id=queue_id, occurred_at=datetime.now(timezone.utc),
                    actor_id=self.host_session_id, deadline_monotonic=self._canonical_deadline(),
                )
                turn_id = build_queued_root_turn_identity(self.session_id, queue_id).turn_id
                if status == "CONSUMED":
                    turns[turn_id] = False
                else:
                    self._hook_context.retire_prompt_candidate(
                        scope=self._hook_root_scope, prompt_candidate_id=queue_id,
                    )
                    if turn_id in turns:
                        turns[turn_id] = True
            if work.queued_root_ids:
                self._queue_wake.set()
                if self._scheduled_tasks is not None:
                    self._scheduled_tasks.wake()
            await _join_tab_stop_operations(
                self._stop_session_close_turn(None, work.manual_compaction_id),
                *(self._stop_session_close_turn(turn_id, None, cancelled_before_start=cancelled)
                  for turn_id, cancelled in turns.items()),
            )
        finally:
            owners = self._tab_close_cell_owners()
            await _join_tab_stop_operations(*(
                owners[host_id].stop_and_settle(cell_id)
                for host_id, cell_id in work.cells if host_id in owners
            ))

    async def _stop_session_close_turn(
        self, turn_id: str | None, compaction_id: str | None, *, cancelled_before_start: bool = False,
    ) -> None:
        if turn_id is None and (compaction_id is None or self.active_manual_compaction_command_id() != compaction_id):
            return
        command_id = random_id("command")
        args = dict(command_id=command_id, admission_deadline_ms=self.control_admission_deadline_ms(),
                    expected_session_id=self.session_id, expected_host_session_id=self.host_session_id)
        if turn_id is not None:
            result = await self.request_stop_turn(target_turn_id=turn_id, **args)
        else:
            result = await self.request_stop_context_compaction(target_command_id=compaction_id, **args)
        if result.status == "PENDING":
            attempt = self._user_control_attempts[command_id]
            assert attempt.task is not None
            await asyncio.shield(attempt.task)
            result = attempt.outcome
        control = result.user_control
        root = None if control is None else control.root
        if result.status == "SUCCEEDED" or (root is not None and root.status in {TurnStatus.COMPLETED, TurnStatus.INTERRUPTED}):
            return
        if cancelled_before_start and self._session_close_root_turn_id() != turn_id:
            # The exact queue item is terminal and never installed a turn.
            return
        raise SessionControlRejected("SESSION_CLOSE_STOP_FAILED", "无法确认会话任务已停止。" + result.public_message)

    async def _stop_session_close_agents(self, work: tuple[tuple[str, str], ...]) -> None:
        work_by_agent: dict[str, list[str]] = {}
        for agent_id, work_id in work:
            work_by_agent.setdefault(agent_id, []).append(work_id)
        running_work = []
        for agent_id, work_ids in work_by_agent.items():
            rows = await self._io.run(
                self.repository.cancel_agent_requests,
                self._lease.guard,
                agent_id=agent_id,
                work_ids=work_ids,
                occurred_at=datetime.now(timezone.utc),
                actor_id=self.host_session_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            running_work.extend((agent_id, row["work_id"]) for row in rows if row["status"] == "running")
        await _join_tab_stop_operations(*(
            self._agent_runtime.stop_active_work(agent_id=agent_id, work_id=work_id)
            for agent_id, work_id in running_work
        ))

    def _control_rejection(
        self,
        request: UserControlRequest,
        code: str,
        message: str,
    ) -> KernelCommandOutcome:
        return KernelCommandOutcome(
            request.command_id,
            "REJECTED",
            request.target.target_id,
            code,
            message,
            user_control=UserControlOutcome(
                operation=request.operation,
                session_id=request.session_id,
                host_session_id=request.host_session_id,
                target=request.target,
                accepted=False,
                execution=UserControlExecution.NOT_STARTED,
            ),
        )

    def _validate_new_control_request_locked(
        self, request: UserControlRequest
    ) -> KernelCommandOutcome | None:
        expected_target_kind = {
            UserControlOperation.STOP_ACTIVE_TURN: UserControlTargetKind.ROOT_TURN,
            UserControlOperation.STOP_CONTEXT_COMPACTION: UserControlTargetKind.MANUAL_COMPACTION,
            UserControlOperation.STOP_CODE_MODE_CELL: UserControlTargetKind.CODE_MODE_CELL,
            UserControlOperation.TERMINATE_BACKGROUND_PROCESS: (
                UserControlTargetKind.BACKGROUND_PROCESS
            ),
        }[request.operation]
        if (
            not request.command_id
            or not request.session_id
            or not request.host_session_id
            or not request.target.target_id
            or request.target.kind is not expected_target_kind
        ):
            return self._control_rejection(
                request,
                "CONTROL_REQUEST_INVALID",
                "The exact control owner and target are required.",
            )
        if (
            request.session_id != self.session_id
            or request.host_session_id != self.host_session_id
        ):
            return self._control_rejection(
                request,
                "OWNER_UNAVAILABLE",
                "The requested Host owner is not current.",
            )
        if self._closing or self._closed:
            return self._control_rejection(
                request,
                "OWNER_UNAVAILABLE",
                "The requested Host owner is closing or closed.",
            )
        deadline_ms = request.admission_deadline_ms
        now_ms = int(self._control_monotonic() * 1000)
        if (
            not is_valid_id(request.command_id, "command")
            or type(deadline_ms) is not int
            or deadline_ms < 1
            or deadline_ms > now_ms + CONTROL_ADMISSION_WINDOW_MS
        ):
            return self._control_rejection(
                request,
                "CONTROL_REQUEST_INVALID",
                "The control command identity is invalid.",
            )
        if now_ms >= deadline_ms:
            return self._control_rejection(
                request,
                "CONTROL_REQUEST_EXPIRED",
                "The control command admission deadline has expired.",
            )
        return None

    def _retire_control_attempts_locked(self) -> None:
        now_ms = int(self._control_monotonic() * 1000)
        for command_id, attempt in tuple(self._user_control_attempts.items()):
            if attempt.task is not None and not attempt.task.done():
                continue
            if (
                attempt.retain_until_monotonic_ms is not None
                and now_ms >= attempt.retain_until_monotonic_ms
            ):
                self._user_control_attempts.pop(command_id, None)

    def _existing_control_outcome_locked(
        self, request: UserControlRequest
    ) -> KernelCommandOutcome | None:
        self._retire_control_attempts_locked()
        attempt = self._user_control_attempts.get(request.command_id)
        if attempt is None:
            return None
        if attempt.request != request:
            return self._control_rejection(
                request,
                "CONTROL_COMMAND_CONFLICT",
                "The control command identity is already bound to another request.",
            )
        return attempt.outcome

    def _install_control_attempt_locked(
        self,
        request: UserControlRequest,
        pending: KernelCommandOutcome,
        *,
        process_at_admission: TerminalProcessInfo | None = None,
    ) -> _UserControlAttempt:
        attempt = _UserControlAttempt(
            request=request,
            outcome=pending,
            process_at_admission=process_at_admission,
        )
        self._user_control_attempts[request.command_id] = attempt
        return attempt

    def _finish_control_attempt_locked(
        self, attempt: _UserControlAttempt, outcome: KernelCommandOutcome
    ) -> None:
        current = self._user_control_attempts.get(attempt.request.command_id)
        if current is not attempt:
            return
        attempt.outcome = outcome
        finished_ms = int(self._control_monotonic() * 1000)
        attempt.finished_monotonic_ms = finished_ms
        deadline_ms = attempt.request.admission_deadline_ms
        assert deadline_ms is not None
        attempt.retain_until_monotonic_ms = max(
            deadline_ms,
            finished_ms + CONTROL_RESULT_RETENTION_MS,
        )

    async def restore_missing_workspace(self) -> None:
        self._require_open()
        self._lease = await self._io.run(
            self.repository.renew_host_writer,
            self._lease.guard,
            memory_domain_id=self._memory_domain_id,
            lease_seconds=self._deadlines.policy.writer_lease_seconds,
            deadline_monotonic=self._canonical_deadline(),
        )
        worker = asyncio.create_task(
            asyncio.to_thread(
                _restore_workspace_directory, self.workspace.workspace_root
            )
        )
        _, cancelled, _ = await _join_task_beyond_logical_deadline(
            worker, deadline_monotonic=self._canonical_deadline()
        )
        created = worker.result()
        self._lease = await self._io.run(
            self.repository.renew_host_writer,
            self._lease.guard,
            memory_domain_id=self._memory_domain_id,
            lease_seconds=self._deadlines.policy.writer_lease_seconds,
            deadline_monotonic=self._canonical_deadline(),
        )
        if created:
            await self.record_workspace_recreated(datetime.now(timezone.utc))
        else:
            self.workspace_gate.wake()
            self._queue_wake.set()
        if cancelled is not None:
            raise cancelled

    async def record_workspace_recreated(self, created_at: datetime) -> None:
        async with self._lock:
            if not self._closing and not self._closed:
                self._pending_workspace_recreated = created_at
            self.workspace_gate.wake()
            self._queue_wake.set()

    async def take_user_actions(self) -> tuple[RuntimeObservationItem, ...] | None:
        """Take the current advisory batch once; later operations form a new batch."""
        async with self._lock:
            if self._closing or self._closed:
                self._pending_workspace_recreated = None
                self._pending_process_controls.clear()
                return None
            workspace = self._pending_workspace_recreated
            processes = tuple(
                self._pending_process_controls[key]
                for key in sorted(self._pending_process_controls)
            )
            self._pending_workspace_recreated = None
            self._pending_process_controls.clear()
        if workspace is None and not processes:
            return None

        # Capture the existing public-projection boundary outside the Host lock.
        # If it is unavailable, the advisory batch can be discarded; physical
        # operation results have already been settled by their owners.
        scrub = None
        if workspace is not None or any(
            item.public_detail is not None for item in processes
        ):
            deadline = self._canonical_deadline()

            def capture_scrub():
                current = self._model_runtime.settings.read().credential_scrub_set()
                for value in self._credential_boundary.capture_scrub_set(
                    deadline_monotonic=deadline
                ).values:
                    current.observe(value)
                return current

            try:
                scrub = await asyncio.to_thread(capture_scrub)
            except Exception:
                return None

        try:
            details = tuple(
                scrub.scrub_text(item.public_detail)
                if item.public_detail is not None and scrub is not None
                else None
                for item in processes
            )
        except Exception:
            return None

        actions = []
        if workspace is not None:
            actions.append(RuntimeObservationItem("用户重新创建了工作区空目录，原文件未恢复；后续应以实际磁盘内容为准。"))
        for item,public_detail in zip(processes,details,strict=True):
            result=item.result
            if result.disposition=="TERMINATION_COMPLETED":
                text="用户已停止后台进程。"
            elif result.disposition=="OWNER_UNAVAILABLE":
                text="用户尝试停止后台进程，但停止失败。"
            else:
                text="用户尝试停止后台进程；当前结果为 "+result.disposition+"，物理状态为 "+result.physical_state+"。尚不能确认已停止。"
            if not isinstance(item.origin,str):
                text+=" 进程来自子代理 "+item.origin[1]+"。"
            if public_detail:
                text+=" 详情："+public_detail
            actions.append(RuntimeObservationItem(text,{"kind":"PROCESS","process_id":item.process_id}))
        return tuple(actions) or None

    def _pending_control_outcome(
        self,
        request: UserControlRequest,
    ) -> KernelCommandOutcome:
        return KernelCommandOutcome(
            request.command_id,
            "PENDING",
            request.target.target_id,
            "CONTROL_ACCEPTED",
            "The exact control request was accepted.",
            user_control=UserControlOutcome(
                operation=request.operation,
                session_id=request.session_id,
                host_session_id=request.host_session_id,
                target=request.target,
                accepted=True,
                execution=UserControlExecution.RUNNING,
            ),
        )

    async def request_stop_context_compaction(
        self,
        *,
        command_id: str,
        admission_deadline_ms: int,
        expected_session_id: str,
        expected_host_session_id: str,
        target_command_id: str,
    ) -> KernelCommandOutcome:
        request = UserControlRequest(
            UserControlOperation.STOP_CONTEXT_COMPACTION,
            command_id,
            expected_session_id,
            expected_host_session_id,
            UserControlTarget(
                UserControlTargetKind.MANUAL_COMPACTION, target_command_id
            ),
            admission_deadline_ms,
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            rejection = self._validate_new_control_request_locked(request)
            if rejection is not None:
                return rejection
            task = self._manual_compaction_command_attempts.get(target_command_id)
            if task is None:
                return self._control_rejection(
                    request,
                    "CONTROL_TARGET_UNAVAILABLE",
                    "The original manual compaction owner is unavailable.",
                )
            if task.done():
                outcome = KernelCommandOutcome(
                    command_id,
                    "SUCCEEDED",
                    target_command_id,
                    "CONTROL_ALREADY_TERMINAL",
                    "The manual compaction has already ended.",
                    user_control=UserControlOutcome(
                        request.operation,
                        request.session_id,
                        request.host_session_id,
                        request.target,
                        False,
                        UserControlExecution.FINISHED,
                    ),
                )
                attempt = self._install_control_attempt_locked(request, outcome)
                self._finish_control_attempt_locked(attempt, outcome)
                return outcome
            attempt = self._install_control_attempt_locked(
                request, self._pending_control_outcome(request)
            )
            attempt.task = asyncio.create_task(
                self._execute_stop_context_compaction(attempt, task),
                name=f"kernel-stop-manual-compaction:{target_command_id}",
            )
            return attempt.outcome

    async def _execute_stop_context_compaction(self, attempt, task) -> None:
        # Capture the exact owner at control admission. A later operation can
        # never be cancelled by resolving this target name a second time.
        task.cancel()
        try:
            result, _cancelled = await self._drain_start_settlement(task)
            stopped = result.public_code == "COMPACTION_CANCELLED"
            outcome = KernelCommandOutcome(
                attempt.request.command_id,
                "SUCCEEDED",
                attempt.request.target.target_id,
                "COMPACTION_STOPPED" if stopped else "CONTROL_ALREADY_TERMINAL",
                "The manual compaction was stopped."
                if stopped
                else "The manual compaction has ended.",
                user_control=UserControlOutcome(
                    attempt.request.operation,
                    attempt.request.session_id,
                    attempt.request.host_session_id,
                    attempt.request.target,
                    True,
                    UserControlExecution.FINISHED,
                ),
            )
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            outcome = KernelCommandOutcome(
                attempt.request.command_id,
                "SUCCEEDED",
                attempt.request.target.target_id,
                "COMPACTION_STOPPED",
                "The manual compaction was stopped before it started.",
                user_control=UserControlOutcome(
                    attempt.request.operation,
                    attempt.request.session_id,
                    attempt.request.host_session_id,
                    attempt.request.target,
                    True,
                    UserControlExecution.FINISHED,
                ),
            )
        except Exception:
            outcome = KernelCommandOutcome(
                attempt.request.command_id,
                "FAILED",
                attempt.request.target.target_id,
                "CONTROL_FAILED",
                "The manual compaction owner could not be settled.",
                user_control=UserControlOutcome(
                    attempt.request.operation,
                    attempt.request.session_id,
                    attempt.request.host_session_id,
                    attempt.request.target,
                    True,
                    UserControlExecution.FINISHED,
                ),
            )
        async with self._lock:
            self._finish_control_attempt_locked(attempt, outcome)

    async def request_stop_turn(
        self,
        *,
        command_id: str,
        admission_deadline_ms: int,
        expected_session_id: str,
        expected_host_session_id: str,
        target_turn_id: str,
    ) -> KernelCommandOutcome:
        request = UserControlRequest(
            UserControlOperation.STOP_ACTIVE_TURN,
            command_id,
            expected_session_id,
            expected_host_session_id,
            UserControlTarget(UserControlTargetKind.ROOT_TURN, target_turn_id),
            admission_deadline_ms,
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            rejection = self._validate_new_control_request_locked(request)
            if rejection is not None:
                return rejection
            self._retire_done_active_root_locked()
            task = self._active_task
            intent = self._active_cancellation_intent
            if (
                task is not None
                and not task.done()
                and self._active_turn_id == target_turn_id
            ):
                if intent is None:
                    raise RuntimeError("active ROOT task lacks cancellation intent")
                intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
                # Stop is effective before any Cell VM/side-effect join can
                # block. Mark the whole turn on this owner loop first, so a
                # later Cell cannot publish while an earlier one is joining.
                self._code_mode.mark_turn_stopped(target_turn_id)
                if task.cancelling() == 0:
                    task.cancel()
                pending = self._pending_control_outcome(request)
                attempt = self._install_control_attempt_locked(request, pending)
                attempt.task = asyncio.create_task(
                    self._execute_stop_turn_attempt(attempt, task),
                    name=f"kernel-user-stop:{target_turn_id}",
                )
                return pending
            external_intent = self._external_new_turn_cancellation_intent
            if (
                self._external_new_turn_accepting
                and external_intent is not None
                and external_intent.turn_id == target_turn_id
            ):
                external_intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
                attempt = self._install_control_attempt_locked(
                    request, self._pending_control_outcome(request)
                )
                attempt.task = asyncio.create_task(
                    self._execute_stop_starting_turn(
                        attempt, self._external_new_turn_settled
                    ),
                    name=f"kernel-stop-starting-turn:{target_turn_id}",
                )
                return attempt.outcome
            admission_owner = self._plan_continuations.owned_task_for_turn(
                target_turn_id
            )
            if (
                admission_owner is not None
                and task is not None
                and not task.done()
                and intent is not None
            ):
                # An automatic successor is already canonical while the origin
                # chain still owns the handoff. Stop that exact chain; its
                # admitted child settles the successor under the first cause.
                intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
                self._code_mode.mark_turn_stopped(target_turn_id)
                if task.cancelling() == 0:
                    task.cancel()
                attempt = self._install_control_attempt_locked(
                    request, self._pending_control_outcome(request)
                )
                attempt.task = asyncio.create_task(
                    self._execute_stop_turn_attempt(attempt, task),
                    name=f"kernel-stop-plan-successor:{target_turn_id}",
                )
                return attempt.outcome
            active_drifted = self._active_turn_id is not None
        terminal = await self._io.run(
            self.repository.read_turn_terminal_outcome,
            session_id=self.session_id,
            turn_id=target_turn_id,
            deadline_monotonic=self._canonical_deadline(),
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            if terminal is None or not str(terminal.get("status") or ""):
                return self._control_rejection(
                    request,
                    "CONTROL_TARGET_NOT_CURRENT"
                    if active_drifted
                    else "CONTROL_TARGET_UNAVAILABLE",
                    "The target ROOT turn is not the current running turn.",
                )
            if str(terminal["status"]) == "RUNNING":
                return self._control_rejection(
                    request,
                    "CONTROL_TARGET_UNAVAILABLE",
                    "The running turn has no current execution owner available to stop.",
                )
            root = RootControlResult(
                str(terminal["status"]),
                str(terminal.get("terminal_reason") or "") or None,
            )
            outcome = KernelCommandOutcome(
                command_id,
                "SUCCEEDED",
                target_turn_id,
                "CONTROL_ALREADY_TERMINAL",
                "The target ROOT turn had already reached a terminal state.",
                user_control=UserControlOutcome(
                    request.operation,
                    request.session_id,
                    request.host_session_id,
                    request.target,
                    False,
                    UserControlExecution.FINISHED,
                    root=root,
                ),
            )
            attempt = self._install_control_attempt_locked(request, outcome)
            self._finish_control_attempt_locked(attempt, outcome)
            return outcome

    async def _execute_stop_starting_turn(
        self, attempt, original_settlement: asyncio.Event
    ) -> None:
        target = attempt.request.target.target_id
        try:
            # This Event belongs to the captured admission, not to a subsequent
            # reservation. The admission keeps its physical write/confirmation
            # owner and observes the already-installed stop cause before binding.
            await original_settlement.wait()
            async with self._lock:
                task = self._active_task if self._active_turn_id == target else None
            if task is not None and not task.done():
                await self._execute_stop_turn_attempt(attempt, task)
                return
            await self._drain_start_settlement(
                asyncio.create_task(
                    self._interrupt_queued_admission_until_safe(
                        turn_id=target, reason="USER_STOPPED"
                    ),
                    name=f"kernel-start-stop-terminal:{target}",
                )
            )
            terminal = await self._io.run(
                self.repository.read_turn_terminal_outcome,
                session_id=self.session_id,
                turn_id=target,
                deadline_monotonic=self._canonical_deadline(),
            )
            root = RootControlResult(
                None if terminal is None else str(terminal["status"]),
                None if terminal is None else terminal.get("terminal_reason"),
            )
            succeeded = root.status == "INTERRUPTED"
            outcome = KernelCommandOutcome(
                attempt.request.command_id,
                "SUCCEEDED" if succeeded else "FAILED",
                target,
                "ROOT_STOPPED" if succeeded else "CONTROL_FAILED",
                "The starting turn was stopped."
                if succeeded
                else "The starting turn did not confirm interruption.",
                user_control=UserControlOutcome(
                    attempt.request.operation,
                    attempt.request.session_id,
                    attempt.request.host_session_id,
                    attempt.request.target,
                    True,
                    UserControlExecution.FINISHED,
                    root=root,
                ),
            )
        except Exception:
            outcome = KernelCommandOutcome(
                attempt.request.command_id,
                "FAILED",
                target,
                "CONTROL_FAILED",
                "The starting turn control could not confirm its terminal result.",
                user_control=UserControlOutcome(
                    attempt.request.operation,
                    attempt.request.session_id,
                    attempt.request.host_session_id,
                    attempt.request.target,
                    True,
                    UserControlExecution.FINISHED,
                ),
            )
        async with self._lock:
            self._finish_control_attempt_locked(attempt, outcome)

    async def _execute_stop_turn_attempt(
        self, attempt: _UserControlAttempt, task: asyncio.Task[KernelRunResult]
    ) -> None:
        try:
            # A starting-turn admission may bind its task after Stop was
            # accepted. It uses the same early cancellation boundary here.
            self._code_mode.mark_turn_stopped(attempt.request.target.target_id)
            if not task.done() and task.cancelling() == 0:
                task.cancel()
            await self._code_mode.stop_turn(attempt.request.target.target_id)
            await asyncio.gather(task, return_exceptions=True)
            await self._settle_active_root_task(task)
            terminal = await self._io.run(
                self.repository.read_turn_terminal_outcome,
                session_id=self.session_id,
                turn_id=attempt.request.target.target_id,
                deadline_monotonic=self._canonical_deadline(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = KernelCommandOutcome(
                attempt.request.command_id,
                "FAILED",
                attempt.request.target.target_id,
                "CONTROL_FAILED",
                "The ROOT control owner failed before confirming interruption.",
                user_control=UserControlOutcome(
                    attempt.request.operation,
                    attempt.request.session_id,
                    attempt.request.host_session_id,
                    attempt.request.target,
                    True,
                    UserControlExecution.FINISHED,
                ),
            )
            async with self._lock:
                self._finish_control_attempt_locked(attempt, outcome)
            return
        root = RootControlResult(
            None if terminal is None else str(terminal.get("status") or "") or None,
            None
            if terminal is None
            else str(terminal.get("terminal_reason") or "") or None,
        )
        succeeded = root.status == "INTERRUPTED"
        outcome = KernelCommandOutcome(
            attempt.request.command_id,
            "SUCCEEDED" if succeeded else "FAILED",
            attempt.request.target.target_id,
            "ROOT_STOPPED" if succeeded else "CONTROL_FAILED",
            "The target ROOT turn was stopped."
            if succeeded
            else "The target ROOT turn did not confirm interruption.",
            user_control=UserControlOutcome(
                attempt.request.operation,
                attempt.request.session_id,
                attempt.request.host_session_id,
                attempt.request.target,
                True,
                UserControlExecution.FINISHED,
                root=root,
            ),
        )
        async with self._lock:
            self._finish_control_attempt_locked(attempt, outcome)

    def code_mode_cells(self) -> tuple[dict[str, object], ...]:
        return self._code_mode.snapshot_cell_index()

    async def request_stop_code_mode_cell(
        self,
        *,
        command_id: str,
        admission_deadline_ms: int,
        expected_session_id: str,
        expected_host_session_id: str,
        cell_id: str,
    ) -> KernelCommandOutcome:
        request = UserControlRequest(
            UserControlOperation.STOP_CODE_MODE_CELL,
            command_id,
            expected_session_id,
            expected_host_session_id,
            UserControlTarget(UserControlTargetKind.CODE_MODE_CELL, cell_id),
            admission_deadline_ms,
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            rejection = self._validate_new_control_request_locked(request)
            if rejection is not None:
                return rejection
            current = next(
                (
                    cell
                    for cell in self._code_mode.snapshot_cell_index()
                    if cell["cell_id"] == cell_id
                ),
                None,
            )
            if current is None:
                return self._control_rejection(
                    request,
                    "CONTROL_TARGET_UNAVAILABLE",
                    "The script execution is unavailable.",
                )
            if current["state"] in {"completed", "failed", "terminated"}:
                result = CodeModeControlResult(
                    cell_id, str(current["state"]), int(current["pending_calls"])
                )
                outcome = KernelCommandOutcome(
                    command_id,
                    "SUCCEEDED",
                    cell_id,
                    "CONTROL_ALREADY_TERMINAL",
                    "The script has already stopped.",
                    user_control=UserControlOutcome(
                        request.operation,
                        request.session_id,
                        request.host_session_id,
                        request.target,
                        False,
                        UserControlExecution.FINISHED,
                        cell=result,
                    ),
                )
                attempt = self._install_control_attempt_locked(request, outcome)
                self._finish_control_attempt_locked(attempt, outcome)
                return outcome
            pending = self._pending_control_outcome(request)
            attempt = self._install_control_attempt_locked(request, pending)
            attempt.task = asyncio.create_task(
                self._execute_stop_code_mode_cell(attempt),
                name=f"kernel-user-stop-cell:{cell_id}",
            )
            return pending

    async def _execute_stop_code_mode_cell(self, attempt: _UserControlAttempt) -> None:
        request = attempt.request
        try:
            actual = await self._code_mode.stop(request.target.target_id)
            cell = CodeModeControlResult(
                str(actual["target"]["cell_id"]),
                str(actual["state"]),
                int(actual["pending_calls"]),
            )
            outcome = KernelCommandOutcome(
                request.command_id,
                "SUCCEEDED",
                cell.cell_id,
                "CODE_MODE_CELL_STOPPED",
                "The script stopped; accepted work keeps its own lifecycle.",
                user_control=UserControlOutcome(
                    request.operation,
                    request.session_id,
                    request.host_session_id,
                    request.target,
                    True,
                    UserControlExecution.FINISHED,
                    cell=cell,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = KernelCommandOutcome(
                request.command_id,
                "FAILED",
                request.target.target_id,
                "CONTROL_FAILED",
                "The script owner could not confirm stopping.",
                user_control=UserControlOutcome(
                    request.operation,
                    request.session_id,
                    request.host_session_id,
                    request.target,
                    True,
                    UserControlExecution.FINISHED,
                ),
            )
        async with self._lock:
            self._finish_control_attempt_locked(attempt, outcome)

    async def subagent_capacity_state(self) -> tuple[int, int]:
        return await self._agent_runtime.capacity_state()

    async def set_subagent_capacity(self, target: int) -> tuple[int, int]:
        async with self._lock:
            self._require_open()
            await self._io.run(
                self.repository.validate_host_writer,
                self._lease.guard,
                deadline_monotonic=self._canonical_deadline(),
            )
        await self._agent_runtime.set_capacity_target(target)
        await self._agent_runtime.wake_agent_requests(
            self.root_session_id or self.session_id
        )
        return await self._agent_runtime.capacity_state()

    async def request_terminate_background_process(
        self,
        *,
        command_id: str,
        admission_deadline_ms: int,
        expected_session_id: str,
        expected_host_session_id: str,
        process_id: str,
    ) -> KernelCommandOutcome:
        request = UserControlRequest(
            UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
            command_id,
            expected_session_id,
            expected_host_session_id,
            UserControlTarget(UserControlTargetKind.BACKGROUND_PROCESS, process_id),
            admission_deadline_ms,
        )
        async with self._lock:
            existing = self._existing_control_outcome_locked(request)
            if existing is not None:
                return existing
            rejection = self._validate_new_control_request_locked(request)
            if rejection is not None:
                return rejection
            process = next(
                (
                    item
                    for item in self._tools.list_background_terminal_processes()
                    if item.process_id == process_id
                ),
                None,
            )
            if process is None:
                return self._control_rejection(
                    request,
                    "CONTROL_TARGET_UNAVAILABLE",
                    "The background process is unavailable in this Host.",
                )
            self._retire_done_active_root_locked()
            pending = self._pending_control_outcome(request)
            attempt = self._install_control_attempt_locked(
                request, pending, process_at_admission=process
            )
            attempt.task = asyncio.create_task(
                self._execute_terminate_process_attempt(attempt),
                name=f"kernel-user-terminate-process:{process_id}",
            )
            return pending

    def list_background_processes(
        self, *, expected_session_id: str, expected_host_session_id: str
    ) -> tuple[TerminalProcessInfo, ...]:
        self._require_open()
        if (
            expected_session_id != self.session_id
            or expected_host_session_id != self.host_session_id
        ):
            raise KeyError("background process owner is unavailable")
        return tuple(self._tools.list_background_terminal_processes())

    def read_background_process_log(
        self,
        *,
        expected_session_id: str,
        expected_host_session_id: str,
        process_id: str,
        maximum_chars: int,
        since_cursor: str | None,
    ):
        self._require_open()
        if (
            expected_session_id != self.session_id
            or expected_host_session_id != self.host_session_id
        ):
            raise KeyError("background process owner is unavailable")
        if not any(
            item.process_id == process_id
            for item in self._tools.list_background_terminal_processes()
        ):
            raise KeyError("background process is unavailable")
        return self._tools.read_background_terminal_log(
            process_id,
            maximum_chars=maximum_chars,
            since_cursor=since_cursor,
        )

    async def _execute_terminate_process_attempt(
        self, attempt: _UserControlAttempt
    ) -> None:
        try:
            result = await self._io.run(
                self._tools.terminate_background_terminal_process,
                attempt.request.target.target_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            process = ProcessControlResult(
                result.disposition.value,
                result.result.status.value,
                result.result.exit_code,
                result.physical_state,
                result.group_alive,
            )
            physical_failed = (
                result.disposition.value == "PHYSICAL_SETTLEMENT_INCOMPLETE"
            )
        except Exception as exc:
            process = ProcessControlResult(
                "OWNER_UNAVAILABLE",
                "unknown",
                None,
                "unknown",
                None,
            )
            physical_failed = True
            physical_detail = str(exc)
        else:
            physical_detail = None
        new_control_attempt = (
            result.disposition.value != "ALREADY_TERMINAL"
            if physical_detail is None
            else True
        )
        failed = physical_failed
        outcome = KernelCommandOutcome(
            attempt.request.command_id,
            "FAILED" if failed else "SUCCEEDED",
            attempt.request.target.target_id,
            "CONTROL_PARTIAL_FAILURE"
            if failed
            else "CONTROL_ALREADY_TERMINAL"
            if not new_control_attempt
            else "BACKGROUND_CONTROL_COMPLETED",
            physical_detail
            or (
                "The background process control completed."
                if not failed
                else "The background process control was only partially completed."
            ),
            user_control=UserControlOutcome(
                attempt.request.operation,
                attempt.request.session_id,
                attempt.request.host_session_id,
                attempt.request.target,
                new_control_attempt,
                UserControlExecution.FINISHED,
                process=process,
            ),
        )
        async with self._lock:
            self._finish_control_attempt_locked(attempt, outcome)
            info = attempt.process_at_admission
            if (
                new_control_attempt
                and info is not None
                and not self._closing
                and not self._closed
            ):
                origin = (
                    "ROOT"
                    if info.origin.conversation_scope_kind == "ROOT"
                    else ("AGENT", info.origin.session_id)
                )
                self._pending_process_controls[info.process_id] = (
                    _ProcessControlObservation(
                        info.process_id,
                        origin,
                        datetime.now(timezone.utc),
                        process,
                        outcome.public_message if physical_failed else None,
                    )
                )

    async def query_control_command(
        self, request: UserControlRequest
    ) -> KernelControlQueryResult:
        if (
            request.session_id != self.session_id
            or request.host_session_id != self.host_session_id
        ):
            return KernelControlQueryResult(ControlQueryStatus.OWNER_UNAVAILABLE, None)
        async with self._lock:
            self._retire_control_attempts_locked()
            attempt = self._user_control_attempts.get(request.command_id)
            if attempt is not None:
                if attempt.request != request:
                    return KernelControlQueryResult(
                        ControlQueryStatus.FOUND,
                        self._control_rejection(
                            request,
                            "CONTROL_COMMAND_CONFLICT",
                            "The control command identity conflicts.",
                        ),
                    )
                return KernelControlQueryResult(
                    ControlQueryStatus.FOUND, attempt.outcome
                )
        return KernelControlQueryResult(ControlQueryStatus.RESULT_UNAVAILABLE, None)
