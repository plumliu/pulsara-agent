"""Resolve and verify the separately built Go terminal client executable."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from pulsara_agent.terminal_protocol.codec import (
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    PROTOCOL_SCHEMA_FINGERPRINT,
)


# The probe is a separately started executable and may contend with PostgreSQL
# and compiler workers during Host startup.  Keep it bounded, but do not make
# ordinary scheduler delay look like a protocol incompatibility.
_VERSION_QUERY_TIMEOUT_SECONDS = 10.0
_MAXIMUM_VERSION_OUTPUT_BYTES = 16 * 1024
_MAXIMUM_VERSION_ERROR_BYTES = 2 * 1024
_VERSION_PROCESS_GRACE_SECONDS = 1.0


class TerminalClientBinaryError(RuntimeError):
    """The Go client is absent, unsafe, or incompatible with this server."""


@dataclass(frozen=True, slots=True)
class TerminalClientBinary:
    path: Path
    version: str
    commit: str
    protocol_major: int
    protocol_minor: int
    schema_fingerprint: str
    dependency_lock_fingerprint: str
    go_version: str
    goos: str
    goarch: str


async def resolve_terminal_client_binary(
    explicit_path: Path | str | None = None,
) -> TerminalClientBinary:
    """Return the single verified client binary; never silently fall back."""

    candidate = _resolve_candidate(explicit_path)
    _validate_physical_binary(candidate)
    try:
        process = await asyncio.create_subprocess_exec(
            str(candidate),
            "--version-json",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            _communicate_version_process(process),
            timeout=_VERSION_QUERY_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        if "process" in locals():
            await _reap_version_process(process)
        raise TerminalClientBinaryError(
            "terminal client version verification timed out"
        ) from exc
    except TerminalClientBinaryError:
        if "process" in locals():
            await _reap_version_process(process)
        raise
    except OSError as exc:
        if "process" in locals():
            await _reap_version_process(process)
        raise TerminalClientBinaryError(
            "terminal client version verification could not start"
        ) from exc
    if process.returncode != 0:
        detail = stderr[:512].decode("utf-8", errors="replace").strip()
        raise TerminalClientBinaryError(
            f"terminal client version verification failed: {detail or process.returncode}"
        )
    if len(stdout) == 0 or len(stdout) > _MAXIMUM_VERSION_OUTPUT_BYTES:
        raise TerminalClientBinaryError("terminal client version output is invalid")
    try:
        payload = json.loads(stdout)
        identity = TerminalClientBinary(
            path=candidate,
            version=str(payload["version"]),
            commit=str(payload["commit"]),
            protocol_major=int(payload["protocol_major"]),
            protocol_minor=int(payload["protocol_minor"]),
            schema_fingerprint=str(payload["schema_fingerprint"]),
            dependency_lock_fingerprint=str(payload["dependency_lock_fingerprint"]),
            go_version=str(payload["go_version"]),
            goos=str(payload["goos"]),
            goarch=str(payload["goarch"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TerminalClientBinaryError(
            "terminal client version output is malformed"
        ) from exc
    if (
        identity.protocol_major != PROTOCOL_MAJOR
        or identity.protocol_minor != PROTOCOL_MINOR
        or identity.schema_fingerprint != PROTOCOL_SCHEMA_FINGERPRINT
    ):
        raise TerminalClientBinaryError(
            "terminal client protocol identity does not match the Python server"
        )
    return identity


def _resolve_candidate(explicit_path: Path | str | None) -> Path:
    if explicit_path is not None:
        return Path(os.path.abspath(Path(explicit_path).expanduser()))
    configured = os.environ.get("PULSARA_TUI_BINARY")
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser()))
    scripts = Path(sysconfig.get_path("scripts"))
    installed = scripts / "pulsara-tui"
    if installed.exists():
        return Path(os.path.abspath(installed))
    raise TerminalClientBinaryError(
        "pulsara-tui is not installed; set PULSARA_TUI_BINARY to an explicit verified build"
    )


def _validate_physical_binary(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise TerminalClientBinaryError(
            f"terminal client binary not found: {path}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TerminalClientBinaryError("terminal client path must be a regular file")
    if metadata.st_uid != os.geteuid():
        raise TerminalClientBinaryError("terminal client binary has a foreign owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise TerminalClientBinaryError(
            "terminal client binary must not be group/world writable"
        )
    if not os.access(path, os.X_OK):
        raise TerminalClientBinaryError("terminal client binary is not executable")


async def _communicate_version_process(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise TerminalClientBinaryError(
            "terminal client version verification pipes are unavailable"
        )
    stdout_task = asyncio.create_task(
        _read_bounded_stream(
            process.stdout,
            maximum_bytes=_MAXIMUM_VERSION_OUTPUT_BYTES,
            label="output",
        )
    )
    stderr_task = asyncio.create_task(
        _read_bounded_stream(
            process.stderr,
            maximum_bytes=_MAXIMUM_VERSION_ERROR_BYTES,
            label="error output",
        )
    )
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        stdout, stderr, _ = await asyncio.gather(*tasks)
        return stdout, stderr
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _read_bounded_stream(
    stream: asyncio.StreamReader,
    *,
    maximum_bytes: int,
    label: str,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(4096, maximum_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum_bytes:
            raise TerminalClientBinaryError(
                f"terminal client version {label} exceeds its hard bound"
            )


async def _reap_version_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        await asyncio.shield(process.wait())
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(process.wait()), timeout=_VERSION_PROCESS_GRACE_SECONDS
        )
        return
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await asyncio.shield(process.wait())


__all__ = [
    "TerminalClientBinary",
    "TerminalClientBinaryError",
    "resolve_terminal_client_binary",
]
