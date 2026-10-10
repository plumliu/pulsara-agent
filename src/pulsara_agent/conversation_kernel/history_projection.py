"""Transaction-local, rebuildable QA/QAQ documents from canonical ROOT rows."""

from __future__ import annotations

from contextlib import contextmanager
import logging

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.conversation_kernel.prompt_content import (
    decode_canonical_prompt_body,
)
from pulsara_agent.conversation_kernel.session_text import decode_saved_text
from pulsara_agent.llm.input import LLMTextPart, PromptAnnotationPart


@contextmanager
def history_projection_savepoint(connection, *, session_id):
    """Discard failed derived writes without aborting the canonical transaction.

    Readers validate against canonical content; an old projection is never
    accepted as a replacement for a failed refresh. No recovery job is needed.
    """
    try:
        with connection.transaction():
            yield
    except Exception:
        logging.getLogger(__name__).warning(
            "History projection refresh failed for session %s", session_id,
            exc_info=True,
        )


def canonical_history_message(connection, entry, *, include_tools=False):
    """Keep field coordinates and full annotations; quote never enters the index."""
    kind = entry["entry_kind"]
    if kind in {"USER_MESSAGE", "USER_STEER"}:
        if entry["scheduled_input"] is not None:
            return None
        role = "user"
        body = entry["inline_content"]
        if body is None:
            body = connection.execute(
                "SELECT body FROM pulsara_v3.blobs WHERE id=%s AND workspace_id=%s",
                (entry["blob_id"], entry["workspace_id"]),
            ).fetchone()["body"]
        # Verify immutable bytes using the existing canonical reader before decoding.
        decode_saved_text(
            {
                "kind": "user",
                "content": body,
                "size": entry["content_size"],
                "digest": entry["content_digest"],
                "media_type": entry["content_media_type"],
            }
        )
        parts = decode_canonical_prompt_body(bytes(body)).parts
        fields = []
        body_ranges = []
        shown = []
        annotated = any(isinstance(p, PromptAnnotationPart) for p in parts)
        offset = 0
        for i, part in enumerate(parts):
            if isinstance(part, LLMTextPart):
                display, searchable, field = part.text, part.text, "text"
                within = 0
            elif isinstance(part, PromptAnnotationPart):
                prefix = (
                    "历史引用（被引用的原文）：\n" + part.quote + "\n历史用户批注：\n"
                )
                display, searchable, field = (
                    prefix + (part.comment or ""),
                    part.comment or "",
                    "comment",
                )
                within = len(prefix)
            else:
                continue
            if shown:
                offset += 1
            if isinstance(part, PromptAnnotationPart):
                quote_start = offset + len("历史引用（被引用的原文）：\n")
                body_ranges.append([quote_start, quote_start + len(part.quote)])
                body_ranges.append([offset + within, offset + len(display)])
            else:
                body_ranges.append([offset, offset + len(display)])
            fields.append(
                {
                    "part": i,
                    "block": None,
                    "field": field,
                    "text": searchable,
                    "display_start": offset + within,
                }
            )
            shown.append(display)
            offset += len(display)
        label = "用户补充" if kind == "USER_STEER" else "用户输入"
    else:
        if kind not in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST", "TOOL_RESULT"}:
            return None
        if kind == "TOOL_RESULT":
            if not include_tools:
                return None
            fact = connection.execute(
                "SELECT r.*,a.tool_name FROM pulsara_v3.tool_results r JOIN pulsara_v3.assistant_message_blocks a ON a.session_id=r.session_id AND a.assistant_entry_id=r.tool_call_entry_id AND a.tool_call_id=r.tool_call_id WHERE r.session_id=%s AND r.result_entry_id=%s",
                (entry["session_id"], entry["id"]),
            ).fetchone()
            if fact is None:
                raise RuntimeError("saved tool result canonical edge is missing")
            if fact["tool_name"] in {
                "search_sessions",
                "search_session_content",
                "read_session_content",
            }:
                return None
            body = entry["inline_content"]
            text = decode_saved_text(
                {
                    "kind": "tool",
                    "content": body,
                    "size": entry["content_size"],
                    "digest": entry["content_digest"],
                    "media_type": entry["content_media_type"],
                }
            )
            if fact["output_artifact_disposition"] in {"AVAILABLE", "INCOMPLETE"}:
                blob = connection.execute(
                    "SELECT body,logical_size,logical_digest FROM pulsara_v3.blobs WHERE id=%s AND workspace_id=%s",
                    (fact["output_artifact_blob_id"], entry["workspace_id"]),
                ).fetchone()
                if blob is None:
                    raise RuntimeError("saved tool artifact is missing")
                artifact = decode_saved_text(
                    {
                        "kind": "tool",
                        "content": blob["body"],
                        "size": blob["logical_size"],
                        "digest": blob["logical_digest"],
                    }
                )
                text += "\n" + artifact
            role, label, shown = "tool", "工具结果", [fact["tool_name"] + "\n" + text]
            fields = [
                {
                    "block": None,
                    "part": None,
                    "field": "tool_result",
                    "text": shown[0],
                    "display_start": 0,
                }
            ]
            annotated = False
        else:
            blocks = connection.execute(
                "SELECT a.*,COALESCE(a.inline_content,b.body) AS content,a.content_size AS size,a.content_digest AS digest,'assistant' AS kind FROM pulsara_v3.assistant_message_blocks a LEFT JOIN pulsara_v3.blobs b ON b.id=a.blob_id AND b.workspace_id=a.workspace_id WHERE a.session_id=%s AND a.assistant_entry_id=%s AND (a.block_kind='TEXT' OR (%s AND a.block_kind='TOOL_CALL')) ORDER BY a.block_ordinal",
                (entry["session_id"], entry["id"], include_tools),
            ).fetchall()
            shown, fields, offset = [], [], 0
            for block in blocks:
                if block["block_kind"] == "TEXT":
                    text = decode_saved_text(block)
                else:
                    import json

                    if block["tool_name"] in {
                        "search_sessions",
                        "search_session_content",
                        "read_session_content",
                    }:
                        continue
                    text = (
                        block["tool_name"]
                        + "\n"
                        + json.dumps(block["tool_arguments"], ensure_ascii=False)
                    )
                if shown:
                    offset += 1
                fields.append(
                    {
                        "block": block["id"],
                        "part": None,
                        "field": "text"
                        if block["block_kind"] == "TEXT"
                        else "tool_call",
                        "text": text,
                        "display_start": offset,
                    }
                )
                shown.append(text)
                offset += len(text)
            if not shown:
                return None
            role, annotated = "assistant", False
            final = connection.execute(
                "SELECT 1 FROM pulsara_v3.turns WHERE session_id=%s AND final_entry_id=%s UNION ALL SELECT 1 FROM pulsara_v3.imported_history_groups WHERE session_id=%s AND final_entry_id=%s",
                (entry["session_id"], entry["id"], entry["session_id"], entry["id"]),
            ).fetchone()
            label = "助手回答" if final is not None else "助手进展"
    return {
        "entry_id": entry["id"],
        "origin_entry_id": entry["origin_entry_id"],
        "sequence": entry["entry_sequence"],
        "role": role,
        "label": label,
        "at": entry["accepted_at"].isoformat(),
        "text": "\n".join(shown),
        "annotated": annotated,
        "fields": fields,
        "body_ranges": body_ranges if role == "user" else [[0, len("\n".join(shown))]],
    }


