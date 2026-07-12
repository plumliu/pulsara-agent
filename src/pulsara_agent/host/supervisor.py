"""Workspace-scoped in-memory terminal supervisors.

The supervisor is the sole owner of a workspace's shared ``TerminalSessionManager``
(contract §4). It tracks borrowers as typed, generation-stamped leases and exposes
only synchronous, fast state transitions; the actual blocking cleanup (process kill,
reader-thread join, session prune) is performed by the manager and MUST be run by
HostCore off the event loop and outside any held async lock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pulsara_agent.runtime.terminal import (
    TerminalOwnerContext,
    TerminalSessionManager,
    WorkspaceTerminalLease,
)


class WorkspaceLifecycleState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class WorkspaceClosingError(RuntimeError):
    """Raised when a new terminal lease is requested on a closing/closed workspace."""


class DuplicateTerminalOwnerError(RuntimeError):
    """Raised when a host session id already holds a lease on this workspace."""


@dataclass(frozen=True, slots=True)
class WorkspaceTerminalSnapshot:
    """Typed admin-facing view of one workspace terminal pool (contract §8)."""

    workspace_key: str
    workspace_root: str
    state: str
    owner_session_count: int
    owner_host_session_ids: tuple[str, ...]
    live_process_count: int
    finished_process_count: int
    pending_completion_count: int
    terminal_session_count: int
    owner_session_distribution: dict[str, int]
    processes: tuple[dict[str, object], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "workspace_key": self.workspace_key,
            "workspace_root": self.workspace_root,
            "state": self.state,
            "owner_session_count": self.owner_session_count,
            "owner_host_session_ids": list(self.owner_host_session_ids),
            "live_process_count": self.live_process_count,
            "finished_process_count": self.finished_process_count,
            "pending_completion_count": self.pending_completion_count,
            "terminal_session_count": self.terminal_session_count,
            "owner_session_distribution": dict(self.owner_session_distribution),
            "processes": [dict(process) for process in self.processes],
        }


@dataclass(slots=True)
class WorkspaceTerminalSupervisor:
    workspace_key: str
    workspace_root: Path
    terminal_sessions: TerminalSessionManager = field(init=False)
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    _state: WorkspaceLifecycleState = field(default=WorkspaceLifecycleState.OPEN, init=False)
    _leases: dict[str, WorkspaceTerminalLease] = field(default_factory=dict, init=False, repr=False)
    _generation_counter: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        self.terminal_sessions = TerminalSessionManager(self.workspace_root)

    @property
    def state(self) -> WorkspaceLifecycleState:
        return self._state

    @property
    def is_accepting(self) -> bool:
        return self._state is WorkspaceLifecycleState.OPEN

    @property
    def owner_count(self) -> int:
        return len(self._leases)

    @property
    def owner_session_ids(self) -> tuple[str, ...]:
        return tuple(self._leases)

    def has_owner(self, host_session_id: str) -> bool:
        return host_session_id in self._leases

    def is_current_lease(self, lease: WorkspaceTerminalLease) -> bool:
        """Return whether ``lease`` is still publishable on this supervisor."""
        if self._state is not WorkspaceLifecycleState.OPEN:
            return False
        current = self._leases.get(lease.owner.host_session_id)
        return current is not None and current.generation == lease.generation

    def attach(self, owner: TerminalOwnerContext) -> WorkspaceTerminalLease:
        """Reserve a unique, generation-stamped lease for one host session.

        Fails closed once the workspace is closing/closed, and rejects a second
        lease for the same host_session_id principal (registry uniqueness is the
        first line of defense; this is the second).
        """
        if self._state is not WorkspaceLifecycleState.OPEN:
            raise WorkspaceClosingError(
                f"workspace {self.workspace_key} is {self._state.value}; cannot attach"
            )
        if owner.host_session_id in self._leases:
            raise DuplicateTerminalOwnerError(
                f"host session already holds a terminal lease: {owner.host_session_id}"
            )
        self.terminal_sessions.activate_owner(owner.host_session_id)
        self._generation_counter += 1
        lease = WorkspaceTerminalLease(
            workspace_key=self.workspace_key,
            owner=owner,
            generation=self._generation_counter,
            manager=self.terminal_sessions,
        )
        self._leases[owner.host_session_id] = lease
        self.last_active_at = time.monotonic()
        return lease

    def release_lease(self, lease: WorkspaceTerminalLease) -> str | None:
        """Drop a lease (state transition only) and return the owner id to clean up.

        Idempotent and generation-safe: a stale or superseded lease release is a
        no-op returning None, so a duplicate release can never kill a newer
        borrower's processes. The returned host_session_id (if any) is what the
        caller must hand to ``terminal_sessions.release_owner`` off-lock.
        """
        current = self._leases.get(lease.owner.host_session_id)
        if current is None or current.generation != lease.generation:
            return None
        self._leases.pop(lease.owner.host_session_id, None)
        self.last_active_at = time.monotonic()
        return lease.owner.host_session_id

    def mark_closing(self) -> bool:
        """Transition OPEN -> CLOSING so new attaches fail. Returns True once."""
        if self._state is not WorkspaceLifecycleState.OPEN:
            return False
        self._state = WorkspaceLifecycleState.CLOSING
        self.last_active_at = time.monotonic()
        return True

    def mark_closed(self) -> None:
        """Transition to CLOSED and forget all leases. The caller is responsible
        for running ``terminal_sessions.shutdown()`` off-lock for the all-kill."""
        self._state = WorkspaceLifecycleState.CLOSED
        self._leases.clear()
        self.last_active_at = time.monotonic()

    def snapshot(self) -> WorkspaceTerminalSnapshot:
        processes = self.terminal_sessions.list_processes()
        distribution = {
            owner: count
            for owner, count in self.terminal_sessions.owner_session_counts().items()
            if owner is not None
        }
        return WorkspaceTerminalSnapshot(
            workspace_key=self.workspace_key,
            workspace_root=str(self.workspace_root),
            state=self._state.value,
            owner_session_count=len(self._leases),
            owner_host_session_ids=tuple(self._leases),
            live_process_count=self.terminal_sessions.live_process_count(),
            finished_process_count=self.terminal_sessions.finished_process_count(),
            pending_completion_count=self.terminal_sessions.pending_completion_count(),
            terminal_session_count=self.terminal_sessions.session_count(),
            owner_session_distribution=distribution,
            processes=tuple(process.to_payload(include_owner=True) for process in processes),
        )

    def summary(self) -> dict[str, object]:
        return self.snapshot().to_payload()
