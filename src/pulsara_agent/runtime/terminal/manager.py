"""Terminal session manager."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

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
    max_pending_completion_records: int = 8
    finished_ttl_seconds: float = 3600.0
    shell: TerminalShellConfig | None = None
    env_builder: TerminalEnvBuilder | None = None
    env_config: TerminalEnvConfig | None = None
    _sessions: dict[tuple[str | None, str], TerminalSession] = field(default_factory=dict, init=False, repr=False)
    process_registry: ProcessRegistry = field(init=False)
    _released_owners: set[str] = field(default_factory=set, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        if self.max_pending_completion_records < 0:
            raise ValueError("max_pending_completion_records must be >= 0")
        if self.shell is None:
            self.shell = detect_terminal_shell()
        if self.env_builder is None:
            self.env_builder = TerminalEnvBuilder(config=self.env_config or TerminalEnvConfig.from_env())
        self.process_registry = ProcessRegistry(
            max_live_processes=self.max_live_processes,
            max_finished_processes=self.max_finished_processes,
            max_pending_completion_records=self.max_pending_completion_records,
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
        with self._lock:
            if self._closed:
                raise RuntimeError("terminal session manager is closed")
            if owner_host_session_id is not None and owner_host_session_id in self._released_owners:
                raise RuntimeError(f"terminal owner has been released: {owner_host_session_id}")
            key = (owner_host_session_id, normalized)
            if key in self._sessions:
                return self._sessions[key]
            if len(self._sessions) >= self.max_sessions:
                # Shared-pool capacity is workspace-wide; surface the owner
                # distribution so a limit hit is diagnosable per owner.
                raise ValueError(
                    f"terminal session limit reached: max {self.max_sessions} "
                    f"(workspace_root={self.workspace_root}, "
                    f"owner_sessions={self._owner_session_counts_locked()})"
                )
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

    def activate_owner(self, owner_host_session_id: str) -> None:
        """Allow a newly issued supervisor lease to use this manager."""
        with self._lock:
            if self._closed:
                raise RuntimeError("terminal session manager is closed")
            self._released_owners.discard(owner_host_session_id)
        self.process_registry.activate_owner(owner_host_session_id)

    def list_session_ids(self) -> list[str]:
        with self._lock:
            return sorted(session_id for _owner, session_id in self._sessions)

    def current_cwd(
        self,
        session_id: str | None = None,
        *,
        owner_host_session_id: str | None = None,
    ) -> Path:
        normalized = self._normalize_session_id(session_id)
        with self._lock:
            session = self._sessions.get((owner_host_session_id, normalized))
            if session is None:
                return self.workspace_root
            return session.current_cwd

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def owner_session_counts(self) -> dict[str | None, int]:
        """Per-owner terminal session counts, for shared-capacity diagnostics."""
        with self._lock:
            return self._owner_session_counts_locked()

    def _owner_session_counts_locked(self) -> dict[str | None, int]:
        counts: dict[str | None, int] = {}
        for owner, _session_id in self._sessions:
            counts[owner] = counts.get(owner, 0) + 1
        return counts

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

    def monitorable_process(
        self,
        process_id: str,
        *,
        owner_host_session_id: str,
        origin_runtime_session_id: str,
    ):
        return self.process_registry.monitorable_process(
            process_id,
            owner_host_session_id=owner_host_session_id,
            origin_runtime_session_id=origin_runtime_session_id,
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

    def drain_pending_completions(
        self,
        owner_host_session_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        """Drain canonical process-completion owners before monitor terminalization."""

        self.process_registry.drain_pending_completions(
            owner_host_session_id,
            timeout_seconds=timeout_seconds,
        )

    def release_owner(
        self,
        owner_host_session_id: str,
        *,
        completion_drain_timeout_seconds: float = 1.0,
    ):
        """Release everything a single owner holds in this shared manager.

        This kills/drains the owner's yielded processes AND drops the owner's
        terminal sessions (and their cwd state) from ``_sessions``. Dropping the
        session keys is what restores ``max_sessions`` capacity: ``kill_owned``
        alone clears the ProcessRegistry but leaves stale ``(owner, session_id)``
        keys that would otherwise permanently occupy the shared workspace pool
        until the whole manager is destroyed (audit P0-7).

        Synchronous: the underlying kill waits on process groups and joins reader
        threads, so callers on an event loop must run this via asyncio.to_thread
        and outside any held async lock.
        """
        results = self.process_registry.release_owner(
            owner_host_session_id,
            completion_drain_timeout_seconds=completion_drain_timeout_seconds,
        )
        with self._lock:
            self._released_owners.add(owner_host_session_id)
            stale_keys = [key for key in self._sessions if key[0] == owner_host_session_id]
            for key in stale_keys:
                self._sessions.pop(key, None)
        return results

    def list_owned(self, owner_host_session_id: str):
        return self.process_registry.list_owned(owner_host_session_id)

    def has_live_processes(self, *, owner_host_session_id: str | None = None) -> bool:
        return self.process_registry.live_count(owner_host_session_id=owner_host_session_id) > 0

    def live_process_count(self, *, owner_host_session_id: str | None = None) -> int:
        return self.process_registry.live_count(owner_host_session_id=owner_host_session_id)

    def finished_process_count(self, *, owner_host_session_id: str | None = None) -> int:
        return self.process_registry.finished_count(owner_host_session_id=owner_host_session_id)

    def pending_completion_count(self, *, owner_host_session_id: str | None = None) -> int:
        return self.process_registry.pending_completion_count(
            owner_host_session_id=owner_host_session_id
        )

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._sessions.clear()
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
