"""Local foreground terminal backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pulsara_agent.runtime.terminal.guard import CommandGuard
from pulsara_agent.runtime.terminal.models import (
    TerminalBackendType,
    TerminalRequest,
    TerminalResult,
    TerminalSessionState,
    TerminalStatus,
)
from pulsara_agent.runtime.terminal.process import (
    read_captured_cwd,
    snapshot_process,
    spawn_local_process,
    wait_for_process,
)
from pulsara_agent.runtime.terminal.shell import TerminalShellConfig, detect_terminal_shell


@dataclass(slots=True)
class LocalTerminalBackend:
    backend_type: TerminalBackendType = TerminalBackendType.LOCAL
    shell: TerminalShellConfig = field(default_factory=detect_terminal_shell)

    def execute(self, request: TerminalRequest, state: TerminalSessionState) -> TerminalResult:
        guard = CommandGuard(state.workspace_root)
        decision = guard.validate(request, current_cwd=state.current_cwd)
        if not decision.allowed:
            return TerminalResult(
                status=TerminalStatus.BLOCKED,
                output="",
                exit_code=-1,
                cwd=str(state.current_cwd),
                error=decision.error,
                metadata={
                    "command": request.command,
                    "policy_code": decision.code,
                    "suggested_args": decision.suggested_args,
                },
            )

        assert decision.effective_cwd is not None
        output_callback = request.metadata.get("output_callback")
        if not callable(output_callback):
            output_callback = None
        process = spawn_local_process(
            terminal_session_id=state.session_id,
            command=request.command,
            cwd=decision.effective_cwd,
            artifact_root=state.workspace_root / ".pulsara" / "terminal-output",
            max_output_chars=request.max_output_chars,
            backend_type=self.backend_type,
            background=False,
            capture_cwd=True,
            output_callback=output_callback,
            shell=self.shell,
        )
        completed = wait_for_process(process, timeout_seconds=request.timeout_seconds, kill_on_timeout=True)
        final_cwd = state.current_cwd
        error: str | None = None
        observed_cwd = read_captured_cwd(process)
        result = snapshot_process(process, cwd=final_cwd)

        if observed_cwd is not None:
            if _is_within_workspace(observed_cwd, state.workspace_root):
                final_cwd = observed_cwd
            elif result.status is not TerminalStatus.TIMEOUT:
                error = "command ended outside workspace_root; current_cwd was not updated"
                return TerminalResult(
                    status=TerminalStatus.BLOCKED,
                    output=result.output,
                    exit_code=result.exit_code,
                    cwd=str(final_cwd),
                    timed_out=result.timed_out,
                    truncated=result.truncated,
                    error=error,
                    metadata=result.metadata,
                )

        if not completed and result.status is TerminalStatus.TIMEOUT:
            error = f"command timed out after {request.timeout_seconds} seconds"

        return TerminalResult(
            status=result.status,
            output=result.output,
            exit_code=result.exit_code,
            cwd=str(final_cwd),
            timed_out=result.timed_out,
            truncated=result.truncated,
            error=error,
            full_output_ref=result.full_output_ref,
            metadata=result.metadata,
        )


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    return path == workspace_root or workspace_root in path.parents
