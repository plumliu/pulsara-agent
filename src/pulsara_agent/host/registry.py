"""In-memory HostSession registry and lifecycle policy."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from pulsara_agent.host.session import HostSession


@dataclass(frozen=True, slots=True)
class HostSessionSummary:
    host_session_id: str
    conversation_id: str
    runtime_session_id: str
    workspace_kind: str
    workspace_root: str
    display_label: str
    created_at: float
    last_active_at: float
    closed: bool
    active_run_id: str | None
    has_live_processes: bool
    idle_with_live_processes: bool = False


class HostSessionRegistry:
    def __init__(
        self,
        *,
        idle_ttl_seconds: float | None = 6 * 3600,
        live_process_idle_ttl_seconds: float | None = None,
        max_sessions: int = 64,
    ) -> None:
        self.idle_ttl_seconds = idle_ttl_seconds
        self.live_process_idle_ttl_seconds = live_process_idle_ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, HostSession] = {}
        self._by_conversation: dict[str, str] = {}
        self._idle_with_live_processes: set[str] = set()
        self._lock = asyncio.Lock()

    async def add(self, session: HostSession) -> HostSession:
        async with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError(f"host session limit reached: max {self.max_sessions}")
            self._sessions[session.host_session_id] = session
            self._by_conversation[session.conversation_id] = session.host_session_id
            return session

    async def get(self, host_session_id: str) -> HostSession:
        async with self._lock:
            try:
                return self._sessions[host_session_id]
            except KeyError as exc:
                raise KeyError(f"host session not found: {host_session_id}") from exc

    async def find_by_conversation(self, conversation_id: str) -> HostSession | None:
        async with self._lock:
            session_id = self._by_conversation.get(conversation_id)
            return self._sessions.get(session_id) if session_id is not None else None

    async def close_session(self, host_session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(host_session_id, None)
            if session is not None:
                self._by_conversation.pop(session.conversation_id, None)
                self._idle_with_live_processes.discard(host_session_id)
        if session is not None:
            session.close()

    async def list_sessions(self) -> list[HostSessionSummary]:
        async with self._lock:
            sessions = list(self._sessions.values())
            idle = set(self._idle_with_live_processes)
        return [self._summary(session, idle_with_live_processes=session.host_session_id in idle) for session in sessions]

    async def sweep_idle(
        self,
        *,
        now: float | None = None,
        close_session: Callable[[str], None] | None = None,
    ) -> list[str]:
        if self.idle_ttl_seconds is None:
            return []
        now = time.monotonic() if now is None else now
        to_close: list[str] = []
        async with self._lock:
            for session_id, session in self._sessions.items():
                if session.active_run_id is not None:
                    continue
                idle_for = now - session.last_active_at
                if idle_for < self.idle_ttl_seconds:
                    continue
                if session.has_live_processes and self.live_process_idle_ttl_seconds is None:
                    self._idle_with_live_processes.add(session_id)
                    continue
                if (
                    session.has_live_processes
                    and self.live_process_idle_ttl_seconds is not None
                    and idle_for < self.live_process_idle_ttl_seconds
                ):
                    self._idle_with_live_processes.add(session_id)
                    continue
                to_close.append(session_id)
        for session_id in to_close:
            if close_session is not None:
                close_session(session_id)
            else:
                await self.close_session(session_id)
        return to_close

    def _summary(self, session: HostSession, *, idle_with_live_processes: bool) -> HostSessionSummary:
        data = session.summary()
        return HostSessionSummary(
            host_session_id=str(data["host_session_id"]),
            conversation_id=str(data["conversation_id"]),
            runtime_session_id=str(data["runtime_session_id"]),
            workspace_kind=str(data["workspace_kind"]),
            workspace_root=str(data["workspace_root"]),
            display_label=str(data["display_label"]),
            created_at=float(data["created_at"]),
            last_active_at=float(data["last_active_at"]),
            closed=bool(data["closed"]),
            active_run_id=data["active_run_id"] if isinstance(data["active_run_id"], str) else None,
            has_live_processes=bool(data["has_live_processes"]),
            idle_with_live_processes=idle_with_live_processes,
        )
