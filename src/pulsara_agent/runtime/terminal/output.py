"""Output processing helpers for terminal runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)=([^\s]+)"),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-]+)"),
]


@dataclass(frozen=True, slots=True)
class ProcessedOutput:
    text: str
    truncated: bool = False


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_replace_secret, redacted)
    return redacted


def finalize_output(text: str, *, max_chars: int) -> ProcessedOutput:
    cleaned = redact_secrets(strip_ansi(text)).strip()
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

