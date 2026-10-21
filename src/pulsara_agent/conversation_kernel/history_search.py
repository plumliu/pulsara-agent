"""One frozen-scope BM25 search and canonical refetch owner for history."""

from __future__ import annotations
from dataclasses import dataclass
from psycopg import IsolationLevel
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pulsara_agent.ports.session_content import SessionContentRange
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.retrieval.pg_search import lexical_predicate, lexical_parameters
from pulsara_agent.conversation_kernel.history_projection import (
    build_history_document,
    canonical_history_message,
)
from pulsara_agent.conversation_kernel.history_excerpt import (
    HistoryCandidate,
    excerpt_history,
)
from pulsara_agent.conversation_kernel.session_text import display_title


@dataclass(frozen=True, slots=True)
class FrozenHistoryReadScope:
    session_id: str
    memory_domain_id: str
    current_range: SessionContentRange
    visible_entry_ids: tuple[str, ...] = ()


def search_history(
    connection,
    *,
    scope,
    query,
    limit,
    target_session_id=None,
    automatic=False,
    include_tools=False,
    excluded_question_ids=(),
):
    current = connection.execute(
        "SELECT s.*,w.workspace_kind FROM pulsara_v3.sessions s JOIN pulsara_v3.workspaces w ON w.memory_domain_id=s.memory_domain_id AND w.id=s.workspace_id WHERE s.id=%s AND s.memory_domain_id=%s",
        (scope.session_id, scope.memory_domain_id),
    ).fetchone()
    if current is None:
        raise ValueError("history caller is no longer authorized")
    visible = _visible_history_identities(connection, scope) if automatic else []
    # All eligibility and copy folding precede LIMIT. QA/QAQ use one scoring
    # index and mutually exclusive branches; excluded Q2 never contributes.
    eligible = """e.session_id=d.session_id AND e.conversation_scope_kind='ROOT'
        AND (e.session_id<>%s OR (e.entry_sequence<=%s AND e.id IS DISTINCT FROM %s))"""
    params = [*lexical_parameters(query), scope.memory_domain_id, include_tools]
    filters = ["s.memory_domain_id=%s", "d.include_tools=%s"]
    if target_session_id is not None:
        filters.append("s.id=%s")
        params.append(target_session_id)
    if automatic:
        filters.append(
            "(w.workspace_kind=%s AND (%s='transient' OR s.workspace_id=%s))"
        )
        params.extend(
            [
                current["workspace_kind"],
                current["workspace_kind"],
                current["workspace_id"],
            ]
        )
    filters.append(
        f"NOT EXISTS(SELECT 1 FROM unnest(d.entry_ids) x(id) LEFT JOIN pulsara_v3.transcript_entries e ON e.id=x.id WHERE e.id IS NULL OR NOT ({eligible}))"
    )
    params.extend(
        [
            scope.session_id,
            scope.current_range.through_sequence,
            scope.current_range.excluded_entry_id,
        ]
    )
    q2_eligible = eligible
    if visible:
        q2_eligible += """ AND NOT EXISTS(
            SELECT 1 FROM pulsara_v3.history_search_documents full_doc,
            jsonb_array_elements(full_doc.identity) q,jsonb_array_elements(%s::jsonb) v
            WHERE full_doc.session_id=d.session_id AND full_doc.q1_entry_id=d.q1_entry_id
            AND full_doc.variant='QAQ' AND full_doc.include_tools=d.include_tools
            AND q->>'region'='Q2' AND q-'region'=v)"""
    filters.append(
        f"((d.variant='QAQ' AND EXISTS(SELECT 1 FROM pulsara_v3.transcript_entries e WHERE e.id=d.q2_entry_id AND {q2_eligible})) OR (d.variant='QA' AND NOT EXISTS(SELECT 1 FROM pulsara_v3.transcript_entries e WHERE e.id=d.q2_entry_id AND {q2_eligible})))"
    )
    for _ in range(2):
        params.extend(
            [
                scope.session_id,
                scope.current_range.through_sequence,
                scope.current_range.excluded_entry_id,
            ]
        )
        if visible:
            params.append(Jsonb(visible))
    if visible:
        filters.append(
            "NOT EXISTS(SELECT 1 FROM jsonb_array_elements(d.identity) m JOIN jsonb_array_elements(%s::jsonb) v ON m-'region'=v)"
        )
        params.append(Jsonb(visible))
    # Re-score unseen units on each page. Neither old scores nor ordinal
    # offsets survive corpus-statistic changes between model/tool calls.
    unconsumed = ""
    if excluded_question_ids:
        # Apply after scoring and copy folding: consumed IDs must not become
        # part of the lexical engine query and change the scoring expression.
        unconsumed = " AND NOT (q1_entry_id=ANY(%s::text[]))"
        params.append(list(excluded_question_ids))
    params.append(limit)
    rows = connection.execute(
        f"""WITH matched AS MATERIALIZED (
      SELECT d.*,s.title,paradedb.score(d.id)::double precision AS score
      FROM pulsara_v3.history_search_documents d JOIN pulsara_v3.sessions s ON s.id=d.session_id
       JOIN pulsara_v3.workspaces w ON w.id=s.workspace_id AND w.memory_domain_id=s.memory_domain_id
      WHERE {lexical_predicate("d.body")} AND {" AND ".join(filters)}
    ), folded AS (SELECT *,row_number() OVER(PARTITION BY identity ORDER BY session_id,q1_entry_id) AS representative FROM matched)
    SELECT * FROM folded WHERE representative=1 {unconsumed}
    ORDER BY score DESC,session_id,q1_entry_id LIMIT %s""",
        params,
    ).fetchall()
    # Positions are needed only for already admitted units. This is a bounded
    # positional read of those IDs, never a candidate refill beyond LIMIT.
    if rows:
        positioned = connection.execute(
            f"""SELECT d.id,d.body,
              paradedb.snippet_positions(d.body) AS hits,
              paradedb.snippet_positions(d.body_en) AS hits_en,
              paradedb.snippet_positions(d.body_tech) AS hits_tech
            FROM pulsara_v3.history_search_documents d
            WHERE {lexical_predicate("d.body")} AND d.id=ANY(%s)""",
            (*lexical_parameters(query), [row["id"] for row in rows]),
        ).fetchall()
        positions = {row["id"]: row for row in positioned}
        for row in rows:
            matched = positions.get(row["id"])
            for field in ("hits", "hits_en", "hits_tech"):
                row[field] = (
                    matched[field]
                    if matched is not None and matched["body"] == row["body"]
                    else ()
                )
    return rows


