"""One frozen-scope BM25 search and canonical refetch owner for history."""

from __future__ import annotations
from dataclasses import dataclass
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
    after=None,
):
    current = connection.execute(
        "SELECT s.*,w.workspace_kind FROM pulsara_v3.sessions s JOIN pulsara_v3.workspaces w ON w.memory_domain_id=s.memory_domain_id AND w.id=s.workspace_id WHERE s.id=%s AND s.memory_domain_id=%s",
        (scope.session_id, scope.memory_domain_id),
    ).fetchone()
    if current is None:
        raise ValueError("history caller is no longer authorized")
    visible = []
    if automatic and scope.visible_entry_ids:
        entries = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=ANY(%s)",
            (scope.session_id, list(scope.visible_entry_ids)),
        ).fetchall()
        for entry in entries:
            m = canonical_history_message(connection, entry)
            if m is not None:
                visible.append(
                    {
                        "origin": m["origin_entry_id"],
                        "role": m["role"],
                        "label": m["label"],
                        "text": m["text"],
                    }
                )
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
    continuation = ""
    if after is not None:
        score, session, q1 = after
        continuation = (
            " AND (score<%s OR (score=%s AND (session_id,q1_entry_id)>(%s,%s)))"
        )
        params.extend([score, score, session, q1])
    params.append(limit)
    rows = connection.execute(
        f"""WITH matched AS MATERIALIZED (
      SELECT d.*,s.title,paradedb.score(d.id)::double precision AS score
      FROM pulsara_v3.history_search_documents d JOIN pulsara_v3.sessions s ON s.id=d.session_id
       JOIN pulsara_v3.workspaces w ON w.id=s.workspace_id AND w.memory_domain_id=s.memory_domain_id
      WHERE {lexical_predicate("d.body")} AND {" AND ".join(filters)}
    ), folded AS (SELECT *,row_number() OVER(PARTITION BY identity ORDER BY session_id,q1_entry_id) AS representative FROM matched)
    SELECT * FROM folded WHERE representative=1 {continuation}
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


def refetch_history(connection, row, *, scope, include_tools=False):
    canonical = build_history_document(
        connection,
        row["session_id"],
        row["q1_entry_id"],
        include_tools=include_tools,
        variant=row["variant"],
    )
    if canonical is None or any(
        canonical[key] != row[key]
        for key in ("body", "messages", "positions", "identity", "q2_entry_id")
    ):
        return None
    for m in canonical["messages"]:
        if m["entry_id"] in scope.visible_entry_ids or (
            row["session_id"] == scope.session_id
            and (
                m["sequence"] > scope.current_range.through_sequence
                or m["entry_id"] == scope.current_range.excluded_entry_id
            )
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
    )


class PostgresHistoryQuery:
    def __init__(self, provider):
        self.provider = provider

    def automatic_candidates(self, *, scope, query, deadline_monotonic):
        with self.provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = search_history(
                connection, scope=scope, query=query, limit=20, automatic=True
            )
            return tuple(
                candidate
                for row in rows
                if (candidate := refetch_history(connection, row, scope=scope))
                is not None
            )

    def canonical_refetch(self, *, scope, candidates, deadline_monotonic):
        result = []
        with self.provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            for candidate in candidates:
                authorized = connection.execute(
                    "SELECT 1 FROM pulsara_v3.sessions WHERE id=%s AND memory_domain_id=%s",
                    (candidate.session_id, scope.memory_domain_id),
                ).fetchone()
                if authorized is None:
                    continue
                canonical = build_history_document(
                    connection,
                    candidate.session_id,
                    candidate.q1_entry_id,
                    variant="QAQ"
                    if any(m["region"] == "Q2" for m in candidate.document["messages"])
                    else "QA",
                )
                if canonical == candidate.document:
                    result.append(candidate)
        return tuple(result)
