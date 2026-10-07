"""Shared canonical text decoding for UI discovery and model history queries."""

from hashlib import sha256
import itertools
import re

from pulsara_agent.conversation_kernel.prompt_content import (
    PROMPT_BODY_MEDIA_TYPE,
    decode_canonical_prompt_body,
)
from pulsara_agent.llm.input import LLMTextPart, PromptAnnotationPart


def display_title(row):
    suffix = row["id"].removeprefix("session:")
    return row["title"] or f"会话 {(suffix or row['id'])[:8]}"


def decode_saved_text(row):
    if row["kind"] == "title":
        return display_title(row)
    content = row["content"]
    if content is None:
        raise RuntimeError("saved session content is missing")
    content = bytes(content)
    if (
        len(content) != row["size"]
        or "sha256:" + sha256(content).hexdigest() != row["digest"]
    ):
        raise RuntimeError("saved session content is corrupt")
    try:
        if row["kind"] == "user" or row.get("media_type") == PROMPT_BODY_MEDIA_TYPE:
            return "\n".join(
                p.text if isinstance(p, LLMTextPart) else p.comment or ""
                for p in decode_canonical_prompt_body(content).parts
                if isinstance(p, (LLMTextPart, PromptAnnotationPart))
            )
        return content.decode("utf-8")
    except (ValueError, TypeError) as exc:
        raise RuntimeError("saved session content cannot be decoded") from exc


def entry_documents(rows):
    """Rows ordered by entry and ascending block ordinal; yield one message view."""
    for _, group in itertools.groupby(rows, key=lambda row: row["entry_id"]):
        first = None
        texts = []
        for row in group:
            if first is None:
                first = row
            texts.append(decode_saved_text(row))
        yield {**first, "text": "\n".join(texts)}


def keyword_patterns(query):
    return [
        re.compile(re.escape(term), re.IGNORECASE)
        for term in dict.fromkeys(query.split())
    ]


def match_position(patterns, segments):
    """All terms in source segments; generated section labels cannot be hits."""
    positions = []
    for pattern in patterns:
        match = next(
            (
                offset + m.start()
                for offset, text in segments
                if (m := pattern.search(text)) is not None
            ),
            None,
        )
        if match is None:
            return None
        positions.append(match)
    return min(positions) if positions else 0
