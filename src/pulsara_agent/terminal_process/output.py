"""Process-local sanitized Terminal output authority.

Raw subprocess bytes terminate at :class:`TerminalOutputOwner`.  The owner
retains only public UTF-8 bytes, exposes monotonic cursor reads, and notifies
bounded process-local subscribers after releasing its lock.  It deliberately
has no serializer, database adapter, or recovery seam.
"""

from __future__ import annotations

from pulsara_agent.identity import random_id, require_id

from base64 import urlsafe_b64decode, urlsafe_b64encode
import codecs
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from threading import Condition, RLock
from time import monotonic
from typing import Callable, Iterator

from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
    ToolOutputSourceFormatHint,
)
from pulsara_agent.ports.tool_output import ToolOutputBodyCandidate


TERMINAL_RETAINED_OUTPUT_HARD_BYTES = 16 * 1024 * 1024
TERMINAL_HOST_RETAINED_HARD_BYTES = 128 * 1024 * 1024
TERMINAL_SANITIZER_CARRY_HARD_BYTES = 4096
_ESCAPE_SUPPRESSED_MARKER = "[TERMINAL_ESCAPE_SEQUENCE_SUPPRESSED]"
_SENSITIVE_TOKEN_MARKER = "[REDACTED_OVERSIZE_TOKEN]"


class TerminalOutputReadDisposition(StrEnum):
    CURRENT_SNAPSHOT = "CURRENT_SNAPSHOT"
    EXACT_DELTA = "EXACT_DELTA"
    GAP = "GAP"
    INVALID_CURSOR = "INVALID_CURSOR"
    UNAVAILABLE = "UNAVAILABLE"


class TerminalOutputSourceCoverage(StrEnum):
    COMPLETE = "COMPLETE"
    RETAINED_SNAPSHOT = "RETAINED_SNAPSHOT"
    UNAVAILABLE = "UNAVAILABLE"


class InvalidTerminalOutputCursor(ValueError):
    """The supplied cursor cannot select output from this process stream."""

    def __init__(self) -> None:
        super().__init__(TerminalOutputReadDisposition.INVALID_CURSOR.value)


@dataclass(frozen=True, slots=True)
class TerminalSanitizerPolicyV1:
    maximum_undecided_utf8_carry: int = TERMINAL_SANITIZER_CARRY_HARD_BYTES
    maximum_unterminated_escape_carry: int = TERMINAL_SANITIZER_CARRY_HARD_BYTES
    malformed_utf8_policy: str = "replace"
    oversized_sensitive_token_policy: str = "redact_entire_token"
    unterminated_escape_overflow_policy: str = (
        "suppress_sequence_and_emit_bounded_marker"
    )


@dataclass(frozen=True, slots=True)
class TerminalOutputCursor:
    owner_epoch: str
    process_id: str
    stream_id: str
    sanitized_utf8_offset: int

    def __post_init__(self) -> None:
        require_id(self.process_id, "process")
        require_id(self.stream_id, "stream")
        if not self.owner_epoch:
            raise ValueError("terminal output cursor identity is incomplete")
        if self.sanitized_utf8_offset < 0:
            raise ValueError("terminal output cursor offset is negative")


@dataclass(frozen=True, slots=True)
class TerminalOutputSnapshot:
    process_id: str
    stream_id: str
    output_revision: int
    retained_from_offset: int
    through_offset: int
    text: str
    process_status: str
    exit_code: int | None
    source_coverage: TerminalOutputSourceCoverage
    disposition: TerminalOutputReadDisposition
    output_cursor: str
    retained_from_cursor: str
    gap_before_output: bool
    truncated_by_response_bound: bool

    def __post_init__(self) -> None:
        encoded = self.text.encode("utf-8")
        if (
            self.retained_from_offset < 0
            or self.through_offset < self.retained_from_offset
        ):
            raise ValueError("terminal output snapshot range is invalid")
        if self.disposition is TerminalOutputReadDisposition.EXACT_DELTA and (
            len(encoded) > self.through_offset - self.retained_from_offset
        ):
            raise ValueError("terminal output delta exceeds its authority range")


