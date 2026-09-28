"""Read one terminal worker's public effective scope as advisory material.

The ROOT Fork anchor remains ROOT-only. This reader shares canonical row and
blob content verification but never imports executable provider state.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Mapping

from psycopg import Connection

from pulsara_agent.model_input.contracts import (
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.conversation_kernel.prompt_storage import hydrate_canonical_snapshot_owner
from pulsara_agent.conversation_kernel.compaction.prompt import compaction_snapshot_image_parts
from pulsara_agent.model_input.lowering import compaction_snapshot_sections, lower_retained_request_content


def _public_utf8(row: Mapping[str, object], *, field: str) -> str:
    body = row.get("body")
    if body is None or row.get("content_codec") != "utf-8":
        raise ConversationKernelConflict(f"worker history {field} is unreadable")
    raw = bytes(body)
    if (
        len(raw) != int(row["content_size"])
        or "sha256:" + sha256(raw).hexdigest() != row["content_digest"]
    ):
        raise ConversationKernelConflict(f"worker history {field} failed content verification")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConversationKernelConflict(f"worker history {field} is not UTF-8") from exc


def read_terminal_worker_public_history(
    connection: Connection, *, session_id: str, source_task_id: str,
    through_sequence: int, binding_revision_id: str,
) -> str:
    source = connection.execute(
        """SELECT t.objective, t.parent_context_body, t.dependency_context_body,
                  t.terminal_material_body, t.worker_history_body,
                  t.status,
                  revision.base_kind, revision.source_through_sequence,
                  revision.context_snapshot_id
           FROM pulsara_v3.subagent_tasks AS t
           JOIN pulsara_v3.turns AS turn
             ON turn.session_id=t.session_id AND turn.scope_subagent_task_id=t.id
           JOIN pulsara_v3.turn_context_binding_revisions AS revision
             ON revision.session_id=turn.session_id
            AND revision.turn_id=turn.id
            AND revision.id=%s
           WHERE t.session_id=%s AND t.id=%s""",
        (binding_revision_id, session_id, source_task_id),
    ).fetchone()
    if source is None or source["status"] not in {
        "COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "BLOCKED_DEPENDENCY_FAILED"
    }:
        raise ConversationKernelConflict("worker history source is not terminal or readable")
    floor = int(source["source_through_sequence"]) if source["base_kind"] == "SNAPSHOT" else 0
    if floor > through_sequence:
        raise ConversationKernelConflict("worker history cut precedes adopted compaction")
    snapshot_text: str | None = None
    retained_requests: list[dict[str, object]] = []
    if source["base_kind"] == "SNAPSHOT":
        snapshot = connection.execute(
            """SELECT s.*
               FROM pulsara_v3.context_snapshots AS s
               WHERE s.session_id=%s AND s.id=%s""",
            (session_id, source["context_snapshot_id"]),
        ).fetchone()
        if snapshot is None:
            raise ConversationKernelConflict("worker history adopted snapshot is absent")
        carrier = hydrate_canonical_snapshot_owner(
            connection, row=snapshot, context_snapshot_id=str(source["context_snapshot_id"])
        )
        if compaction_snapshot_image_parts(carrier):
            raise ConversationKernelConflict("worker history contains unsupported multimodal material")
        snapshot_text = carrier.earlier_context_summary
        retained_requests = [
            {"kind": request.item_kind.value, "section": section,
             "text": "".join(part.text for part in lower_retained_request_content(request))}
            for section, request in compaction_snapshot_sections(carrier)
        ]
    image = connection.execute(
        """SELECT 1 FROM pulsara_v3.canonical_image_refs AS image
           JOIN pulsara_v3.transcript_entries AS entry
             ON entry.session_id=image.session_id AND entry.id=image.transcript_entry_id
           WHERE entry.session_id=%s AND entry.scope_subagent_task_id=%s
             AND entry.entry_sequence > %s AND entry.entry_sequence <= %s
           LIMIT 1""",
        (session_id, source_task_id, floor, through_sequence),
    ).fetchone()
    if image is not None:
        raise ConversationKernelConflict("worker history contains unsupported multimodal material")
    rows = connection.execute(
        """SELECT e.id, e.entry_sequence, e.entry_kind, e.content_digest,
                  e.content_size, e.content_codec,
                  COALESCE(e.inline_content, blob.body) AS body,
                  result.tool_call_id, result.result_state
           FROM pulsara_v3.transcript_entries AS e
           LEFT JOIN pulsara_v3.blobs AS blob
             ON blob.id=e.blob_id AND blob.workspace_id=e.workspace_id
           LEFT JOIN pulsara_v3.tool_results AS result
             ON result.session_id=e.session_id AND result.result_entry_id=e.id
           WHERE e.session_id=%s AND e.scope_subagent_task_id=%s
             AND e.entry_sequence > %s AND e.entry_sequence <= %s
           ORDER BY e.entry_sequence LIMIT %s""",
        (session_id, source_task_id, floor, through_sequence, MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS + 1),
    ).fetchall()
    if len(rows) > MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS:
        raise ConversationKernelConflict("worker history exceeds the existing provider input item budget")
    entry_ids = [str(row["id"]) for row in rows]
    blocks_by_entry: dict[str, list[dict[str, object]]] = {}
    if entry_ids:
        blocks = connection.execute(
            """SELECT b.assistant_entry_id, b.block_kind, b.tool_call_id,
                      b.tool_name, b.tool_arguments, b.content_digest,
                      b.content_size, b.content_codec,
                      COALESCE(b.inline_content, blob.body) AS body
               FROM pulsara_v3.assistant_message_blocks AS b
               LEFT JOIN pulsara_v3.blobs AS blob
                 ON blob.id=b.blob_id AND blob.workspace_id=b.workspace_id
               WHERE b.session_id=%s AND b.assistant_entry_id=ANY(%s)
               ORDER BY b.assistant_entry_id, b.block_ordinal""",
            (session_id, entry_ids),
        ).fetchall()
        for block in blocks:
            if block["block_kind"] == "TOOL_CALL":
                value: dict[str, object] = {
                    "kind": "tool_request", "call_id": block["tool_call_id"],
                    "tool_name": block["tool_name"], "arguments": block["tool_arguments"],
                }
            else:
                value = {"kind": str(block["block_kind"]).lower(), "text": _public_utf8(block, field="assistant block")}
            blocks_by_entry.setdefault(str(block["assistant_entry_id"]), []).append(value)
    history: list[dict[str, object]] = []
    settled_calls = {str(row["tool_call_id"]) for row in rows if row["tool_call_id"] is not None}
    for row in rows:
        kind = str(row["entry_kind"])
        value: dict[str, object] = {"entry_id": row["id"], "sequence": row["entry_sequence"], "kind": kind}
        if kind.startswith("ASSISTANT_"):
            value["blocks"] = blocks_by_entry.get(str(row["id"]), [])
            for block in value["blocks"]:
                if block["kind"] == "tool_request" and str(block["call_id"]) not in settled_calls:
                    block["historical_result"] = "UNKNOWN_OR_UNFINISHED"
        else:
            value["text"] = _public_utf8(row, field="entry")
            if kind == "TOOL_RESULT":
                value["tool_call_id"] = row["tool_call_id"]
                value["result_state"] = row["result_state"]
        history.append(value)
    frames = []
    if source["base_kind"] != "SNAPSHOT" and source["worker_history_body"] is not None:
        frames.extend(json.loads(str(source["worker_history_body"]))["pulsara_worker_history"]["frames"])
    frames.append({
            "source_task_id": source_task_id,
            "source_status": source["status"],
            "through_sequence": through_sequence,
            "objective": source["objective"],
            "initial_public_sources": None if source["base_kind"] == "SNAPSHOT" else {
                "parent_context": source["parent_context_body"],
                "dependency_results": source["dependency_context_body"],
                "terminal_material": source["terminal_material_body"],
            },
            "adopted_summary": snapshot_text,
            "retained_requests": retained_requests,
            "adopted_summary_through_sequence": floor if snapshot_text is not None else None,
            "committed_public_entries": history,
    })
    package = {"pulsara_worker_history": {
        "frames": frames,
        "content_semantics": "ADVISORY_HISTORY_NOT_EXECUTABLE",
    }}
    rendered = canonical_json_bytes(package)
    if len(rendered) > MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES:
        raise ConversationKernelConflict("worker history exceeds the existing provider input byte budget")
    return rendered.decode("utf-8")
