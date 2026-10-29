"""Process-local scheduling for independently owned Agent sessions.

Canonical session, queue and turn rows own identity, ordering and outcomes.
This owner only allocates the existing physical worker capacity and arranges
normal Host loading. It never runs a second conversation engine.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.host import KernelHostCore

# Existing physical execution bound, shared by the whole Core. Logical work is
# admitted to canonical queues regardless of the number of occupied slots.
DEFAULT_AGENT_EXECUTION_CAPACITY = 4
AGENT_UNLOAD_IDLE = timedelta(hours=1)
AGENT_DELETE_IDLE = timedelta(days=3)


class AgentSessionRuntime:
    """Shared live coordination; losing this owner never replays a turn."""

    def __init__(self, core: KernelHostCore) -> None:
        self.core = core
        self._io = KernelSessionIO()
        self._capacity = DEFAULT_AGENT_EXECUTION_CAPACITY
        self._occupied: set[str] = set()
        self._changed = asyncio.Condition()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._revision = 0
        self._completion_notifications: dict[str, dict[str, None]] = {}
        self._completion_delivery = asyncio.Lock()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="kernel-agent-session-scheduler")
            self._wake.set()

    async def completed_work(self, root_session_id: str, work_id: str) -> None:
        # Only references wait here. The receiving canonical queue owns actual
        # delivered text; a process crash may lose this advisory notification.
        self._completion_notifications.setdefault(root_session_id, {})[work_id] = None
        try:
            await self.deliver_pending_completions(root_session_id)
        except Exception:
            logging.getLogger(__name__).exception("Agent completion notification remains pending")
        await self.notify_changed()

    async def deliver_pending_completions(self, root_session_id: str) -> None:
        async with self._completion_delivery:
            pending = self._completion_notifications.get(root_session_id)
            if not pending:
                return
            for work_id in tuple(pending):
                try:
                    delivered = await self.core._deliver_agent_completion(root_session_id, work_id)
                except Exception:
                    logging.getLogger(__name__).exception("Agent completion notification remains pending")
                    continue
                if delivered:
                    pending.pop(work_id, None)
            if not pending:
                self._completion_notifications.pop(root_session_id, None)

    def forget_root(self, session_id: str) -> None:
        self._completion_notifications.pop(session_id, None)

    def shared_terminal_owner(self, root_session_id: str):
        root = self.core._live_agent_session(root_session_id)
        if root is None:
            raise RuntimeError("Agent physical resources require the owning ROOT Host")
        return root._tools

    def wake_monitors(self) -> None:
        for session in tuple(self.core._sessions.values()):
            session._monitor_wake.set()

    def read_deadline(self) -> float:
        return self.core._canonical_deadline()

    async def wake_agent_requests(self, root_session_id: str | None = None) -> None:
        self.start()
        self._wake.set()
        await self.notify_changed()

    async def notify_changed(self) -> None:
        async with self._changed:
            self._revision += 1
            self._changed.notify_all()
        self._wake.set()

    def agent_change_revision(self) -> int:
        return self._revision

    async def wait_for_agent_change(self, after_revision: int) -> None:
        async with self._changed:
            await self._changed.wait_for(lambda: self._revision != after_revision or self._closing)

    async def capacity_state(self) -> tuple[int, int]:
        async with self._changed:
            return self._capacity, len(self._occupied)

    async def set_capacity_target(self, target: int) -> bool:
        if isinstance(target, bool) or not isinstance(target, int) or target < 1:
            raise ValueError("Agent execution capacity must be a positive integer")
        async with self._changed:
            raised = target > self._capacity
            self._capacity = target
            self._changed.notify_all()
        self._wake.set()
        return raised

    async def try_acquire(self, session_id: str) -> bool:
        async with self._changed:
            if session_id in self._occupied:
                return True
            if self._closing or len(self._occupied) >= self._capacity:
                return False
            self._occupied.add(session_id)
            return True

    async def release(self, session_id: str) -> None:
        async with self._changed:
            self._occupied.discard(session_id)
            self._changed.notify_all()
        self._wake.set()

    @asynccontextmanager
    async def request_admission(self, root_session_id: str, requests):
        """Join the Core's existing admission/retirement gate for a batch."""
        ids = frozenset((root_session_id, *(request.agent_id for request in requests)))
        while True:
            async with self.core._lock:
                if self.core._closing:
                    raise RuntimeError("Kernel Host core is closing")
                retirements = tuple({
                    operation.settlement for session_id in ids
                    if (operation := self.core._session_retirements.get(session_id)) is not None
                })
                if not retirements:
                    settlement = asyncio.get_running_loop().create_future()
                    self.core._open_attempts[settlement] = ids
                    break
            # A retirement that won first finishes before this new logical
            # admission checks current canonical existence and ownership.
            await asyncio.gather(*(asyncio.shield(waiter) for waiter in retirements))
        try:
            yield
        finally:
            await self.core._settle_session_open(settlement)

    @asynccontextmanager
    async def suspend_execution(self, context):
        """An Agent waiting on other Agents yields its physical execution slot."""
        session_id = context.session_id
        async with self._changed:
            owned = session_id in self._occupied
        if owned:
            await self.release(session_id)
        try:
            yield
        finally:
            if owned and not asyncio.current_task().cancelling():
                async with self._changed:
                    await self._changed.wait_for(
                        lambda: self._closing or len(self._occupied) < self._capacity
                    )
                    if not self._closing:
                        self._occupied.add(session_id)

    async def send_agent_steer(self, *, agent_id: str, message: str, source_context):
        return await self.core._send_agent_message(agent_id, message, source_context)

    async def stop_active_work(self, *, agent_id: str, work_id: str | None = None):
        return await self.core._stop_agent_active_work(agent_id, work_id)

    async def pending_message_sources(self, *, recipient_session_id, source_agent_ids,
                                      after_by_sender, invocation_context):
        repository = await self.core._ensure_resources()
        live = self.core._live_agent_session(recipient_session_id)
        accepted_through = 0
        if live is not None:
            from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
            view = live._input_continuity.current_view(
                ProviderInputContinuityScope(recipient_session_id, live._session_scope_kind)
            )
            if view is not None:
                accepted_through = view.canonical_frontier.through_sequence
        return await self._io.run(
            repository.pending_agent_message_sources,
            session_id=recipient_session_id,
            turn_id=(None if getattr(getattr(invocation_context, "invocation_source", None), "kind", None) == "CODE_MODE_CALL" else invocation_context.turn_id),
            source_session_ids=tuple(source_agent_ids),
            after_by_sender=after_by_sender,
            accepted_through_sequence=accepted_through,
            deadline_monotonic=self.core._canonical_deadline(),
        )

    async def _run(self) -> None:
        while not self._closing:
            try:
                # The timeout discovers durable admissions made by another
                # process and evaluates the specified idle retirement clock.
                # It is an observation interval, never a work lifetime bound.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=30)
                except TimeoutError:
                    pass
                self._wake.clear()
                if not self._closing:
                    await self._advance()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Agent session scheduling failed")

    async def _advance(self) -> None:
        repository = await self.core._ensure_resources()
        for root_id in tuple(self._completion_notifications):
            await self.deliver_pending_completions(root_id)
        rows = await self._io.run(repository.list_all_agent_sessions,
                                       deadline_monotonic=self.core._canonical_deadline())
        for row in rows:
            if self._closing:
                return
            session_id = str(row["id"])
            await self._io.run(repository.resolve_agent_dependencies,
                                   session_id=session_id,
                                   deadline_monotonic=self.core._canonical_deadline())
            # Re-read after dependency settlement; no stale status authorizes
            # a turn or retirement. Both mutations recheck canonical facts.
            current = await self._io.run(repository.read_agent_runtime_state,
                                             session_id=session_id,
                                             deadline_monotonic=self.core._canonical_deadline())
            if current is None:
                continue
            live = self.core._live_agent_session(session_id)
            root_id = str(current["root_session_id"])
            if current["has_pending_work"] or current["has_running_turn"]:
                if root_id in self.core._suspended_agent_roots:
                    continue
                if live is not None:
                    live.wake_queued_inputs()
                elif (self.core._live_agent_session(root_id) is not None
                      and (current["head_ready"] or current["has_running_turn"])
                      and await self.try_acquire(session_id)):
                    try:
                        await self.core._load_agent_session(current)
                    except asyncio.CancelledError:
                        await self.release(session_id)
                        raise
                    except Exception:
                        await self.release(session_id)
                        logging.getLogger(__name__).exception("Agent session could not be loaded")
                continue
            idle_since = current.get("idle_since")
            if idle_since is None:
                await self._io.run(repository.mark_agent_idle,
                                       session_id=session_id, idle=True,
                                       at=datetime.now(timezone.utc),
                                       deadline_monotonic=self.core._canonical_deadline())
                continue
            idle_duration = datetime.now(timezone.utc) - idle_since
            if idle_duration >= AGENT_DELETE_IDLE:
                await self.core._retire_idle_agent(current, delete=True)
            elif idle_duration >= AGENT_UNLOAD_IDLE and live is not None:
                await self.core._retire_idle_agent(current, delete=False)

    async def aclose(self) -> None:
        self._closing = True
        self._wake.set()
        async with self._changed:
            self._changed.notify_all()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._io.aclose(deadline_monotonic=self.core._canonical_deadline())


__all__ = ["AgentSessionRuntime", "AGENT_UNLOAD_IDLE", "AGENT_DELETE_IDLE"]
