"""Terminal session state and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulsara_agent.runtime.terminal.backend import TerminalBackend
from pulsara_agent.runtime.terminal.models import (
    TerminalRequest,
    TerminalResult,
    TerminalSessionState,
)


@dataclass(slots=True)
class TerminalSession:
    state: TerminalSessionState
    backend: TerminalBackend

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def current_cwd(self) -> Path:
        return self.state.current_cwd

    def execute(self, request: TerminalRequest) -> TerminalResult:
        result = self.backend.execute(request, self.state)
        result_cwd = Path(result.cwd).expanduser().resolve()
        if _is_within_workspace(result_cwd, self.state.workspace_root):
            self.state.current_cwd = result_cwd
        return result


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    return path == workspace_root or workspace_root in path.parents

