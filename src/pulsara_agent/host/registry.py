"""In-memory HostSession registry: a pure index and idle-candidate finder.

Per the workspace terminal lifecycle contract §3 the registry is NOT a second
close coordinator: it never calls ``session.aclose()`` and never touches a
supervisor. It only reserves/publishes identities (fail-closed on duplicates),
hands sessions back by id, marks CLOSING, and reports idle candidates. HostCore
is the sole lifecycle coordinator that acts on those candidates.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import uuid4

from pulsara_agent.host.session import HostSession


class DuplicateHostSessionError(RuntimeError):
    """Raised when a host_session_id / conversation_id is already reserved or live."""


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


@dataclass(frozen=True, slots=True)
class SessionReservation:
    host_session_id: str
    conversation_id: str
    token: str


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
        self._reserved_sessions: dict[str, str] = {}
        self._reserved_conversations: dict[str, str] = {}
        self._closing: set[str] = set()
        self._idle_with_live_processes: set[str] = set()
        self._lock = asyncio.Lock()

    async def reserve(self, host_session_id: str, conversation_id: str) -> SessionReservation:
        """Reserve a unique identity pair before any resource is built.

        Fails closed on a duplicate host_session_id OR conversation_id, whether
        the collision is with a live session or another in-flight reservation.
        This is the correctness condition for terminal ownership: two borrowers
        must never share an owner principal (contract §3, audit P0-3).
        """
        async with self._lock:
            live_count = len(self._sessions) + len(self._reserved_sessions)
            if live_count >= self.max_sessions:
                raise RuntimeError(f"host session limit reached: max {self.max_sessions}")
            if host_session_id in self._sessions or host_session_id in self._reserved_sessions:
                raise DuplicateHostSessionError(f"host_session_id already in use: {host_session_id}")
            if conversation_id in self._by_conversation or conversation_id in self._reserved_conversations:
                raise DuplicateHostSessionError(f"conversation_id already in use: {conversation_id}")
            token = uuid4().hex
            self._reserved_sessions[host_session_id] = token
            self._reserved_conversations[conversation_id] = token
            return SessionReservation(
                host_session_id=host_session_id,
                conversation_id=conversation_id,
                token=token,
            )

    async def publish(self, reservation: SessionReservation, session: HostSession) -> HostSession:
        async with self._lock:
            if (
                self._reserved_sessions.get(reservation.host_session_id) != reservation.token
                or self._reserved_conversations.get(reservation.conversation_id) != reservation.token
            ):
                raise RuntimeError(f"reservation already consumed or released: {reservation.host_session_id}")
            if session.host_session_id != reservation.host_session_id:
                raise RuntimeError("published session id does not match reservation")
            if session.conversation_id != reservation.conversation_id:
                raise RuntimeError("published conversation id does not match reservation")
            self._reserved_sessions.pop(reservation.host_session_id)
            self._reserved_conversations.pop(reservation.conversation_id)
            self._sessions[session.host_session_id] = session
            self._by_conversation[session.conversation_id] = session.host_session_id
            return session

    async def release_reservation(self, reservation: SessionReservation) -> None:
        """Roll back this exact reservation without touching a newer ABA successor."""
        async with self._lock:
            if self._reserved_sessions.get(reservation.host_session_id) == reservation.token:
                self._reserved_sessions.pop(reservation.host_session_id)
            if self._reserved_conversations.get(reservation.conversation_id) == reservation.token:
                self._reserved_conversations.pop(reservation.conversation_id)

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

    async def begin_close(self, host_session_id: str) -> HostSession | None:
        """Mark a session CLOSING and return it, or None if absent/already closing.

        Idempotent: a second begin_close for the same id returns None so the
        caller treats a repeated close as a no-op. The session stays in the index
        (still discoverable/diagnosable) until ``finish_close``.
        """
        async with self._lock:
            if host_session_id in self._closing:
                return None
            session = self._sessions.get(host_session_id)
            if session is None:
                return None
            self._closing.add(host_session_id)
            # Close the HostSession mutation gate in the same registry critical
            # section that marks the identity CLOSING. No resume/new turn/stop can
            # slip into the await gap before HostCore calls session.aclose().
            session.begin_close()
            return session

    async def finish_close(self, host_session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(host_session_id, None)
            if session is not None:
                self._by_conversation.pop(session.conversation_id, None)
            self._closing.discard(host_session_id)
            self._idle_with_live_processes.discard(host_session_id)

    async def list_sessions(self) -> list[HostSessionSummary]:
        async with self._lock:
            sessions = list(self._sessions.values())
            idle = set(self._idle_with_live_processes)
        return [self._summary(session, idle_with_live_processes=session.host_session_id in idle) for session in sessions]

    async def list_idle_candidates(self, *, now: float | None = None) -> list[str]:
        """Return ids eligible for idle close. Pure discovery, no side effects.

        It never closes a session and never touches a supervisor (contract §3).
        It does refresh the ``idle_with_live_processes`` diagnostic flag, which is
        an internal annotation surfaced by ``list_sessions``, not an external
        teardown effect.
        """
        if self.idle_ttl_seconds is None:
            return []
        now = time.monotonic() if now is None else now
        candidates: list[str] = []
        async with self._lock:
            self._idle_with_live_processes.clear()
            for session_id, session in self._sessions.items():
                if session_id in self._closing:
                    continue
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
                candidates.append(session_id)
        return candidates

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
