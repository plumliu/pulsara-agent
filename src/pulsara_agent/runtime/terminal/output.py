"""Bounded, cursor-addressed sanitized output journal for terminal processes."""

from __future__ import annotations

import codecs
import errno
import json
import os
import queue
import re
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Condition, Event, RLock, Thread, Timer
from typing import Callable, Literal
from uuid import uuid4

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_observation import (
    TerminalOutputCursorFact,
    TerminalOutputDeltaAttributionFact,
    TerminalOutputDeltaSemanticFact,
    TerminalOutputRecoveryReferenceFact,
    TerminalOutputSanitizationContractFact,
    TerminalOutputSpoolGapFact,
    TerminalOutputSpoolPolicyFact,
    TerminalOutputSpoolWriterStateFact,
    TerminalOutputStreamIdentityFact,
    UnavailableRecoveredTerminalOutputDeltaFact,
)


_SECRET_KEY_RE = re.compile(
    r"(?i)^[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=.*$"
)
_EMPTY_SHA256 = f"sha256:{sha256(b'').hexdigest()}"
_DEFAULT_RETAINED_SEGMENTS = 512
_DEFAULT_RETAINED_CHARS = 1_048_576
_DEFAULT_RETAINED_BYTES = 2_097_152
_DEFAULT_SEGMENT_BYTES = 64 * 1024
_DEFAULT_PARTIAL_LINE_QUIET_SECONDS = 0.25


class _SpoolAuthorityConflict(RuntimeError):
    """A committed spool page could not be verified byte-for-byte."""


def _contract() -> TerminalOutputSanitizationContractFact:
    return build_frozen_fact(
        TerminalOutputSanitizationContractFact,
        schema_version="terminal_output_sanitization_contract.v1",
        contract_id="pulsara.terminal-output-sanitizer",
        contract_version=1,
        utf8_error_policy="replace",
        ansi_normalization_contract_fingerprint=context_fingerprint(
            "terminal-ansi-normalization-contract:v1",
            {"csi": "strip", "other_escape": "strip"},
        ),
        control_character_policy="preserve_newline_normalize_cr",
        secret_redaction_contract_fingerprint=context_fingerprint(
            "terminal-secret-redaction-contract:v1",
            {"key_assignment": True, "bearer": True},
        ),
        maximum_sanitizer_carry_utf8_bytes=4096,
        oversized_sensitive_token_policy="redact_entire_token",
        partial_line_policy_fingerprint=context_fingerprint(
            "terminal-partial-line-policy:v1",
            {"quiet": True, "explicit": True, "terminal": True},
        ),
    )


DEFAULT_TERMINAL_OUTPUT_SANITIZATION_CONTRACT = _contract()


def _spool_policy() -> TerminalOutputSpoolPolicyFact:
    return build_frozen_fact(
        TerminalOutputSpoolPolicyFact,
        schema_version="terminal_output_spool_policy.v1",
        maximum_spool_utf8_bytes=8 * 1024 * 1024,
        page_utf8_bytes=64 * 1024,
        maximum_pending_spool_utf8_bytes=512 * 1024,
        page_fsync_timeout_ms=2_000,
        terminal_drain_timeout_ms=2_000,
        overflow_policy="evict_oldest_complete_page_with_gap",
        file_permission_mode="0600",
        page_commit_contract_fingerprint=context_fingerprint(
            "terminal-spool-page-commit:v1",
            ["temp", "length+sha256", "fsync", "rename", "manifest-cas"],
        ),
        retention_horizon_policy_fingerprint=context_fingerprint(
            "terminal-spool-retention-horizon:v1",
            {"owner": "process", "bounded": True},
        ),
    )


DEFAULT_TERMINAL_OUTPUT_SPOOL_POLICY = _spool_policy()


@dataclass(frozen=True, slots=True)
class ProcessedOutput:
    text: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class TerminalOutputJournalSegment:
    segment_index: int
    start_cursor: TerminalOutputCursorFact
    end_cursor: TerminalOutputCursorFact
    sanitized_text: str
    seal_reason: Literal[
        "line_boundary",
        "carriage_return_boundary",
        "partial_line_quiet",
        "explicit_observation_boundary",
        "segment_size_boundary",
        "process_terminal",
    ]
    content_sha256: str

    def __post_init__(self) -> None:
        if self.segment_index < 0:
            raise ValueError("terminal output segment index must be non-negative")
        if self.start_cursor.stream_identity != self.end_cursor.stream_identity:
            raise ValueError("terminal output segment stream identity mismatch")
        if self.content_sha256 != _sha256_text(self.sanitized_text):
            raise ValueError("terminal output segment content hash mismatch")
        if (
            self.end_cursor.sanitized_char_offset
            - self.start_cursor.sanitized_char_offset
            != len(self.sanitized_text)
            or self.end_cursor.sanitized_utf8_byte_offset
            - self.start_cursor.sanitized_utf8_byte_offset
            != len(self.sanitized_text.encode("utf-8"))
        ):
            raise ValueError("terminal output segment cursor delta mismatch")