class IncrementalTerminalSanitizer:
    """One streaming automaton for decoding, ANSI/OSC suppression and redaction."""

    def __init__(self, policy: TerminalSanitizerPolicyV1 | None = None) -> None:
        self.policy = policy or TerminalSanitizerPolicyV1()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending_cr = False
        self._escape_mode: str | None = None
        self._escape = ""
        self._escape_discarding = False
        self._token = ""
        self._token_discarding = False
        self._token_passthrough = False
        self._awaiting_bearer = False
        self._bearer_separator = ""
        self._finished = False

    @property
    def undecided_utf8_bytes(self) -> int:
        return len(self._token.encode("utf-8")) + len(self._escape.encode("utf-8"))

    def feed(self, raw: bytes) -> bytes:
        if self._finished:
            raise RuntimeError("terminal sanitizer is finalized")
        if not raw:
            return b""
        decoded = self._decoder.decode(raw, final=False)
        normalized = self._consume_text(decoded, final=False)
        return self._consume_tokens(normalized, final=False).encode("utf-8")

    def quiet_boundary(self) -> bytes:
        """Expose only text already proven safe; undecided prefixes stay private."""

        if self._finished:
            return b""
        return b""

    def finalize(self) -> bytes:
        if self._finished:
            return b""
        decoded = self._decoder.decode(b"", final=True)
        normalized = self._consume_text(decoded, final=True)
        public = self._consume_tokens(normalized, final=True)
        self._finished = True
        return public.encode("utf-8")

    @classmethod
    def one_shot(cls, raw: bytes) -> bytes:
        sanitizer = cls()
        return sanitizer.feed(raw) + sanitizer.finalize()

    def _consume_text(self, text: str, *, final: bool) -> str:
        output: list[str] = []
        for char in text:
            if self._escape_mode is not None:
                self._consume_escape(char, output)
                continue
            if char == "\x1b":
                self._escape_mode = "ESC"
                self._escape = char
                continue
            if self._pending_cr:
                self._pending_cr = False
                if char == "\n":
                    continue
            if char == "\r":
                output.append("\n")
                self._pending_cr = True
            elif (
                char in {"\n", "\t"}
                or ord(char) >= 0x20
                and not 0x7F <= ord(char) <= 0x9F
            ):
                output.append(char)
        if final:
            self._pending_cr = False
            if self._escape_mode is not None:
                # No unterminated escape payload is ever released at EOF.
                if not self._escape_discarding:
                    output.append(_ESCAPE_SUPPRESSED_MARKER)
                self._reset_escape()
        return "".join(output)

    def _consume_escape(self, char: str, output: list[str]) -> None:
        if not self._escape_discarding:
            self._escape += char
            if (
                len(self._escape.encode("utf-8"))
                > self.policy.maximum_unterminated_escape_carry
            ):
                output.append(_ESCAPE_SUPPRESSED_MARKER)
                self._escape_discarding = True
                self._escape = ""
        mode = self._escape_mode
        if mode == "ESC":
            if char == "[":
                self._escape_mode = "CSI"
            elif char == "]":
                self._escape_mode = "OSC"
            else:
                self._reset_escape()
            return
        if mode == "CSI" and "@" <= char <= "~":
            self._reset_escape()
            return
        if mode == "OSC":
            if char == "\x07":
                self._reset_escape()
            elif char == "\x1b":
                self._escape_mode = "OSC_ESC"
            return
        if mode == "OSC_ESC":
            if char == "\\":
                self._reset_escape()
            else:
                self._escape_mode = "OSC"

    def _reset_escape(self) -> None:
        self._escape_mode = None
        self._escape = ""
        self._escape_discarding = False

    def _consume_tokens(self, text: str, *, final: bool) -> str:
        output: list[str] = []
        for char in text:
            if char.isspace():
                if self._token_passthrough:
                    self._token_passthrough = False
                    output.append(char)
                else:
                    output.append(self._close_token(char))
                continue
            if self._token_passthrough:
                output.append(char)
                continue
            if self._token_discarding:
                continue
            self._token += char
            if (
                len(self._token.encode("utf-8"))
                > self.policy.maximum_undecided_utf8_carry
            ):
                if self._awaiting_bearer or _sensitive_assignment_prefix(self._token):
                    # A recognized sensitive token remains private until its
                    # boundary, regardless of raw chunking.
                    self._token = ""
                    self._token_discarding = True
                else:
                    # The closed assignment-key grammar is bounded by the
                    # carry.  Once a non-sensitive key/body exceeds it, later
                    # bytes cannot retroactively turn that prefix into a
                    # valid secret assignment.  Stream the proven-safe token
                    # instead of destroying large ordinary tool output.
                    output.append(self._token)
                    self._token = ""
                    self._token_passthrough = True
        if final:
            output.append(self._close_token(""))
            if self._awaiting_bearer:
                output.append("Bearer" + self._bearer_separator)
                self._awaiting_bearer = False
                self._bearer_separator = ""
        return "".join(output)

    def _close_token(self, separator: str) -> str:
        token = self._token
        discarded = self._token_discarding
        self._token = ""
        self._token_discarding = False
        if self._awaiting_bearer:
            if token or discarded:
                self._awaiting_bearer = False
                bearer_separator = self._bearer_separator
                self._bearer_separator = ""
                return "Bearer" + bearer_separator + "<redacted>" + separator
            self._bearer_separator += separator
            return ""
        if discarded:
            return _SENSITIVE_TOKEN_MARKER + separator
        if not token:
            return separator
        if token.casefold() == "bearer":
            self._awaiting_bearer = True
            self._bearer_separator = separator
            return ""
        if "=" in token:
            key, _value = token.split("=", 1)
            folded = key.casefold()
            if any(name in folded for name in ("key", "token", "secret", "password")):
                return f"{key}=<redacted>" + separator
        return token + separator


