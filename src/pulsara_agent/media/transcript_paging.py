"""Complete transcript source pages, with exact source and character positions.

The shared text packer owns Unicode splitting. Its quote renders the entire
JSON result, including the full next invocation and the real call's allowance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Callable

from pulsara_agent.primitives.text_paging import (
    TextPosition,
    line_starts,
    offset_position,
    paginate_text,
    position_offset,
)
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBudget,
    ToolResultPageBoundary,
)


def json_body(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class TranscriptItem:
    key: str
    index: int
    start: float
    end: float
    text: str
    source: str
    speaker: str | None = None
    text_offset: int = 0
    more_text: bool = False

    def fragment(
        self, text: str, position: TextPosition, request: tuple[float, float]
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "from": self.start,
            "to": self.end,
            "text": text,
            "source": self.source,
            "position": {"index": self.index, **position.public_value()},
        }
        if self.speaker is not None:
            value["speaker"] = self.speaker
        if self.start < request[0] or self.end > request[1]:
            value["visible_range"] = {
                "from": max(self.start, request[0]),
                "to": min(self.end, request[1]),
            }
        return value


@dataclass(frozen=True, slots=True)
class TranscriptPosition:
    key: str
    index: int
    text: TextPosition = TextPosition()

    def public_value(self) -> dict[str, object]:
        return {"key": self.key, "index": self.index, **self.text.public_value()}

    @classmethod
    def parse(cls, value: object) -> TranscriptPosition:
        if not isinstance(value, Mapping) or set(value) != {
            "key",
            "index",
            "line",
            "char_offset",
        }:
            raise ValueError("Invalid transcript text position")
        if (
            not isinstance(value["key"], str)
            or not value["key"]
            or type(value["index"]) is not int
            or value["index"] < 0
        ):
            raise ValueError("Invalid transcript source position")
        return cls(
            value["key"],
            value["index"],
            TextPosition(value["line"], value["char_offset"]),
        )


def pack_items(
    items: tuple[TranscriptItem, ...],
    *,
    position: TranscriptPosition | None,
    request: tuple[float, float],
    budget: ToolResultPageBudget,
    payload: Callable[
        [list[dict[str, object]], TranscriptPosition | None], dict[str, object]
    ],
) -> tuple[list[dict[str, object]], TranscriptPosition | None]:
    """Pack original items without changing their real timestamp granularity."""
    start = 0
    if position is not None:
        found = next(
            (
                i
                for i, item in enumerate(items)
                if (item.key, item.index) == (position.key, position.index)
            ),
            None,
        )
        if found is None:
            raise ValueError(
                "Transcript continuation source position is no longer available"
            )
        start = found
    result: list[dict[str, object]] = []
    for offset in range(start, len(items)):
        item = items[offset]
        absolute_offset = (
            position.text.char_offset
            if position is not None and offset == start
            else item.text_offset
        )
        if position is not None and offset == start and position.text.line != 1:
            raise ValueError(
                "Transcript continuation requires its flat original character position"
            )
        relative_offset = absolute_offset - item.text_offset
        if relative_offset < 0 or relative_offset > len(item.text):
            raise ValueError("Transcript fragment starts after its continuation")
        text_position = offset_position(relative_offset, line_starts(item.text))
        following = items[offset + 1] if offset + 1 < len(items) else None

        def next_position(next_text: TextPosition | None) -> TranscriptPosition | None:
            if next_text is not None:
                return TranscriptPosition(
                    item.key,
                    item.index,
                    TextPosition(
                        1, item.text_offset + position_offset(item.text, next_text)
                    ),
                )
            if item.more_text:
                return TranscriptPosition(
                    item.key,
                    item.index,
                    TextPosition(1, item.text_offset + len(item.text)),
                )
            return (
                None
                if following is None
                else TranscriptPosition(following.key, following.index)
            )

        def fits(text: str, next_text: TextPosition | None) -> bool:
            fragment = item.fragment(text, TextPosition(1, absolute_offset), request)
            return budget.fits(
                json_body(payload([*result, fragment], next_position(next_text)))
            )

        try:
            page = paginate_text(item.text, position=text_position, fits=fits)
        except ToolResultPageBoundary:
            if not result:
                raise
            return result, TranscriptPosition(
                item.key, item.index, TextPosition(1, absolute_offset)
            )
        result.append(
            item.fragment(page.text, TextPosition(1, absolute_offset), request)
        )
        if page.next_position is not None or item.more_text:
            return result, next_position(page.next_position)
    if not result and not budget.fits(json_body(payload([], None))):
        raise ToolResultPageBoundary(
            "Transcript control facts do not fit the result allowance"
        )
    return result, None
