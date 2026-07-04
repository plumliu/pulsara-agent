"""Environment construction for local terminal subprocesses."""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from pulsara_agent.runtime.terminal.shell import TerminalShellConfig


SANE_FALLBACK_PATH = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

BASE_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "PATH",
        "SSH_AUTH_SOCK",
        "XAUTHORITY",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
        "XDG_CURRENT_DESKTOP",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)

TOOLCHAIN_ENV_ALLOWLIST = frozenset(
    {
        "NVM_DIR",
        "VOLTA_HOME",
        "PNPM_HOME",
        "BUN_INSTALL",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "PYENV_ROOT",
        "RBENV_ROOT",
        "ASDF_DIR",
        "MISE_DATA_DIR",
        "MISE_CONFIG_DIR",
        "MISE_CACHE_DIR",
        "HOMEBREW_PREFIX",
        "HOMEBREW_CELLAR",
        "HOMEBREW_REPOSITORY",
        "GOPATH",
        "GOROOT",
    }
)

DEFAULT_ENV_ALLOWLIST = BASE_ENV_ALLOWLIST | TOOLCHAIN_ENV_ALLOWLIST

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHELL_SNAPSHOT_SENTINEL = "__PULSARA_ENV_START__"
_MAX_SHELL_SNAPSHOT_BYTES = 1_000_000

_SECRET_VALUE_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]

_PATH_STRUCTURAL_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SSH_AUTH_SOCK",
        "XAUTHORITY",
    }
)


class _SnapshotOutputTooLarge(RuntimeError):
    """Raised when a login-shell snapshot emits more data than v1 will parse."""


@dataclass(frozen=True, slots=True)
class TerminalEnvConfig:
    enable_shell_snapshot: bool = True
    shell_snapshot_ttl_seconds: float = 300.0
    shell_snapshot_timeout_seconds: float = 5.0
    inherit_allowlist: frozenset[str] = frozenset()
    passthrough_names: frozenset[str] = frozenset()
    extra_path_prepends: tuple[Path, ...] = ()
    enable_venv_overlay: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TerminalEnvConfig":
        env = env or os.environ
        return cls(
            enable_shell_snapshot=_env_bool(env.get("PULSARA_TERMINAL_SHELL_SNAPSHOT"), default=True),
            shell_snapshot_ttl_seconds=_env_float(
                env.get("PULSARA_TERMINAL_SHELL_SNAPSHOT_TTL_SECONDS"),
                default=300.0,
                minimum=0.0,
            ),
            shell_snapshot_timeout_seconds=_env_float(
                env.get("PULSARA_TERMINAL_SHELL_SNAPSHOT_TIMEOUT_SECONDS"),
                default=5.0,
                minimum=0.1,
            ),
            inherit_allowlist=_parse_env_names(env.get("PULSARA_TERMINAL_ENV_INHERIT_ALLOWLIST", "")),
            passthrough_names=_parse_env_names(env.get("PULSARA_TERMINAL_ENV_PASSTHROUGH_NAMES", "")),
            extra_path_prepends=_parse_paths(env.get("PULSARA_TERMINAL_EXTRA_PATH_PREPENDS", "")),
            enable_venv_overlay=_env_bool(env.get("PULSARA_TERMINAL_VENV_OVERLAY"), default=True),
        )


@dataclass(frozen=True, slots=True)
class TerminalEnvSnapshot:
    env: dict[str, str]
    created_at: float
    shell_path: Path
    source: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalEnvBuildResult:
    env: dict[str, str]
    diagnostics: dict[str, object]


@dataclass(slots=True)
class _SnapshotCacheEntry:
    key: tuple[object, ...]
    snapshot: TerminalEnvSnapshot