def _sensitive_assignment_prefix(token: str) -> bool:
    key = token.split("=", 1)[0].casefold()
    return any(name in key for name in ("key", "token", "secret", "password"))


OutputSubscriber = Callable[[bytes, int, int], None]


class TerminalOutputOwner:
    """Bounded retained UTF-8 ring and cursor authority for one process."""

    def __init__(
        self,
        *,
        owner_epoch: str,
        process_id: str,
        maximum_bytes: int = TERMINAL_RETAINED_OUTPUT_HARD_BYTES,
        retained_bytes_changed: Callable[["TerminalOutputOwner"], None] | None = None,
    ) -> None:
        if maximum_bytes <= 0 or maximum_bytes > TERMINAL_RETAINED_OUTPUT_HARD_BYTES:
            raise ValueError("terminal retained output bound is invalid")
        self.owner_epoch = owner_epoch
        self.process_id = process_id
        self.stream_id = random_id("stream")
        self.maximum_bytes = maximum_bytes
        self._sanitizer = IncrementalTerminalSanitizer()
        self._retained = bytearray()
        self._retained_from = 0
        self._through = 0
        self._revision = 0
        self._status = "running"
        self._exit_code: int | None = None
        self._unavailable = False
        self._finalized = False
        self._subscribers: dict[str, OutputSubscriber] = {}
        self._observation_leases = 0
        self._inflight_reads = 0
        self._reads_closed = False
        self._retained_bytes_changed = retained_bytes_changed
        self._lock = RLock()
        self._condition = Condition(self._lock)

    @property
    def retained_utf8_bytes(self) -> int:
        with self._lock:
            return len(self._retained)

    @property
    def through_offset(self) -> int:
        with self._lock:
            return self._through

    @property
    def output_revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def observation_lease_count(self) -> int:
        with self._lock:
            return self._observation_leases

    @property
    def inflight_read_count(self) -> int:
        with self._lock:
            return self._inflight_reads

    @contextmanager
    def reading(self) -> Iterator[None]:
        """Protect an admitted Registry operation with the existing read count.

        Registry admission takes its lock before entering this scope. The scope
        releases the output lock while the operation runs, so waits and physical
        joins never block output delivery or the release of another reader.
        """

        with self._condition:
            if self._reads_closed:
                raise KeyError(self.process_id)
            self._inflight_reads += 1
        try:
            yield
        finally:
            with self._condition:
                self._inflight_reads -= 1
                self._condition.notify_all()

    def close_reads_if_idle(self) -> bool:
        """Atomically stop future reads only after all existing users finish."""

        with self._condition:
            if self._inflight_reads or self._observation_leases:
                return False
            self._reads_closed = True
            self._condition.notify_all()
            return True

    def wait_for_readers(self, timeout_seconds: float | None) -> None:
        with self._condition:
            if self._inflight_reads or self._observation_leases:
                self._condition.wait(timeout_seconds)

    def append_raw(self, raw: bytes) -> bytes:
        with self._lock:
            try:
                # Decoder/token carry and retained bytes form one stream cut.
                # Concurrent feeds must not race on the incremental state.
                public = self._sanitizer.feed(raw)
            except Exception:
                self._unavailable = True
                self._revision += 1
                self._condition.notify_all()
                return b""
            start, end = self._append_public_locked(public)
        self._notify_append(public, start, end)
        return public

    def finalize(self, *, status: str, exit_code: int | None) -> bytes:
        with self._lock:
            try:
                public = self._sanitizer.finalize()
            except Exception:
                public = b""
                self._unavailable = True
            start, end = self._append_public_locked(public)
            if not self._finalized:
                self._finalized = True
                self._status = status
                self._exit_code = exit_code
                self._revision += 1
                self._condition.notify_all()
        self._notify_append(public, start, end)
        self._notify_subscribers(b"", end, end)
        return public

    def update_lifecycle(self, *, status: str, exit_code: int | None) -> None:
        with self._lock:
            if self._status == status and self._exit_code == exit_code:
                return
            self._status = status
            self._exit_code = exit_code
            self._revision += 1
            self._condition.notify_all()

    def _append_public_locked(self, public: bytes) -> tuple[int, int]:
        start = self._through
        if not public:
            return start, start
        # The sanitizer only returns valid UTF-8.  Assert that invariant before
        # it can become retained authority.
        public.decode("utf-8")
        self._retained.extend(public)
        self._through += len(public)
        self._evict_to_bound_locked(self.maximum_bytes)
        self._revision += 1
        self._condition.notify_all()
        return start, self._through

    def _notify_append(self, public: bytes, start: int, end: int) -> None:
        if not public:
            return
        callback = self._retained_bytes_changed
        if callback is not None:
            callback(self)
        self._notify_subscribers(public, start, end)

    def evict_oldest_to(self, maximum_retained_bytes: int) -> int:
        with self._lock:
            before = len(self._retained)
            self._evict_to_bound_locked(max(0, maximum_retained_bytes))
            removed = before - len(self._retained)
            if removed:
                self._revision += 1
                self._condition.notify_all()
            return removed

    def _evict_to_bound_locked(self, maximum: int) -> None:
        excess = len(self._retained) - maximum
        if excess <= 0:
            return
        boundary = min(excess, len(self._retained))
        while (
            boundary < len(self._retained) and self._retained[boundary] & 0xC0 == 0x80
        ):
            boundary += 1
        del self._retained[:boundary]
        self._retained_from += boundary

    def subscribe(self, callback: OutputSubscriber) -> tuple[str, TerminalOutputCursor]:
        with self._lock:
            token = random_id("subscription")
            while token in self._subscribers:
                token = random_id("subscription")
            return token, self.install_subscription(token, callback)

    def install_subscription(
        self, token: str, callback: OutputSubscriber
    ) -> TerminalOutputCursor:
        """Install a caller-prepared identity for rollback-safe ownership."""

        if not token:
            raise ValueError("terminal output subscription token is empty")
        with self._lock:
            if self._reads_closed:
                raise KeyError(self.process_id)
            if token in self._subscribers:
                raise RuntimeError("terminal output subscription already exists")
            self._subscribers[token] = callback
            self._observation_leases += 1
            cursor = self._cursor(self._through)
        return cursor

    def unsubscribe(self, token: str) -> bool:
        with self._lock:
            removed = self._subscribers.pop(token, None)
            if removed is None:
                return False
            self._observation_leases -= 1
            self._condition.notify_all()
            return True

    def _notify_subscribers(self, public: bytes, start: int, end: int) -> None:
        with self._lock:
            callbacks = tuple(self._subscribers.values())
        for callback in callbacks:
            try:
                callback(public, start, end)
            except Exception:
                # A process-local consumer can detach itself after a failure;
                # it never owns subprocess progress.
                continue

    def snapshot(
        self, *, maximum_chars: int, since_cursor: str | None = None
    ) -> TerminalOutputSnapshot:
        with self._lock:
            if self._reads_closed:
                raise KeyError(self.process_id)
            self._inflight_reads += 1
            try:
                return self._snapshot_locked(
                    maximum_chars=maximum_chars, since_cursor=since_cursor
                )
            finally:
                self._inflight_reads -= 1
                self._condition.notify_all()

    def capture_cursor(self) -> str:
        """Freeze a stream position without copying or consuming retained text."""

        with self._lock:
            if self._reads_closed:
                raise KeyError(self.process_id)
            return encode_terminal_output_cursor(self._cursor(self._through))

    def snapshot_with_body_candidate(
        self,
        *,
        maximum_chars: int,
        since_cursor: str | None = None,
    ) -> tuple[TerminalOutputSnapshot, ToolOutputBodyCandidate]:
        """Freeze one tail and its complete public source range in one cut.

        Cursor reads select a new logical tool observation.  The saved body
        must therefore be the complete bytes selected by that cursor cut, not
        the process's entire retained ring.  Both carriers are frozen under
        one output lock so bytes appended after the cut cannot rewrite the
        accepted observation.
        """

        with self._lock:
            if self._reads_closed:
                raise KeyError(self.process_id)
            self._inflight_reads += 1
            try:
                disposition, gap, raw = self._range_locked(since_cursor)
                text = raw.decode("utf-8")
                snapshot = self._snapshot_from_range_locked(
                    maximum_chars=maximum_chars,
                    since_cursor=since_cursor,
                    disposition=disposition,
                    gap=gap,
                    text=text,
                )
                candidate = self._body_candidate_from_range_locked(
                    disposition=disposition, gap=gap, text=text
                )
                return snapshot, candidate
            finally:
                self._inflight_reads -= 1
                self._condition.notify_all()


    def _snapshot_locked(
        self, *, maximum_chars: int, since_cursor: str | None
    ) -> TerminalOutputSnapshot:
        disposition, gap, raw = self._range_locked(since_cursor)
        return self._snapshot_from_range_locked(
            maximum_chars=maximum_chars,
            since_cursor=since_cursor,
            disposition=disposition,
            gap=gap,
            text=raw.decode("utf-8"),
        )

    def _snapshot_from_range_locked(
        self,
        *,
        maximum_chars: int,
        since_cursor: str | None,
        disposition: TerminalOutputReadDisposition,
        gap: bool,
        text: str,
    ) -> TerminalOutputSnapshot:
        if maximum_chars <= 0:
            raise ValueError("terminal output response bound must be positive")
        range_start = self._retained_from
        if (
            since_cursor is not None
            and disposition is TerminalOutputReadDisposition.EXACT_DELTA
        ):
            range_start = decode_terminal_output_cursor(
                since_cursor
            ).sanitized_utf8_offset
        visible, response_truncated = _tail_by_chars(text, maximum_chars)
        return TerminalOutputSnapshot(
            process_id=self.process_id,
            stream_id=self.stream_id,
            output_revision=self._revision,
            retained_from_offset=range_start,
            through_offset=self._through,
            text=visible,
            process_status=self._status,
            exit_code=self._exit_code,
            source_coverage=self._source_coverage_locked(),
            disposition=(
                TerminalOutputReadDisposition.UNAVAILABLE
                if self._unavailable
                else disposition
            ),
            output_cursor=encode_terminal_output_cursor(self._cursor(self._through)),
            retained_from_cursor=encode_terminal_output_cursor(
                self._cursor(self._retained_from)
            ),
            gap_before_output=gap,
            truncated_by_response_bound=response_truncated,
        )

    def _range_locked(
        self, since_cursor: str | None
    ) -> tuple[TerminalOutputReadDisposition, bool, bytes]:
        disposition = TerminalOutputReadDisposition.CURRENT_SNAPSHOT
        gap = self._retained_from > 0
        if since_cursor is None:
            return disposition, gap, bytes(self._retained)
        cursor = decode_terminal_output_cursor(since_cursor)
        if (
            cursor.owner_epoch != self.owner_epoch
            or cursor.process_id != self.process_id
            or cursor.stream_id != self.stream_id
            or cursor.sanitized_utf8_offset > self._through
        ):
            raise InvalidTerminalOutputCursor()
        if cursor.sanitized_utf8_offset < self._retained_from:
            return TerminalOutputReadDisposition.GAP, True, bytes(self._retained)
        relative = cursor.sanitized_utf8_offset - self._retained_from
        return (
            TerminalOutputReadDisposition.EXACT_DELTA,
            False,
            bytes(memoryview(self._retained)[relative:]),
        )

    def _source_coverage_locked(self) -> TerminalOutputSourceCoverage:
        if self._unavailable:
            return TerminalOutputSourceCoverage.UNAVAILABLE
        if self._retained_from > 0:
            return TerminalOutputSourceCoverage.RETAINED_SNAPSHOT
        return TerminalOutputSourceCoverage.COMPLETE

    def body_candidate(
        self, *, since_cursor: str | None = None
    ) -> ToolOutputBodyCandidate:
        with self._lock:
            if self._reads_closed:
                raise KeyError(self.process_id)
            disposition, gap, raw = self._range_locked(since_cursor)
            return self._body_candidate_from_range_locked(
                disposition=disposition, gap=gap, text=raw.decode("utf-8")
            )

    def _body_candidate_from_range_locked(
        self,
        *,
        disposition: TerminalOutputReadDisposition,
        gap: bool,
        text: str,
    ) -> ToolOutputBodyCandidate:
        # A cursor that is still retained selects an exact new range even if
        # older, unrelated process output was previously evicted.  GAP and a
        # sanitizer failure can prove only the retained public snapshot and
        # must never claim COMPLETE body coverage.
        incomplete = (
            gap or disposition is TerminalOutputReadDisposition.GAP or self._unavailable
        )
        coverage_reason = None
        if self._unavailable:
            coverage_reason = (
                ToolOutputSourceCoverageReason.TERMINAL_SANITIZER_UNAVAILABLE
            )
        elif incomplete:
            coverage_reason = ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP
        return ToolOutputBodyCandidate(
            text=text,
            source_coverage=(
                ToolOutputSourceCoverage.RETAINED_SNAPSHOT
                if incomplete
                else ToolOutputSourceCoverage.COMPLETE
            ),
            source_coverage_reason=coverage_reason,
            source_format=ToolOutputSourceFormatHint.TEXT,
        )

    def wait_for_revision(self, revision: int, timeout_seconds: float | None) -> int:
        deadline = None if timeout_seconds is None else monotonic() + timeout_seconds
        with self._condition:
            while self._revision <= revision and not self._finalized:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._condition.wait(remaining)
        # Revision publication precedes the out-of-lock Host retention callback.
        # A waiter must settle that same policy before exposing its observation.
        if self._retained_bytes_changed is not None:
            self._retained_bytes_changed(self)
        return self.output_revision

    def _cursor(self, offset: int) -> TerminalOutputCursor:
        return TerminalOutputCursor(
            self.owner_epoch, self.process_id, self.stream_id, offset
        )


