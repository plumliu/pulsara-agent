"""Terminal session manager."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pulsara_agent.runtime.terminal.env import TerminalEnvBuilder, TerminalEnvConfig
from pulsara_agent.runtime.terminal.models import TerminalBackendType, TerminalSessionState
from pulsara_agent.runtime.terminal.process import ProcessRegistry
from pulsara_agent.runtime.terminal.session import TerminalSession
from pulsara_agent.runtime.terminal.shell import TerminalShellConfig, detect_terminal_shell


DEFAULT_TERMINAL_SESSION_ID = "default"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


@dataclass(slots=True)
class TerminalSessionManager:
    workspace_root: Path
    max_sessions: int = 4
    max_live_processes: int = 8
    max_finished_processes: int = 32
    finished_ttl_seconds: float = 3600.0
    shell: TerminalShellConfig | None = None
    env_builder: TerminalEnvBuilder | None = None
    env_config: TerminalEnvConfig | None = None
    _sessions: dict[tuple[str | None, str], TerminalSession] = field(default_factory=dict, init=False, repr=False)
    process_registry: ProcessRegistry = field(init=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        if self.shell is None:
            self.shell = detect_terminal_shell()
        if self.env_builder is None:
            self.env_builder = TerminalEnvBuilder(config=self.env_config or TerminalEnvConfig.from_env())
        self.process_registry = ProcessRegistry(
            max_live_processes=self.max_live_processes,
            max_finished_processes=self.max_finished_processes,
            finished_ttl_seconds=self.finished_ttl_seconds,
        )

    def get_or_create(
        self,
        session_id: str | None = None,
        *,
        owner_host_session_id: str | None = None,
        owner_conversation_id: str | None = None,
    ) -> TerminalSession:
        normalized = self._normalize_session_id(session_id)
        key = (owner_host_session_id, normalized)
        if key in self._sessions:
            return self._sessions[key]
        if len(self._sessions) >= self.max_sessions:
            raise ValueError(f"terminal session limit reached: max {self.max_sessions}")
        session = TerminalSession(
            state=TerminalSessionState(
                session_id=normalized,
                workspace_root=self.workspace_root,
                current_cwd=self.workspace_root,
                backend_type=TerminalBackendType.LOCAL,
                backend_metadata={"shell": self.shell.to_metadata()},
                owner_host_session_id=owner_host_session_id,
                owner_conversation_id=owner_conversation_id,
            ),
            process_registry=self.process_registry,
            shell=self.shell,
            env_builder=self.env_builder,
        )
        self._sessions[key] = session
        return session

    def list_session_ids(self) -> list[str]:
        return sorted(session_id for _owner, session_id in self._sessions)

    def poll_process(
        self,
        process_id: str,
        *,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ):
        return self.process_registry.poll(
            process_id,
            max_output_chars=max_output_chars,
            owner_host_session_id=owner_host_session_id,
        )

    def wait_process(
        self,
        process_id: str,
        *,
        timeout_seconds: int | None = None,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ):
        return self.process_registry.wait(
            process_id,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            owner_host_session_id=owner_host_session_id,
        )

    def kill_process(
        self,
        process_id: str,
        *,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ):
        return self.process_registry.kill(
            process_id,
            max_output_chars=max_output_chars,
            owner_host_session_id=owner_host_session_id,
        )

    def write_process(
        self,
        process_id: str,
        data: str,
        *,
        append_newline: bool = False,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ):
        return self.process_registry.write(
            process_id,
            data,
            append_newline=append_newline,
            max_output_chars=max_output_chars,
            owner_host_session_id=owner_host_session_id,
        )

    def close_process_stdin(
        self,
        process_id: str,
        *,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ):
        return self.process_registry.close_stdin(
            process_id,
            max_output_chars=max_output_chars,
            owner_host_session_id=owner_host_session_id,
        )

    def list_processes(
        self,
        *,
        owner_host_session_id: str | None = None,
        include_finished: bool = True,
        include_running: bool = True,
    ):
        return self.process_registry.list_processes(
            owner_host_session_id=owner_host_session_id,
            include_finished=include_finished,
            include_running=include_running,
        )

    def log_process(
        self,
        process_id: str,
        *,
        max_output_chars: int | None = None,
        owner_host_session_id: str | None = None,
    ):
        return self.process_registry.log(
            process_id,
            max_output_chars=max_output_chars,
            owner_host_session_id=owner_host_session_id,
        )

    def kill_owned(self, owner_host_session_id: str):
        return self.process_registry.kill_owned(owner_host_session_id)

    def list_owned(self, owner_host_session_id: str):
        return self.process_registry.list_owned(owner_host_session_id)

    def has_live_processes(self, *, owner_host_session_id: str | None = None) -> bool:
        return self.process_registry.live_count(owner_host_session_id=owner_host_session_id) > 0

    def live_process_count(self, *, owner_host_session_id: str | None = None) -> int:
        return self.process_registry.live_count(owner_host_session_id=owner_host_session_id)

    def finished_process_count(self, *, owner_host_session_id: str | None = None) -> int:
        return self.process_registry.finished_count(owner_host_session_id=owner_host_session_id)

    def shutdown(self) -> None:
        self.process_registry.shutdown()

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
