"""Output processing helpers for terminal runtime."""

from __future__ import annotations

import re
import codecs
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)=([^\s]+)"),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-]+)"),
]


@dataclass(frozen=True, slots=True)
class ProcessedOutput:
    text: str
    truncated: bool = False


@dataclass(slots=True)
class OutputAccumulator:
    """Thread-safe line-redacting output accumulator for live processes."""

    artifact_path: Path | None = None
    artifact_threshold_chars: int | None = None
    _decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"),
        init=False,
        repr=False,
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _text_parts: list[str] = field(default_factory=list, init=False, repr=False)
    _pending_line: str = field(default="", init=False, repr=False)
    _finished: bool = field(default=False, init=False, repr=False)
    _total_chars: int = field(default=0, init=False, repr=False)
    _artifact_written_chars: int = field(default=0, init=False, repr=False)

    def append(self, chunk: bytes) -> str:
        if not chunk:
            return ""
        text = self._decoder.decode(chunk)
        if not text:
            return ""
        with self._lock:
            self._pending_line += text
            return self._flush_complete_lines_locked()

    def finish(self) -> str:
        with self._lock:
            if self._finished:
                return ""
            flushed_parts: list[str] = []
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._pending_line += tail
            if self._pending_line:
                cleaned = _clean_output(self._pending_line)
                self._append_cleaned_locked(cleaned)
                flushed_parts.append(cleaned)
                self._pending_line = ""
            self._finished = True
            return "".join(flushed_parts)

    def snapshot(self, *, max_chars: int) -> ProcessedOutput:
        with self._lock:
            return finalize_output("".join(self._text_parts), max_chars=max_chars)

    @property
    def full_output_path(self) -> Path | None:
        path = self.artifact_path
        if path is None or not path.exists():
            return None
        return path

    def has_snapshot_text(self, *, max_chars: int) -> bool:
        return bool(self.snapshot(max_chars=max_chars).text)

    def _flush_complete_lines_locked(self) -> str:
        parts = self._pending_line.splitlines(keepends=True)
        if not parts:
            return ""
        if parts[-1].endswith(("\n", "\r")):
            complete = parts
            self._pending_line = ""
        else:
            complete = parts[:-1]
            self._pending_line = parts[-1]
        flushed_parts: list[str] = []
        for line in complete:
            cleaned = _clean_output(line)
            self._append_cleaned_locked(cleaned)
            flushed_parts.append(cleaned)
        return "".join(flushed_parts)

    def _append_cleaned_locked(self, cleaned: str) -> None:
        if not cleaned:
            return
        self._text_parts.append(cleaned)
        self._total_chars += len(cleaned)
        self._maybe_write_artifact_locked()

    def _maybe_write_artifact_locked(self) -> None:
        path = self.artifact_path
        threshold = self.artifact_threshold_chars
        if path is None or threshold is None or self._total_chars <= threshold:
            return
        text = "".join(self._text_parts)
        delta = text[self._artifact_written_chars :]
        if not delta:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(delta)
        self._artifact_written_chars = len(text)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_replace_secret, redacted)
    return redacted


def finalize_output(text: str, *, max_chars: int) -> ProcessedOutput:
    cleaned = _clean_output(text).strip()
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


def _replace_secret(match: re.Match[str]) -> str:
    if match.lastindex == 2:
        return f"{match.group(1)}=[REDACTED]"
    return "[REDACTED]"


def _clean_output(text: str) -> str:
    return redact_secrets(strip_ansi(text))
