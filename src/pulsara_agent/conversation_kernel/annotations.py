"""Canonical assistant-source admission and transaction-local fork remapping."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from psycopg import Connection

from pulsara_agent.llm.input import (
    FrozenPromptContent,
    PromptAnnotationPart,
    PromptAnnotationSource,
)
from pulsara_agent.conversation_kernel.prompt_storage import _read_body


class AnnotationSourceInvalid(ValueError):
    """A new user annotation does not refer to its session's committed body."""


def utf16_slice(text: str, start: int, end: int) -> str:
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise AnnotationSourceInvalid("批注引用范围无效，请重新选择原文")
    encoded = text.encode("utf-16-le")
    if end * 2 > len(encoded):
        raise AnnotationSourceInvalid("批注引用超出原文范围，请重新选择")
    try:
        return encoded[start * 2 : end * 2].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise AnnotationSourceInvalid("批注不能拆开一个字符，请重新选择") from exc


def validate_annotation_sources(
    connection: Connection,
    *,
    session_id: str,
    content: FrozenPromptContent,
) -> None:
    """Runs within existing ingress transaction; never during history hydration."""
    bodies: dict[str, str] = {}
    for part in content.parts:
        if not isinstance(part, PromptAnnotationPart):
            continue
        source = part.source
        if source is None:
            raise AnnotationSourceInvalid("新建批注缺少来源消息")
        if source.entry_id not in bodies:
            entry = connection.execute(
                """SELECT workspace_id FROM pulsara_v3.transcript_entries
                   WHERE session_id=%s AND id=%s AND entry_kind IN
                   ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')""",
                (session_id, source.entry_id),
            ).fetchone()
            if entry is None:
                raise AnnotationSourceInvalid("批注来源不是当前会话的已保存回复")
            rows = connection.execute(
                """SELECT * FROM pulsara_v3.assistant_message_blocks
                   WHERE session_id=%s AND assistant_entry_id=%s AND block_kind='TEXT'
                   ORDER BY block_ordinal""",
                (session_id, source.entry_id),
            ).fetchall()
            texts = [
                _read_body(
                    connection,
                    row=row,
                    workspace_id=str(entry["workspace_id"]),
                    expected_media_type="text/plain",
                    expected_codec="utf-8",
                ).decode("utf-8")
                for row in rows
            ]
            bodies[source.entry_id] = "\n\n".join(text for text in texts if text)
        if utf16_slice(bodies[source.entry_id], source.start, source.end) != part.quote:
            raise AnnotationSourceInvalid("批注引用与已保存原文不一致，请重新选择")


def remap_annotation_sources(
    content: FrozenPromptContent,
    entry_map: Mapping[str, str],
) -> FrozenPromptContent:
    parts = []
    for part in content.parts:
        if isinstance(part, PromptAnnotationPart) and part.source is not None:
            child_id = entry_map.get(part.source.entry_id)
            part = replace(
                part,
                source=None
                if child_id is None
                else PromptAnnotationSource(
                    child_id,
                    part.source.start,
                    part.source.end,
                ),
            )
        parts.append(part)
    return FrozenPromptContent(tuple(parts))
