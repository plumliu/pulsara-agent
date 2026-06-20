"""Workspace-scoped in-memory terminal supervisors."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from pulsara_agent.runtime.terminal import TerminalSessionManager


@dataclass(slots=True)
class WorkspaceTerminalSupervisor:
    workspace_key: str
    workspace_root: Path
    terminal_sessions: TerminalSessionManager = field(init=False)
    owner_sessions: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        self.terminal_sessions = TerminalSessionManager(self.workspace_root)

    def attach(self, owner_host_session_id: str) -> TerminalSessionManager:
        self.owner_sessions.add(owner_host_session_id)
        self.last_active_at = time.monotonic()
        return self.terminal_sessions

    def detach(self, owner_host_session_id: str, *, kill_owned: bool = True) -> None:
        self.owner_sessions.discard(owner_host_session_id)
        if kill_owned:
            self.terminal_sessions.kill_owned(owner_host_session_id)
        self.last_active_at = time.monotonic()

    def shutdown(self) -> None:
        self.terminal_sessions.shutdown()
        self.owner_sessions.clear()

    def summary(self) -> dict[str, object]:
        return {
            "workspace_key": self.workspace_key,
            "workspace_root": str(self.workspace_root),
            "owner_session_count": len(self.owner_sessions),
            "live_process_count": self.terminal_sessions.live_process_count(),
            "finished_process_count": self.terminal_sessions.finished_process_count(),
        }