class _StreamingAnsiNormalizer:
    __slots__ = ("_escape", "_pending_cr")

    def __init__(self) -> None:
        self._escape = ""
        self._pending_cr = False

    def feed(self, text: str, *, final: bool = False) -> str:
        output: list[str] = []
        for char in text:
            if self._escape:
                self._escape += char
                if len(self._escape) == 2 and self._escape[1] != "[":
                    self._escape = ""
                elif len(self._escape) >= 3 and "@" <= char <= "~":
                    self._escape = ""
                elif len(self._escape.encode("utf-8")) > 256:
                    self._escape = ""
                continue
            if char == "\x1b":
                self._escape = char
                continue
            if self._pending_cr:
                self._pending_cr = False
                if char == "\n":
                    continue
            if char == "\r":
                output.append("\n")
                self._pending_cr = True
            elif char in {"\n", "\t"} or ord(char) >= 32:
                output.append(char)
        if final:
            self._escape = ""
            self._pending_cr = False
        return "".join(output)


class _StreamingSecretSanitizer:
    __slots__ = (
        "_awaiting_bearer",
        "_bearer_separator",
        "_discard_oversized",
        "_passthrough_token",
        "_sensitive_assignment_key",
        "_maximum_carry_bytes",
        "_pending_token",
    )

    def __init__(self, *, maximum_carry_bytes: int) -> None:
        self._maximum_carry_bytes = maximum_carry_bytes
        self._pending_token = ""
        self._awaiting_bearer = False
        self._bearer_separator = ""
        self._discard_oversized = False
        self._passthrough_token = False
        self._sensitive_assignment_key: str | None = None

    def feed(self, text: str) -> str:
        output: list[str] = []
        for char in text:
            if not char.isspace():
                if self._discard_oversized:
                    continue
                if self._passthrough_token:
                    output.append(char)
                    continue
                self._pending_token += char
                if self._sensitive_assignment_key is None and "=" in self._pending_token:
                    key = self._pending_token.split("=", 1)[0]
                    if _SECRET_KEY_RE.match(f"{key}="):
                        self._sensitive_assignment_key = key
                        self._pending_token = ""
                        self._discard_oversized = True
                        continue
                if len(self._pending_token.encode("utf-8")) > self._maximum_carry_bytes:
                    if self._awaiting_bearer:
                        self._pending_token = ""
                        self._discard_oversized = True
                    else:
                        output.append(self._pending_token)
                        self._pending_token = ""
                        self._passthrough_token = True
                continue
            output.append(self._close_token(char))
        return "".join(output)

    def flush_boundary(self) -> str:
        return self._close_token("")

    def finish(self) -> str:
        return self._close_token("")

    def _close_token(self, separator: str) -> str:
        token = self._pending_token
        oversized = self._discard_oversized
        passthrough = self._passthrough_token
        sensitive_key = self._sensitive_assignment_key
        self._pending_token = ""
        self._discard_oversized = False
        self._passthrough_token = False
        self._sensitive_assignment_key = None
        if self._awaiting_bearer:
            if token or oversized:
                self._awaiting_bearer = False
                self._bearer_separator = ""
                return "[REDACTED]" + separator
            if separator:
                self._bearer_separator += separator
                return ""
            value = "bearer" + self._bearer_separator
            self._awaiting_bearer = False
            self._bearer_separator = ""
            return value
        if sensitive_key is not None:
            return f"{sensitive_key}=[REDACTED]" + separator
        if oversized:
            return "[REDACTED_OVERSIZE_TOKEN]" + separator
        if passthrough:
            return separator
        if not token:
            return separator
        if token.casefold() == "bearer":
            self._awaiting_bearer = True
            self._bearer_separator = separator
            return ""
        if _SECRET_KEY_RE.match(token):
            key = token.split("=", 1)[0]
            return f"{key}=[REDACTED]" + separator
        return token + separator


@dataclass(frozen=True, slots=True)
class _SpoolPageCandidate:
    page_index: int
    start_cursor: TerminalOutputCursorFact
    end_cursor: TerminalOutputCursorFact
    content: bytes
    content_sha256: str


@dataclass(frozen=True, slots=True)
class _CommittedSpoolPage:
    candidate: _SpoolPageCandidate
    path: Path


