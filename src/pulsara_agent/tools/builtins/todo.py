"""Pure contract for the exact-run, process-local todo tool.

The adapter owns no current state. Production mutation is performed by the
Host-scoped TODO owner only after the canonical ToolResult is confirmed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import unicodedata

from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.todo import (
    MAXIMUM_TODO_CANONICAL_JSON_BYTES,
    MAXIMUM_TODO_ITEMS,
    MAXIMUM_TODO_TEXT_UTF8_BYTES,
    todo_snapshot_canonical_json,
)



class TodoStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class TodoValidationError(ValueError):
    """One safe, model-actionable pre-attempt validation failure."""


@dataclass(frozen=True, slots=True)
class FrozenTodoItem:
    ordinal: int
    text: str
    status: TodoStatus
    item_fingerprint: str

    def __post_init__(self) -> None:
        if self.ordinal < 0 or not self.text:
            raise ValueError("frozen TODO item is invalid")
        if not isinstance(self.status, TodoStatus):
            raise TypeError("TODO status must be closed")
        if not self.item_fingerprint.startswith("sha256:"):
            raise ValueError("TODO item fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class FrozenTodoCandidate:
    ordered_items: tuple[FrozenTodoItem, ...]
    pending_count: int
    in_progress_count: int
    completed_count: int
    canonical_json_utf8_bytes: int
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.pending_count + self.in_progress_count + self.completed_count
            != len(self.ordered_items)
            or self.in_progress_count > 1
            or not 0
            <= self.canonical_json_utf8_bytes
            <= MAXIMUM_TODO_CANONICAL_JSON_BYTES
            or not self.candidate_fingerprint.startswith("sha256:")
        ):
            raise ValueError("frozen TODO candidate is invalid")


def parse_todo_replacement(arguments: Mapping[str, object]) -> FrozenTodoCandidate:
    """Validate and recursively freeze one complete replacement snapshot."""

    if set(arguments) != {"items"}:
        raise TodoValidationError("todo accepts only the items field")
    raw_items = arguments.get("items")
    if not isinstance(raw_items, (list, tuple)):
        raise TodoValidationError("todo items must be an array")
    if len(raw_items) > MAXIMUM_TODO_ITEMS:
        raise TodoValidationError(
            f"todo accepts at most {MAXIMUM_TODO_ITEMS} items"
        )

    frozen: list[FrozenTodoItem] = []
    seen: set[str] = set()
    counts = {status: 0 for status in TodoStatus}
    for ordinal, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping) or set(raw_item) != {"text", "status"}:
            raise TodoValidationError(
                "each todo item requires only text and status"
            )
        text = raw_item.get("text")
        status_value = raw_item.get("status")
        if not isinstance(text, str) or not isinstance(status_value, str):
            raise TodoValidationError("todo item text and status must be strings")
        _validate_text(text)
        try:
            status = TodoStatus(status_value)
        except ValueError as exc:
            raise TodoValidationError("todo item status is invalid") from exc
        if text in seen:
            raise TodoValidationError("todo contains duplicate item text")
        seen.add(text)
        counts[status] += 1
        frozen.append(
            FrozenTodoItem(
                ordinal=ordinal,
                text=text,
                status=status,
                item_fingerprint=context_fingerprint(
                    "pulsara:todo-item:v1",
                    {"ordinal": ordinal, "text": text, "status": status.value},
                ),
            )
        )
    if counts[TodoStatus.IN_PROGRESS] > 1:
        raise TodoValidationError("todo accepts at most one in_progress item")

    canonical = todo_candidate_canonical_json(tuple(frozen))
    size = len(canonical)
    if size > MAXIMUM_TODO_CANONICAL_JSON_BYTES:
        raise TodoValidationError("todo snapshot exceeds 32 KiB")
    return FrozenTodoCandidate(
        ordered_items=tuple(frozen),
        pending_count=counts[TodoStatus.PENDING],
        in_progress_count=counts[TodoStatus.IN_PROGRESS],
        completed_count=counts[TodoStatus.COMPLETED],
        canonical_json_utf8_bytes=size,
        candidate_fingerprint=context_fingerprint(
            "pulsara:todo-replacement:v1",
            {
                "items": tuple(
                    {"text": item.text, "status": item.status.value}
                    for item in frozen
                )
            },
        ),
    )


def todo_candidate_canonical_json(items: Sequence[FrozenTodoItem]) -> bytes:
    """Return the unique aggregate-byte quote used by every TODO path."""

    return todo_snapshot_canonical_json(
        tuple((item.text, item.status.value) for item in items)
    )


def _validate_text(text: str) -> None:
    if not text:
        raise TodoValidationError("todo item text must not be empty")
    if text != unicodedata.normalize("NFC", text):
        raise TodoValidationError("todo item text must already be Unicode NFC")
    if text != text.strip():
        raise TodoValidationError(
            "todo item text must not have surrounding whitespace"
        )
    if len(text.encode("utf-8")) > MAXIMUM_TODO_TEXT_UTF8_BYTES:
        raise TodoValidationError(
            f"todo item text exceeds {MAXIMUM_TODO_TEXT_UTF8_BYTES} UTF-8 bytes"
        )
    if any(
        character in {"\n", "\r", "\u2028", "\u2029"}
        or ord(character) == 0
        or ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        for character in text
    ):
        raise TodoValidationError("todo item text must be one safe line")


@dataclass(frozen=True, slots=True)
class TodoTool:
    """Thin catalog marker; DirectKernelToolPort owns production execution."""

    name: str = "todo"

    def execute(self, _call: ToolCall) -> ToolExecutionResult:
        raise RuntimeError("todo must execute through its exact-run local-state owner")


__all__ = [
    "FrozenTodoCandidate",
    "FrozenTodoItem",
    "MAXIMUM_TODO_CANONICAL_JSON_BYTES",
    "MAXIMUM_TODO_ITEMS",
    "MAXIMUM_TODO_TEXT_UTF8_BYTES",
    "TodoStatus",
    "TodoTool",
    "TodoValidationError",
    "parse_todo_replacement",
    "todo_candidate_canonical_json",
]
