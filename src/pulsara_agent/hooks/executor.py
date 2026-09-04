"""Host-user command execution with bounded pipes and process-group drain."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from time import monotonic
from typing import Mapping

from pulsara_agent.hooks.contracts import (
    FrozenHookDefinition,
    HookDiagnostic,
    HookDispatchScopeRef,
    HookEventType,
    JsonValue,
)
from pulsara_agent.model_input.contracts import (
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
)
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialBoundaryTimedOut,
)


MAXIMUM_HOOK_CAPTURE_BYTES = 1024 * 1024
HOOK_PROCESS_GROUP_ABORT_GRACE_SECONDS = 5.0
SYNCHRONOUS_COMMAND_SLOTS = 16
BACKGROUND_COMMAND_SLOTS = 8
API_KEY_REPLACEMENT = b"[REDACTED_CREDENTIAL]"


@dataclass(slots=True)
class HookSecretScrubSet:
    _values: set[bytes] = field(default_factory=set, repr=False)

    @classmethod
    def capture(cls) -> "HookSecretScrubSet":
        return cls()

    def observe(self, value: str | bytes | None) -> None:
        if not value:
            return
        encoded = value.encode("utf-8") if isinstance(value, str) else value
        if encoded:
            self._values.add(encoded)

    @property
    def values(self) -> tuple[bytes, ...]:
        return tuple(sorted(self._values, key=lambda item: (-len(item), item)))

    def contains(self, raw: bytes | str) -> bool:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        return any(secret in encoded for secret in self.values)

    def scrub_bytes(self, raw: bytes) -> bytes:
        replacement = API_KEY_REPLACEMENT
        if any(secret in replacement for secret in self.values):
            replacement = b""
        value = raw
        for secret in self.values:
            value = value.replace(secret, replacement)
        if any(secret in value for secret in self.values):
            raise ValueError("API key scrub postcondition failed")
        return value

    def scrub_text(self, raw: str) -> str:
        return self.scrub_bytes(raw.encode("utf-8")).decode("utf-8")

    def scrub_json(self, root: JsonValue) -> JsonValue:
        if isinstance(root, str):
            return self.scrub_text(root)
        if isinstance(root, list):
            return [self.scrub_json(item) for item in root]
        if isinstance(root, dict):
            scrubbed: dict[str, JsonValue] = {}
            for key, item in root.items():
                safe_key = self.scrub_text(key)
                if safe_key != key or safe_key in scrubbed:
                    raise ValueError("API key scrub changed a JSON object key")
                scrubbed[safe_key] = self.scrub_json(item)
            return scrubbed
        return root


@dataclass(frozen=True, slots=True)
class HookExecutionRequest:
    definition: FrozenHookDefinition
    public_stdin: Mapping[str, JsonValue]
    scope: HookDispatchScopeRef = field(repr=False, compare=False)
    event_dispatch_ordinal: int
    source_ordinal: int
    cwd: Path
    project_dir: Path | None
    inherited_deadline_monotonic: float | None
    cancellation_signal: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class HookCommandExecution:
    definition: FrozenHookDefinition
    event_dispatch_ordinal: int
    source_ordinal: int
    exit_code: int | None
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    failure_code: str | None = None
    diagnostics: tuple[HookDiagnostic, ...] = ()
    scrub_set: HookSecretScrubSet = field(
        repr=False, compare=False, default_factory=HookSecretScrubSet
    )


class HookCommandExecutor:
    def __init__(self, *, credential_boundary: ProcessCredentialBoundary) -> None:
        self._credential_boundary = credential_boundary
        self._sync_slots = asyncio.Semaphore(SYNCHRONOUS_COMMAND_SLOTS)
        self._background_slots = asyncio.Semaphore(BACKGROUND_COMMAND_SLOTS)
        self._processes: set[asyncio.subprocess.Process] = set()
        self._closed = False
        self._lock = asyncio.Lock()
        self._host_close_deadline: float | None = None
        self._host_close_signal = asyncio.Event()

    def begin_host_close(self, *, deadline_monotonic: float) -> None:
        """Bound ordinary attempts without fencing the later terminal lane."""

        current = self._host_close_deadline
        self._host_close_deadline = (
            deadline_monotonic if current is None else min(current, deadline_monotonic)
        )
        self._host_close_signal.set()

    async def execute(self, request: HookExecutionRequest) -> HookCommandExecution:
        definition = request.definition
        scrub = HookSecretScrubSet.capture()
        now = monotonic()
        effective_deadline = now + definition.timeout_seconds
        if request.inherited_deadline_monotonic is not None:
            effective_deadline = min(
                effective_deadline, request.inherited_deadline_monotonic
            )
        terminal = definition.event_type is HookEventType.SESSION_END_EVENT
        if self._host_close_deadline is not None:
            effective_deadline = min(effective_deadline, self._host_close_deadline)
        slot = self._background_slots if definition.asynchronous else self._sync_slots
        acquired = False
        try:
            remaining = effective_deadline - monotonic()
            if remaining <= 0:
                return _failure(request, scrub, "HOOK_TIMEOUT")
            acquired, close_won = await _acquire_slot(
                slot,
                timeout=remaining,
                host_close_signal=(None if terminal else self._host_close_signal),
            )
            if close_won:
                return _failure(request, scrub, "HOOK_CANCELLED")
            if not acquired:
                return _failure(request, scrub, "HOOK_TIMEOUT")
            if (
                self._closed
                or _signal_is_set(request.cancellation_signal)
                or (not terminal and self._host_close_signal.is_set())
            ):
                return _failure(request, scrub, "HOOK_CANCELLED")
            async with self._credential_boundary.async_guard(
                deadline_monotonic=effective_deadline
            ) as guard:
                scrub.observe(guard.value)
            command = definition.selected_command(windows=sys.platform == "win32")
            overlay = dict(definition.provenance.declaration_environment)
            overlay["PULSARA_HOOK_SOURCE_DIR"] = str(
                definition.provenance.identity.canonical_path.parent
            )
            if request.project_dir is not None:
                overlay["PULSARA_PROJECT_DIR"] = str(request.project_dir)
            if scrub.contains(command) or any(
                scrub.contains(key) or scrub.contains(value)
                for key, value in overlay.items()
            ):
                return _failure(request, scrub, "API_KEY_VALUE_PRESENT")
            try:
                public = scrub.scrub_json(dict(request.public_stdin))
                stdin = json.dumps(
                    public,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError):
                return _failure(request, scrub, "HOOK_STDIN_INVALID")
            if len(stdin) > MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES:
                return _failure(request, scrub, "HOOK_STDIN_BOUND_EXCEEDED")
            environment = _spawn_environment(scrub, overlay)
            return await self._spawn_and_drain(
                request,
                command=command,
                stdin=stdin,
                environment=environment,
                scrub=scrub,
                effective_deadline=effective_deadline,
                host_close_signal=(None if terminal else self._host_close_signal),
            )
        except asyncio.CancelledError:
            raise
        except ProcessCredentialBoundaryTimedOut:
            return _failure(request, scrub, "HOOK_TIMEOUT")
        except Exception:
            return _failure(request, scrub, "HOOK_EXECUTOR_FAILURE")
        finally:
            if acquired:
                slot.release()

    async def _spawn_and_drain(
        self,
        request: HookExecutionRequest,
        *,
        command: str,
        stdin: bytes,
        environment: Mapping[str, str],
        scrub: HookSecretScrubSet,
        effective_deadline: float,
        host_close_signal: asyncio.Event | None,
    ) -> HookCommandExecution:
        creation: dict[str, object] = {}
        if sys.platform == "win32":  # pragma: no cover - Windows branch
            creation["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            creation["start_new_session"] = True
        # Environment construction is not the process-creation boundary: a
        # caller-owned credential may rotate while this attempt waits for
        # scheduling.  The final sink therefore observes the boundary value and
        # rejects the exact reviewed command rather than rewriting it.
        process: asyncio.subprocess.Process | None = None
        cancelled: asyncio.CancelledError | None = None
        try:
            async with self._credential_boundary.async_guard(
                deadline_monotonic=effective_deadline
            ) as guard:
                scrub.observe(guard.value)
                if (
                    guard.contains(command)
                    or guard.contains(stdin)
                    or any(
                        guard.contains(key) or guard.contains(value)
                        for key, value in environment.items()
                    )
                ):
                    return _failure(request, scrub, "API_KEY_VALUE_PRESENT")
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_shell(
                        command,
                        cwd=request.cwd,
                        env=environment,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **creation,
                    ),
                    name="hook-process-spawn-admission",
                )
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError as exc:
                    cancelled = exc
                    process = await asyncio.shield(spawn)
        except ProcessCredentialBoundaryTimedOut:
            return _failure(request, scrub, "HOOK_TIMEOUT")
        except Exception:
            return _failure(request, scrub, "HOOK_SPAWN_FAILED")
        assert process is not None
        if cancelled is not None:
            await _abort_process_group(process, physical_deadline=effective_deadline)
            raise cancelled
        async with self._lock:
            if self._closed:
                await _abort_process_group(
                    process,
                    physical_deadline=monotonic(),
                )
                return _failure(request, scrub, "HOOK_CANCELLED")
            self._processes.add(process)
        overflow = asyncio.Event()
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(
            _bounded_drain(process.stdout, overflow), name="hook-stdout-drain"
        )
        stderr_task = asyncio.create_task(
            _bounded_drain(process.stderr, overflow), name="hook-stderr-drain"
        )
        wait_task = asyncio.create_task(process.wait(), name="hook-process-wait")
        overflow_task = asyncio.create_task(overflow.wait(), name="hook-overflow-wait")
        cancel_tasks = tuple(
            task
            for task in (
                _cancellation_wait_task(request.cancellation_signal),
                _cancellation_wait_task(host_close_signal),
            )
            if task is not None
        )
        stdin_task = asyncio.create_task(
            _write_stdin(process, stdin), name="hook-stdin-write"
        )
        try:
            failure_code: str | None = None
            while not wait_task.done():
                remaining = max(0.0, effective_deadline - monotonic())
                if not remaining:
                    failure_code = "HOOK_TIMEOUT"
                    break
                watched = {wait_task, overflow_task}
                if not stdin_task.done():
                    watched.add(stdin_task)
                watched.update(cancel_tasks)
                done, _pending = await asyncio.wait(
                    watched,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    failure_code = "HOOK_TIMEOUT"
                    break
                if overflow_task in done and overflow.is_set():
                    failure_code = "HOOK_CAPTURE_BOUND_EXCEEDED"
                    break
                if any(task in done for task in cancel_tasks):
                    failure_code = "HOOK_CANCELLED"
                    break
                if stdin_task in done:
                    stdin_failure = stdin_task.exception()
                    if stdin_failure is not None:
                        failure_code = "HOOK_STDIN_WRITE_FAILED"
                        break
            if failure_code is not None:
                inherited = request.inherited_deadline_monotonic
                physical_deadline = (
                    effective_deadline + HOOK_PROCESS_GROUP_ABORT_GRACE_SECONDS
                )
                if inherited is not None:
                    physical_deadline = min(physical_deadline, inherited)
                if (
                    host_close_signal is not None
                    and self._host_close_deadline is not None
                ):
                    physical_deadline = min(
                        physical_deadline, self._host_close_deadline
                    )
                await _abort_process_group(process, physical_deadline=physical_deadline)
            else:
                await wait_task
            if not stdin_task.done():
                stdin_task.cancel()
            await asyncio.gather(stdin_task, return_exceptions=True)
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            if overflow.is_set() and failure_code is None:
                failure_code = "HOOK_CAPTURE_BOUND_EXCEEDED"
            return HookCommandExecution(
                request.definition,
                request.event_dispatch_ordinal,
                request.source_ordinal,
                process.returncode,
                stdout,
                stderr,
                failure_code,
                () if failure_code is None else (_diagnostic(request, failure_code),),
                scrub,
            )
        except BaseException:
            inherited = request.inherited_deadline_monotonic
            physical_deadline = monotonic() + HOOK_PROCESS_GROUP_ABORT_GRACE_SECONDS
            if inherited is not None:
                physical_deadline = min(physical_deadline, inherited)
            if host_close_signal is not None and self._host_close_deadline is not None:
                physical_deadline = min(physical_deadline, self._host_close_deadline)
            await asyncio.shield(
                _abort_process_group(process, physical_deadline=physical_deadline)
            )
            stdin_task.cancel()
            await asyncio.shield(
                asyncio.gather(
                    stdin_task, stdout_task, stderr_task, return_exceptions=True
                )
            )
            raise
        finally:
            wait_task.cancel()
            overflow_task.cancel()
            stdin_task.cancel()
            for cancel_task in cancel_tasks:
                cancel_task.cancel()
            async with self._lock:
                self._processes.discard(process)

    async def aclose(self, *, deadline_monotonic: float | None = None) -> None:
        async with self._lock:
            self._closed = True
            processes = tuple(self._processes)
        physical_deadline = monotonic() + HOOK_PROCESS_GROUP_ABORT_GRACE_SECONDS
        if deadline_monotonic is not None:
            physical_deadline = min(physical_deadline, deadline_monotonic)
        await asyncio.gather(
            *(
                _abort_process_group(process, physical_deadline=physical_deadline)
                for process in processes
            ),
            return_exceptions=True,
        )


async def _bounded_drain(
    reader: asyncio.StreamReader, overflow: asyncio.Event
) -> bytes:
    chunks: list[bytes] = []
    retained = 0
    while True:
        chunk = await reader.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        if retained + len(chunk) <= MAXIMUM_HOOK_CAPTURE_BYTES:
            chunks.append(chunk)
            retained += len(chunk)
        else:
            overflow.set()


async def _write_stdin(process: asyncio.subprocess.Process, stdin: bytes) -> None:
    assert process.stdin is not None
    try:
        process.stdin.write(stdin)
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        # A command may deliberately exit without consuming all stdin. Its
        # process exit and bounded captures remain the authoritative outcome.
        process.stdin.close()


async def _acquire_slot(
    slot: asyncio.Semaphore,
    *,
    timeout: float,
    host_close_signal: asyncio.Event | None,
) -> tuple[bool, bool]:
    acquire = asyncio.create_task(slot.acquire(), name="hook-slot-admission")
    close = _cancellation_wait_task(host_close_signal)
    watched = {acquire} | ({close} if close is not None else set())
    try:
        done, _pending = await asyncio.wait(
            watched,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if close is not None and close in done:
            if acquire in done:
                slot.release()
            else:
                acquire.cancel()
                await asyncio.gather(acquire, return_exceptions=True)
            return False, True
        if acquire in done:
            return True, False
        acquire.cancel()
        await asyncio.gather(acquire, return_exceptions=True)
        return False, False
    finally:
        if not acquire.done():
            acquire.cancel()
            await asyncio.gather(acquire, return_exceptions=True)
        if close is not None:
            close.cancel()


async def _abort_process_group(
    process: asyncio.subprocess.Process, *, physical_deadline: float
) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    _signal_process_group(process, terminate=True)
    remaining = max(0.0, physical_deadline - monotonic())
    if remaining:
        try:
            await asyncio.wait_for(process.wait(), timeout=remaining)
            return
        except TimeoutError:
            pass
    _signal_process_group(process, terminate=False)
    await process.wait()


def _signal_process_group(
    process: asyncio.subprocess.Process, *, terminate: bool
) -> None:
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows branch
            process.terminate() if terminate else process.kill()
        else:
            os.killpg(process.pid, signal.SIGTERM if terminate else signal.SIGKILL)
    except ProcessLookupError:
        pass


def _spawn_environment(
    scrub: HookSecretScrubSet, overlay: Mapping[str, str]
) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        safe_key = scrub.scrub_text(key)
        if safe_key != key:
            continue
        safe_value = scrub.scrub_text(value)
        if safe_value != value:
            continue
        environment[key] = safe_value
    environment.update(overlay)
    return environment


def _signal_is_set(signal_value: object | None) -> bool:
    method = getattr(signal_value, "is_set", None)
    return bool(method()) if callable(method) else False


def _cancellation_wait_task(signal_value: object | None) -> asyncio.Task[object] | None:
    method = getattr(signal_value, "wait", None)
    if not callable(method):
        return None
    return asyncio.create_task(method(), name="hook-owner-cancellation")


def _diagnostic(request: HookExecutionRequest, code: str) -> HookDiagnostic:
    return HookDiagnostic(
        code,
        code,
        event_type=request.definition.event_type,
        source_label=request.definition.provenance.display_label,
        event_dispatch_ordinal=request.event_dispatch_ordinal,
        source_ordinal=request.source_ordinal,
        definition_ordinal=request.definition.source_local_definition_ordinal,
    )


def _failure(
    request: HookExecutionRequest,
    scrub: HookSecretScrubSet,
    code: str,
) -> HookCommandExecution:
    return HookCommandExecution(
        request.definition,
        request.event_dispatch_ordinal,
        request.source_ordinal,
        None,
        b"",
        b"",
        code,
        (_diagnostic(request, code),),
        scrub,
    )


__all__ = [
    "HookCommandExecution",
    "HookCommandExecutor",
    "HookExecutionRequest",
    "HookSecretScrubSet",
]