class _BoundedSpoolWriter:
    """Independent bounded writer; terminal reader submission is never blocking."""

    def __init__(
        self,
        *,
        stream_identity: TerminalOutputStreamIdentityFact,
        initial_cursor: TerminalOutputCursorFact,
        policy: TerminalOutputSpoolPolicyFact,
        spool_root: Path | None,
        fault_injector: Callable[[str], None] | None,
    ) -> None:
        self.stream_identity = stream_identity
        self.policy = policy
        self.root = Path(
            tempfile.mkdtemp(
                prefix=f"pulsara-{stream_identity.process_id}-",
                dir=str(spool_root) if spool_root is not None else None,
            )
        )
        os.chmod(self.root, 0o700)
        self.locator_id = f"terminal-spool-file:{self.root}"
        queue_items = max(
            1,
            policy.maximum_pending_spool_utf8_bytes // policy.page_utf8_bytes,
        )
        self._queue: queue.Queue[_SpoolPageCandidate | None] = queue.Queue(
            maxsize=queue_items
        )
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._pages: deque[_CommittedSpoolPage] = deque()
        self._pending_bytes = 0
        self._accepting = True
        self._closed = False
        self._stop_requested = Event()
        self._fault_injector = fault_injector
        self._journal_cursor = initial_cursor
        self._spooled_cursor = initial_cursor
        self._retained_cursor = initial_cursor
        self._latest_gap: TerminalOutputSpoolGapFact | None = None
        self._writer_state: Literal[
            "active", "degraded", "closed", "authority_untrusted"
        ] = "active"
        self._thread = Thread(
            target=self._run,
            daemon=True,
            name=f"pulsara-terminal-spool-{stream_identity.process_id}",
        )
        self._thread.start()

    def update_journal_cursor(self, cursor: TerminalOutputCursorFact) -> None:
        with self._lock:
            self._journal_cursor = cursor

    def submit(self, segment: TerminalOutputJournalSegment) -> None:
        data = segment.sanitized_text.encode("utf-8")
        if not data:
            return
        candidate = _SpoolPageCandidate(
            page_index=segment.segment_index,
            start_cursor=segment.start_cursor,
            end_cursor=segment.end_cursor,
            content=data,
            content_sha256=f"sha256:{sha256(data).hexdigest()}",
        )
        with self._lock:
            if not self._accepting or self._writer_state != "active":
                return
            if self._pending_bytes + len(data) > self.policy.maximum_pending_spool_utf8_bytes:
                self._degrade_locked(
                    reason="writer_queue_overflow",
                    start=segment.start_cursor,
                    end=segment.end_cursor,
                )
                return
            self._pending_bytes += len(data)
        try:
            self._queue.put_nowait(candidate)
        except queue.Full:
            with self._lock:
                self._pending_bytes -= len(data)
                self._degrade_locked(
                    reason="writer_queue_overflow",
                    start=segment.start_cursor,
                    end=segment.end_cursor,
                )

    def close(self, *, deadline_ms: int | None = None) -> None:
        timeout_ms = deadline_ms or self.policy.terminal_drain_timeout_ms
        with self._lock:
            if self._closed and not self._thread.is_alive():
                return
            self._accepting = False
        self._stop_requested.set()
        self._thread.join(timeout_ms / 1000)
        with self._lock:
            if self._thread.is_alive():
                self._degrade_locked(
                    reason="terminal_drain_timeout",
                    start=self._spooled_cursor,
                    end=self._journal_cursor,
                )
            self._closed = True
            if self._writer_state == "active":
                self._writer_state = "closed"
            self._condition.notify_all()

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while self._pending_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def state_fact(self) -> TerminalOutputSpoolWriterStateFact:
        with self._lock:
            return build_frozen_fact(
                TerminalOutputSpoolWriterStateFact,
                schema_version="terminal_output_spool_writer_state.v1",
                stream_identity=self.stream_identity,
                journal_end_cursor=self._journal_cursor,
                successfully_spooled_cursor=self._spooled_cursor,
                retained_start_cursor=self._retained_cursor,
                writer_state=self._writer_state,
                latest_gap=self._latest_gap,
                spool_policy_fingerprint=self.policy.policy_fingerprint,
            )

    def recovery_reference(self) -> TerminalOutputRecoveryReferenceFact:
        return build_frozen_fact(
            TerminalOutputRecoveryReferenceFact,
            schema_version="terminal_output_recovery_reference.v1",
            spool_locator_id=self.locator_id,
            spool_writer_state=self.state_fact(),
            spool_manifest_reference=None,
            spool_policy_fingerprint=self.policy.policy_fingerprint,
        )

    def read_available_text(
        self,
        *,
        start: TerminalOutputCursorFact,
        end: TerminalOutputCursorFact,
    ) -> tuple[str, TerminalOutputCursorFact]:
        """Read the exact retained spool interval without blocking the producer."""

        with self._lock:
            available_start = max(
                (start, self._retained_cursor),
                key=lambda item: (
                    item.sanitized_char_offset,
                    item.sanitized_utf8_byte_offset,
                ),
            )
            pages = tuple(self._pages)
        chunks: list[str] = []
        for page in pages:
            candidate = page.candidate
            if candidate.end_cursor.sanitized_char_offset <= available_start.sanitized_char_offset:
                continue
            if candidate.start_cursor.sanitized_char_offset >= end.sanitized_char_offset:
                break
            payload = page.path.read_bytes()
            if f"sha256:{sha256(payload).hexdigest()}" != candidate.content_sha256:
                with self._lock:
                    self._writer_state = "authority_untrusted"
                    self._condition.notify_all()
                raise _SpoolAuthorityConflict("terminal spool committed page hash conflict")
            text = payload.decode("utf-8")
            first = max(
                0,
                available_start.sanitized_char_offset
                - candidate.start_cursor.sanitized_char_offset,
            )
            last = min(
                len(text),
                end.sanitized_char_offset
                - candidate.start_cursor.sanitized_char_offset,
            )
            chunks.append(text[first:last])
        return "".join(chunks), available_start

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return self._pending_bytes

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return sum(len(item.candidate.content) for item in self._pages)

    def destroy(self) -> None:
        self.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self) -> None:
        while True:
            try:
                candidate = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._stop_requested.is_set():
                    return
                continue
            if candidate is None:  # pragma: no cover - legacy in-process sentinel
                return
            try:
                self._commit_page(candidate)
            except BaseException as exc:
                with self._lock:
                    if isinstance(exc, _SpoolAuthorityConflict):
                        self._writer_state = "authority_untrusted"
                        self._latest_gap = build_frozen_fact(
                            TerminalOutputSpoolGapFact,
                            schema_version="terminal_output_spool_gap.v1",
                            start_cursor=candidate.start_cursor,
                            end_cursor=candidate.end_cursor,
                            reason="write_io_error",
                        )
                        self._condition.notify_all()
                    else:
                        self._degrade_locked(
                            reason=_spool_failure_reason(exc),
                            start=candidate.start_cursor,
                            end=candidate.end_cursor,
                        )
            finally:
                with self._condition:
                    self._pending_bytes = max(
                        0, self._pending_bytes - len(candidate.content)
                    )
                    self._condition.notify_all()
            if self._stop_requested.is_set() and self._queue.empty():
                return

    def _commit_page(self, candidate: _SpoolPageCandidate) -> None:
        if self._fault_injector is not None:
            self._fault_injector("before_write")
        final = self.root / f"page-{candidate.page_index:08d}.bin"
        temporary = self.root / f".{final.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(candidate.content)
                handle.flush()
                if self._fault_injector is not None:
                    self._fault_injector("before_fsync")
                started = time.monotonic()
                os.fsync(handle.fileno())
                elapsed_ms = (time.monotonic() - started) * 1000
                if elapsed_ms > self.policy.page_fsync_timeout_ms:
                    raise TimeoutError("terminal spool fsync timeout")
            observed = temporary.read_bytes()
            if self._fault_injector is not None:
                self._fault_injector("after_write_before_verify")
            if len(observed) != len(candidate.content) or (
                f"sha256:{sha256(observed).hexdigest()}" != candidate.content_sha256
            ):
                raise _SpoolAuthorityConflict(
                    "terminal spool partial page/hash conflict"
                )
            os.replace(temporary, final)
            self._commit_manifest(candidate, final)
        finally:
            temporary.unlink(missing_ok=True)

    def _commit_manifest(self, candidate: _SpoolPageCandidate, final: Path) -> None:
        with self._lock:
            if self._writer_state != "active":
                final.unlink(missing_ok=True)
                return
            page = _CommittedSpoolPage(candidate=candidate, path=final)
            self._pages.append(page)
            self._spooled_cursor = candidate.end_cursor
            self._enforce_quota_locked()
            manifest = {
                "schema_version": "terminal_output_spool_manifest.v1",
                "pages": [
                    {
                        "page_index": item.candidate.page_index,
                        "name": item.path.name,
                        "content_sha256": item.candidate.content_sha256,
                        "start_cursor": item.candidate.start_cursor.model_dump(
                            mode="json"
                        ),
                        "end_cursor": item.candidate.end_cursor.model_dump(
                            mode="json"
                        ),
                    }
                    for item in self._pages
                ],
            }
            manifest_path = self.root / "manifest.json"
            temporary = self.root / f".manifest.{uuid4().hex}.tmp"
            payload = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            try:
                with temporary.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, manifest_path)
            finally:
                temporary.unlink(missing_ok=True)

    def _enforce_quota_locked(self) -> None:
        total = sum(len(item.candidate.content) for item in self._pages)
        first_evicted: TerminalOutputCursorFact | None = None
        last_evicted: TerminalOutputCursorFact | None = None
        while self._pages and total > self.policy.maximum_spool_utf8_bytes:
            page = self._pages.popleft()
            total -= len(page.candidate.content)
            page.path.unlink(missing_ok=True)
            first_evicted = first_evicted or page.candidate.start_cursor
            last_evicted = page.candidate.end_cursor
            self._retained_cursor = page.candidate.end_cursor
        if first_evicted is not None and last_evicted is not None:
            self._latest_gap = build_frozen_fact(
                TerminalOutputSpoolGapFact,
                schema_version="terminal_output_spool_gap.v1",
                start_cursor=first_evicted,
                end_cursor=last_evicted,
                reason="quota_evicted",
            )

    def _degrade_locked(
        self,
        *,
        reason: Literal[
            "write_enospc",
            "write_permission_denied",
            "write_io_error",
            "writer_queue_overflow",
            "fsync_timeout",
            "terminal_drain_timeout",
        ],
        start: TerminalOutputCursorFact,
        end: TerminalOutputCursorFact,
    ) -> None:
        if self._writer_state == "authority_untrusted":
            return
        self._writer_state = "degraded"
        self._latest_gap = build_frozen_fact(
            TerminalOutputSpoolGapFact,
            schema_version="terminal_output_spool_gap.v1",
            start_cursor=start,
            end_cursor=end,
            reason=reason,
        )
        self._condition.notify_all()