def build_history_document(
    connection, session_id, q1_id, *, include_tools=False, variant="QAQ"
):
    q1 = connection.execute(
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
        (session_id, q1_id),
    ).fetchone()
    if (
        q1 is None
        or q1["scheduled_input"] is not None
        or q1["entry_kind"] != "USER_MESSAGE"
    ):
        return None
    q2 = connection.execute(
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND conversation_scope_kind='ROOT' AND entry_kind='USER_MESSAGE' AND entry_sequence>%s ORDER BY entry_sequence LIMIT 1",
        (session_id, q1["entry_sequence"]),
    ).fetchone()
    rows = connection.execute(
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND conversation_scope_kind='ROOT' AND entry_sequence>=%s AND COALESCE(turn_id,imported_history_group_id)=COALESCE(%s,%s) ORDER BY entry_sequence",
        (
            session_id,
            q1["entry_sequence"],
            q1["turn_id"],
            q1["imported_history_group_id"],
        ),
    ).fetchall()
    messages = []
    for entry in rows:
        try:
            message = canonical_history_message(
                connection, entry, include_tools=include_tools
            )
        except RuntimeError:
            # An undecodable/corrupt canonical body cannot form a complete candidate.
            return None
        if message is not None:
            message["region"] = "Q1" if entry["id"] == q1_id else "A"
            messages.append(message)
    if not messages or not any(m["region"] == "A" for m in messages):
        return None
    if q2 is not None and q2["scheduled_input"] is not None:
        q2 = None
    # Q2 is optional, and its content qualification precedes branch scoring.
    # A failure to restore Q2 must not discard an otherwise complete QA.
    try:
        q2_message = canonical_history_message(connection, q2) if q2 is not None else None
    except RuntimeError:
        q2_message = None
    if q2_message is None:
        q2 = None
    if variant == "QAQ":
        if q2_message is None:
            return None
        q2_message["region"] = "Q2"
        messages.append(q2_message)
    body, positions, offset = [], [], 0
    for mi, message in enumerate(messages):
        for field in message["fields"]:
            if not field["text"]:
                continue
            if body:
                offset += 1
            length = len(field["text"].encode("utf-8"))
            positions.append(
                {
                    "start": offset,
                    "end": offset + length,
                    "message": mi,
                    "block": field["block"],
                    "part": field["part"],
                    "field": field["field"],
                    "display_start": field["display_start"],
                }
            )
            body.append(field["text"])
            offset += length
    identity = [
        {
            "region": m["region"],
            "origin": m["origin_entry_id"],
            "role": m["role"],
            "label": m["label"],
            "text": m["text"],
        }
        for m in messages
    ]
    return {
        "messages": messages,
        "body": "\n".join(body),
        "positions": positions,
        "identity": identity,
        "q2_entry_id": None if q2 is None else q2["id"],
    }


