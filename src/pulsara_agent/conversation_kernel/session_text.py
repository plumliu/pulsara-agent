"""Shared canonical text decoding for UI discovery and model history queries."""

from hashlib import sha256
import itertools
import re

from pulsara_agent.conversation_kernel.prompt_content import (
    PROMPT_BODY_MEDIA_TYPE,
    decode_canonical_prompt_body,
    CanonicalPromptImageDescriptor,
)
from pulsara_agent.llm.input import (
    LLMTextPart,
    PromptAnnotationPart,
    HISTORICAL_IMAGE_MARKER,
    image_reference_text,
)


def prompt_history_text(parts, *, image_references: bool | None):
    """Project typed parts, keeping generated image spans out of lexical fields.

    None is the caller-neutral search display; bool selects same/other-workspace
    exact reading. Spans belong to this view, not to a stored authorization.
    """
    shown, segments, bounds = [], [], []
    offset = 0
    for part in parts:
        is_image = isinstance(part, CanonicalPromptImageDescriptor)
        if is_image:
            if image_references is None:
                text = HISTORICAL_IMAGE_MARKER
            elif image_references:
                text = image_reference_text(
                    image_ref=part.image_ref,
                    media_type=part.media_type,
                    width=part.width,
                    height=part.height,
                )
            else:
                text = (
                    f"[另一工作区的历史图片；{part.media_type}；"
                    "此处未包含图像内容，不属于当前工作区的图片读取范围]"
                )
        elif isinstance(part, LLMTextPart):
            text = part.text
        elif isinstance(part, PromptAnnotationPart):
            text = part.comment or ""
        else:
            raise TypeError("unknown canonical prompt part")
        if shown:
            offset += 1
        if is_image:
            bounds.append((offset, offset + len(text)))
        else:
            segments.append((offset, text))
        shown.append(text)
        offset += len(text)
    return "\n".join(shown), tuple(segments), tuple(bounds)


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


def visualization_history_text(connection, entry, *, image_references):
    """Metadata-only, neutral image recovery for existing message owners."""
    from pulsara_agent.conversation_kernel.visualization import saved_visualizations
    if entry["entry_kind"] != "ASSISTANT_MESSAGE":
        return ()
    result = []
    for item in saved_visualizations(connection, entry["session_id"], entry["id"]):
        if "image_ref" not in item:
            continue
        descriptor = CanonicalPromptImageDescriptor(item["image_ref"], item["content_digest"], item["content_size"], item["media_type"], item["width"], item["height"])
        description, _, _ = prompt_history_text((descriptor,), image_references=image_references)
        result.append(f"已保存展示项 {item['ordinal'] + 1}：{description}")
    return tuple(result)