@dataclass(slots=True)
class TerminalEnvBuilder:
    config: TerminalEnvConfig = field(default_factory=TerminalEnvConfig.from_env)
    parent_env: Mapping[str, str] | None = None
    time_fn: Callable[[], float] = time.monotonic
    # Shared by sessions in today's synchronous tool loop; add a lock before enabling concurrent tool execution.
    _snapshot_cache: _SnapshotCacheEntry | None = field(default=None, init=False, repr=False)

    def build(
        self,
        *,
        cwd: Path,
        workspace_root: Path,
        shell: TerminalShellConfig,
    ) -> TerminalEnvBuildResult:
        parent_env = self.parent_env if self.parent_env is not None else os.environ
        sanitized_parent, parent_diag = sanitize_subprocess_env(
            parent_env,
            config=self.config,
        )
        snapshot = self._snapshot(shell=shell, parent_env=sanitized_parent, workspace_root=workspace_root)

        env = dict(sanitized_parent)
        snapshot_path = ""
        if snapshot is not None:
            snapshot_path = snapshot.env.get("PATH", "")
            for name, value in snapshot.env.items():
                if name != "PATH":
                    env[name] = value

        parent_path = sanitized_parent.get("PATH", "")
        venv_overlay = (
            find_nearest_venv_bin(cwd, workspace_root) if self.config.enable_venv_overlay else None
        )
        path_entries = merge_path_entries(
            [
                *(str(path) for path in ([venv_overlay] if venv_overlay is not None else [])),
                *(str(path) for path in self.config.extra_path_prepends if path.exists()),
                *split_path(snapshot_path),
                *split_path(parent_path),
                *SANE_FALLBACK_PATH,
            ]
        )
        env["PATH"] = os.pathsep.join(path_entries)

        diagnostics: dict[str, object] = {
            **parent_diag,
            "shell_snapshot_used": bool(snapshot and snapshot.error is None),
            "shell_snapshot_error": snapshot.error if snapshot and snapshot.error else None,
            "venv_overlay": str(venv_overlay) if venv_overlay is not None else None,
            "path_entries_count": len(path_entries),
        }
        return TerminalEnvBuildResult(env=env, diagnostics=diagnostics)

    def _snapshot(
        self,
        *,
        shell: TerminalShellConfig,
        parent_env: Mapping[str, str],
        workspace_root: Path,
    ) -> TerminalEnvSnapshot | None:
        if not self.config.enable_shell_snapshot:
            return None
        now = self.time_fn()
        key = self._snapshot_cache_key(shell=shell, parent_env=parent_env, workspace_root=workspace_root)
        if self._snapshot_cache is not None:
            cached = self._snapshot_cache.snapshot
            if self._snapshot_cache.key == key and now - cached.created_at <= self.config.shell_snapshot_ttl_seconds:
                return cached
        snapshot = capture_shell_env_snapshot(
            shell=shell,
            parent_env=parent_env,
            config=self.config,
            now=now,
        )
        self._snapshot_cache = _SnapshotCacheEntry(key=key, snapshot=snapshot)
        return snapshot

    def _snapshot_cache_key(
        self,
        *,
        shell: TerminalShellConfig,
        parent_env: Mapping[str, str],
        workspace_root: Path,
    ) -> tuple[object, ...]:
        home = parent_env.get("HOME", "")
        allowed_names = DEFAULT_ENV_ALLOWLIST | self.config.inherit_allowlist | self.config.passthrough_names
        safe_signature = tuple(sorted((name, parent_env.get(name, "")) for name in allowed_names))
        return (
            str(shell.path),
            home,
            str(workspace_root),
            _startup_file_signature(shell, home),
            safe_signature,
            tuple(sorted(self.config.inherit_allowlist)),
            tuple(sorted(self.config.passthrough_names)),
        )


def sanitize_subprocess_env(
    parent_env: Mapping[str, str],
    *,
    config: TerminalEnvConfig | None = None,
) -> tuple[dict[str, str], dict[str, object]]:
    config = config or TerminalEnvConfig(enable_shell_snapshot=False)
    allowed = DEFAULT_ENV_ALLOWLIST | config.inherit_allowlist | config.passthrough_names
    result: dict[str, str] = {}
    removed_count = 0
    secret_value_removed_count = 0
    for name, value in parent_env.items():
        if name == "PWD":
            removed_count += 1
            continue
        if name not in allowed:
            removed_count += 1
            continue
        if (
            name not in config.passthrough_names
            and not _is_path_structural_env_name(name)
            and _looks_like_secret_value(value)
        ):
            secret_value_removed_count += 1
            removed_count += 1
            continue
        result[name] = value
    return result, {
        "sanitized_env_removed_count": removed_count,
        "sanitized_env_secret_value_removed_count": secret_value_removed_count,
    }


def build_default_subprocess_env() -> dict[str, str]:
    env, _ = sanitize_subprocess_env(os.environ, config=TerminalEnvConfig(enable_shell_snapshot=False))
    if not env.get("PATH"):
        env["PATH"] = os.pathsep.join(SANE_FALLBACK_PATH)
    return env


