"""One process-local linearization gate for one caller-owned credential value.

The boundary is deliberately narrow: it is neither a credential store nor a
service locator.  Application bootstrap constructs one instance and injects it
into every irreversible package/process/network/provider admission owner.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import inspect
import os
from threading import Lock
from time import monotonic
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Protocol

import httpx


PROCESS_CREDENTIAL_REPLACEMENT = b"[REDACTED_CREDENTIAL]"


class CredentialCancellationPort(Protocol):
    def cancellation_requested(self) -> bool: ...


class ProcessCredentialBoundaryCancelled(RuntimeError):
    pass


class ProcessCredentialBoundaryTimedOut(TimeoutError):
    pass


@dataclass(slots=True)
class _HttpAdmissionAttempt:
    boundary: "ProcessCredentialBoundary"
    guard: "ProcessCredentialGuard"
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    claimed: bool = False

    def claim_first_physical_request(self) -> bool:
        if self.claimed:
            return False
        self.claimed = True
        return True


_CURRENT_HTTP_ADMISSION: ContextVar[_HttpAdmissionAttempt | None] = ContextVar(
    "pulsara-current-http-admission", default=None
)
_ORIGINAL_HTTP_TRACE_EXTENSION = "pulsara_original_http_trace"


@dataclass(frozen=True, slots=True)
class ProcessCredentialGuard:
    """Gate-owned current value observation."""

    value: str

    @property
    def nonempty_bytes(self) -> bytes | None:
        return os.fsencode(self.value) if self.value else None

    def contains(self, raw: bytes | str) -> bool:
        if not self.value:
            return False
        encoded = os.fsencode(raw) if isinstance(raw, str) else raw
        return os.fsencode(self.value) in encoded


@dataclass(slots=True)
class ProcessCredentialScrubSet:
    """Attempt-local exact values observed across rotations."""

    _values: set[bytes] = field(default_factory=set, repr=False)

    def observe(self, value: str | bytes | None) -> None:
        if not value:
            return
        encoded = os.fsencode(value) if isinstance(value, str) else value
        if encoded:
            self._values.add(encoded)

    @property
    def values(self) -> tuple[bytes, ...]:
        return tuple(sorted(self._values, key=lambda item: (-len(item), item)))

    def contains(self, raw: bytes | str) -> bool:
        encoded = os.fsencode(raw) if isinstance(raw, str) else raw
        return any(secret in encoded for secret in self.values)

    def scrub_bytes(self, raw: bytes) -> bytes:
        replacement = PROCESS_CREDENTIAL_REPLACEMENT
        if any(secret in replacement for secret in self.values):
            replacement = b""
        value = raw
        for secret in self.values:
            value = value.replace(secret, replacement)
        if any(secret in value for secret in self.values):
            raise ValueError("credential scrub postcondition failed")
        return value

    def scrub_text(self, raw: str) -> str:
        return os.fsdecode(self.scrub_bytes(os.fsencode(raw)))

    def scrub_json(self, root: object) -> object:
        if isinstance(root, str):
            return self.scrub_text(root)
        if isinstance(root, list):
            return [self.scrub_json(item) for item in root]
        if isinstance(root, tuple):
            return tuple(self.scrub_json(item) for item in root)
        if isinstance(root, dict):
            scrubbed: dict[object, object] = {}
            for key, item in root.items():
                safe_key: object = self.scrub_text(key) if isinstance(key, str) else key
                if safe_key != key or safe_key in scrubbed:
                    raise ValueError("credential scrub changed an object key")
                scrubbed[safe_key] = self.scrub_json(item)
            return scrubbed
        return root


class ProcessCredentialBoundary:
    """One non-reentrant threading gate shared by sync and async sinks."""

    def __init__(self, value: str = "") -> None:
        if not isinstance(value, str):
            raise TypeError("credential boundary value must be text")
        self._gate = Lock()
        self._value = value
        self._snapshot = value

    @property
    def last_boundary_snapshot(self) -> str:
        """Diagnostic-free process-local value; never serialize or log it."""

        return self._snapshot

    def capture_scrub_set(
        self,
        *,
        deadline_monotonic: float | None = None,
        cancellation: CredentialCancellationPort | None = None,
    ) -> ProcessCredentialScrubSet:
        """Observe the caller-provided exact value under the shared gate."""

        scrub = ProcessCredentialScrubSet()
        with self.sync_guard(
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        ) as guard:
            scrub.observe(guard.value)
        return scrub

    @contextmanager
    def sync_guard(
        self,
        *,
        deadline_monotonic: float | None = None,
        cancellation: CredentialCancellationPort | None = None,
    ) -> Iterator[ProcessCredentialGuard]:
        self._acquire(
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )
        try:
            value = self._value
            self._snapshot = value
            yield ProcessCredentialGuard(value)
        finally:
            self._gate.release()

    @asynccontextmanager
    async def async_guard(
        self,
        *,
        deadline_monotonic: float | None = None,
        cancellation: CredentialCancellationPort | None = None,
    ) -> AsyncIterator[ProcessCredentialGuard]:
        # The worker may already be queued in ``threading.Lock.acquire`` when
        # its caller is cancelled.  Shield and join it, then immediately
        # release an acquired token before propagating cancellation.
        task = asyncio.create_task(
            asyncio.to_thread(
                self._acquire,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            ),
            name="pulsara-credential-gate-acquire",
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated caller cancellation still cannot detach the queued
            # thread.  A boundary timeout/cancel discovered by that thread is
            # deliberately discarded here so it cannot mask the caller's
            # original ``CancelledError``.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except (ProcessCredentialBoundaryCancelled, ProcessCredentialBoundaryTimedOut):
                    break
            if task.done() and not task.cancelled():
                try:
                    task.result()
                except (
                    ProcessCredentialBoundaryCancelled,
                    ProcessCredentialBoundaryTimedOut,
                ):
                    pass
                else:
                    self._gate.release()
            raise
        try:
            value = self._value
            self._snapshot = value
            yield ProcessCredentialGuard(value)
        finally:
            self._gate.release()

    def rotate_sync(
        self,
        value: str | None,
        *,
        deadline_monotonic: float | None = None,
        cancellation: CredentialCancellationPort | None = None,
    ) -> None:
        with self.sync_guard(
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        ):
            self._value = value or ""
            self._snapshot = self._value

    async def rotate_async(
        self,
        value: str | None,
        *,
        deadline_monotonic: float | None = None,
        cancellation: CredentialCancellationPort | None = None,
    ) -> None:
        async with self.async_guard(
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        ):
            self._value = value or ""
            self._snapshot = self._value

    def _acquire(
        self,
        *,
        deadline_monotonic: float | None,
        cancellation: CredentialCancellationPort | None,
    ) -> None:
        while True:
            if cancellation is not None and cancellation.cancellation_requested():
                raise ProcessCredentialBoundaryCancelled
            if deadline_monotonic is not None:
                remaining = deadline_monotonic - monotonic()
                if remaining <= 0:
                    raise ProcessCredentialBoundaryTimedOut
                wait = min(remaining, 0.05)
            else:
                wait = 0.05
            if self._gate.acquire(timeout=wait):
                # A cancel/deadline that won while the syscall completed must
                # not retain the token or enter an irreversible sink.
                if cancellation is not None and cancellation.cancellation_requested():
                    self._gate.release()
                    raise ProcessCredentialBoundaryCancelled
                if (
                    deadline_monotonic is not None
                    and monotonic() >= deadline_monotonic
                ):
                    self._gate.release()
                    raise ProcessCredentialBoundaryTimedOut
                return


class ProcessCredentialBoundAsyncClient(httpx.AsyncClient):
    """HTTPX client whose every physical request crosses the shared key gate."""

    def __init__(
        self,
        *,
        credential_boundary: ProcessCredentialBoundary,
        credential_header_names: frozenset[bytes] = frozenset(),
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._credential_boundary = credential_boundary
        self._credential_header_names = frozenset(
            item.lower() for item in credential_header_names
        )

    async def _send_single_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        inherited = _CURRENT_HTTP_ADMISSION.get()
        use_inherited = (
            inherited is not None
            and inherited.boundary is self._credential_boundary
            and inherited.claim_first_physical_request()
        )
        guard_context = None
        released = False
        if use_inherited:
            assert inherited is not None
            guard = inherited.guard
        else:
            guard_context = self._credential_boundary.async_guard()
            guard = await guard_context.__aenter__()

        async def settle_admission() -> None:
            nonlocal released
            if use_inherited:
                assert inherited is not None
                inherited.settled.set()
                return
            if released:
                return
            released = True
            assert guard_context is not None
            await guard_context.__aexit__(None, None, None)

        try:
            _validate_http_request_secret_boundary(
                request,
                guard,
                credential_header_names=self._credential_header_names,
            )
            original_trace = request.extensions.get(
                _ORIGINAL_HTTP_TRACE_EXTENSION,
                request.extensions.get("trace"),
            )
            request.extensions[_ORIGINAL_HTTP_TRACE_EXTENSION] = original_trace

            async def trace(name: str, info: dict[str, object]) -> None:
                if name.endswith("send_request_body.complete") or name.endswith(
                    ".failed"
                ):
                    await settle_admission()
                if original_trace is not None:
                    value = original_trace(name, info)
                    if inspect.isawaitable(value):
                        await value

            request.extensions["trace"] = trace
            return await super()._send_single_request(request)
        finally:
            # Mock/custom transports may not implement httpcore trace events.
            # Their return/raise still settles FULL/not-FULL before this release.
            await settle_admission()


async def admit_process_credential_http_operation(
    *,
    credential_boundary: ProcessCredentialBoundary,
    guarded_values: tuple[bytes | str, ...],
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Hold the gate until one bound physical HTTP request settles admission."""

    async with credential_boundary.async_guard() as guard:
        if any(guard.contains(value) for value in guarded_values):
            raise ValueError("HTTP admission contains the protected credential")
        attempt = _HttpAdmissionAttempt(credential_boundary, guard)
        token = _CURRENT_HTTP_ADMISSION.set(attempt)
        try:
            operation_task = asyncio.create_task(
                operation(), name="process-credential-http-operation"
            )
        finally:
            _CURRENT_HTTP_ADMISSION.reset(token)
        settled_task = asyncio.create_task(
            attempt.settled.wait(), name="process-credential-http-admission-settled"
        )
        try:
            done, _pending = await asyncio.wait(
                (operation_task, settled_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                settled_task.cancel()
                await asyncio.gather(settled_task, return_exceptions=True)
                return operation_task.result()
        except asyncio.CancelledError:
            operation_task.cancel()
            settled_task.cancel()
            await asyncio.gather(
                operation_task, settled_task, return_exceptions=True
            )
            raise
        finally:
            if not settled_task.done():
                settled_task.cancel()
                await asyncio.gather(settled_task, return_exceptions=True)

    try:
        return await operation_task
    except asyncio.CancelledError:
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise


def _validate_http_request_secret_boundary(
    request: httpx.Request,
    guard: ProcessCredentialGuard,
    *,
    credential_header_names: frozenset[bytes],
) -> None:
    try:
        content = request.content
    except httpx.RequestNotRead as exc:
        raise ValueError("streaming HTTP request body cannot be admitted") from exc
    if (
        guard.contains(str(request.url))
        or guard.contains(content)
        or any(
            guard.contains(name)
            or (
                name.lower() not in credential_header_names
                and guard.contains(value)
            )
            for name, value in request.headers.raw
        )
    ):
        raise ValueError("HTTP request contains the protected credential")


__all__ = [
    "CredentialCancellationPort",
    "PROCESS_CREDENTIAL_REPLACEMENT",
    "ProcessCredentialBoundary",
    "ProcessCredentialBoundaryCancelled",
    "ProcessCredentialBoundaryTimedOut",
    "ProcessCredentialBoundAsyncClient",
    "ProcessCredentialGuard",
    "ProcessCredentialScrubSet",
    "admit_process_credential_http_operation",
]
