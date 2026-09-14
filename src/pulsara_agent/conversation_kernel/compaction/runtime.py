"""Host-owned process-local scheduling for context compaction.

The owner deliberately contains no canonical data and performs no I/O.  It
linearizes the single expensive summary lane, exact-scope admission fences,
manual waiters, and the bounded automatic-failure circuit.  Canonical source
cuts and adoption winners remain repository/reader facts.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar
from uuid import uuid4

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionAttemptPhase,
    CompactionDisposition,
    CompactionOutcome,
    CompactionScope,
    CompactionTrigger,
    ResolvedCompactionPolicy,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True, eq=False)
class CompactionWriteReservation:
    """Exact process-local owner of one canonical writer/compaction exclusion."""

    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None

    def __post_init__(self) -> None:
        if (self.scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("compaction write reservation scope union is invalid")


@dataclass(frozen=True, slots=True)
class ManualCompactionRequest:
    request_id: str
    command_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    expected_turn_id: str | None
    force: bool

    def __post_init__(self) -> None:
        if not self.request_id or not self.command_id:
            raise ValueError("manual compaction identity is incomplete")
        if (self.scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("manual compaction scope union is invalid")


@dataclass(slots=True)
class _ManualWaiter:
    request: ManualCompactionRequest
    future: asyncio.Future[CompactionOutcome]
    started: bool = False


class HostCompactionRuntimeOwner:
    """One process-local compaction owner for one Host session."""

    def __init__(self, *, policy: ResolvedCompactionPolicy | None = None) -> None:
        self.policy = policy or ResolvedCompactionPolicy()
        self._summary_lane = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._fenced_scopes: dict[
            tuple[ModelInputScopeKind, str | None],
            tuple[str, CompactionTrigger, CompactionAttemptPhase],
        ] = {}
        self._manual: dict[
            tuple[ModelInputScopeKind, str | None], _ManualWaiter
        ] = {}
        self._manual_outcomes: OrderedDict[
            str, tuple[ManualCompactionRequest, CompactionOutcome]
        ] = OrderedDict()
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._owned_tasks: set[asyncio.Task[object]] = set()
        self._settlement_tasks: set[asyncio.Task[object]] = set()
        self._auto_failures: dict[
            tuple[ModelInputScopeKind, str | None], int
        ] = {}
        self._closing = False
        self._host_install_fence: Callable[
            [
                CompactionScope,
                CompactionTrigger,
                str,
                asyncio.Task[object],
                CompactionWriteReservation | None,
                str | None,
            ],
            Awaitable[None],
        ] | None = None
        self._host_remove_fence: Callable[
            [CompactionScope, str, asyncio.Task[object]], Awaitable[None]
        ] | None = None

    def bind_host_fence_callbacks(
        self,
        *,
        install: Callable[
            [
                CompactionScope,
                CompactionTrigger,
                str,
                asyncio.Task[object],
                CompactionWriteReservation | None,
                str | None,
            ],
            Awaitable[None],
        ],
        remove: Callable[
            [CompactionScope, str, asyncio.Task[object]], Awaitable[None]
        ],
    ) -> None:
        if self._host_install_fence is not None or self._host_remove_fence is not None:
            raise RuntimeError("compaction Host fence callbacks are already bound")
        self._host_install_fence = install
        self._host_remove_fence = remove

    def start_owned(
        self, operation: Awaitable[_T], *, name: str
    ) -> asyncio.Task[_T]:
        """Install one Host-owned settlement task before its first await."""

        if self._closing:
            if hasattr(operation, "close"):
                operation.close()  # type: ignore[union-attr]
            raise RuntimeError("compaction owner is closing")
        task = asyncio.create_task(operation, name=name)
        self._owned_tasks.add(task)
        task.add_done_callback(self._owned_tasks.discard)
        return task

    def start_settlement(
        self, operation: Awaitable[_T], *, name: str
    ) -> asyncio.Task[_T]:
        """Install an admitted canonical/installation settlement to drain.

        Unlike a pre-adoption summary operation, an exact admitted settlement
        is never cancelled by Host close.  Close only detaches its waiters and
        joins this same task to a terminal FULL/NONE/CONFLICT or stale-writer
        outcome.
        """

        if self._closing:
            if hasattr(operation, "close"):
                operation.close()  # type: ignore[union-attr]
            raise RuntimeError("compaction owner is closing")
        task = asyncio.create_task(operation, name=name)
        self._settlement_tasks.add(task)
        task.add_done_callback(self._settlement_tasks.discard)
        return task

    def install_fence_under_host_lock(
        self,
        *,
        scope: CompactionScope,
        trigger: CompactionTrigger,
        attempt_id: str,
        owner_task: asyncio.Task[object],
    ) -> None:
        """Install only while the composing Host's canonical lock is held."""

        key = self.scope_key(scope.scope_kind, scope.scope_subagent_task_id)
        if self._closing:
            raise RuntimeError("compaction owner is closing")
        if key in self._fenced_scopes:
            raise RuntimeError("compaction scope is already fenced")
        self._fenced_scopes[key] = (
            attempt_id,
            trigger,
            CompactionAttemptPhase.PREPARING,
        )
        self._active_tasks.add(owner_task)

    def remove_fence_under_host_lock(
        self,
        *,
        scope: CompactionScope,
        attempt_id: str,
        owner_task: asyncio.Task[object],
    ) -> None:
        key = self.scope_key(scope.scope_kind, scope.scope_subagent_task_id)
        current = self._fenced_scopes.get(key)
        if current is not None and current[0] == attempt_id:
            self._fenced_scopes.pop(key, None)
        self._active_tasks.discard(owner_task)

    @staticmethod
    def scope_key(
        scope_kind: ModelInputScopeKind, scope_subagent_task_id: str | None
    ) -> tuple[ModelInputScopeKind, str | None]:
        if (scope_kind is ModelInputScopeKind.ROOT) != (
            scope_subagent_task_id is None
        ):
            raise ValueError("compaction runtime scope union is invalid")
        return scope_kind, scope_subagent_task_id

    async def request_manual(
        self,
        *,
        command_id: str,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        expected_turn_id: str | None,
        force: bool,
    ) -> tuple[ManualCompactionRequest, asyncio.Future[CompactionOutcome]]:
        key = self.scope_key(scope_kind, scope_subagent_task_id)
        request = ManualCompactionRequest(
            request_id=f"compaction-request:{uuid4().hex}",
            command_id=command_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            expected_turn_id=expected_turn_id,
            force=force,
        )
        async with self._state_lock:
            if self._closing:
                raise RuntimeError("compaction owner is closing")
            completed = self._manual_outcomes.get(command_id)
            if completed is not None:
                previous, outcome = completed
                if (
                    previous.scope_kind is not scope_kind
                    or previous.scope_subagent_task_id != scope_subagent_task_id
                    or previous.expected_turn_id != expected_turn_id
                    or previous.force != force
                ):
                    raise RuntimeError("manual compaction command identity conflict")
                future = asyncio.get_running_loop().create_future()
                future.set_result(outcome)
                return previous, future
            existing = self._manual.get(key)
            if existing is not None:
                if (
                    existing.request.command_id != command_id
                    or existing.request.expected_turn_id != expected_turn_id
                    or existing.request.force != force
                ):
                    raise RuntimeError("another manual compaction is pending")
                return existing.request, existing.future
            future = asyncio.get_running_loop().create_future()
            self._manual[key] = _ManualWaiter(request, future)
            return request, future

    async def find_manual(
        self,
        *,
        command_id: str,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> asyncio.Future[CompactionOutcome] | None:
        """Return only an existing process-local attempt/outcome; never create."""

        key = self.scope_key(scope_kind, scope_subagent_task_id)
        async with self._state_lock:
            waiter = self._manual.get(key)
            if waiter is not None and waiter.request.command_id == command_id:
                return waiter.future
            completed = self._manual_outcomes.get(command_id)
            if completed is None:
                return None
            _request, outcome = completed
            future = asyncio.get_running_loop().create_future()
            future.set_result(outcome)
            return future

    async def claim_manual_execution(
        self, request: ManualCompactionRequest
    ) -> bool:
        key = self.scope_key(request.scope_kind, request.scope_subagent_task_id)
        async with self._state_lock:
            waiter = self._manual.get(key)
            if (
                waiter is None
                or waiter.request.request_id != request.request_id
                or waiter.started
            ):
                return False
            waiter.started = True
            return True

    async def take_manual(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        turn_id: str,
    ) -> ManualCompactionRequest | None:
        key = self.scope_key(scope_kind, scope_subagent_task_id)
        async with self._state_lock:
            waiter = self._manual.get(key)
            if waiter is None:
                return None
            expected = waiter.request.expected_turn_id
            if expected is not None and expected != turn_id:
                self._manual.pop(key, None)
                outcome = CompactionOutcome(
                    CompactionDisposition.FAILED,
                    turn_id,
                    None,
                    None,
                    "TARGET_TURN_CHANGED",
                )
                self._manual_outcomes[waiter.request.command_id] = (
                    waiter.request,
                    outcome,
                )
                self._manual_outcomes.move_to_end(waiter.request.command_id)
                while len(self._manual_outcomes) > 256:
                    self._manual_outcomes.popitem(last=False)
                if not waiter.future.done():
                    waiter.future.set_result(outcome)
                return None
            if waiter.started:
                return None
            waiter.started = True
            return waiter.request

    async def settle_manual(
        self, request: ManualCompactionRequest, outcome: CompactionOutcome
    ) -> None:
        key = self.scope_key(
            request.scope_kind, request.scope_subagent_task_id
        )
        async with self._state_lock:
            waiter = self._manual.get(key)
            if waiter is None or waiter.request.request_id != request.request_id:
                return
            self._manual.pop(key, None)
            self._manual_outcomes[request.command_id] = (request, outcome)
            self._manual_outcomes.move_to_end(request.command_id)
            while len(self._manual_outcomes) > 256:
                self._manual_outcomes.popitem(last=False)
            if not waiter.future.done():
                waiter.future.set_result(outcome)

    async def run_fenced(
        self,
        *,
        scope: CompactionScope,
        trigger: CompactionTrigger,
        operation: Callable[[], Awaitable[_T]],
        admitted_writer: CompactionWriteReservation | None = None,
        pending_root_turn_id: str | None = None,
    ) -> _T:
        """Run one exact scope after acquiring the Host-wide summary lane."""

        async with self._summary_lane:
            attempt_id = f"compaction-attempt:{uuid4().hex}"
            current = asyncio.current_task()
            if current is None:
                raise RuntimeError("compaction attempt lacks an asyncio owner")
            install = self._host_install_fence
            remove = self._host_remove_fence
            if (install is None) != (remove is None):
                raise RuntimeError("compaction Host fence callbacks are incomplete")
            if install is None:
                async with self._state_lock:
                    self.install_fence_under_host_lock(
                        scope=scope,
                        trigger=trigger,
                        attempt_id=attempt_id,
                        owner_task=current,
                    )
            else:
                await install(
                    scope,
                    trigger,
                    attempt_id,
                    current,
                    admitted_writer,
                    pending_root_turn_id,
                )
            try:
                return await operation()
            finally:
                if remove is None:
                    async with self._state_lock:
                        self.remove_fence_under_host_lock(
                            scope=scope,
                            attempt_id=attempt_id,
                            owner_task=current,
                        )
                else:
                    await remove(scope, attempt_id, current)

    def is_fenced(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> bool:
        key = self.scope_key(scope_kind, scope_subagent_task_id)
        return key in self._fenced_scopes

    def advance_phase(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        phase: CompactionAttemptPhase,
    ) -> None:
        """Advance the bounded public phase for the exact fenced scope."""

        key = self.scope_key(scope_kind, scope_subagent_task_id)
        current = self._fenced_scopes.get(key)
        if current is None:
            raise RuntimeError("compaction phase scope is not fenced")
        attempt_id, trigger, _old_phase = current
        self._fenced_scopes[key] = (attempt_id, trigger, phase)

    def current_projection(
        self,
    ) -> tuple[bool, str | None, str | None, str | None]:
        if not self._fenced_scopes:
            return False, None, None, None
        (scope_kind, task_id), (_attempt, trigger, phase) = next(
            iter(self._fenced_scopes.items())
        )
        scope = scope_kind.value if task_id is None else f"{scope_kind.value}:{task_id}"
        return True, trigger.value, phase.value, scope

    def automatic_allowed(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> bool:
        key = self.scope_key(scope_kind, scope_subagent_task_id)
        return self._auto_failures.get(key, 0) < self.policy.maximum_consecutive_auto_failures

    def record_automatic_failure(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> None:
        key = self.scope_key(scope_kind, scope_subagent_task_id)
        self._auto_failures[key] = min(
            self.policy.maximum_consecutive_auto_failures,
            self._auto_failures.get(key, 0) + 1,
        )

    def reset_automatic_failures(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> None:
        key = self.scope_key(scope_kind, scope_subagent_task_id)
        self._auto_failures.pop(key, None)

    async def aclose(self) -> None:
        async with self._state_lock:
            self._closing = True
            cancellable = tuple(self._active_tasks | self._owned_tasks)
            settlements = tuple(self._settlement_tasks)
            waiters = tuple(self._manual.values())
            self._manual.clear()
            for waiter in waiters:
                if not waiter.future.done():
                    waiter.future.set_result(
                        CompactionOutcome(
                            CompactionDisposition.FAILED,
                            waiter.request.expected_turn_id or "unknown",
                            None,
                            None,
                            "HOST_CLOSING",
                        )
                    )
        if cancellable or settlements:
            current = asyncio.current_task()
            for item in cancellable:
                if item is not current and not item.done():
                    item.cancel()
            await asyncio.gather(
                *(
                    asyncio.shield(item)
                    for item in (*cancellable, *settlements)
                    if item is not current
                ),
                return_exceptions=True,
            )


__all__ = [
    "HostCompactionRuntimeOwner",
    "ManualCompactionRequest",
]
