"""Host-owned shell and default-deny subprocess environment builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
from threading import Condition, RLock
from time import monotonic, sleep
from typing import Mapping


_MAXIMUM_SNAPSHOT_BYTES = 1_000_000
_SENTINEL = b"PULSARA_TERMINAL_ENV_V1\0"
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


_INERT_NAMES = frozenset(
    "HOME USER LOGNAME SHELL TMPDIR TEMP TMP LANG LC_ALL LC_CTYPE TERM COLORTERM "
    "XDG_SESSION_TYPE XDG_CURRENT_DESKTOP XDG_DATA_HOME XDG_CONFIG_HOME "
    "XDG_CACHE_HOME XDG_STATE_HOME NVM_DIR VOLTA_HOME PNPM_HOME BUN_INSTALL "
    "CARGO_HOME RUSTUP_HOME PYENV_ROOT RBENV_ROOT ASDF_DIR MISE_DATA_DIR "
    "MISE_CONFIG_DIR MISE_CACHE_DIR HOMEBREW_PREFIX HOMEBREW_CELLAR "
    "HOMEBREW_REPOSITORY GOPATH GOROOT PATH".split()
)
_ACTIVE_DENY_RE = re.compile(
    r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|COOKIE|AUTH|SOCKET|ENDPOINT|"
    r"PROXY|DISPLAY|DBUS|SSH_|GIT_|PYTHONPATH|NODE_OPTIONS|LD_PRELOAD|DYLD_)"
)


@dataclass(frozen=True, slots=True)
class TerminalShellConfig:
    path: Path
    name: str

    def command_argv(self, command: str) -> tuple[str, ...]:
        return (str(self.path), "-c", command)

    def probe_argv(self, command: str) -> tuple[str, ...]:
        # zsh and bash both accept -l -i -c.  /bin/sh implementations may not
        # support interactive startup cleanly, so use login-only there.
        if self.name in {"zsh", "bash", "ksh"}:
            return (str(self.path), "-l", "-i", "-c", command)
        return (str(self.path), "-l", "-c", command)


@dataclass(frozen=True, slots=True)
class TerminalEnvConfig:
    enable_shell_snapshot: bool = True
    shell_snapshot_ttl_seconds: float = 300.0
    shell_snapshot_timeout_seconds: float = 5.0
    inherit_allowlist: frozenset[str] = frozenset()
    passthrough_names: frozenset[str] = frozenset()
    extra_path_prepends: tuple[Path, ...] = ()
    venv_overlay: bool = True

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str] | None = None
    ) -> "TerminalEnvConfig":
        values = environ or os.environ
        return cls(
            enable_shell_snapshot=_bool(
                values.get("PULSARA_TERMINAL_SHELL_SNAPSHOT"), True
            ),
            shell_snapshot_ttl_seconds=_positive_float(
                values.get("PULSARA_TERMINAL_SHELL_SNAPSHOT_TTL_SECONDS"), 300.0
            ),
            shell_snapshot_timeout_seconds=_positive_float(
                values.get("PULSARA_TERMINAL_SHELL_SNAPSHOT_TIMEOUT_SECONDS"), 5.0
            ),
            inherit_allowlist=_names(
                values.get("PULSARA_TERMINAL_ENV_INHERIT_ALLOWLIST")
            ),
            passthrough_names=_names(
                values.get("PULSARA_TERMINAL_ENV_PASSTHROUGH_NAMES")
            ),
            extra_path_prepends=tuple(
                Path(value).expanduser()
                for value in _csv(values.get("PULSARA_TERMINAL_EXTRA_PATH_PREPENDS"))
            ),
            venv_overlay=_bool(values.get("PULSARA_TERMINAL_VENV_OVERLAY"), True),
        )


@dataclass(frozen=True, slots=True)
class TerminalEnvironment:
    values: dict[str, str]
    diagnostic: dict[str, object]
    shell: TerminalShellConfig


@dataclass(frozen=True, slots=True)
class _Snapshot:
    values: dict[str, str]
    created_at: float
    cache_key: tuple[object, ...]


class _ProbeAttemptState(StrEnum):
    SPAWNING = "SPAWNING"
    RUNNING = "RUNNING"
    JOINED = "JOINED"


@dataclass(slots=True)
class _ProbeAttempt:
    condition: Condition
    state: _ProbeAttemptState = _ProbeAttemptState.SPAWNING
    done: bool = False
    snapshot: _Snapshot | None = None
    error_code: str | None = None
    process: subprocess.Popen[bytes] | None = None


@dataclass(slots=True)
class TerminalEnvironmentOwner:
    workspace_root: Path
    config: TerminalEnvConfig = field(default_factory=TerminalEnvConfig.from_environ)
    parent_env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    _cache: dict[tuple[object, ...], _Snapshot] = field(
        default_factory=dict, init=False
    )
    _attempts: dict[tuple[object, ...], _ProbeAttempt] = field(
        default_factory=dict, init=False
    )
    _lock: RLock = field(default_factory=RLock, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()

    def build(self, *, cwd: Path) -> TerminalEnvironment:
        shell = detect_terminal_shell(self.parent_env)
        base, removed_count, secret_count = _sanitize_environment(
            self.parent_env,
            allowlist=_INERT_NAMES | self.config.inherit_allowlist,
            passthrough=self.config.passthrough_names,
        )
        snapshot: _Snapshot | None = None
        snapshot_error: str | None = None
        if self.config.enable_shell_snapshot:
            snapshot, snapshot_error = self._snapshot(shell)
        if snapshot is not None:
            for name, value in snapshot.values.items():
                if name != "PATH":
                    base[name] = value
        path_entries: list[str] = []
        venv = (
            find_nearest_venv_bin(cwd, self.workspace_root)
            if self.config.venv_overlay
            else None
        )
        if venv is not None:
            path_entries.append(str(venv))
        path_entries.extend(
            str(path.resolve())
            for path in self.config.extra_path_prepends
            if path.is_dir()
        )
        if snapshot is not None:
            path_entries.extend(_split_path(snapshot.values.get("PATH", "")))
        path_entries.extend(_split_path(base.get("PATH", "")))
        path_entries.extend(
            (
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            )
        )
        base["PATH"] = os.pathsep.join(
            dict.fromkeys(item for item in path_entries if item)
        )
        base["SHELL"] = str(shell.path)
        return TerminalEnvironment(
            values=base,
            shell=shell,
            diagnostic={
                "shell_path": str(shell.path),
                "shell_name": shell.name,
                "shell_snapshot_used": snapshot is not None,
                "shell_snapshot_error_code": snapshot_error,
                "removed_variable_count": removed_count,
                "secret_shaped_value_removed_count": secret_count,
                "nearest_venv_path": None if venv is None else str(venv),
                "path_entry_count": len(dict.fromkeys(path_entries)),
            },
        )

    def _snapshot(
        self, shell: TerminalShellConfig
    ) -> tuple[_Snapshot | None, str | None]:
        key = _snapshot_key(shell, self.workspace_root, self.parent_env, self.config)
        now = monotonic()
        with self._lock:
            if self._closed:
                return None, "OWNER_CLOSED"
            cached = self._cache.get(key)
            if (
                cached is not None
                and now - cached.created_at <= self.config.shell_snapshot_ttl_seconds
            ):
                return cached, None
            attempt = self._attempts.get(key)
            if attempt is None:
                attempt = _ProbeAttempt(Condition(self._lock))
                self._attempts[key] = attempt
                owner = True
            else:
                owner = False
            if not owner:
                deadline = now + self.config.shell_snapshot_timeout_seconds
                while not attempt.done and not self._closed:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return None, "PROBE_WAIT_TIMEOUT"
                    attempt.condition.wait(remaining)
                return attempt.snapshot, attempt.error_code
        snapshot: _Snapshot | None = None
        error_code: str | None = None
        try:
            values = self._run_probe(shell, attempt)
            snapshot = _Snapshot(values, monotonic(), key)
        except TimeoutError:
            error_code = "PROBE_TIMEOUT"
        except OverflowError:
            error_code = "PROBE_OVERSIZE"
        except (OSError, ValueError, subprocess.SubprocessError):
            error_code = "PROBE_FAILED"
        with self._lock:
            attempt.snapshot = snapshot
            attempt.error_code = error_code
            attempt.done = True
            if snapshot is not None and not self._closed:
                self._cache[key] = snapshot
            attempt.condition.notify_all()
            self._attempts.pop(key, None)
        return snapshot, error_code

    def _run_probe(
        self, shell: TerminalShellConfig, attempt: _ProbeAttempt
    ) -> dict[str, str]:
        command = "printf 'PULSARA_TERMINAL_ENV_V1\\0'; env -0"
        # Popen and process-owner publication share the same linearization
        # lock as close.  Close either seals admission before Popen, or sees
        # the exact RUNNING process and waits until this attempt is JOINED.
        with self._lock:
            if self._closed:
                attempt.state = _ProbeAttemptState.JOINED
                attempt.condition.notify_all()
                raise TimeoutError
            try:
                process = subprocess.Popen(
                    shell.probe_argv(command),
                    cwd=self.workspace_root,
                    env={
                        name: value
                        for name, value in self.parent_env.items()
                        if name
                        in {
                            "HOME",
                            "USER",
                            "LOGNAME",
                            "SHELL",
                            "PATH",
                            "LANG",
                            "LC_ALL",
                            "LC_CTYPE",
                            "TERM",
                        }
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except BaseException:
                attempt.state = _ProbeAttemptState.JOINED
                attempt.condition.notify_all()
                raise
            attempt.process = process
            attempt.state = _ProbeAttemptState.RUNNING
            attempt.condition.notify_all()
        assert process.stdout is not None
        deadline = monotonic() + self.config.shell_snapshot_timeout_seconds
        data = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        reached_eof = False
        try:
            while not reached_eof:
                with self._lock:
                    if self._closed:
                        raise TimeoutError
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                for _key, _mask in selector.select(min(0.05, remaining)):
                    chunk = os.read(process.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        reached_eof = True
                        break
                    data.extend(chunk)
                    if len(data) > _MAXIMUM_SNAPSHOT_BYTES:
                        raise OverflowError
            if process.wait(timeout=max(0.01, deadline - monotonic())) != 0:
                raise ValueError("shell probe exited non-zero")
            # Startup files must not leave background descendants attached to
            # the probe group.  EOF plus an empty process group is the exact
            # physical-success boundary for cache installation.
            if _process_group_exists(process.pid):
                raise ValueError("shell probe left a live process group")
        except BaseException:
            _terminate_and_join(process)
            raise
        finally:
            selector.close()
            process.stdout.close()
            with self._lock:
                attempt.process = None
                attempt.state = _ProbeAttemptState.JOINED
                attempt.condition.notify_all()
        if not data.startswith(_SENTINEL):
            raise ValueError("shell probe sentinel is missing")
        parsed: dict[str, str] = {}
        for item in bytes(data[len(_SENTINEL) :]).split(b"\0"):
            if not item or b"=" not in item:
                continue
            name_raw, value_raw = item.split(b"=", 1)
            name = name_raw.decode("utf-8", "strict")
            value = value_raw.decode("utf-8", "strict")
            if _NAME_RE.fullmatch(name):
                parsed[name] = value
        sanitized, _removed, _secrets = _sanitize_environment(
            parsed,
            allowlist=_INERT_NAMES | self.config.inherit_allowlist,
            passthrough=self.config.passthrough_names,
        )
        return sanitized

    def close(self, *, timeout_seconds: float) -> None:
        deadline = monotonic() + timeout_seconds
        with self._lock:
            self._closed = True
            attempts = tuple(self._attempts.values())
            for attempt in attempts:
                attempt.condition.notify_all()
            for attempt in attempts:
                while not attempt.done:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "terminal environment probe did not physically join"
                        )
                    attempt.condition.wait(remaining)
                if attempt.state is not _ProbeAttemptState.JOINED:
                    raise RuntimeError(
                        "terminal environment probe completed without physical join"
                    )
            self._cache.clear()


def detect_terminal_shell(
    environ: Mapping[str, str] | None = None,
) -> TerminalShellConfig:
    values = environ or os.environ
    candidates = (values.get("SHELL"), "/bin/zsh", "/bin/bash", "/bin/sh")
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return TerminalShellConfig(path.resolve(), path.name)
    raise RuntimeError("no executable terminal shell is available")


def find_nearest_venv_bin(cwd: Path, workspace_root: Path) -> Path | None:
    root = workspace_root.resolve()
    current = cwd.resolve()
    if current != root and root not in current.parents:
        return None
    while True:
        candidate = current / ".venv" / "bin"
        if candidate.is_dir():
            return candidate
        if current == root:
            return None
        current = current.parent


def _sanitize_environment(
    values: Mapping[str, str], *, allowlist: frozenset[str], passthrough: frozenset[str]
) -> tuple[dict[str, str], int, int]:
    result: dict[str, str] = {}
    removed = 0
    secrets = 0
    for name, value in values.items():
        if name in passthrough:
            result[name] = value
            continue
        if name not in allowlist or _ACTIVE_DENY_RE.search(name):
            removed += 1
            continue
        if _SECRET_VALUE_RE.search(value):
            removed += 1
            secrets += 1
            continue
        result[name] = value
    return result, removed, secrets


def _snapshot_key(
    shell: TerminalShellConfig,
    workspace: Path,
    environ: Mapping[str, str],
    config: TerminalEnvConfig,
) -> tuple[object, ...]:
    home = Path(environ.get("HOME", "")).expanduser()
    startup_names = {
        "zsh": (".zshenv", ".zprofile", ".zshrc", ".zlogin"),
        "bash": (".bash_profile", ".bash_login", ".profile", ".bashrc"),
    }.get(shell.name, (".profile",))
    signature: list[tuple[str, int, int]] = []
    for name in startup_names:
        path = home / name
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path), stat.st_mtime_ns, stat.st_size))
    return (
        str(shell.path),
        str(home),
        str(workspace),
        tuple(signature),
        tuple(sorted(config.inherit_allowlist)),
        tuple(sorted(config.passthrough_names)),
        tuple(str(item) for item in config.extra_path_prepends),
        config.venv_overlay,
    )


def _terminate_and_join(
    process: subprocess.Popen[bytes], *, timeout: float = 1.0
) -> None:
    deadline = monotonic() + max(0.01, timeout)
    # The shell may already have exited while a startup-file descendant still
    # owns the probe pipe.  Signal the whole detached group regardless of the
    # leader's Popen state.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=min(0.25, max(0.01, deadline - monotonic())))
    except subprocess.TimeoutExpired:
        pass
    group_grace = min(deadline, monotonic() + 0.25)
    while _process_group_exists(process.pid) and monotonic() < group_grace:
        sleep(0.01)
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.wait(timeout=max(0.01, deadline - monotonic()))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("shell environment probe did not physically join") from exc
    while _process_group_exists(process.pid) and monotonic() < deadline:
        sleep(0.005)
    if _process_group_exists(process.pid):
        raise TimeoutError("shell environment probe group did not physically join")


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _split_path(value: str) -> list[str]:
    return [item for item in value.split(os.pathsep) if item]


def _names(value: str | None) -> frozenset[str]:
    names = frozenset(_csv(value))
    if any(not _NAME_RE.fullmatch(name) for name in names):
        raise ValueError("terminal environment allowlist contains an invalid name")
    return names


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    folded = value.strip().casefold()
    if folded in {"1", "true", "yes", "on"}:
        return True
    if folded in {"0", "false", "no", "off"}:
        return False
    raise ValueError("terminal environment boolean is invalid")


def _positive_float(value: str | None, default: float) -> float:
    parsed = default if value is None else float(value)
    if parsed <= 0:
        raise ValueError("terminal environment duration must be positive")
    return parsed


__all__ = [
    "TerminalEnvConfig",
    "TerminalEnvironment",
    "TerminalEnvironmentOwner",
    "TerminalShellConfig",
    "detect_terminal_shell",
    "find_nearest_venv_bin",
]
