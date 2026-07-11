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
    runtime_session_id: str | None = None


@dataclass(slots=True)
class SessionCloseAttempt:
    """One linearized close attempt shared by its owner and all waiters."""

    host_session_id: str
    token: str
    session: HostSession
    completion: asyncio.Future[None]
    close_conversation_requested: bool = False
    sealed_close_conversation: bool | None = None
    manifest_close_pending_after_physical: bool = False


@dataclass(frozen=True, slots=True)
class ManifestCloseRetryAttempt:
    host_session_id: str
    runtime_session_id: str
    token: str
    completion: asyncio.Future[None]


@dataclass(slots=True)
class ManifestCloseTombstone:
    """Bounded retry identity after physical HostSession teardown."""

    host_session_id: str
    runtime_session_id: str
    conversation_id: str
    retry_attempt: ManifestCloseRetryAttempt | None = None


@dataclass(frozen=True, slots=True)
class SessionCloseClaim:
    attempt: SessionCloseAttempt | None
    is_owner: bool
    requires_manifest_close_after_wait: bool = False
    manifest_retry_attempt: ManifestCloseRetryAttempt | None = None


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
        self._by_runtime_session: dict[str, str] = {}
        self._reserved_runtime_sessions: dict[str, str] = {}
        self._close_attempts: dict[str, SessionCloseAttempt] = {}
        self._manifest_close_tombstones: dict[str, ManifestCloseTombstone] = {}
        self._idle_with_live_processes: set[str] = set()
        self._lock = asyncio.Lock()

    async def reserve(
        self,
        host_session_id: str,
        conversation_id: str,
        *,
        runtime_session_id: str | None = None,
    ) -> SessionReservation:
        """Reserve a unique identity pair before any resource is built.

        Fails closed on a duplicate host_session_id OR conversation_id, whether
        the collision is with a live session or another in-flight reservation.
        This is the correctness condition for terminal ownership: two borrowers
        must never share an owner principal (contract §3, audit P0-3).
        """
        async with self._lock:
            live_count = (
                len(self._sessions)
                + len(self._reserved_sessions)
                + len(self._manifest_close_tombstones)
            )
            if live_count >= self.max_sessions:
                raise RuntimeError(f"host session limit reached: max {self.max_sessions}")
            if (
                host_session_id in self._sessions
                or host_session_id in self._reserved_sessions
                or host_session_id in self._manifest_close_tombstones
            ):
                raise DuplicateHostSessionError(f"host_session_id already in use: {host_session_id}")
            if conversation_id in self._by_conversation or conversation_id in self._reserved_conversations:
                raise DuplicateHostSessionError(f"conversation_id already in use: {conversation_id}")
            if any(
                item.conversation_id == conversation_id
                for item in self._manifest_close_tombstones.values()
            ):
                raise DuplicateHostSessionError(
                    f"conversation_id pending close: {conversation_id}"
                )
            if runtime_session_id is not None:
                runtime_tombstoned = any(
                    item.runtime_session_id == runtime_session_id
                    for item in self._manifest_close_tombstones.values()
                )
                if (
                    runtime_session_id in self._by_runtime_session
                    or runtime_session_id in self._reserved_runtime_sessions
                    or runtime_tombstoned
                ):
                    raise DuplicateHostSessionError(
                        f"runtime_session_id already in use or pending close: {runtime_session_id}"
                    )
            token = uuid4().hex
            self._reserved_sessions[host_session_id] = token
            self._reserved_conversations[conversation_id] = token
            if runtime_session_id is not None:
                self._reserved_runtime_sessions[runtime_session_id] = token
            return SessionReservation(
                host_session_id=host_session_id,
                conversation_id=conversation_id,
                token=token,
                runtime_session_id=runtime_session_id,
            )

    async def publish(self, reservation: SessionReservation, session: HostSession) -> HostSession:
        async with self._lock:
            if (
                self._reserved_sessions.get(reservation.host_session_id) != reservation.token
                or self._reserved_conversations.get(reservation.conversation_id) != reservation.token
                or (
                    reservation.runtime_session_id is not None
                    and self._reserved_runtime_sessions.get(
                        reservation.runtime_session_id
                    )
                    != reservation.token
                )
            ):
                raise RuntimeError(f"reservation already consumed or released: {reservation.host_session_id}")
            if session.host_session_id != reservation.host_session_id:
                raise RuntimeError("published session id does not match reservation")
            if session.conversation_id != reservation.conversation_id:
                raise RuntimeError("published conversation id does not match reservation")
            if (
                reservation.runtime_session_id is not None
                and session.runtime_session_id != reservation.runtime_session_id
            ):
                raise RuntimeError("published runtime session id does not match reservation")
            runtime_owner = self._by_runtime_session.get(session.runtime_session_id)
            runtime_tombstoned = any(
                item.runtime_session_id == session.runtime_session_id
                for item in self._manifest_close_tombstones.values()
            )
            if runtime_owner is not None or runtime_tombstoned:
                raise DuplicateHostSessionError(
                    f"runtime_session_id already in use or pending close: {session.runtime_session_id}"
                )
            self._reserved_sessions.pop(reservation.host_session_id)
            self._reserved_conversations.pop(reservation.conversation_id)
            if reservation.runtime_session_id is not None:
                self._reserved_runtime_sessions.pop(reservation.runtime_session_id, None)
            self._sessions[session.host_session_id] = session
            self._by_conversation[session.conversation_id] = session.host_session_id
            self._by_runtime_session[session.runtime_session_id] = session.host_session_id
            return session

    async def release_reservation(self, reservation: SessionReservation) -> None:
        """Roll back this exact reservation without touching a newer ABA successor."""
        async with self._lock:
            if self._reserved_sessions.get(reservation.host_session_id) == reservation.token:
                self._reserved_sessions.pop(reservation.host_session_id)
            if self._reserved_conversations.get(reservation.conversation_id) == reservation.token:
                self._reserved_conversations.pop(reservation.conversation_id)
            if (
                reservation.runtime_session_id is not None
                and self._reserved_runtime_sessions.get(reservation.runtime_session_id)
                == reservation.token
            ):
                self._reserved_runtime_sessions.pop(reservation.runtime_session_id)

    async def retain_failed_open_manifest_close(
        self,
        reservation: SessionReservation,
        *,
        runtime_session_id: str,
    ) -> None:
        """Replace an unpublished reservation with a manifest-close tombstone.

        Durable open writes the manifest after required initialization but
        before registry publication. If publication fails, this atomic move
        prevents a concurrent resume from entering before the manifest is
        durably marked closed.
        """

        async with self._lock:
            if (
                self._reserved_sessions.get(reservation.host_session_id)
                != reservation.token
                or self._reserved_conversations.get(reservation.conversation_id)
                != reservation.token
                or (
                    reservation.runtime_session_id is not None
                    and self._reserved_runtime_sessions.get(
                        reservation.runtime_session_id
                    )
                    != reservation.token
                )
            ):
                raise RuntimeError(
                    "failed-open manifest finalization requires the current reservation"
                )
            if (
                runtime_session_id in self._by_runtime_session
                or any(
                    item.runtime_session_id == runtime_session_id
                    for item in self._manifest_close_tombstones.values()
                )
            ):
                raise DuplicateHostSessionError(
                    "runtime_session_id already live or pending manifest close: "
                    f"{runtime_session_id}"
                )
            self._reserved_sessions.pop(reservation.host_session_id, None)
            self._reserved_conversations.pop(reservation.conversation_id, None)
            if reservation.runtime_session_id is not None:
                self._reserved_runtime_sessions.pop(
                    reservation.runtime_session_id,
                    None,
                )
            self._manifest_close_tombstones[reservation.host_session_id] = (
                ManifestCloseTombstone(
                    host_session_id=reservation.host_session_id,
                    runtime_session_id=runtime_session_id,
                    conversation_id=reservation.conversation_id,
                )
            )

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

    async def claim_close(
        self,
        host_session_id: str,
        *,
        close_conversation: bool,
    ) -> SessionCloseClaim:
        """Join the current close attempt or atomically become its owner.

        The attempt future is the result carrier as well as the waiter barrier.
        Ownership, attempt publication, and HostSession mutation-gate closing
        are linearized by this registry lock.
        """
        async with self._lock:
            existing = self._close_attempts.get(host_session_id)
            if existing is not None:
                requires_followup = False
                if close_conversation:
                    if existing.sealed_close_conversation is None:
                        existing.close_conversation_requested = True
                    elif not existing.sealed_close_conversation:
                        # The owner has crossed the intent seal. Preserve the
                        # explicit-close contract by performing an idempotent
                        # manifest close after the shared physical close.
                        requires_followup = True
                        existing.manifest_close_pending_after_physical = True
                return SessionCloseClaim(
                    attempt=existing,
                    is_owner=False,
                    requires_manifest_close_after_wait=requires_followup,
                )
            session = self._sessions.get(host_session_id)
            if session is None:
                tombstone = self._manifest_close_tombstones.get(host_session_id)
                if tombstone is not None and close_conversation:
                    retry = tombstone.retry_attempt
                    if retry is not None:
                        return SessionCloseClaim(
                            attempt=None,
                            is_owner=False,
                            manifest_retry_attempt=retry,
                        )
                    retry = ManifestCloseRetryAttempt(
                        host_session_id=host_session_id,
                        runtime_session_id=tombstone.runtime_session_id,
                        token=uuid4().hex,
                        completion=asyncio.get_running_loop().create_future(),
                    )
                    tombstone.retry_attempt = retry
                    return SessionCloseClaim(
                        attempt=None,
                        is_owner=True,
                        manifest_retry_attempt=retry,
                    )
                return SessionCloseClaim(attempt=None, is_owner=False)
            attempt = SessionCloseAttempt(
                host_session_id=host_session_id,
                token=uuid4().hex,
                session=session,
                completion=asyncio.get_running_loop().create_future(),
                close_conversation_requested=close_conversation,
            )
            self._close_attempts[host_session_id] = attempt
            # Close the HostSession mutation gate in the same registry critical
            # section that marks the identity CLOSING. No resume/new turn/stop can
            # slip into the await gap before HostCore calls session.aclose().
            session.begin_close()
            return SessionCloseClaim(attempt=attempt, is_owner=True)

    async def seal_close_intent(self, attempt: SessionCloseAttempt) -> bool:
        """Freeze the merged intent for this exact attempt before finalization."""

        async with self._lock:
            if self._close_attempts.get(attempt.host_session_id) is not attempt:
                raise RuntimeError("session close attempt is no longer current")
            if attempt.sealed_close_conversation is None:
                attempt.sealed_close_conversation = attempt.close_conversation_requested
            return attempt.sealed_close_conversation

    async def finish_close(
        self,
        attempt: SessionCloseAttempt,
        *,
        error: BaseException | None = None,
        manifest_close_pending: bool = False,
    ) -> bool:
        """Conditionally finish this exact attempt and resolve all waiters."""

        async with self._lock:
            if self._close_attempts.get(attempt.host_session_id) is not attempt:
                return False
            session = self._sessions.pop(attempt.host_session_id, None)
            if session is not None:
                self._by_conversation.pop(session.conversation_id, None)
                if (
                    self._by_runtime_session.get(session.runtime_session_id)
                    == attempt.host_session_id
                ):
                    self._by_runtime_session.pop(session.runtime_session_id, None)
            if manifest_close_pending or attempt.manifest_close_pending_after_physical:
                self._manifest_close_tombstones[attempt.host_session_id] = (
                    ManifestCloseTombstone(
                        host_session_id=attempt.host_session_id,
                        runtime_session_id=attempt.session.runtime_session_id,
                        conversation_id=attempt.session.conversation_id,
                    )
                )
            self._close_attempts.pop(attempt.host_session_id, None)
            self._idle_with_live_processes.discard(attempt.host_session_id)
            _resolve_close_attempt(attempt, error=error)
            return True

    async def finish_manifest_close_retry(
        self,
        attempt: ManifestCloseRetryAttempt,
        *,
        error: BaseException | None = None,
    ) -> bool:
        """Resolve one manifest-only retry and retain tombstone on failure."""

        async with self._lock:
            tombstone = self._manifest_close_tombstones.get(attempt.host_session_id)
            if tombstone is None or tombstone.retry_attempt is not attempt:
                return False
            if error is None:
                self._manifest_close_tombstones.pop(attempt.host_session_id, None)
            else:
                tombstone.retry_attempt = None
            _resolve_manifest_retry(attempt, error=error)
            return True

    async def abort_manifest_close_retry(
        self,
        attempt: ManifestCloseRetryAttempt,
        *,
        error: BaseException,
    ) -> bool:
        """Release a cancelled/broken retry owner without losing tombstone."""

        async with self._lock:
            tombstone = self._manifest_close_tombstones.get(attempt.host_session_id)
            if tombstone is None or tombstone.retry_attempt is not attempt:
                return False
            tombstone.retry_attempt = None
            _resolve_manifest_retry(attempt, error=error)
            return True

    async def complete_manifest_close(
        self,
        *,
        host_session_id: str,
        runtime_session_id: str,
    ) -> None:
        """Clear a late-explicit tombstone after idempotent manifest success."""

        async with self._lock:
            tombstone = self._manifest_close_tombstones.get(host_session_id)
            if (
                tombstone is not None
                and tombstone.runtime_session_id == runtime_session_id
                and tombstone.retry_attempt is None
            ):
                self._manifest_close_tombstones.pop(host_session_id, None)

    async def list_manifest_close_tombstones(self) -> tuple[tuple[str, str], ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (item.host_session_id, item.runtime_session_id)
                    for item in self._manifest_close_tombstones.values()
                )
            )

    async def has_manifest_close_tombstone_for_runtime(
        self,
        runtime_session_id: str,
    ) -> bool:
        async with self._lock:
            return any(
                item.runtime_session_id == runtime_session_id
                for item in self._manifest_close_tombstones.values()
            )

    async def abort_close(
        self,
        attempt: SessionCloseAttempt,
        *,
        error: BaseException,
    ) -> bool:
        """Keep a failed-to-drain session indexed and make close retryable.

        The HostSession mutation gate intentionally remains CLOSING. Only the
        registry ownership marker is reset so a later close attempt can resume
        draining without reopening the session to user/runtime mutations.
        """

        async with self._lock:
            if self._close_attempts.get(attempt.host_session_id) is not attempt:
                return False
            self._close_attempts.pop(attempt.host_session_id, None)
            _resolve_close_attempt(attempt, error=error)
            return True

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
                if session_id in self._close_attempts:
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


def _resolve_close_attempt(
    attempt: SessionCloseAttempt,
    *,
    error: BaseException | None,
) -> None:
    if attempt.completion.done():
        return
    if error is None:
        attempt.completion.set_result(None)
    else:
        attempt.completion.set_exception(error)


def _resolve_manifest_retry(
    attempt: ManifestCloseRetryAttempt,
    *,
    error: BaseException | None,
) -> None:
    if attempt.completion.done():
        return
    if error is None:
        attempt.completion.set_result(None)
    else:
        attempt.completion.set_exception(error)
