"""Long-lived conversation session wrapper around AgentRuntime."""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from pulsara_agent.event import AgentEvent
from pulsara_agent.host.identity import ResolvedWorkspace
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.runtime.approval import (
    ApprovalResolution,
    PendingApproval,
    pending_approval_from_state,
)
from pulsara_agent.runtime.agent import AgentRunResult
from pulsara_agent.runtime.state import LoopState, LoopStatus
from pulsara_agent.runtime.wiring import AgentRuntimeWiring


class HostSessionBusyError(RuntimeError):
    """Raised when a HostSession already has an active run."""


class HostSessionPendingApprovalError(RuntimeError):
    """Raised when a HostSession is suspended on a pending approval."""


@dataclass(slots=True)
class HostSession:
    host_session_id: str
    conversation_id: str
    workspace: ResolvedWorkspace
    wiring: AgentRuntimeWiring
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    closed: bool = False
    active_run_id: str | None = None
    stopping_run_id: str | None = None
    suspended_run_id: str | None = None
    pending_approval: PendingApproval | None = None
    _suspended_state: LoopState | None = None
    _active_state: LoopState | None = None
    _active_task: asyncio.Task[AgentRunResult] | None = None
    _run_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def runtime_session_id(self) -> str:
        return self.wiring.runtime_wiring.runtime_session.runtime_session_id

    @property
    def has_live_processes(self) -> bool:
        return self.wiring.runtime_wiring.runtime_session.terminal_sessions.has_live_processes(
            owner_host_session_id=self.host_session_id
        )

    async def run_turn(
        self,
        user_input: str,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AgentRunResult:
        if self.closed:
            raise RuntimeError("host session is closed")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        if self.pending_approval is not None:
            raise HostSessionPendingApprovalError(
                "host session has a pending approval; resolve or deny it before starting a new turn"
            )
        if self._run_lock.locked():
            raise HostSessionBusyError("host session already has an active run")
        async with self._run_lock:
            prior_messages = self._prior_messages()
            state = self.wiring.agent_runtime.new_state()
            self.active_run_id = state.run_id
            self._active_state = state
            self.last_active_at = time.monotonic()
            task = asyncio.create_task(
                self.wiring.agent_runtime.run_task(
                    user_input,
                    prior_messages=prior_messages,
                    state=state,
                    active_skill_names=active_skill_names,
                )
            )
            self._active_task = task
            try:
                try:
                    result = await task
                except asyncio.CancelledError:
                    if not state.scratchpad.get("stop_requested"):
                        raise
                    result = await self.wiring.agent_runtime.abort_run(state)
                self._capture_pending_approval(result.state)
                return result
            finally:
                self._active_task = None
                self._active_state = None
                self.active_run_id = None
                self.stopping_run_id = None
                self.last_active_at = time.monotonic()

    async def stream_turn(
        self,
        user_input: str,
        *,
        active_skill_names: frozenset[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if self.closed:
            raise RuntimeError("host session is closed")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        if self.pending_approval is not None:
            raise HostSessionPendingApprovalError(
                "host session has a pending approval; resolve or deny it before starting a new turn"
            )
        if self._run_lock.locked():
            raise HostSessionBusyError("host session already has an active run")
        async with self._run_lock:
            prior_messages = self._prior_messages()
            state = self.wiring.agent_runtime.new_state()
            self.active_run_id = state.run_id
            self.last_active_at = time.monotonic()
            try:
                async for event in self.wiring.agent_runtime.stream_task(
                    user_input,
                    prior_messages=prior_messages,
                    state=state,
                    active_skill_names=active_skill_names,
                ):
                    yield event
            finally:
                self._capture_pending_approval(state)
                self.active_run_id = None
                self.last_active_at = time.monotonic()

    def get_pending_approval(self) -> PendingApproval | None:
        return self.pending_approval

    async def resolve_approval(self, resolution: ApprovalResolution) -> AgentRunResult:
        if self.closed:
            raise RuntimeError("host session is closed")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_approval(resolution.approval_id)
        if self._run_lock.locked():
            raise HostSessionBusyError("host session already has an active run")
        async with self._run_lock:
            state = self._require_suspended_state(pending)
            self.active_run_id = state.run_id
            self.last_active_at = time.monotonic()
            try:
                result = await self.wiring.agent_runtime.resume_after_approval(state, resolution)
                self._capture_pending_approval(result.state)
                return result
            finally:
                self.active_run_id = None
                self.last_active_at = time.monotonic()

    async def stream_approval_resolution(
        self,
        resolution: ApprovalResolution,
    ) -> AsyncIterator[AgentEvent]:
        if self.closed:
            raise RuntimeError("host session is closed")
        if self.stopping_run_id is not None:
            raise HostSessionBusyError("host session is stopping an active run")
        pending = self._require_pending_approval(resolution.approval_id)
        if self._run_lock.locked():
            raise HostSessionBusyError("host session already has an active run")
        async with self._run_lock:
            state = self._require_suspended_state(pending)
            self.active_run_id = state.run_id
            self.last_active_at = time.monotonic()
            try:
                async for event in self.wiring.agent_runtime.stream_after_approval(state, resolution):
                    yield event
            finally:
                self._capture_pending_approval(state)
                self.active_run_id = None
                self.last_active_at = time.monotonic()

    async def stop_current_turn(
        self,
        *,
        reason: str = "user_stop",
        timeout: float = 2.0,
    ) -> AgentRunResult | None:
        if self.closed:
            raise RuntimeError("host session is closed")
        async with self._stop_lock:
            if self.pending_approval is not None:
                if self._run_lock.locked():
                    raise HostSessionBusyError("host session already has an active run")
                async with self._run_lock:
                    pending = self.pending_approval
                    if pending is None:
                        return None
                    state = self._require_suspended_state(pending)
                    self.active_run_id = state.run_id
                    self.stopping_run_id = state.run_id
                    self.last_active_at = time.monotonic()
                    try:
                        result = await self.wiring.agent_runtime.abort_run(state, reason=reason)
                        self._capture_pending_approval(result.state)
                        return result
                    finally:
                        self.active_run_id = None
                        self.stopping_run_id = None
                        self.last_active_at = time.monotonic()

            task = self._active_task
            state = self._active_state
            if task is None or state is None:
                return None
            if task.done():
                return None
            self.stopping_run_id = state.run_id
            state.scratchpad["stop_requested"] = True
            task.cancel()
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.CancelledError:
                return await self.wiring.agent_runtime.abort_run(state, reason=reason)
            except TimeoutError:
                return None

    def replay_events(self, *, after_sequence: int | None = None) -> list[AgentEvent]:
        return self.wiring.runtime_wiring.event_log.iter(after_sequence=after_sequence)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.pending_approval = None
        self._suspended_state = None
        self._active_state = None
        self._active_task = None
        self.stopping_run_id = None
        self.suspended_run_id = None
        self.wiring.agent_runtime.close()
        self._cleanup_workspace_root()

    def summary(self) -> dict[str, object]:
        return {
            "host_session_id": self.host_session_id,
            "conversation_id": self.conversation_id,
            "runtime_session_id": self.runtime_session_id,
            "workspace_kind": self.workspace.workspace_kind,
            "workspace_root": str(self.workspace.workspace_root),
            "display_label": self.workspace.display_label,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "closed": self.closed,
            "active_run_id": self.active_run_id,
            "stopping_run_id": self.stopping_run_id,
            "is_stopping": self.stopping_run_id is not None,
            "suspended_run_id": self.suspended_run_id,
            "pending_approval": self.pending_approval.to_dict() if self.pending_approval is not None else None,
            "has_live_processes": self.has_live_processes,
        }

    def _prior_messages(self):
        return rebuild_prior_messages(self.wiring.runtime_wiring.event_log)

    def _capture_pending_approval(self, state: LoopState) -> None:
        if state.status is LoopStatus.WAITING_USER:
            self.pending_approval = pending_approval_from_state(state, self.host_session_id)
            self._suspended_state = state
            self.suspended_run_id = state.run_id
            return
        self.pending_approval = None
        self._suspended_state = None
        self.suspended_run_id = None

    def _require_pending_approval(self, approval_id: str) -> PendingApproval:
        if self.pending_approval is None:
            raise ValueError("host session has no pending approval")
        if self.pending_approval.approval_id != approval_id:
            raise ValueError("approval id does not match the pending approval")
        return self.pending_approval

    def _require_suspended_state(self, pending: PendingApproval) -> LoopState:
        if self._suspended_state is None:
            raise ValueError("host session has no suspended approval state")
        if self._suspended_state.run_id != pending.run_id:
            raise ValueError("suspended state does not match pending approval")
        return self._suspended_state

    def _cleanup_workspace_root(self) -> None:
        if not self.workspace.cleanup_workspace_root_on_close:
            return
        shutil.rmtree(self.workspace.workspace_root, ignore_errors=True)
