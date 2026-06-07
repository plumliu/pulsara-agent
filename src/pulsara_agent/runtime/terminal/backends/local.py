"""Local foreground terminal backend."""

from __future__ import annotations

import codecs
import os
import select
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from pulsara_agent.runtime.terminal.guard import CommandGuard
from pulsara_agent.runtime.terminal.models import (
    TerminalBackendType,
    TerminalRequest,
    TerminalResult,
    TerminalSessionState,
    TerminalStatus,
)
from pulsara_agent.runtime.terminal.output import finalize_output


_TIMEOUT_EXIT_CODE = 124


@dataclass(slots=True)
class LocalTerminalBackend:
    backend_type: TerminalBackendType = TerminalBackendType.LOCAL
    _cwd_file: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._cwd_file = Path(tempfile.gettempdir()) / f"pulsara-terminal-cwd-{uuid4().hex}.txt"

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
                metadata={"command": request.command},
            )

        assert decision.effective_cwd is not None
        self._clear_cwd_file()
        proc = self._spawn(request.command, cwd=decision.effective_cwd)
        try:
            stdout, timed_out = self._communicate(proc, timeout_seconds=request.timeout_seconds)
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        final_cwd = state.current_cwd
        runtime_status = TerminalStatus.SUCCESS
        error: str | None = None
        observed_cwd = self._read_cwd_file()

        if observed_cwd is not None:
            if _is_within_workspace(observed_cwd, state.workspace_root):
                final_cwd = observed_cwd
            else:
                runtime_status = TerminalStatus.BLOCKED
                error = "command ended outside workspace_root; current_cwd was not updated"

        processed = finalize_output(stdout, max_chars=request.max_output_chars)
        if timed_out:
            runtime_status = TerminalStatus.TIMEOUT
            error = f"command timed out after {request.timeout_seconds} seconds"
        elif runtime_status is TerminalStatus.SUCCESS and proc.returncode != 0:
            runtime_status = TerminalStatus.ERROR

        return TerminalResult(
            status=runtime_status,
            output=processed.text,
            exit_code=_TIMEOUT_EXIT_CODE if timed_out else (proc.returncode or 0),
            cwd=str(final_cwd),
            timed_out=timed_out,
            truncated=processed.truncated,
            error=error,
            metadata={
                "command": request.command,
                "backend_type": self.backend_type.value,
            },
        )

    def _spawn(self, command: str, *, cwd: Path) -> subprocess.Popen[bytes]:
        wrapped = self._wrap_command(command, cwd)
        return subprocess.Popen(
            ["bash", "-c", wrapped],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

    def _wrap_command(self, command: str, cwd: Path) -> str:
        quoted_cwd = shlex.quote(str(cwd))
        quoted_cwd_file = shlex.quote(str(self._cwd_file))
        escaped = command.replace("'", "'\\''")
        return "\n".join(
            [
                f"builtin cd -- {quoted_cwd} || exit 126",
                f"eval '{escaped}'",
                "__pulsara_ec=$?",
                f"pwd -P > {quoted_cwd_file} 2>/dev/null || true",
                "exit $__pulsara_ec",
            ]
        )

    def _communicate(self, proc: subprocess.Popen[bytes], *, timeout_seconds: int) -> tuple[str, bool]:
        chunks: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        timed_out = False
        try:
            deadline = time.monotonic() + timeout_seconds
            idle_after_exit = 0
            while True:
                if proc.poll() is not None and idle_after_exit >= 3:
                    break
                if time.monotonic() > deadline and proc.poll() is None:
                    timed_out = True
                    self._terminate_process_group(proc)
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    chunks.append(decoder.decode(chunk))
                    idle_after_exit = 0
                elif proc.poll() is not None:
                    idle_after_exit += 1
            tail = decoder.decode(b"", final=True)
            if tail:
                chunks.append(tail)
        finally:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._kill_process_group(proc)
                proc.wait(timeout=1)
        return "".join(chunks), timed_out

    def _read_cwd_file(self) -> Path | None:
        try:
            raw = self._cwd_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if not raw:
            return None
        try:
            return Path(raw).resolve()
        except OSError:
            return None

    def _clear_cwd_file(self) -> None:
        try:
            self._cwd_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _terminate_process_group(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._kill_process_group(proc)

    def _kill_process_group(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    return path == workspace_root or workspace_root in path.parents