class SanitizedOutputJournal:
    """Stateful sanitized stream with bounded memory, cursor snapshots, and spool."""

    def __init__(
        self,
        *,
        process_id: str | None = None,
        journal_instance_id: str | None = None,
        sanitization_contract: TerminalOutputSanitizationContractFact = DEFAULT_TERMINAL_OUTPUT_SANITIZATION_CONTRACT,
        spool_policy: TerminalOutputSpoolPolicyFact = DEFAULT_TERMINAL_OUTPUT_SPOOL_POLICY,
        max_retained_segments: int = _DEFAULT_RETAINED_SEGMENTS,
        max_retained_chars: int = _DEFAULT_RETAINED_CHARS,
        max_retained_utf8_bytes: int = _DEFAULT_RETAINED_BYTES,
        max_segment_utf8_bytes: int = _DEFAULT_SEGMENT_BYTES,
        partial_line_quiet_seconds: float = _DEFAULT_PARTIAL_LINE_QUIET_SECONDS,
        spool_root: Path | None = None,
        spool_fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if min(
            max_retained_segments,
            max_retained_chars,
            max_retained_utf8_bytes,
            max_segment_utf8_bytes,
        ) <= 0:
            raise ValueError("terminal output journal bounds must be positive")
        if partial_line_quiet_seconds <= 0:
            raise ValueError("terminal output quiet bound must be positive")
        process_id = process_id or f"unbound_{uuid4().hex}"
        self.sanitization_contract = sanitization_contract
        self.stream_identity = build_frozen_fact(
            TerminalOutputStreamIdentityFact,
            schema_version="terminal_output_stream_identity.v1",
            process_id=process_id,
            journal_instance_id=journal_instance_id or f"journal_{uuid4().hex}",
        )
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._normalizer = _StreamingAnsiNormalizer()
        self._sanitizer = _StreamingSecretSanitizer(
            maximum_carry_bytes=sanitization_contract.maximum_sanitizer_carry_utf8_bytes
        )
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._segments: deque[TerminalOutputJournalSegment] = deque()
        self._open_text = ""
        self._open_start_cursor = self._build_cursor(0, 0, _EMPTY_SHA256)
        self._end_cursor = self._open_start_cursor
        self._retained_start_cursor = self._open_start_cursor
        self._digest = sha256()
        self._next_segment_index = 0
        self._revision = 0
        self._last_output_monotonic = time.monotonic()
        self._finished = False
        self._quiet_generation = 0
        self._quiet_timer: Timer | None = None
        self._partial_line_quiet_seconds = partial_line_quiet_seconds
        self._max_retained_segments = max_retained_segments
        self._max_retained_chars = max_retained_chars
        self._max_retained_utf8_bytes = max_retained_utf8_bytes
        self._max_segment_utf8_bytes = max_segment_utf8_bytes
        self._spool = _BoundedSpoolWriter(
            stream_identity=self.stream_identity,
            initial_cursor=self._end_cursor,
            policy=spool_policy,
            spool_root=spool_root,
            fault_injector=spool_fault_injector,
        )

    @property
    def initial_cursor(self) -> TerminalOutputCursorFact:
        return self._build_cursor(0, 0, _EMPTY_SHA256)

    @property
    def end_cursor(self) -> TerminalOutputCursorFact:
        with self._lock:
            return self._end_cursor

    @property
    def retained_start_cursor(self) -> TerminalOutputCursorFact:
        with self._lock:
            return self._retained_start_cursor

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def last_output_monotonic(self) -> float:
        """Monotonic timestamp of the latest sanitized output mutation."""

        with self._lock:
            return self._last_output_monotonic

    @property
    def retained_segment_count(self) -> int:
        with self._lock:
            return len(self._segments) + bool(self._open_text)

    @property
    def retained_utf8_bytes(self) -> int:
        with self._lock:
            return sum(len(item.sanitized_text.encode("utf-8")) for item in self._segments) + len(
                self._open_text.encode("utf-8")
            )

    @property
    def spool_retained_bytes(self) -> int:
        return self._spool.retained_bytes

    @property
    def spool_pending_bytes(self) -> int:
        return self._spool.pending_bytes

    def append(self, chunk: bytes) -> str:
        if not chunk:
            return ""
        with self._lock:
            if self._finished:
                raise RuntimeError("cannot append to a finished terminal journal")
            decoded = self._decoder.decode(chunk)
            normalized = self._normalizer.feed(decoded)
            published = self._sanitizer.feed(normalized)
            self._append_published_locked(published)
            self._arm_quiet_timer_locked()
            return published

    def seal_quiet(self) -> str:
        with self._lock:
            if self._finished:
                return ""
            published = self._sanitizer.flush_boundary()
            self._append_published_locked(published)
            self._seal_locked("partial_line_quiet")
            self._cancel_quiet_timer_locked()
            return published

    def seal_for_observation(self) -> str:
        with self._lock:
            if self._finished:
                return ""
            published = self._sanitizer.flush_boundary()
            self._append_published_locked(published)
            self._seal_locked("explicit_observation_boundary")
            self._cancel_quiet_timer_locked()
            return published

    def finish(self) -> str:
        with self._lock:
            if self._finished:
                return ""
            decoded = self._decoder.decode(b"", final=True)
            normalized = self._normalizer.feed(decoded, final=True)
            published = self._sanitizer.feed(normalized) + self._sanitizer.finish()
            self._append_published_locked(published)
            self._seal_locked("process_terminal")
            self._finished = True
            self._cancel_quiet_timer_locked()
            self._revision += 1
            self._condition.notify_all()
        self._spool.close()
        return published

    def snapshot(self, *, max_chars: int) -> ProcessedOutput:
        self.seal_for_observation()
        text, available_start = self._available_full_text()
        truncated_by_retention = available_start != self.initial_cursor
        result = finalize_output(text, max_chars=max_chars, already_clean=True)
        return ProcessedOutput(
            text=result.text,
            truncated=result.truncated or truncated_by_retention,
        )

    def snapshot_since(
        self,
        start_cursor: TerminalOutputCursorFact,
        *,
        max_chars: int,
        seal: bool = True,
    ) -> tuple[TerminalOutputDeltaSemanticFact, TerminalOutputDeltaAttributionFact]:
        if seal:
            self.seal_for_observation()
        with self._lock:
            if start_cursor.stream_identity != self.stream_identity:
                raise ValueError("terminal output snapshot cursor stream mismatch")
            if not _cursor_offsets_le(start_cursor, self._end_cursor):
                raise ValueError("terminal output snapshot cursor exceeds journal end")
            available_start = max(
                (start_cursor, self._retained_start_cursor),
                key=lambda item: (
                    item.sanitized_char_offset,
                    item.sanitized_utf8_byte_offset,
                ),
            )
            text, first_index, last_index = self._text_between_locked(
                available_start, self._end_cursor
            )
            end = self._end_cursor
        clipped = (
            ProcessedOutput(text=text, truncated=False)
            if len(text) <= max_chars
            else finalize_output(text, max_chars=max_chars, already_clean=True)
        )
        truncated = available_start != start_cursor or clipped.truncated
        delta = build_frozen_fact(
            TerminalOutputDeltaSemanticFact,
            schema_version="terminal_output_delta_semantic.v1",
            availability="available",
            requested_start_cursor=start_cursor,
            available_start_cursor=available_start,
            end_cursor=end,
            output_preview=clipped.text,
            delta_content_sha256=_sha256_text(clipped.text),
            truncated=truncated,
        )
        attribution = build_frozen_fact(
            TerminalOutputDeltaAttributionFact,
            schema_version="terminal_output_delta_attribution.v1",
            delta_semantic_fingerprint=delta.delta_semantic_fingerprint,
            full_output_artifact_ref=None,
            retained_segment_first_index=first_index,
            retained_segment_last_index=last_index,
        )
        return delta, attribution

    def full_text(self) -> str:
        self.seal_for_observation()
        return self._available_full_text()[0]

    def _available_full_text(self) -> tuple[str, TerminalOutputCursorFact]:
        self._spool.wait_idle(
            self._spool.policy.terminal_drain_timeout_ms / 1000
        )
        with self._lock:
            retained = "".join(item.sanitized_text for item in self._segments)
            retained_start = self._retained_start_cursor
            end = self._end_cursor
        if retained_start == self.initial_cursor:
            return retained, retained_start
        spool_state = self._spool.state_fact()
        if not _cursor_offsets_le(end, spool_state.successfully_spooled_cursor):
            return retained, retained_start
        try:
            spooled, available_start = self._spool.read_available_text(
                start=self.initial_cursor,
                end=end,
            )
        except (OSError, _SpoolAuthorityConflict):
            return retained, retained_start
        if available_start == self.initial_cursor:
            return spooled, available_start
        return retained, retained_start

    def full_size_bytes(self) -> int:
        return len(self.full_text().encode("utf-8"))

    def has_snapshot_text(self, *, max_chars: int) -> bool:
        return bool(self.snapshot(max_chars=max_chars).text)

    def wait_for_revision(self, revision: int, timeout_seconds: float | None) -> int:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        with self._condition:
            while self._revision <= revision and not self._finished:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self._revision

    def spool_writer_state(self) -> TerminalOutputSpoolWriterStateFact:
        return self._spool.state_fact()

    def recovery_reference(self) -> TerminalOutputRecoveryReferenceFact:
        return self._spool.recovery_reference()

    def close(self, *, destroy_spool: bool = False) -> None:
        if not self._finished:
            self.finish()
        if destroy_spool:
            self._spool.destroy()

    def _append_published_locked(self, text: str) -> None:
        if not text:
            return
        self._last_output_monotonic = time.monotonic()
        parts = text.splitlines(keepends=True)
        for part in parts:
            self._append_piece_locked(part)
            if part.endswith("\n"):
                self._seal_locked("line_boundary")

    def _append_piece_locked(self, text: str) -> None:
        remaining = text
        while remaining:
            current_bytes = len(self._open_text.encode("utf-8"))
            capacity = self._max_segment_utf8_bytes - current_bytes
            prefix, remaining = _take_utf8_prefix(remaining, capacity)
            if not prefix and self._open_text:
                self._seal_locked("segment_size_boundary")
                continue
            if not prefix:
                prefix, remaining = _take_utf8_prefix(
                    remaining, self._max_segment_utf8_bytes
                )
            encoded = prefix.encode("utf-8")
            self._open_text += prefix
            self._digest.update(encoded)
            self._end_cursor = self._build_cursor(
                self._end_cursor.sanitized_char_offset + len(prefix),
                self._end_cursor.sanitized_utf8_byte_offset + len(encoded),
                f"sha256:{self._digest.copy().hexdigest()}",
            )
            self._spool.update_journal_cursor(self._end_cursor)
            self._revision += 1
            self._condition.notify_all()
            if len(self._open_text.encode("utf-8")) >= self._max_segment_utf8_bytes:
                self._seal_locked("segment_size_boundary")

    def _seal_locked(
        self,
        reason: Literal[
            "line_boundary",
            "carriage_return_boundary",
            "partial_line_quiet",
            "explicit_observation_boundary",
            "segment_size_boundary",
            "process_terminal",
        ],
    ) -> None:
        if not self._open_text:
            return
        segment = TerminalOutputJournalSegment(
            segment_index=self._next_segment_index,
            start_cursor=self._open_start_cursor,
            end_cursor=self._end_cursor,
            sanitized_text=self._open_text,
            seal_reason=reason,
            content_sha256=_sha256_text(self._open_text),
        )
        self._segments.append(segment)
        self._next_segment_index += 1
        self._open_text = ""
        self._open_start_cursor = self._end_cursor
        self._spool.submit(segment)
        self._evict_locked()
        self._revision += 1
        self._condition.notify_all()

    def _arm_quiet_timer_locked(self) -> None:
        self._quiet_generation += 1
        generation = self._quiet_generation
        if self._quiet_timer is not None:
            self._quiet_timer.cancel()
        timer = Timer(
            self._partial_line_quiet_seconds,
            self._quiet_seal,
            args=(generation,),
        )
        timer.daemon = True
        self._quiet_timer = timer
        timer.start()

    def _cancel_quiet_timer_locked(self) -> None:
        self._quiet_generation += 1
        timer = self._quiet_timer
        self._quiet_timer = None
        if timer is not None:
            timer.cancel()

    def _quiet_seal(self, generation: int) -> None:
        with self._lock:
            if self._finished or generation != self._quiet_generation:
                return
            published = self._sanitizer.flush_boundary()
            self._append_published_locked(published)
            self._seal_locked("partial_line_quiet")
            self._quiet_timer = None

    def _evict_locked(self) -> None:
        chars = sum(len(item.sanitized_text) for item in self._segments)
        utf8_bytes = sum(
            len(item.sanitized_text.encode("utf-8")) for item in self._segments
        )
        while self._segments and (
            len(self._segments) > self._max_retained_segments
            or chars > self._max_retained_chars
            or utf8_bytes > self._max_retained_utf8_bytes
        ):
            item = self._segments.popleft()
            chars -= len(item.sanitized_text)
            utf8_bytes -= len(item.sanitized_text.encode("utf-8"))
            self._retained_start_cursor = item.end_cursor

    def _text_between_locked(
        self,
        start: TerminalOutputCursorFact,
        end: TerminalOutputCursorFact,
    ) -> tuple[str, int | None, int | None]:
        if start == end:
            return "", None, None
        output: list[str] = []
        indexes: list[int] = []
        for segment in self._segments:
            if segment.end_cursor.sanitized_char_offset <= start.sanitized_char_offset:
                continue
            if segment.start_cursor.sanitized_char_offset >= end.sanitized_char_offset:
                break
            segment_start = max(
                0,
                start.sanitized_char_offset
                - segment.start_cursor.sanitized_char_offset,
            )
            segment_end = min(
                len(segment.sanitized_text),
                end.sanitized_char_offset
                - segment.start_cursor.sanitized_char_offset,
            )
            output.append(segment.sanitized_text[segment_start:segment_end])
            indexes.append(segment.segment_index)
        return (
            "".join(output),
            indexes[0] if indexes else None,
            indexes[-1] if indexes else None,
        )

    def _build_cursor(
        self, chars: int, utf8_bytes: int, prefix_sha256: str
    ) -> TerminalOutputCursorFact:
        return build_frozen_fact(
            TerminalOutputCursorFact,
            schema_version="terminal_output_cursor.v1",
            stream_identity=self.stream_identity,
            sanitized_char_offset=chars,
            sanitized_utf8_byte_offset=utf8_bytes,
            canonical_prefix_sha256=prefix_sha256,
            sanitizer_contract_fingerprint=self.sanitization_contract.contract_fingerprint,
        )


# Transitional import name is intentionally an alias, not a second authority.
# Production construction is architecture-guarded to SanitizedOutputJournal.
def strip_ansi(text: str) -> str:
    normalizer = _StreamingAnsiNormalizer()
    return normalizer.feed(text, final=True)


def redact_secrets(text: str) -> str:
    sanitizer = _StreamingSecretSanitizer(
        maximum_carry_bytes=(
            DEFAULT_TERMINAL_OUTPUT_SANITIZATION_CONTRACT.maximum_sanitizer_carry_utf8_bytes
        )
    )
    return sanitizer.feed(text) + sanitizer.finish()


def finalize_output(
    text: str, *, max_chars: int, already_clean: bool = False
) -> ProcessedOutput:
    cleaned = (text if already_clean else _clean_output(text)).strip()
    if len(cleaned) <= max_chars:
        return ProcessedOutput(text=cleaned, truncated=False)
    head_chars = int(max_chars * 0.4)
    tail_chars = max_chars - head_chars
    omitted = len(cleaned) - head_chars - tail_chars
    notice = (
        f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted "
        f"out of {len(cleaned)} total] ...\n\n"
    )
    return ProcessedOutput(
        text=cleaned[:head_chars] + notice + cleaned[-tail_chars:],
        truncated=True,
    )


def _clean_output(text: str) -> str:
    return redact_secrets(strip_ansi(text))


def _sha256_text(value: str) -> str:
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def _take_utf8_prefix(value: str, maximum_bytes: int) -> tuple[str, str]:
    if maximum_bytes <= 0:
        return "", value
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value, ""
    end = maximum_bytes
    while end > 0:
        try:
            prefix = encoded[:end].decode("utf-8")
            return prefix, encoded[end:].decode("utf-8")
        except UnicodeDecodeError:
            end -= 1
    first = value[0]
    return first, value[1:]


def _cursor_offsets_le(
    left: TerminalOutputCursorFact, right: TerminalOutputCursorFact
) -> bool:
    return (
        left.stream_identity == right.stream_identity
        and left.sanitized_char_offset <= right.sanitized_char_offset
        and left.sanitized_utf8_byte_offset <= right.sanitized_utf8_byte_offset
    )


def _spool_failure_reason(exc: BaseException) -> Literal[
    "write_enospc",
    "write_permission_denied",
    "write_io_error",
    "fsync_timeout",
]:
    if isinstance(exc, TimeoutError):
        return "fsync_timeout"
    if isinstance(exc, PermissionError):
        return "write_permission_denied"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "write_enospc"
    return "write_io_error"


def recover_terminal_output_delta(
    *,
    recovery_reference: TerminalOutputRecoveryReferenceFact,
    requested_start_cursor: TerminalOutputCursorFact,
    terminal_cursor: TerminalOutputCursorFact,
    max_chars: int,
) -> TerminalOutputDeltaSemanticFact | UnavailableRecoveredTerminalOutputDeltaFact:
    """Rehydrate an exact retained spool range or return typed unavailability."""

    state = recovery_reference.spool_writer_state
    if (
        requested_start_cursor.stream_identity != state.stream_identity
        or terminal_cursor.stream_identity != state.stream_identity
    ):
        raise ValueError("terminal output recovery stream mismatch")
    reason = _recovery_unavailable_reason(state)
    if (
        requested_start_cursor.sanitized_char_offset
        < state.retained_start_cursor.sanitized_char_offset
        or requested_start_cursor.sanitized_utf8_byte_offset
        < state.retained_start_cursor.sanitized_utf8_byte_offset
    ):
        reason = "spool_range_evicted"
    locator_prefix = "terminal-spool-file:"
    if not recovery_reference.spool_locator_id.startswith(locator_prefix):
        reason = reason or "artifact_gc_confirmed"
        root = None
    else:
        root = Path(recovery_reference.spool_locator_id[len(locator_prefix) :])
    manifest_path = None if root is None else root / "manifest.json"
    if reason is None and (manifest_path is None or not manifest_path.exists()):
        reason = "artifact_gc_confirmed"
    if reason is not None:
        return build_frozen_fact(
            UnavailableRecoveredTerminalOutputDeltaFact,
            schema_version="unavailable_recovered_terminal_output_delta.v1",
            availability="unavailable_recovered",
            requested_start_cursor=requested_start_cursor,
            terminal_cursor=terminal_cursor,
            recovery_reason=reason,
        )
    assert manifest_path is not None and root is not None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        chunks: list[str] = []
        for item in manifest["pages"]:
            start = TerminalOutputCursorFact.model_validate(item["start_cursor"])
            end = TerminalOutputCursorFact.model_validate(item["end_cursor"])
            if end.sanitized_char_offset <= requested_start_cursor.sanitized_char_offset:
                continue
            if start.sanitized_char_offset >= terminal_cursor.sanitized_char_offset:
                break
            payload = (root / item["name"]).read_bytes()
            if f"sha256:{sha256(payload).hexdigest()}" != item["content_sha256"]:
                raise _SpoolAuthorityConflict("terminal recovery page hash conflict")
            text_value = payload.decode("utf-8")
            first = max(
                0,
                requested_start_cursor.sanitized_char_offset
                - start.sanitized_char_offset,
            )
            last = min(
                len(text_value),
                terminal_cursor.sanitized_char_offset - start.sanitized_char_offset,
            )
            chunks.append(text_value[first:last])
        text_value = "".join(chunks)
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        _SpoolAuthorityConflict,
    ):
        return build_frozen_fact(
            UnavailableRecoveredTerminalOutputDeltaFact,
            schema_version="unavailable_recovered_terminal_output_delta.v1",
            availability="unavailable_recovered",
            requested_start_cursor=requested_start_cursor,
            terminal_cursor=terminal_cursor,
            recovery_reason="spool_write_failed",
        )
    expected_chars = (
        terminal_cursor.sanitized_char_offset
        - requested_start_cursor.sanitized_char_offset
    )
    expected_bytes = (
        terminal_cursor.sanitized_utf8_byte_offset
        - requested_start_cursor.sanitized_utf8_byte_offset
    )
    if len(text_value) != expected_chars or len(text_value.encode("utf-8")) != expected_bytes:
        return build_frozen_fact(
            UnavailableRecoveredTerminalOutputDeltaFact,
            schema_version="unavailable_recovered_terminal_output_delta.v1",
            availability="unavailable_recovered",
            requested_start_cursor=requested_start_cursor,
            terminal_cursor=terminal_cursor,
            recovery_reason="spool_write_failed",
        )
    preview = (
        ProcessedOutput(text=text_value, truncated=False)
        if len(text_value) <= max_chars
        else finalize_output(text_value, max_chars=max_chars, already_clean=True)
    )
    return build_frozen_fact(
        TerminalOutputDeltaSemanticFact,
        schema_version="terminal_output_delta_semantic.v1",
        availability="available",
        requested_start_cursor=requested_start_cursor,
        available_start_cursor=requested_start_cursor,
        end_cursor=terminal_cursor,
        output_preview=preview.text,
        delta_content_sha256=_sha256_text(preview.text),
        truncated=preview.truncated,
    )


