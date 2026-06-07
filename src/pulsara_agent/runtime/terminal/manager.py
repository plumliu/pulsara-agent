"""Terminal session manager."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pulsara_agent.runtime.terminal.backends.local import LocalTerminalBackend
from pulsara_agent.runtime.terminal.models import TerminalBackendType, TerminalSessionState
from pulsara_agent.runtime.terminal.session import TerminalSession


DEFAULT_TERMINAL_SESSION_ID = "default"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


@dataclass(slots=True)
class TerminalSessionManager:
    workspace_root: Path
    max_sessions: int = 4
    _sessions: dict[str, TerminalSession] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()

    def get_or_create(self, session_id: str | None = None) -> TerminalSession:
        normalized = self._normalize_session_id(session_id)
        if normalized in self._sessions:
            return self._sessions[normalized]
        if len(self._sessions) >= self.max_sessions:
            raise ValueError(f"terminal session limit reached: max {self.max_sessions}")
        session = TerminalSession(
            state=TerminalSessionState(
                session_id=normalized,
                workspace_root=self.workspace_root,
                current_cwd=self.workspace_root,
                backend_type=TerminalBackendType.LOCAL,
            ),
            backend=LocalTerminalBackend(),
        )
        self._sessions[normalized] = session
        return session

    def list_session_ids(self) -> list[str]:
        return sorted(self._sessions)

    def _normalize_session_id(self, session_id: str | None) -> str:
        value = session_id or DEFAULT_TERMINAL_SESSION_ID
        if not isinstance(value, str):
            raise ValueError("terminal session_id must be a string")
        value = value.strip()
        if not value:
            value = DEFAULT_TERMINAL_SESSION_ID
        if not _SESSION_ID_RE.fullmatch(value):
            raise ValueError(
                "terminal session_id must be 1-32 chars of letters, numbers, underscore, or hyphen"
            )
        return value

