"""Terminal session state and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulsara_agent.runtime.terminal.backend import TerminalBackend
from pulsara_agent.runtime.terminal.guard import CommandGuard
from pulsara_agent.runtime.terminal.models import (
    TerminalRequest,
    TerminalResult,
    TerminalSessionState,
    TerminalStatus,
)
from pulsara_agent.runtime.terminal.process import ProcessLimitError, ProcessRegistry, snapshot_process
from pulsara_agent.runtime.terminal.shell import TerminalShellConfig


@dataclass(slots=True)
class TerminalSession:
    state: TerminalSessionState
    backend: TerminalBackend
    process_registry: ProcessRegistry
    shell: TerminalShellConfig

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def current_cwd(self) -> Path:
        return self.state.current_cwd

    def execute(self, request: TerminalRequest) -> TerminalResult:
        if request.background:
            return self._start_background(request)
        result = self.backend.execute(request, self.state)
        result_cwd = Path(result.cwd).expanduser().resolve()
        if _is_within_workspace(result_cwd, self.state.workspace_root):
            self.state.current_cwd = result_cwd
        return result

    def _start_background(self, request: TerminalRequest) -> TerminalResult:
        guard = CommandGuard(self.state.workspace_root)
        decision = guard.validate(request, current_cwd=self.state.current_cwd)
        if not decision.allowed:
            return TerminalResult(
                status=TerminalStatus.BLOCKED,
                output="",
                exit_code=-1,
                cwd=str(self.state.current_cwd),
                error=decision.error,
                metadata={
                    "command": request.command,
                    "policy_code": decision.code,
                    "suggested_args": decision.suggested_args,
                },
            )
        assert decision.effective_cwd is not None
        try:
            process = self.process_registry.start_background(
                terminal_session_id=self.session_id,
                command=request.command,
                cwd=decision.effective_cwd,
                artifact_root=self.state.workspace_root / ".pulsara" / "terminal-output",
                max_output_chars=request.max_output_chars,
                backend_type=self.state.backend_type,
                tty=request.tty,
                shell=self.shell,
            )
        except ProcessLimitError as exc:
            return TerminalResult(
                status=TerminalStatus.BLOCKED,
                output="",
                exit_code=-1,
                cwd=str(self.state.current_cwd),
                error=str(exc),
                metadata={"command": request.command},
            )
        return snapshot_process(process)


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    return path == workspace_root or workspace_root in path.parents
