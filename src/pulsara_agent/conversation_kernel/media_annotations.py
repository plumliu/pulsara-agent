"""Read time annotations from their accepted tool result and frozen call."""

from __future__ import annotations

from time import monotonic

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.identity import require_id
from pulsara_agent.ports.session_content import SESSION_QUERY_MAX_ITEMS
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


def read_media_annotations(
    repository,
    *,
    memory_domain_id,
    path,
    modified_ns,
    after,
    deadline_monotonic,
):
    if after is not None:
        require_id(after, "entry")
    # Forks retain the origin entry. Pick one accessible copy, preferring the
    # original, so forking or deleting a conversation cannot multiply a note.
    with (
        repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection,
        connection.cursor(name="media_annotations") as rows,
    ):
        rows.itersize = SESSION_QUERY_MAX_ITEMS
        rows.execute(
            """
            SELECT DISTINCT ON (e.origin_entry_id) e.origin_entry_id, a.tool_arguments,
                   r.result_facts
              FROM pulsara_v3.sessions s
              JOIN pulsara_v3.tool_results r ON r.session_id=s.id
              JOIN pulsara_v3.transcript_entries e ON e.id=r.result_entry_id AND e.session_id=r.session_id
              JOIN pulsara_v3.content_parts a ON a.session_id=r.session_id
               AND a.entry_id=r.tool_call_entry_id AND a.tool_call_id=r.tool_call_id
             WHERE s.memory_domain_id=%s AND s.root_session_id IS NULL
               AND a.type='tool_call' AND a.tool_name='annotate_media'
               AND r.result_state='SUCCESS' AND r.result_origin_kind='PHYSICAL_ATTEMPT'
               AND (%s::text IS NULL OR e.origin_entry_id > %s)
             ORDER BY e.origin_entry_id, (e.id=e.origin_entry_id) DESC, e.id
            """,
            (memory_domain_id, after, after),
        )
        items = []
        last_scanned = after
        for row in rows:
            # A source with many unrelated notes can resume scanning without
            # conflating an unfinished scan with a genuinely empty history.
            if last_scanned != after and monotonic() >= deadline_monotonic:
                return {"items": items, "next": last_scanned}
            last_scanned = row["origin_entry_id"]
            binding = row["result_facts"]
            if binding["canonical_path"] != path or binding["file_mtime_ns"] != modified_ns:
                continue
            if len(items) == SESSION_QUERY_MAX_ITEMS:
                return {"items": items, "next": items[-1]["resultId"]}
            args = row["tool_arguments"]
            items.append(
                {
                    "resultId": row["origin_entry_id"],
                    "from": args["from"],
                    "to": args["to"],
                    "text": args["text"],
                }
            )
        return {"items": items, "next": None}
