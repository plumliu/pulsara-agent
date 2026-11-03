"""One Unicode line/column pager for files and frozen result bodies."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re
from typing import Callable

from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary


@dataclass(frozen=True, slots=True)
class TextPosition:
    line: int = 1
    char_offset: int = 0

    def __post_init__(self) -> None:
        if type(self.line) is not int or self.line < 1:
            raise ValueError("line must be a positive integer")
        if type(self.char_offset) is not int or self.char_offset < 0:
            raise ValueError("char_offset must be a nonnegative integer")

    def public_value(self) -> dict[str, int]:
        return {"line": self.line, "char_offset": self.char_offset}


@dataclass(frozen=True, slots=True)
class TextPage:
    text: str
    start: TextPosition
    next_position: TextPosition | None
    start_offset: int
    end_offset: int


def line_starts(text: str) -> tuple[int, ...]:
    """CRLF is one line ending; columns count Unicode characters, not bytes."""
    return (0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", text)))


def position_offset(
    text: str, position: TextPosition, starts: tuple[int, ...] | None = None
) -> int:
    starts = line_starts(text) if starts is None else starts
    if position.line > len(starts):
        # A first read past EOF is an empty read, as it was for PATH reads.
        if position.char_offset == 0:
            return len(text)
        raise ValueError("text position is past EOF")
    start = starts[position.line - 1]
    end = starts[position.line] if position.line < len(starts) else len(text)
    if position.char_offset > end - start:
        raise ValueError("char_offset is outside its line")
    return start + position.char_offset


def offset_position(offset: int, starts: tuple[int, ...]) -> TextPosition:
    index = bisect_right(starts, offset) - 1
    return TextPosition(index + 1, offset - starts[index])


def _label_text(text: str, first_line: int) -> str:
    if not text:
        return ""
    starts = line_starts(text)
    parts = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        if start < end:
            parts.append(f"{first_line + index}|{text[start:end]}")
    return "".join(parts)


def paginate_text(
    text: str,
    *,
    position: TextPosition = TextPosition(),
    fits: Callable[[str, TextPosition | None], bool],
    max_chars: int | None = None,
    max_lines: int | None = None,
    line_labels: bool = False,
) -> TextPage:
    """Choose a complete-line prefix, splitting a line only when necessary.

    ``fits`` renders the *whole* caller-specific result including its exact next
    call.  The character ceiling is merely an admission optimization: a Unicode
    character occupies at least one UTF-8 byte, so a native ToolResult cannot
    carry more characters than its existing logical byte boundary.
    """
    if max_chars is not None and (type(max_chars) is not int or max_chars < 1):
        raise ValueError("max_chars must be a positive integer")
    if max_lines is not None and (type(max_lines) is not int or max_lines < 1):
        raise ValueError("max_lines must be a positive integer")
    starts = line_starts(text)
    begin = position_offset(text, position, starts)
    ceiling = len(text)
    if max_lines is not None:
        boundary = position.line - 1 + max_lines
        if boundary < len(starts):
            ceiling = min(ceiling, starts[boundary])
    ceiling = min(
        ceiling, begin + (max_chars or MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES)
    )

    def candidate(end: int) -> tuple[str, TextPosition | None]:
        raw = text[begin:end]
        body = _label_text(raw, position.line) if line_labels else raw
        return body, offset_position(end, starts) if end < len(text) else None

    if begin == len(text):
        body, next_position = candidate(begin)
        if not fits(body, next_position):
            raise ToolResultPageBoundary(
                "Text page metadata does not fit its result budget"
            )
        return TextPage(body, position, next_position, begin, begin)

    # Fit the longest character prefix, then prefer its last complete line.
    low, high = begin + 1, ceiling
    winner: int | None = None
    full_body, full_next = candidate(ceiling)
    if fits(full_body, full_next):
        winner = ceiling
    else:
        while low <= high:
            middle = (low + high) // 2
            body, next_position = candidate(middle)
            if fits(body, next_position):
                winner = middle
                low = middle + 1
            else:
                high = middle - 1
    if winner is None:
        raise ToolResultPageBoundary(
            "A text character and its continuation do not fit the result budget"
        )
    complete = starts[bisect_right(starts, winner) - 1]
    if complete > begin:
        # EOF is also a complete final line even without a trailing newline.
        end = winner if winner == len(text) else complete
        body, next_position = candidate(end)
        if not fits(body, next_position):
            # A shorter next position can have a longer rendered number. Never
            # assume that this metadata change preserves the quote.
            end = winner
    else:
        end = winner
    body, next_position = candidate(end)
    if not fits(body, next_position):
        raise ToolResultPageBoundary(
            "The final text page does not fit its result budget"
        )
    return TextPage(body, position, next_position, begin, end)