def _recovery_unavailable_reason(
    state: TerminalOutputSpoolWriterStateFact,
) -> Literal[
    "spool_range_evicted",
    "spool_write_failed",
    "spool_writer_queue_overflow",
    "spool_fsync_timeout",
    "spool_terminal_drain_timeout",
    "artifact_gc_confirmed",
] | None:
    gap = state.latest_gap
    if state.writer_state == "authority_untrusted":
        return "spool_write_failed"
    if gap is None:
        return None
    return {
        "quota_evicted": "spool_range_evicted",
        "write_enospc": "spool_write_failed",
        "write_permission_denied": "spool_write_failed",
        "write_io_error": "spool_write_failed",
        "writer_queue_overflow": "spool_writer_queue_overflow",
        "fsync_timeout": "spool_fsync_timeout",
        "terminal_drain_timeout": "spool_terminal_drain_timeout",
    }[gap.reason]


__all__ = [
    "DEFAULT_TERMINAL_OUTPUT_SANITIZATION_CONTRACT",
    "DEFAULT_TERMINAL_OUTPUT_SPOOL_POLICY",
    "ProcessedOutput",
    "SanitizedOutputJournal",
    "TerminalOutputJournalSegment",
    "finalize_output",
    "redact_secrets",
    "recover_terminal_output_delta",
    "strip_ansi",
]
