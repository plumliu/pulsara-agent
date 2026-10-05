"""Cold session search over canonical visible text; no derived durable authority."""

from __future__ import annotations

import base64
from datetime import datetime
from hashlib import sha256
import heapq
import itertools
import json
import re
from time import monotonic

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.prompt_content import (
    decode_canonical_prompt_body,
)
from pulsara_agent.llm.input import LLMTextPart, PromptAnnotationPart
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.conversation_kernel.root_status import LATEST_ROOT_SUMMARY_SQL

# UI page/preview budgets, never a cap on the corpus searched.
MAX_PAGE_SIZE = 50
SNIPPET_CHARACTERS = 240

_SESSION_SQL = f"""
SELECT s.id, s.title, s.workspace_id, s.memory_domain_id, s.lifecycle,
       s.writer_generation, s.latest_entry_sequence, s.model_call_binding,
       {LATEST_ROOT_SUMMARY_SQL} AS latest_root_turn,
       w.workspace_kind, w.workspace_root, w.workspace_label,
       CASE WHEN s.lifecycle='ARCHIVED' THEN s.updated_at ELSE
         GREATEST(s.created_at, (SELECT e.accepted_at FROM pulsara_v3.transcript_entries e
          WHERE e.session_id=s.id ORDER BY e.entry_sequence DESC LIMIT 1)) END AS updated_at
FROM pulsara_v3.sessions s JOIN pulsara_v3.workspaces w
 ON w.id=s.workspace_id AND w.memory_domain_id=s.memory_domain_id
WHERE s.memory_domain_id=%s AND (%s='ALL' OR s.lifecycle=%s)
"""

_DOCUMENT_SQL = (
    """
WITH sessions AS MATERIALIZED ("""
    + _SESSION_SQL
    + """), documents AS (
 SELECT s.id AS session_id, NULL::text AS entry_id, 0::bigint AS sequence,
        0::integer AS ordinal, 'title'::text AS kind, NULL::bytea AS content,
        NULL::text AS digest, NULL::bigint AS size FROM sessions s
 UNION ALL
 SELECT e.session_id, e.id, e.entry_sequence, 0, 'user',
        COALESCE(e.inline_content,b.body), e.content_digest,e.content_size
 FROM sessions s JOIN pulsara_v3.transcript_entries e ON e.session_id=s.id
 LEFT JOIN pulsara_v3.blobs b ON b.id=e.blob_id AND b.workspace_id=e.workspace_id
 WHERE e.conversation_scope_kind='ROOT' AND e.entry_kind IN ('USER_MESSAGE','USER_STEER')
 UNION ALL
 SELECT e.session_id,e.id,e.entry_sequence,a.block_ordinal,'assistant',
        COALESCE(a.inline_content,b.body),a.content_digest,a.content_size
 FROM sessions s JOIN pulsara_v3.transcript_entries e ON e.session_id=s.id
 JOIN pulsara_v3.assistant_message_blocks a ON a.assistant_entry_id=e.id AND a.session_id=e.session_id
 LEFT JOIN pulsara_v3.blobs b ON b.id=a.blob_id AND b.workspace_id=a.workspace_id
 WHERE e.conversation_scope_kind='ROOT' AND e.entry_kind IN ('ASSISTANT_MESSAGE','ASSISTANT_TOOL_REQUEST')
   AND a.block_kind='TEXT'
)
SELECT s.*,d.entry_id,d.sequence,d.ordinal,d.kind,d.content,d.digest,d.size
FROM sessions s JOIN documents d ON d.session_id=s.id
ORDER BY s.id,d.sequence DESC,d.ordinal DESC
"""
)


def display_title(row):
    suffix = row["id"].removeprefix("session:")
    return row["title"] or f"会话 {(suffix or row['id'])[:8]}"


def _position(row):
    return row["rank"], -row["updated_at"].timestamp(), row["id"]


def _cursor_position(cursor, query, lifecycle):
    if cursor is None:
        return None
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            set(value) != {"query", "lifecycle", "rank", "at", "id"}
            or value["query"] != query
            or value["lifecycle"] != lifecycle
        ):
            raise ValueError()
        if (
            type(value["rank"]) is not int
            or not 0 <= value["rank"] <= 3
            or not isinstance(value["id"], str)
            or not value["id"]
        ):
            raise ValueError()
        at = datetime.fromisoformat(value["at"])
        if at.tzinfo is None:
            raise ValueError()
        return value["rank"], -at.timestamp(), value["id"]
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise ValueError("invalid session search cursor") from exc