def refresh_history_projection(connection, *, session_id, entry_sequence):
    """Only the affected question and its predecessor; no full-history scan."""
    with connection.cursor(row_factory=dict_row) as cursor:
        origin = cursor.execute(
            "SELECT q.entry_sequence FROM pulsara_v3.transcript_entries e JOIN pulsara_v3.transcript_entries q ON q.session_id=e.session_id AND COALESCE(q.turn_id,q.imported_history_group_id)=COALESCE(e.turn_id,e.imported_history_group_id) AND q.entry_kind='USER_MESSAGE' AND q.conversation_scope_kind='ROOT' WHERE e.session_id=%s AND e.entry_sequence=%s",
            (session_id, entry_sequence),
        ).fetchone()
        if origin is None:
            return
        questions = cursor.execute(
            "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND conversation_scope_kind='ROOT' AND entry_kind='USER_MESSAGE' AND entry_sequence<=%s ORDER BY entry_sequence DESC LIMIT 2",
            (session_id, origin["entry_sequence"]),
        ).fetchall()
        for question in questions:
            refresh_history_question(cursor, session_id, question["id"])


def refresh_history_question(connection, session_id, q1_id):
    connection.execute(
        "DELETE FROM pulsara_v3.history_search_documents WHERE session_id=%s AND q1_entry_id=%s",
        (session_id, q1_id),
    )
    for tools in (False, True):
        for variant in ("QA", "QAQ"):
            document = build_history_document(
                connection, session_id, q1_id, include_tools=tools, variant=variant
            )
            if document is None:
                continue
            messages = document["messages"]
            connection.execute(
                """INSERT INTO pulsara_v3.history_search_documents(id,session_id,q1_entry_id,q2_entry_id,variant,include_tools,entry_ids,identity,body,positions,messages)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    f"{session_id}/{q1_id}/{variant}/{int(tools)}",
                    session_id,
                    q1_id,
                    document["q2_entry_id"],
                    variant,
                    tools,
                    [m["entry_id"] for m in messages],
                    Jsonb(document["identity"]),
                    document["body"],
                    Jsonb(document["positions"]),
                    Jsonb(messages),
                ),
            )


def rebuild_history_projection(connection, *, page_size=100):
    """Disposable rebuild, keyset pages; page size never bounds corpus size."""
    after = ""
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("DELETE FROM pulsara_v3.history_search_documents")
        while True:
            rows = cursor.execute(
                "SELECT id,session_id FROM pulsara_v3.transcript_entries WHERE conversation_scope_kind='ROOT' AND entry_kind='USER_MESSAGE' AND id>%s ORDER BY id LIMIT %s",
                (after, page_size),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                refresh_history_question(cursor, row["session_id"], row["id"])
            after = rows[-1]["id"]
