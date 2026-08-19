"""Provider- and transport-neutral byte truth for lightweight TODO snapshots."""

from __future__ import annotations

from collections.abc import Sequence
import json


MAXIMUM_TODO_ITEMS = 64
MAXIMUM_TODO_TEXT_UTF8_BYTES = 512
MAXIMUM_TODO_CANONICAL_JSON_BYTES = 32 * 1024


def todo_snapshot_canonical_json(
    items: Sequence[tuple[str, str]],
) -> bytes:
    """Return the sole aggregate-byte quote shared by tool and live DTOs."""

    return json.dumps(
        {
            "items": [
                {"text": text, "status": status} for text, status in items
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "MAXIMUM_TODO_CANONICAL_JSON_BYTES",
    "MAXIMUM_TODO_ITEMS",
    "MAXIMUM_TODO_TEXT_UTF8_BYTES",
    "todo_snapshot_canonical_json",
]