def _text(row):
    if row["kind"] == "title":
        return display_title(row)
    content = row["content"]
    if content is None:
        raise RuntimeError("search source content is missing")
    content = bytes(content)
    if (
        len(content) != row["size"]
        or "sha256:" + sha256(content).hexdigest() != row["digest"]
    ):
        raise RuntimeError("search source content is corrupt")
    try:
        if row["kind"] == "assistant":
            return content.decode("utf-8")
        parts = decode_canonical_prompt_body(content).parts
        return "\n".join(
            part.text if isinstance(part, LLMTextPart) else part.comment or ""
            for part in parts
            if isinstance(part, (LLMTextPart, PromptAnnotationPart))
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeError("search source content cannot be decoded") from exc


def search_session_page(
    repository,
    *,
    memory_domain_id,
    query,
    lifecycle="ALL",
    cursor=None,
    limit=20,
    deadline_monotonic,
):
    if not isinstance(query, str) or lifecycle not in ("ALL", "OPEN", "ARCHIVED"):
        raise ValueError("invalid session search")
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_PAGE_SIZE
        or (cursor is not None and not isinstance(cursor, str))
    ):
        raise ValueError("invalid session search page")
    query = query.strip()
    after = _cursor_position(cursor, query, lifecycle)
    patterns = [
        re.compile(re.escape(term), re.IGNORECASE)
        for term in dict.fromkeys(query.split())
    ]
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        isolation_level=IsolationLevel.REPEATABLE_READ,
        deadline_monotonic=deadline_monotonic,
    ) as connection:
        # Server cursor avoids loading the entire corpus into Python at once.
        with connection.cursor(name="session_search", row_factory=dict_row) as stream:
            stream.itersize = MAX_PAGE_SIZE
            stream.execute(
                _DOCUMENT_SQL if patterns else _SESSION_SQL + " ORDER BY s.id",
                (memory_domain_id, lifecycle, lifecycle),
            )

            def matches():
                for _, documents in itertools.groupby(
                    stream, key=lambda row: row["id"]
                ):
                    best = None
                    for row in documents:
                        if monotonic() >= deadline_monotonic:
                            raise TimeoutError("session search deadline exceeded")
                        if not patterns:
                            best = {
                                **row,
                                "rank": 3,
                                "match_kind": "recent",
                                "snippet": "",
                            }
                            continue
                        text = _text(row)
                        hits = [pattern.search(text) for pattern in patterns]
                        if not all(hits):
                            continue
                        rank = (
                            (0 if text.casefold() == query.casefold() else 1)
                            if row["kind"] == "title"
                            else 2
                        )
                        if best is not None and best["rank"] <= rank:
                            continue
                        start = max(0, min(hit.start() for hit in hits) - 70)
                        end = min(len(text), start + SNIPPET_CHARACTERS)
                        best = {
                            key: row[key]
                            for key in (
                                "id",
                                "title",
                                "workspace_id",
                                "memory_domain_id",
                                "lifecycle",
                                "writer_generation",
                                "latest_entry_sequence",
                                "model_call_binding",
                                "workspace_kind",
                                "workspace_root",
                                "workspace_label",
                                "updated_at",
                            )
                        }
                        best.update(
                            rank=rank,
                            match_kind=row["kind"],
                            snippet=("…" if start else "")
                            + text[start:end]
                            + ("…" if end < len(text) else ""),
                        )
                    if best is not None and (after is None or _position(best) > after):
                        yield best

            selected = heapq.nsmallest(limit + 1, matches(), key=_position)
    more = len(selected) > limit
    selected = selected[:limit]
    next_cursor = None
    if more:
        last = selected[-1]
        value = dict(
            query=query,
            lifecycle=lifecycle,
            rank=last["rank"],
            at=last["updated_at"].isoformat(),
            id=last["id"],
        )
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(value, ensure_ascii=False).encode()
        ).decode()
    return selected, next_cursor
