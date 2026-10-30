"""Active-text reference boundaries shared by prompt body and annotation comments.

Keep these offsets aligned with frontend/lib/skill-reference.ts. This parser only
selects explicit intent; it never reads files or establishes skill availability.
"""
from __future__ import annotations

import json
import re

from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart, PromptAnnotationPart, PromptContent


def explicit_skill_reference_surfaces(content: PromptContent | FrozenPromptContent) -> tuple[str, ...]:
    """Keep independent editing surfaces independent Markdown documents."""
    body = "\n".join(part.text for part in content.parts if isinstance(part, LLMTextPart))
    comments = tuple(
        part.comment
        for part in content.parts
        if isinstance(part, PromptAnnotationPart) and part.comment
    )
    return ((body,) if body else ()) + comments


def explicit_reference_excluded_ranges(text: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    offset = 0
    fence: tuple[str, int, int] | None = None
    for line in re.split(r"(?<=\n)|(?<=\r)(?!\n)", text):
        marker = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1]
                    and re.fullmatch(r" {0,3}(?:`+|~+)\s*", line)):
                ranges.append((fence[2], offset + len(line)))
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]), offset)
        elif re.match(r"\s*>", line):
            ranges.append((offset, offset + len(line)))
        offset += len(line)
    if fence is not None:
        ranges.append((fence[2], len(text)))
    ticks = iter(re.finditer(r"`+", text))
    for start in ticks:
        if any(left <= start.start() < right for left, right in ranges):
            continue
        end = next((match for match in ticks if match[0] == start[0]), None)
        ranges.append((start.start(), len(text) if end is None else end.end()))
    ranges.extend((match.start(), match.end()) for match in re.finditer(
        r"(?:https?://|www\.)[^\s<>]+|\]\([^\n]*?\)", text, re.IGNORECASE))
    # These are existing file-reference markers, whose paths are data rather
    # than another slash command. Keep the validation aligned with the editor.
    for match in re.finditer(r'【本地(文件夹|文件)(?:（只读副本，([0-9]+) bytes）)?：("(?:\\.|[^"\\])*?")】', text):
        if match[2] is not None:
            digits = match[2].lstrip("0") or "0"
            if (match[1] == "文件夹" or len(digits) > 16
                    or (len(digits) == 16 and digits > "9007199254740991")):
                continue
        try:
            path = json.loads(match[3])
        except ValueError:
            continue
        if (isinstance(path, str) and re.match(r"^(?:/|[A-Za-z]:[\\/]|\\\\)", path)
                and not re.search(r"[\x00-\x1f\x7f]", path)
                and any(re.split(r"[\\/]", path))):
            ranges.append((match.start(), match.end()))
    return tuple(ranges)
