"""Host-scoped, same-session Stage 2 subagent execution.

The manager owns only live asyncio tasks.  Accepted task coordination and the
task-scoped conversation are canonical PostgreSQL facts; no execution attempt,
lease, checkpoint, or resume carrier is durable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Callable, Mapping
from uuid import uuid4

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
    stable_subagent_turn_id,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
)
from pulsara_agent.ports.live_agent_event import (
    SubagentProgressPayload,
    live_digest,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
    TurnAdmissionConfirmationKind,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelRunResult,
    KernelToolResult,
)


SUBAGENT_TOOL_NAMES = frozenset(
    {"spawn_agent", "list_agents", "wait_agent", "stop_agent"}
)
MAXIMUM_LIVE_SUBAGENTS = STAGE2_LIMITS.nonterminal_subagent_hard_items


@dataclass(slots=True)
class _LiveTask:
    task_id: str
    parent_turn_id: str
    objective: str
    task: asyncio.Task[KernelRunResult]
    cancellation_intent: ActiveTurnCancellationIntent
    status: str = "ACTIVE"
    result: KernelRunResult | None = None
    failure_code: str | None = None
    cancellation_reason: str | None = None
    child_result_id: str | None = None


class KernelSubagentManager:
    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        guard: HostWriterGuard,
        host_owner_id: str,
        io_owner: KernelSessionIO,
        live_bus: LiveAgentEventBus,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
    ) -> None:
        self._repository = repository
        self._guard = guard
        self._host_owner_id = host_owner_id
        self._io = io_owner
        self._live_bus = live_bus
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._runner_factory: Callable[[], ConversationKernelRunner] | None = None
        self._tasks: dict[str, _LiveTask] = {}
        self._spawning: set[str] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    @property
    def tool_names(self) -> frozenset[str]:
        return SUBAGENT_TOOL_NAMES

    def bind_runner_factory(
        self, factory: Callable[[], ConversationKernelRunner]
    ) -> None:
        if self._runner_factory is not None:
            raise RuntimeError("subagent runner factory is already bound")
        self._runner_factory = factory

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        parent_turn_id: str,
    ) -> KernelToolResult:
        if tool_name == "spawn_agent":
            return await self._spawn(arguments, parent_turn_id=parent_turn_id)
        if tool_name == "list_agents":
            return await self._list(arguments)
        if tool_name == "wait_agent":
            return await self._wait(arguments)
        if tool_name == "stop_agent":
            return await self._stop(arguments)
        raise KeyError(tool_name)

    async def _spawn(
        self,
        arguments: Mapping[str, object],
        *,
        parent_turn_id: str,
    ) -> KernelToolResult:
        objective = str(arguments.get("task") or "").strip()
        if (
            not objective
            or len(objective.encode("utf-8"))
            > STAGE2_LIMITS.subagent_objective_hard_bytes
        ):
            return _result("APPLICATION_ERROR", {"error": "task is required"})
        async with self._lock:
            if self._closed or self._runner_factory is None:
                return _result(
                    "TOOL_UNAVAILABLE", {"error": "subagent owner is closed"}
                )
            active = sum(not item.task.done() for item in self._tasks.values()) + len(
                self._spawning
            )
            if active >= MAXIMUM_LIVE_SUBAGENTS:
                return _result(
                    "APPLICATION_ERROR",
                    {
                        "error": "subagent capacity is exhausted",
                        "capacity": MAXIMUM_LIVE_SUBAGENTS,
                    },
                )
            task_id = f"subagent-task:{uuid4().hex}"
            self._spawning.add(task_id)
        try:
            await self._io.run(
                self._repository.accept_subagent_task,
                self._guard,
                task_id=task_id,
                parent_turn_id=parent_turn_id,
                objective=objective,
                occurred_at=datetime.now(timezone.utc),
                actor_id=self._host_owner_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            changed = await self._io.run(
                self._repository.set_subagent_task_status,
                self._guard,
                task_id=task_id,
                status="ACTIVE",
                reason=None,
                occurred_at=datetime.now(timezone.utc),
                actor_id=self._host_owner_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            if not changed:
                raise RuntimeError("subagent activation CAS failed")
            self._offer_progress(task_id, parent_turn_id, "ACTIVE", "Subagent started")
        except BaseException:
            async with self._lock:
                self._spawning.discard(task_id)
            raise
        closing_after_accept = False
        async with self._lock:
            self._spawning.discard(task_id)
            if self._closed:
                closing_after_accept = True
            else:
                intent = ActiveTurnCancellationIntent(
                    turn_id=stable_subagent_turn_id(
                        session_id=self._guard.session_id, task_id=task_id
                    ),
                    scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                    scope_subagent_task_id=task_id,
                )
                task = asyncio.create_task(
                    self._run_child(task_id, objective, intent),
                    name=f"kernel-subagent:{task_id}",
                )
                live = _LiveTask(task_id, parent_turn_id, objective, task, intent)
                self._tasks[task_id] = live
        if closing_after_accept:
            await self._terminalize_best_effort(
                task_id, "INTERRUPTED", "HOST_CLOSING"
            )
            self._offer_progress(
                task_id, parent_turn_id, "INTERRUPTED", "Subagent interrupted"
            )
            return _result(
                "TOOL_UNAVAILABLE", {"error": "subagent owner is closed"}
            )
        return _result(
            "SUCCESS",
            {
                "status": "started",
                "subagent_run_id": task_id,
                "conversation_scope": {"kind": "SUBAGENT_TASK", "task_id": task_id},
            },
            remote_identity=task_id,
        )

    async def _run_child(
        self,
        task_id: str,
        objective: str,
        cancellation_intent: ActiveTurnCancellationIntent,
    ) -> KernelRunResult:
        runner_factory = self._runner_factory
        assert runner_factory is not None
        try:
            result = await runner_factory().run_subagent_turn(
                task_id=task_id,
                objective=objective,
                cancellation_intent=cancellation_intent,
            )
            child_result_id = _stable_child_id(task_id, result.final_entry_id)

            async def settle_completed_child() -> None:
                await self._io.run(
                    self._repository.accept_subagent_child,
                    self._guard,
                    child_id=child_result_id,
                    task_id=task_id,
                    child_kind="RESULT",
                    # RESULT is the terminal child after every accepted MESSAGE.
                    # Let the repository derive that exact ordinal in the same
                    # transaction rather than trusting process-local call count.
                    child_ordinal=None,
                    entry_id=result.final_entry_id,
                    occurred_at=datetime.now(timezone.utc),
                    actor_id=task_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                await self._io.run(
                    self._repository.set_subagent_task_status,
                    self._guard,
                    task_id=task_id,
                    status="COMPLETED",
                    reason=None,
                    occurred_at=datetime.now(timezone.utc),
                    actor_id=self._host_owner_id,
                    deadline_monotonic=self._canonical_deadline(),
                )

            completion_settlement = asyncio.create_task(
                settle_completed_child(),
                name=f"kernel-subagent-completion:{task_id}",
            )
            # The exact child turn already has a completed assistant winner.
            # A late stop/close may detach its waiter, but cannot replace that
            # winner or split the result/task settlement.
            await _join_child_settlement(completion_settlement)
            async with self._lock:
                live = self._tasks.get(task_id)
                if live is not None:
                    live.status = "COMPLETED"
                    live.result = result
                    live.child_result_id = child_result_id
                    parent_turn_id = live.parent_turn_id
                else:
                    parent_turn_id = task_id
            self._offer_progress(
                task_id,
                parent_turn_id,
                "COMPLETED",
                result.final_text,
            )
            return result
        except asyncio.CancelledError:
            async with self._lock:
                live = self._tasks.get(task_id)
                reason = (
                    live.cancellation_reason if live is not None else None
                ) or "HOST_CLOSING"
            explicit_stop = reason == "USER_CANCELLED"
            status = "CANCELLED" if explicit_stop else "INTERRUPTED"
            turn_reason = "USER_STOPPED" if explicit_stop else "SESSION_CLOSED"
            cancellation_settlement = asyncio.create_task(
                self._settle_cancelled_child(
                    task_id=task_id,
                    turn_id=cancellation_intent.turn_id,
                    task_status=status,
                    task_reason=reason,
                    turn_reason=turn_reason,
                ),
                name=f"kernel-subagent-cancellation:{task_id}",
            )
            historical = await _join_child_settlement(cancellation_settlement)
            async with self._lock:
                live = self._tasks.get(task_id)
                if live is not None:
                    if historical is not None:
                        live.status = "COMPLETED"
                        live.failure_code = None
                        live.child_result_id = _stable_child_id(
                            task_id, historical.entry_id
                        )
                    else:
                        live.status = status
                        live.failure_code = reason
                    parent_turn_id = live.parent_turn_id
                else:
                    parent_turn_id = task_id
            self._offer_progress(
                task_id,
                parent_turn_id,
                "COMPLETED" if historical is not None else status,
                (
                    "Subagent completed before cancellation"
                    if historical is not None
                    else reason
                ),
            )
            raise
        except BaseException as exc:
            code = f"CHILD_{type(exc).__name__.upper()}"
            await self._terminalize_best_effort(task_id, "FAILED", code)
            async with self._lock:
                live = self._tasks.get(task_id)
                if live is not None:
                    live.status = "FAILED"
                    live.failure_code = code
                    parent_turn_id = live.parent_turn_id
                else:
                    parent_turn_id = task_id
            self._offer_progress(task_id, parent_turn_id, "FAILED", code)
            raise

    async def _terminalize_best_effort(
        self, task_id: str, status: str, reason: str
    ) -> None:
        try:
            await self._io.run(
                self._repository.set_subagent_task_status,
                self._guard,
                task_id=task_id,
                status=status,
                reason=reason,
                occurred_at=datetime.now(timezone.utc),
                actor_id=self._host_owner_id,
                deadline_monotonic=self._canonical_deadline(),
            )
        except BaseException:
            # A writer takeover owns the canonical INTERRUPTED transition.
            pass

    async def _settle_cancelled_child(
        self,
        *,
        task_id: str,
        turn_id: str,
        task_status: str,
        task_reason: str,
        turn_reason: str,
    ) -> AcceptedEntry | None:
        occurred_at = datetime.now(timezone.utc)
        while True:
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_cancelled_subagent_turn_and_task,
                    session_id=self._guard.session_id,
                    task_id=task_id,
                    turn_id=turn_id,
                    task_status=task_status,
                    task_reason=task_reason,
                    turn_reason=turn_reason,
                    occurred_at=occurred_at,
                    actor_id=self._host_owner_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if confirmation.kind is TurnAdmissionConfirmationKind.FULL:
                    return None
                if (
                    confirmation.kind
                    is TurnAdmissionConfirmationKind.HISTORICAL_TERMINAL
                ):
                    accepted = confirmation.accepted
                    if accepted is None:
                        raise RuntimeError(
                            "historical child winner lacks its final entry"
                        )
                    child_result_id = _stable_child_id(task_id, accepted.entry_id)
                    await self._io.run(
                        self._repository.accept_subagent_child,
                        self._guard,
                        child_id=child_result_id,
                        task_id=task_id,
                        child_kind="RESULT",
                        child_ordinal=None,
                        entry_id=accepted.entry_id,
                        occurred_at=occurred_at,
                        actor_id=task_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    completed = await self._io.run(
                        self._repository.set_subagent_task_status,
                        self._guard,
                        task_id=task_id,
                        status="COMPLETED",
                        reason=None,
                        occurred_at=occurred_at,
                        actor_id=self._host_owner_id,
                        deadline_monotonic=self._canonical_deadline(),
                    )
                    if not completed:
                        durable = await self._io.run(
                            self._repository.query_subagent_task,
                            session_id=self._guard.session_id,
                            task_id=task_id,
                            deadline_monotonic=self._canonical_deadline(),
                        )
                        if durable is None or durable.get("status") != "COMPLETED":
                            raise ConversationKernelConflict(
                                "completed child winner conflicts with task state"
                            )
                    return accepted
                if confirmation.kind is TurnAdmissionConfirmationKind.CONFLICT:
                    raise ConversationKernelConflict(
                        "subagent cancellation winner conflicts"
                    )
            except StaleHostWriter:
                return None
            except ConversationKernelConflict:
                raise
            except Exception:
                # NONE and transient confirmation failure both proceed to the
                # same immutable candidate write.  Exact conflicts are never
                # retried or overwritten.
                pass
            try:
                changed = await self._io.run(
                    self._repository.settle_cancelled_subagent_turn_and_task,
                    self._guard,
                    task_id=task_id,
                    turn_id=turn_id,
                    task_status=task_status,
                    task_reason=task_reason,
                    turn_reason=turn_reason,
                    occurred_at=occurred_at,
                    actor_id=self._host_owner_id,
                    deadline_monotonic=self._canonical_deadline(),
                )
                if changed:
                    return None
                # A cancellation can win before the task-scoped turn admission.
                # The guarded task-only CAS is legal only while that exact turn
                # is still absent; if a terminal turn raced us, loop back to the
                # closed confirmation instead of overwriting its lineage.
                task_only_changed = await self._io.run(
                    self._repository.set_subagent_task_status,
                    self._guard,
                    task_id=task_id,
                    status=task_status,
                    reason=task_reason,
                    occurred_at=occurred_at,
                    actor_id=self._host_owner_id,
                    deadline_monotonic=self._canonical_deadline(),
                    require_absent_turn_id=turn_id,
                )
                if task_only_changed:
                    return None
            except StaleHostWriter:
                return None
            except ConversationKernelConflict:
                raise
            except Exception:
                await asyncio.sleep(0.05)

    async def _list(self, arguments: Mapping[str, object]) -> KernelToolResult:
        maximum = int(arguments.get("max_items", 50))
        maximum = max(1, min(maximum, 50))
        durable = await self._io.run(
            self._repository.list_subagent_tasks,
            session_id=self._guard.session_id,
            maximum_items=maximum,
            deadline_monotonic=self._canonical_deadline(),
        )
        rows = [
            {
                "subagent_run_id": str(item["id"]),
                "status": str(item["status"]),
                "objective": str(item["objective"]),
                "result_id": item.get("result_id"),
                "result_entry_id": item.get("result_entry_id"),
                "result_accepted": item.get("accepted_root_entry_id") is not None,
            }
            for item in durable
        ]
        return _result("SUCCESS", {"agents": rows})

    async def _wait(self, arguments: Mapping[str, object]) -> KernelToolResult:
        task_id = str(arguments.get("subagent_run_id") or "")
        timeout = float(arguments.get("timeout_seconds", 30.0))
        if timeout <= 0 or timeout > 300:
            return _result("APPLICATION_ERROR", {"error": "timeout is out of bounds"})
        async with self._lock:
            live = self._tasks.get(task_id)
        if live is None:
            durable = await self._io.run(
                self._repository.query_subagent_task,
                session_id=self._guard.session_id,
                task_id=task_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            if durable is None:
                return _result(
                    "APPLICATION_ERROR", {"error": "subagent is unknown"}
                )
            return _durable_wait_result(durable)
        try:
            await asyncio.wait_for(asyncio.shield(live.task), timeout=timeout)
        except TimeoutError:
            return _result("SUCCESS", {"status": "running", "subagent_run_id": task_id})
        except BaseException:
            pass
        if live.result is not None:
            return _result(
                "SUCCESS",
                {
                    "status": "completed",
                    "subagent_run_id": task_id,
                    "result": live.result.final_text,
                    "result_entry_id": live.result.final_entry_id,
                    "result_id": live.child_result_id,
                },
            )
        if live.status == "COMPLETED":
            # A cancellation can race after the canonical assistant winner but
            # before the runner returns its process-local result.  In that case
            # the manager finishes the durable child/result lineage from the
            # historical winner and reads the resulting product fact here.
            durable = await self._io.run(
                self._repository.query_subagent_task,
                session_id=self._guard.session_id,
                task_id=task_id,
                deadline_monotonic=self._canonical_deadline(),
            )
            if durable is not None:
                return _durable_wait_result(durable)
        return _result(
            "APPLICATION_ERROR",
            {
                "status": live.status.lower(),
                "subagent_run_id": task_id,
                "error_code": live.failure_code,
            },
        )

    async def _stop(self, arguments: Mapping[str, object]) -> KernelToolResult:
        task_id = str(arguments.get("subagent_run_id") or "")
        async with self._lock:
            live = self._tasks.get(task_id)
        if live is None:
            return _result("APPLICATION_ERROR", {"error": "subagent is unknown"})
        if not live.task.done():
            async with self._lock:
                current = self._tasks.get(task_id)
                if current is not None:
                    cause = current.cancellation_intent.install_cause(
                        ForegroundCancellationCause.USER_REQUEST
                    )
                    current.cancellation_reason = (
                        "USER_CANCELLED"
                        if cause is ForegroundCancellationCause.USER_REQUEST
                        else "HOST_CLOSING"
                    )
            live.task.cancel()
            await asyncio.gather(live.task, return_exceptions=True)
        return _result(
            "SUCCESS", {"status": live.status.lower(), "subagent_run_id": task_id}
        )

    async def aclose(self, *, timeout_seconds: float) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(
                item.task for item in self._tasks.values() if not item.task.done()
            )
            for item in self._tasks.values():
                if not item.task.done():
                    cause = item.cancellation_intent.install_cause(
                        ForegroundCancellationCause.HOST_SESSION_CLOSE
                    )
                    item.cancellation_reason = (
                        "USER_CANCELLED"
                        if cause is ForegroundCancellationCause.USER_REQUEST
                        else "HOST_CLOSING"
                    )
        for task in tasks:
            task.cancel()
        close_deadline_expired = False
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
            close_deadline_expired = bool(pending)
            if pending:
                # A child owns process-local tool/provider resources until its
                # task is terminal.  The watchdog selects the close outcome;
                # it never authorizes detaching those physical owners.
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    task.exception()
        # asyncio can cancel a newly-created Task before its coroutine body
        # executes, in which case _run_child() never observes CancelledError.
        # The Host owner still has to install the frozen close disposition for
        # every accepted ACTIVE task.
        async with self._lock:
            unterminalized = tuple(
                item for item in self._tasks.values() if item.status == "ACTIVE"
            )
        for item in unterminalized:
            await self._terminalize_best_effort(
                item.task_id, "INTERRUPTED", "HOST_CLOSING"
            )
            async with self._lock:
                current = self._tasks.get(item.task_id)
                if current is not None and current.status == "ACTIVE":
                    current.status = "INTERRUPTED"
                    current.failure_code = "HOST_CLOSING"
            self._offer_progress(
                item.task_id,
                item.parent_turn_id,
                "INTERRUPTED",
                "HOST_CLOSING",
            )
        if close_deadline_expired:
            raise TimeoutError("subagent owner exited after close deadline")

    def _offer_progress(
        self, task_id: str, parent_turn_id: str, status: str, summary: str
    ) -> None:
        public = summary[:4096]
        self._live_bus.offer_nowait(
            event_type=LiveEventType.SUBAGENT_PROGRESS,
            session_id=self._guard.session_id,
            turn_id=parent_turn_id,
            draft_identity=task_id,
            payload=SubagentProgressPayload(
                task_id,
                status,
                public,
                len(public.encode("utf-8")),
                live_digest(public),
            ),
            scope_kind="SUBAGENT_TASK",
            scope_subagent_task_id=task_id,
            channel_kind=LiveChannelKind.SUBAGENT_EXTENSION,
            generation_id=f"subagent:{task_id}",
            block_id=task_id,
            block_ordinal=0,
            block_kind=LiveBlockKind.OPERATIONAL,
        )


def _result(
    state: str,
    value: Mapping[str, object],
    *,
    remote_identity: str | None = None,
) -> KernelToolResult:
    return KernelToolResult(
        state=state,
        content=json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode(),
        remote_identity=remote_identity,
    )


def _stable_child_id(task_id: str, entry_id: str) -> str:
    from hashlib import sha256

    return "subagent-result:" + sha256(f"{task_id}:{entry_id}".encode()).hexdigest()


def _durable_wait_result(row: Mapping[str, object]) -> KernelToolResult:
    status = str(row["status"])
    if status == "COMPLETED" and row.get("result_id") is not None:
        return _result(
            "SUCCESS",
            {
                "status": "completed",
                "subagent_run_id": str(row["id"]),
                "result_id": str(row["result_id"]),
                "result_entry_id": str(row["result_entry_id"]),
                "result_accepted": row.get("accepted_root_entry_id") is not None,
            },
        )
    return _result(
        "SUCCESS" if status in {"PENDING", "ACTIVE"} else "APPLICATION_ERROR",
        {
            "status": status.lower(),
            "subagent_run_id": str(row["id"]),
            "error_code": row.get("terminal_reason"),
        },
    )


async def _join_child_settlement(
    task: asyncio.Task[AcceptedEntry | None] | asyncio.Task[None],
) -> AcceptedEntry | None:
    """Join one physical settlement despite repeated waiter cancellation."""

    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            # The child task already records the caller's cancellation cause.
            # Additional stop/close calls only detach their waiter and cannot
            # cancel or replace the exact settlement owner.
            continue


__all__ = ["KernelSubagentManager", "MAXIMUM_LIVE_SUBAGENTS", "SUBAGENT_TOOL_NAMES"]