def capture_shell_env_snapshot(
    *,
    shell: TerminalShellConfig,
    parent_env: Mapping[str, str],
    config: TerminalEnvConfig,
    now: float | None = None,
) -> TerminalEnvSnapshot:
    now = time.monotonic() if now is None else now
    if not shell.path.is_absolute() or not _is_executable_file(shell.path):
        return TerminalEnvSnapshot(
            env={},
            created_at=now,
            shell_path=shell.path,
            source="login_shell",
            error="shell path is not an executable absolute path",
        )
    probe_shell = TerminalShellConfig(path=shell.path, login=True, interactive_init=True)
    command = f"printf '%s\\0' {_SHELL_SNAPSHOT_SENTINEL}; env -0"
    try:
        proc = subprocess.Popen(
            probe_shell.argv(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=dict(parent_env),
            close_fds=True,
            start_new_session=True,
        )
        try:
            stdout = _read_bounded_stdout(proc, timeout_seconds=config.shell_snapshot_timeout_seconds)
            if proc.returncode is None:
                proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return TerminalEnvSnapshot(
                env={},
                created_at=now,
                shell_path=shell.path,
                source="login_shell",
                error="shell snapshot timed out",
            )
        except _SnapshotOutputTooLarge:
            proc.kill()
            proc.communicate()
            return TerminalEnvSnapshot(
                env={},
                created_at=now,
                shell_path=shell.path,
                source="login_shell",
                error="shell snapshot exceeded max output size",
            )
    except OSError as exc:
        return TerminalEnvSnapshot(
            env={},
            created_at=now,
            shell_path=shell.path,
            source="login_shell",
            error=f"shell snapshot failed: {exc}",
        )
    if proc.returncode != 0:
        return TerminalEnvSnapshot(
            env={},
            created_at=now,
            shell_path=shell.path,
            source="login_shell",
            error=f"shell snapshot exited with {proc.returncode}",
        )
    raw_env = _parse_env0_after_sentinel(stdout)
    sanitized, _ = sanitize_subprocess_env(raw_env, config=config)
    return TerminalEnvSnapshot(
        env=sanitized,
        created_at=now,
        shell_path=shell.path,
        source="login_shell",
        error=None,
    )


def find_nearest_venv_bin(cwd: Path, workspace_root: Path) -> Path | None:
    try:
        current = cwd.expanduser().resolve()
        root = workspace_root.expanduser().resolve()
    except OSError:
        return None
    while current == root or root in current.parents:
        candidate = current / ".venv" / "bin"
        if candidate.is_dir():
            return candidate
        if current == root:
            break
        current = current.parent
    return None


def split_path(value: str) -> list[str]:
    return [entry for entry in value.split(os.pathsep) if entry]


def merge_path_entries(entries: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for raw in entries:
        entry = raw.strip()
        if not entry or entry in seen:
            continue
        seen.add(entry)
        merged.append(entry)
    return merged


def _parse_env0_after_sentinel(stdout: bytes) -> dict[str, str]:
    parts = stdout.split(b"\0")
    try:
        start = parts.index(_SHELL_SNAPSHOT_SENTINEL.encode("utf-8")) + 1
    except ValueError:
        start = 0
    result: dict[str, str] = {}
    for part in parts[start:]:
        if not part or b"=" not in part:
            continue
        name_bytes, value_bytes = part.split(b"=", 1)
        name = name_bytes.decode("utf-8", errors="replace")
        if not _ENV_NAME_RE.fullmatch(name):
            continue
        result[name] = value_bytes.decode("utf-8", errors="replace")
    return result


def _read_bounded_stdout(proc: subprocess.Popen[bytes], *, timeout_seconds: float) -> bytes:
    if proc.stdout is None:
        return b""
    deadline = time.monotonic() + timeout_seconds
    chunks: list[bytes] = []
    total = 0
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            if proc.poll() is not None:
                remaining = proc.stdout.read()
                if remaining:
                    chunks.append(remaining)
                    total += len(remaining)
                if total > _MAX_SHELL_SNAPSHOT_BYTES:
                    raise _SnapshotOutputTooLarge
                return b"".join(chunks)
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout_seconds)
            for key, _ in selector.select(timeout=remaining_time):
                chunk = key.fileobj.read1(8192)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_SHELL_SNAPSHOT_BYTES:
                    raise _SnapshotOutputTooLarge
    finally:
        selector.close()


def _looks_like_secret_value(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def _is_path_structural_env_name(name: str) -> bool:
    return (
        name in _PATH_STRUCTURAL_NAMES
        or name.endswith("_DIR")
        or name.endswith("_ROOT")
        or name.endswith("_HOME")
        or name.startswith("XDG_")
    )


def _startup_file_signature(shell: TerminalShellConfig, home: str) -> tuple[tuple[str, int | None, int | None], ...]:
    if not home:
        return ()
    home_path = Path(home).expanduser()
    names = _startup_file_names(shell.name)
    signature: list[tuple[str, int | None, int | None]] = []
    for name in names:
        path = home_path / name
        try:
            stat = path.stat()
        except OSError:
            signature.append((str(path), None, None))
        else:
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def _startup_file_names(shell_name: str) -> tuple[str, ...]:
    if shell_name == "zsh":
        return (".zshenv", ".zprofile", ".zshrc", ".zlogin")
    if shell_name == "bash":
        return (".bash_profile", ".bash_login", ".profile", ".bashrc")
    if shell_name == "ksh":
        return (".profile", ".kshrc")
    return (".profile",)


def _parse_env_names(value: str) -> frozenset[str]:
    names = {
        item.strip()
        for item in re.split(r"[,:\s]+", value)
        if item.strip() and _ENV_NAME_RE.fullmatch(item.strip())
    }
    return frozenset(names)


def _parse_paths(value: str) -> tuple[Path, ...]:
    return tuple(Path(item).expanduser() for item in value.split(os.pathsep) if item.strip())


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    return default


def _env_float(value: str | None, *, default: float, minimum: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed >= minimum else default


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False
