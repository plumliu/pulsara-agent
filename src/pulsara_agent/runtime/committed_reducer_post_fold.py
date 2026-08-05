"""No-fail process-owner handoff after a committed semantic fold.

The semantic reducer and its registration high-water are already authoritative
before this service is called.  This owner exists only for idempotent,
process-local control adoption (for example reservation owners and monitor
workers).  A failed callback is retried from the exact committed event carrier;
it never replays the semantic reducer and never changes event durability.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import Callable

from pulsara_agent.blocking_executor import projection_maintenance_executor
from pulsara_agent.event import AgentEvent
from pulsara_agent.primitives.context import context_fingerprint


POST_FOLD_MAX_PENDING_EVENTS = 4_096
POST_FOLD_RETRY_DEADLINE_SECONDS = 30.0


class CommittedReducerPostFoldState(StrEnum):
    CLEAN = "clean"
    RETRY_WAIT = "retry_wait"
    APPLYING = "applying"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CLOSED = "closed"


@dataclass(slots=True)
class _PostFoldOwner:
    reducer_id: str
    callback: Callable[[tuple[AgentEvent, ...]], None]
    pending_by_id: dict[str, AgentEvent] = field(default_factory=dict)
    state: CommittedReducerPostFoldState = CommittedReducerPostFoldState.CLEAN
    task: asyncio.Task[None] | None = None
    physical_generation: int = 0
    retry_not_before: float = 0.0
    last_error_code: str | None = None
    attempt_fingerprint: str | None = None


class CommittedReducerPostFoldService:
    """Own bounded, idempotent post-fold control handoffs until close."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._owners: dict[str, _PostFoldOwner] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting = True

    def register(
        self,
        *,
        reducer_id: str,
        callback: Callable[[tuple[AgentEvent, ...]], None],
    ) -> None:
        with self._lock:
            if reducer_id in self._owners:
                raise ValueError("committed reducer post-fold owner already exists")
            self._owners[reducer_id] = _PostFoldOwner(
                reducer_id=reducer_id,
                callback=callback,
            )

    def bind_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                if not self._loop.is_closed():
                    raise RuntimeError(
                        "committed reducer post-fold loop identity changed"
                    )
                for owner in self._owners.values():
                    if owner.task is not None and not owner.task.done():
                        raise RuntimeError(
                            "closed post-fold loop still owns a live task"
                        )
                    if owner.task is not None:
                        owner.task = None
                        if owner.pending_by_id:
                            owner.state = CommittedReducerPostFoldState.RETRY_WAIT
            self._loop = loop
            reducer_ids = tuple(
                owner.reducer_id
                for owner in self._owners.values()
                if owner.pending_by_id and owner.task is None
            )
        for reducer_id in reducer_ids:
            loop.call_soon(self._start, reducer_id)

    def handoff(
        self,
        *,
        reducer_id: str,
        events: tuple[AgentEvent, ...],
    ) -> None:
        """Try the fast path, otherwise transfer exact events to this owner."""

        if not events:
            return
        with self._lock:
            if not self._accepting:
                raise RuntimeError("committed reducer post-fold service is closing")
            owner = self._owners[reducer_id]
            has_pending = bool(owner.pending_by_id)
        if not has_pending:
            try:
                owner.callback(events)
            except BaseException as exc:
                self._install_failure(owner, events, exc)
            return
        self._merge_pending(owner, events)
        self._schedule(owner.reducer_id)

    def _install_failure(
        self,
        owner: _PostFoldOwner,
        events: tuple[AgentEvent, ...],
        error: BaseException,
    ) -> None:
        self._merge_pending(owner, events)
        with self._lock:
            owner.last_error_code = type(error).__name__.upper()
            owner.state = CommittedReducerPostFoldState.RETRY_WAIT
        self._schedule(owner.reducer_id)

    def _merge_pending(
        self,
        owner: _PostFoldOwner,
        events: tuple[AgentEvent, ...],
    ) -> None:
        with self._lock:
            for event in events:
                existing = owner.pending_by_id.get(event.id)
                if existing is not None and existing != event:
                    owner.state = (
                        CommittedReducerPostFoldState.RECONCILIATION_REQUIRED
                    )
                    raise RuntimeError("post-fold event identity conflicts")
                owner.pending_by_id[event.id] = event
            if len(owner.pending_by_id) > POST_FOLD_MAX_PENDING_EVENTS:
                owner.state = CommittedReducerPostFoldState.RECONCILIATION_REQUIRED
                raise RuntimeError("post-fold pending event bound exceeded")
            ordered = tuple(
                sorted(
                    owner.pending_by_id.values(),
                    key=lambda item: (int(item.sequence or 0), item.id),
                )
            )
            owner.attempt_fingerprint = context_fingerprint(
                "committed-reducer-post-fold-attempt:v1",
                {
                    "reducer_id": owner.reducer_id,
                    "event_identities": tuple(
                        (item.id, int(item.sequence or 0), str(item.type))
                        for item in ordered
                    ),
                },
            )

    def _schedule(self, reducer_id: str) -> None:
        with self._lock:
            loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._start, reducer_id)

    def _start(self, reducer_id: str) -> None:
        with self._lock:
            owner = self._owners[reducer_id]
            if owner.task is not None or not owner.pending_by_id:
                return
            owner.task = asyncio.create_task(
                self._run(owner),
                name=f"committed-reducer-post-fold:{reducer_id}",
            )
            owner.task.add_done_callback(_consume_task_exception)

    async def _run(self, owner: _PostFoldOwner) -> None:
        deadline = monotonic() + POST_FOLD_RETRY_DEADLINE_SECONDS
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if not owner.pending_by_id:
                    owner.state = CommittedReducerPostFoldState.CLEAN
                    owner.task = None
                    return
                snapshot = tuple(
                    sorted(
                        owner.pending_by_id.values(),
                        key=lambda item: (int(item.sequence or 0), item.id),
                    )
                )
                owner.physical_generation += 1
                generation = owner.physical_generation
                owner.state = CommittedReducerPostFoldState.APPLYING
            try:
                await loop.run_in_executor(
                    projection_maintenance_executor(),
                    lambda: owner.callback(snapshot),
                )
            except BaseException as exc:
                with self._lock:
                    owner.last_error_code = type(exc).__name__.upper()
                if monotonic() >= deadline:
                    with self._lock:
                        owner.state = (
                            CommittedReducerPostFoldState.RECONCILIATION_REQUIRED
                        )
                        owner.task = None
                    raise
                delay = min(0.5, 0.025 * (2 ** min(generation - 1, 4)))
                with self._lock:
                    owner.state = CommittedReducerPostFoldState.RETRY_WAIT
                    owner.retry_not_before = monotonic() + delay
                await asyncio.sleep(delay)
                continue
            with self._lock:
                for event in snapshot:
                    owner.pending_by_id.pop(event.id, None)
                owner.last_error_code = None
                owner.retry_not_before = 0.0

    def diagnostics(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(
                {
                    "reducer_id": owner.reducer_id,
                    "state": owner.state.value,
                    "pending_event_count": len(owner.pending_by_id),
                    "attempt_fingerprint": owner.attempt_fingerprint,
                    "physical_generation": owner.physical_generation,
                    "retry_not_before": owner.retry_not_before,
                    "last_error_code": owner.last_error_code,
                }
                for owner in self._owners.values()
            )

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        """Drain currently admitted handoffs while keeping admission open."""

        self.bind_running_loop()
        with self._lock:
            reducer_ids = tuple(
                owner.reducer_id
                for owner in self._owners.values()
                if owner.pending_by_id and owner.task is None
            )
        for reducer_id in reducer_ids:
            self._start(reducer_id)
        while True:
            with self._lock:
                tasks = tuple(
                    owner.task
                    for owner in self._owners.values()
                    if owner.task is not None and not owner.task.done()
                )
                blocked = tuple(
                    owner
                    for owner in self._owners.values()
                    if owner.state
                    is CommittedReducerPostFoldState.RECONCILIATION_REQUIRED
                )
            if blocked:
                raise RuntimeError("committed reducer post-fold close blocked")
            if not tasks:
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("committed reducer post-fold close blocked")
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(*tasks)),
                timeout=remaining,
            )

    async def stop_admission_and_drain(self, *, deadline_monotonic: float) -> None:
        with self._lock:
            self._accepting = False
        await self.drain_pending(deadline_monotonic=deadline_monotonic)
        with self._lock:
            for owner in self._owners.values():
                owner.state = CommittedReducerPostFoldState.CLOSED

    def close_if_idle(self) -> None:
        with self._lock:
            if any(
                owner.pending_by_id
                or (owner.task is not None and not owner.task.done())
                for owner in self._owners.values()
            ):
                raise RuntimeError("committed reducer post-fold service is not idle")
            self._accepting = False
            for owner in self._owners.values():
                owner.state = CommittedReducerPostFoldState.CLOSED


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


__all__ = [
    "CommittedReducerPostFoldService",
    "CommittedReducerPostFoldState",
]