def _visible_history_identities(connection, scope):
    visible = []
    if scope.visible_entry_ids:
        entries = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=ANY(%s)",
            (scope.session_id, list(scope.visible_entry_ids)),
        ).fetchall()
        for entry in entries:
            message = canonical_history_message(connection, entry)
            if message is not None:
                visible.append({
                    "origin": message["origin_entry_id"], "role": message["role"],
                    "label": message["label"], "text": message["text"],
                })
    return visible


def _history_document_is_readable(
    connection, *, session_id, document, variant, scope, automatic,
):
    """Current admission, independent of the candidate's immutable source cut.

    Both reads use this owner. The second read cannot borrow authorization,
    workspace or visibility from a snapshot retained across remote ranking.
    """
    sessions = connection.execute(
        """SELECT s.id,s.workspace_id,w.workspace_kind
        FROM pulsara_v3.sessions s JOIN pulsara_v3.workspaces w
          ON w.id=s.workspace_id AND w.memory_domain_id=s.memory_domain_id
        WHERE s.id=ANY(%s) AND s.memory_domain_id=%s""",
        ([scope.session_id, session_id], scope.memory_domain_id),
    ).fetchall()
    sessions = {row["id"]: row for row in sessions}
    caller, target = sessions.get(scope.session_id), sessions.get(session_id)
    if caller is None or target is None:
        return False
    if automatic and (
        target["workspace_kind"] != caller["workspace_kind"]
        or (caller["workspace_kind"] != "transient"
            and target["workspace_id"] != caller["workspace_id"])
    ):
        return False
    visible = _visible_history_identities(connection, scope) if automatic else []

    def readable(message):
        if message["entry_id"] in scope.visible_entry_ids or (
            session_id == scope.session_id and (
                message["sequence"] > scope.current_range.through_sequence
                or message["entry_id"] == scope.current_range.excluded_entry_id
            )
        ):
            return False
        return {
            "origin": message["origin_entry_id"], "role": message["role"],
            "label": message["label"], "text": message["text"],
        } not in visible

    if not all(readable(message) for message in document["messages"]):
        return False
    # QA deliberately has no Q2 message, but retains its cut-qualified Q2 ID.
    # Repeat the mutually exclusive branch admission against this read's scope.
    q2_readable = False
    if document["q2_entry_id"] is not None:
        entry = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s AND conversation_scope_kind='ROOT'",
            (session_id, document["q2_entry_id"]),
        ).fetchone()
        if entry is not None:
            try:
                q2 = canonical_history_message(connection, entry)
            except RuntimeError:
                q2 = None
            q2_readable = q2 is not None and readable(q2)
    return q2_readable if variant == "QAQ" else not q2_readable


def refetch_history(
    connection, row, *, scope, include_tools=False, automatic=False,
):
    if row["include_tools"] != include_tools:
        return None
    canonical = build_history_document(
        connection,
        row["session_id"],
        row["q1_entry_id"],
        covered_entry_sequence=row["covered_entry_sequence"],
        include_tools=include_tools,
        variant=row["variant"],
    )
    if canonical is None or any(
        canonical[key] != row[key]
        for key in ("body", "messages", "positions", "identity", "q2_entry_id")
    ):
        return None
    if not _history_document_is_readable(
        connection, session_id=row["session_id"], document=canonical,
        variant=row["variant"], scope=scope, automatic=automatic,
    ):
        return None
    excerpt = excerpt_history(
        canonical, (row["hits"], row["hits_en"], row["hits_tech"])
    )
    if excerpt is None:
        return None
    return HistoryCandidate(
        row["session_id"],
        row["q1_entry_id"],
        display_title({"id": row["session_id"], "title": row["title"]})
        if row["title"] is not None
        else "历史会话",
        float(row["score"]),
        canonical,
        excerpt,
        row["covered_entry_sequence"],
        row["variant"],
        row["include_tools"],
    )


class PostgresHistoryQuery:
    def __init__(self, provider):
        self.provider = provider

    def automatic_candidates(self, *, scope, query, deadline_monotonic):
        with self.provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            rows = search_history(
                connection, scope=scope, query=query, limit=20, automatic=True
            )
            return tuple(
                candidate
                for row in rows
                if (candidate := refetch_history(
                    connection, row, scope=scope, automatic=True,
                )) is not None
            )

    def canonical_refetch(self, *, scope, candidates, deadline_monotonic):
        result = []
        with self.provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            for candidate in candidates:
                canonical = build_history_document(
                    connection,
                    candidate.session_id,
                    candidate.q1_entry_id,
                    covered_entry_sequence=candidate.covered_entry_sequence,
                    include_tools=candidate.include_tools,
                    variant=candidate.variant,
                )
                if canonical == candidate.document and _history_document_is_readable(
                    connection, session_id=candidate.session_id, document=canonical,
                    variant=candidate.variant, scope=scope, automatic=True,
                ):
                    result.append(candidate)
        return tuple(result)
