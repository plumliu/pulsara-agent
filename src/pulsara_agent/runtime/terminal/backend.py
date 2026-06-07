"""Terminal backend protocol."""

from __future__ import annotations

from typing import Protocol

from pulsara_agent.runtime.terminal.models import (
    TerminalBackendType,
    TerminalRequest,
    TerminalResult,
    TerminalSessionState,
)


class TerminalBackend(Protocol):
    backend_type: TerminalBackendType

    def execute(self, request: TerminalRequest, state: TerminalSessionState) -> TerminalResult:
        """Execute a foreground terminal request for one terminal session."""