def encode_terminal_output_cursor(cursor: TerminalOutputCursor) -> str:
    payload = {
        "owner_epoch": cursor.owner_epoch,
        "process_id": cursor.process_id,
        "stream_id": cursor.stream_id,
        "sanitized_utf8_offset": cursor.sanitized_utf8_offset,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    envelope = {
        "payload": payload,
        "checksum": sha256(b"terminal-output-cursor:v2\0" + canonical).hexdigest(),
    }
    return (
        urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )


def decode_terminal_output_cursor(value: str) -> TerminalOutputCursor:
    try:
        padded = value + "=" * (-len(value) % 4)
        envelope = json.loads(urlsafe_b64decode(padded.encode("ascii")))
        payload = envelope["payload"]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        expected = sha256(b"terminal-output-cursor:v2\0" + canonical).hexdigest()
        if envelope.get("checksum") != expected:
            raise ValueError
        return TerminalOutputCursor(
            owner_epoch=str(payload["owner_epoch"]),
            process_id=str(payload["process_id"]),
            stream_id=str(payload["stream_id"]),
            sanitized_utf8_offset=int(payload["sanitized_utf8_offset"]),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidTerminalOutputCursor() from exc


def _tail_by_chars(value: str, maximum_chars: int) -> tuple[str, bool]:
    if len(value) <= maximum_chars:
        return value, False
    return value[-maximum_chars:], True


__all__ = [
    "IncrementalTerminalSanitizer",
    "InvalidTerminalOutputCursor",
    "TERMINAL_HOST_RETAINED_HARD_BYTES",
    "TERMINAL_RETAINED_OUTPUT_HARD_BYTES",
    "TERMINAL_SANITIZER_CARRY_HARD_BYTES",
    "TerminalOutputCursor",
    "TerminalOutputOwner",
    "TerminalOutputReadDisposition",
    "TerminalOutputSnapshot",
    "TerminalOutputSourceCoverage",
    "TerminalSanitizerPolicyV1",
    "decode_terminal_output_cursor",
    "encode_terminal_output_cursor",
]
