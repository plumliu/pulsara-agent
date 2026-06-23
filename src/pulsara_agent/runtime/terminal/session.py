"""Terminal session state and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulsara_agent.runtime.terminal.env import TerminalEnvBuilder
from pulsara_agent.runtime.terminal.guard import CommandGuard
from pulsara_agent.runtime.terminal.models import (
    TerminalRequest,
    TerminalResult,
    TerminalSessionState,
    TerminalStatus,
)
from pulsara_agent.runtime.terminal.process import (
    ProcessLimitError,
    ProcessRegistry,
    read_captured_cwd,
    snapshot_process,
)
from pulsara_agent.runtime.terminal.shell import TerminalShellConfig


@dataclass(slots=True)
class TerminalSession:
    state: TerminalSessionState
    process_registry: ProcessRegistry
    shell: TerminalShellConfig
    env_builder: TerminalEnvBuilder

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def current_cwd(self) -> Path:
        return self.state.current_cwd

    def execute(self, request: TerminalRequest) -> TerminalResult:
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
        output_callback = request.metadata.get("output_callback")
        if not callable(output_callback):
            output_callback = None
        record_event = request.metadata.get("record_event")
        if not callable(record_event):
            record_event = None
        env_result = self.env_builder.build(
            cwd=decision.effective_cwd,
            workspace_root=self.state.workspace_root,
            shell=self.shell,
        )
        try:
            process, yielded = self.process_registry.exec_with_yield(
                terminal_session_id=self.session_id,
                command=request.command,
                cwd=decision.effective_cwd,
                artifact_root=self.state.workspace_root / ".pulsara" / "terminal-output",
                max_output_chars=request.max_output_chars,
                yield_time_ms=request.yield_time_ms,
                backend_type=self.state.backend_type,
                tty=request.tty,
                max_lifetime_seconds=request.max_lifetime_seconds,
                output_callback=output_callback,
                shell=self.shell,
                env=env_result.env,
                env_diagnostics=env_result.diagnostics,
                owner_host_session_id=self.state.owner_host_session_id,
                owner_conversation_id=self.state.owner_conversation_id,
                origin_event_context=request.metadata.get("origin_event_context"),
                origin_tool_call_id=request.metadata.get("tool_call_id"),
                record_event=record_event,
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
        final_cwd = self.state.current_cwd
        result = snapshot_process(process, cwd=None if yielded else final_cwd)
        if yielded:
            return result

        observed_cwd = read_captured_cwd(process)
        if observed_cwd is not None:
            if _is_within_workspace(observed_cwd, self.state.workspace_root):
                final_cwd = observed_cwd
                self.state.current_cwd = observed_cwd
            else:
                return TerminalResult(
                    status=TerminalStatus.BLOCKED,
                    output=result.output,
                    exit_code=result.exit_code,
                    cwd=str(final_cwd),
                    timed_out=result.timed_out,
                    truncated=result.truncated,
                    error="command ended outside workspace_root; current_cwd was not updated",
                    full_output_ref=result.full_output_ref,
                    metadata=result.metadata,
                )
        return TerminalResult(
            status=result.status,
            output=result.output,
            exit_code=result.exit_code,
            cwd=str(final_cwd),
            timed_out=result.timed_out,
            truncated=result.truncated,
            error=result.error,
            process_id=result.process_id,
            full_output_ref=result.full_output_ref,
            metadata=result.metadata,
        )


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    return path == workspace_root or workspace_root in path.parents
