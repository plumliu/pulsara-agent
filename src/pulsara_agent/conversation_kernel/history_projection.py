"""Cut-aware QA/QAQ construction from immutable canonical ROOT rows."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from pulsara_agent.conversation_kernel.prompt_content import (
    CanonicalPromptImageDescriptor,
    PROMPT_BODY_MEDIA_TYPE,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    validate_canonical_prompt_owner_metadata,
)
from pulsara_agent.conversation_kernel.session_text import (
    decode_saved_text,
    prompt_history_text,
)
from pulsara_agent.llm.input import (
    LLMTextPart,
    PromptAnnotationPart,
    HISTORICAL_IMAGE_MARKER,
)


def canonical_history_message(connection, entry, *, include_tools=False):
    """Keep field coordinates and full annotations; quote never enters the index."""
    kind = entry["entry_kind"]
    atomic_ranges = []
    body_ranges = None
    if kind in {"USER_MESSAGE", "USER_STEER"}:
        if entry["scheduled_input"] is not None:
            return None
        role = "user"
        parts = validate_canonical_prompt_owner_metadata(
            connection, row=entry, transcript_entry_id=entry["id"]
        ).parts
        fields = []
        body_ranges = []
        shown = []
        annotated = any(isinstance(p, PromptAnnotationPart) for p in parts)
        offset = 0
        for i, part in enumerate(parts):
            if isinstance(part, CanonicalPromptImageDescriptor):
                if shown:
                    offset += 1
                shown.append(HISTORICAL_IMAGE_MARKER)
                atomic_ranges.append([offset, offset + len(HISTORICAL_IMAGE_MARKER)])
                offset += len(HISTORICAL_IMAGE_MARKER)
                continue
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
            if entry["content_media_type"] == PROMPT_BODY_MEDIA_TYPE:
                parts = validate_canonical_prompt_owner_metadata(
                    connection, row=entry, transcript_entry_id=entry["id"]
                ).parts
                text, source_segments, image_bounds = prompt_history_text(
                    parts, image_references=None
                )
            else:
                body = entry["inline_content"]
                if body is None:
                    blob = connection.execute(
                        "SELECT body FROM pulsara_v3.blobs WHERE id=%s AND workspace_id=%s",
                        (entry["blob_id"], entry["workspace_id"]),
                    ).fetchone()
                    body = None if blob is None else blob["body"]
                text = decode_saved_text(
                    {
                        "kind": "tool",
                        "content": body,
                        "size": entry["content_size"],
                        "digest": entry["content_digest"],
                        "media_type": entry["content_media_type"],
                    }
                )
                source_segments, image_bounds = ((0, text),), ()
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
                source_segments += ((len(text) + 1, artifact),)
                text += "\n" + artifact
            role, label, shown = "tool", "工具结果", [fact["tool_name"] + "\n" + text]
            prefix_size = len(fact["tool_name"]) + 1
            segments = ((0, fact["tool_name"]),) + tuple(
                (prefix_size + at, value) for at, value in source_segments
            )
            fields = [
                {
                    "block": None,
                    "part": None,
                    "field": "tool_result",
                    "text": value,
                    "display_start": at,
                }
                for at, value in segments
            ]
            body_ranges = [[at, at + len(value)] for at, value in segments]
            atomic_ranges = [
                [prefix_size + a, prefix_size + b] for a, b in image_bounds
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
    from pulsara_agent.conversation_kernel.session_text import visualization_history_text
    descriptions = visualization_history_text(connection, entry, image_references=None)
    if descriptions:
        if body_ranges is None:
            body_ranges = [[0, len("\n".join(shown))]]
        for description in descriptions:
            start = len("\n".join(shown)) + (1 if shown else 0)
            shown.append(description)
            atomic_ranges.append([start, start + len(description)])
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
        "body_ranges": body_ranges
        if body_ranges is not None
        else [[0, len("\n".join(shown))]],
        **({"atomic_ranges": atomic_ranges} if atomic_ranges else {}),
    }


HistoryVariant = tuple[str, bool]


@dataclass(frozen=True, slots=True)
class HistoryBuildSuccess:
    session_id: str
    memory_domain_id: str | None
    q1_entry_id: str
    covered_entry_sequence: int
    documents: dict[HistoryVariant, dict]

    @property
    def expected_variants(self) -> frozenset[HistoryVariant]:
        return frozenset(self.documents)


@dataclass(frozen=True, slots=True)
class HistoryBuildFailure:
    session_id: str
    q1_entry_id: str
    covered_entry_sequence: int
    stage: Literal["Q1", "A"]
    entry_id: str
    error: RuntimeError


type HistoryBuildResult = HistoryBuildSuccess | HistoryBuildFailure


def _assemble_history_document(messages, q2_entry_id):
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
        "q2_entry_id": q2_entry_id,
    }


def _build_history_group(
    connection, session_id, q1_id, *, covered_entry_sequence, tool_modes
) -> HistoryBuildResult:
    if (
        isinstance(covered_entry_sequence, bool)
        or not isinstance(covered_entry_sequence, int)
        or covered_entry_sequence < 0
    ):
        raise ValueError("history coverage must be a nonnegative entry sequence")
    # The caller owns a short RR transaction. A left join retains the actual
    # domain for legal missing-Q1 outcomes, without inventing a deleted owner.
    q1 = connection.execute(
        """SELECT s.memory_domain_id, e.*
           FROM pulsara_v3.sessions s
           LEFT JOIN pulsara_v3.transcript_entries e
             ON e.session_id=s.id AND e.id=%s AND e.entry_sequence<=%s
           WHERE s.id=%s""",
        (q1_id, covered_entry_sequence, session_id),
    ).fetchone()
    domain = None if q1 is None else str(q1["memory_domain_id"])

    def success(documents):
        return HistoryBuildSuccess(
            session_id, domain, q1_id, covered_entry_sequence, documents
        )

    if (
        q1 is None
        or q1["id"] is None
        or q1["scheduled_input"] is not None
        or q1["entry_kind"] != "USER_MESSAGE"
        or q1["conversation_scope_kind"] != "ROOT"
    ):
        return success({})
    # This is the raw first successor, not the first content-qualified one.
    # Scheduled/broken Q2 must not be skipped in favour of a later question.
    q2 = connection.execute(
        """SELECT * FROM pulsara_v3.transcript_entries
           WHERE session_id=%s AND conversation_scope_kind='ROOT'
             AND entry_kind='USER_MESSAGE' AND entry_sequence>%s
             AND entry_sequence<=%s ORDER BY entry_sequence LIMIT 1""",
        (session_id, q1["entry_sequence"], covered_entry_sequence),
    ).fetchone()
    rows = connection.execute(
        """SELECT * FROM pulsara_v3.transcript_entries
           WHERE session_id=%s AND conversation_scope_kind='ROOT'
             AND entry_sequence>=%s AND entry_sequence<=%s
             AND COALESCE(turn_id,imported_history_group_id)=COALESCE(%s,%s)
           ORDER BY entry_sequence""",
        (
            session_id, q1["entry_sequence"], covered_entry_sequence,
            q1["turn_id"], q1["imported_history_group_id"],
        ),
    ).fetchall()
    messages_by_tools = {tools: [] for tools in tool_modes}
    for entry in rows:
        # User content has the same qualification/rendering in both modes.
        # Other entries decode once per requested mode; QAQ reuses the QA result.
        shared_user = entry["entry_kind"] in {"USER_MESSAGE", "USER_STEER"}
        modes = (tool_modes[0],) if shared_user else tool_modes
        for tools in modes:
            try:
                message = canonical_history_message(
                    connection, entry, include_tools=tools
                )
            except RuntimeError as error:
                return HistoryBuildFailure(
                    session_id, q1_id, covered_entry_sequence,
                    "Q1" if entry["id"] == q1_id else "A", entry["id"], error,
                )
            if message is None:
                continue
            message["region"] = "Q1" if entry["id"] == q1_id else "A"
            messages_by_tools[tools].append(message)
            if shared_user:
                for other in tool_modes[1:]:
                    messages_by_tools[other].append(deepcopy(message))
    if not any(
        any(message["region"] == "A" for message in messages)
        for messages in messages_by_tools.values()
    ):
        return success({})
    if q2 is not None and q2["scheduled_input"] is not None:
        q2 = None
    try:
        q2_message = (
            canonical_history_message(connection, q2) if q2 is not None else None
        )
    except RuntimeError:
        # Q2 is optional. Required Q1/A failures above reject the whole group.
        q2_message = None
    q2_id = None if q2_message is None else q2["id"]
    if q2_message is not None:
        q2_message["region"] = "Q2"
    documents = {}
    for tools, messages in messages_by_tools.items():
        if not any(message["region"] == "A" for message in messages):
            continue
        documents[("QA", tools)] = _assemble_history_document(
            deepcopy(messages), q2_id
        )
        if q2_message is not None:
            documents[("QAQ", tools)] = _assemble_history_document(
                deepcopy(messages) + [deepcopy(q2_message)], q2_id
            )
    return success(documents)


def build_history_group(
    connection, session_id, q1_id, *, covered_entry_sequence: int
) -> HistoryBuildResult:
    """Build all applicable variants at one cut; failures never mean empty.

    Required canonical RuntimeError is returned with its original exception.
    Database/IO/cancellation errors propagate to their transaction owner.
    """
    return _build_history_group(
        connection, session_id, q1_id,
        covered_entry_sequence=covered_entry_sequence, tool_modes=(False, True),
    )


def build_history_document(
    connection, session_id, q1_id, *, covered_entry_sequence: int,
    include_tools=False, variant="QAQ",
):
    """Restore only the requested mode at the candidate's own canonical cut."""
    if variant not in {"QA", "QAQ"}:
        raise ValueError("history document variant is invalid")
    result = _build_history_group(
        connection, session_id, q1_id,
        covered_entry_sequence=covered_entry_sequence, tool_modes=(include_tools,),
    )
    if isinstance(result, HistoryBuildFailure):
        return None
    return result.documents.get((variant, include_tools))
