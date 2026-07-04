"""Deterministic text projection used by the canonical vector index."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping


EMBEDDED_TEXT_BUILDER_VERSION = "memory-embedded-text:v1"
_ALIAS_FIELDS = (
    "triggerTools",
    "triggerActions",
    "triggerFileGlobs",
    "triggerScopes",
    "triggerKeywords",
    "negativeTools",
    "negativeActions",
    "negativeFileGlobs",
)


@dataclass(frozen=True, slots=True)
class EmbeddedMemoryText:
    text: str
    text_hash: str
    builder_version: str = EMBEDDED_TEXT_BUILDER_VERSION


def build_embedded_memory_text(
    node: Mapping[str, Any],
    *,
    document: Mapping[str, Any] | None = None,
) -> EmbeddedMemoryText:
    """Build stable retrieval text without evidence, timeline, or recalled echo text."""

    payload = document or {}
    lines = [
        _line("Type", node.get("memory_type")),
        _line("Scope", node.get("scope")),
        _line("Statement", node.get("statement")),
        _line("Summary", node.get("summary")),
        _line("Applies when", node.get("applies_when")),
        _line("Do not apply when", node.get("do_not_apply_when")),
    ]
    aliases = sorted(
        {
            value.strip()
            for field in _ALIAS_FIELDS
            for value in _string_values(payload.get(field))
            if value.strip()
        }
    )
    if aliases:
        lines.append(f"Aliases: {', '.join(aliases)}")
    text = "\n".join(line for line in lines if line)
    digest = hashlib.sha256(
        f"{EMBEDDED_TEXT_BUILDER_VERSION}\n{text}".encode("utf-8")
    ).hexdigest()
    return EmbeddedMemoryText(text=text, text_hash=digest)


def _line(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return f"{label}: {' '.join(value.split())}"


def _string_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()
