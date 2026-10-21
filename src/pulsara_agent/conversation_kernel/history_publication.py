"""Disposable history coverage and publication from freshly read canonical rows.

PostgreSQL owns snapshots, FK settlement, locking and atomic commit. Session
admission reuses the canonical transaction lock owner; no durable work state is
introduced. Each operation has its existing physical database deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from psycopg import IsolationLevel
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.conversation_kernel._repository.locking import lock_session_admission
from pulsara_agent.conversation_kernel.history_projection import (
    HistoryBuildFailure,
    HistoryBuildSuccess,
    build_history_group,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(frozen=True, slots=True)
class HistoryQuestionKey:
    session_id: str
    q1_entry_id: str


@dataclass(frozen=True, slots=True)
class HistoryProjectionPage:
    questions: tuple[HistoryQuestionKey, ...]
    next_cursor: str
    done: bool


def _metadata_complete(row) -> bool:
    """Recognize positive complete coverage; no rows deliberately mean unknown.

    Cut is source coverage, not the last searchable entry. Only changes to this
    owner tail or its original raw successor make an otherwise complete group
    stale. A newer unrelated session head alone never does.
    """
    documents = row["documents"]
    if not documents:
        return False
    cuts = {document[2] for document in documents}
    if len(cuts) != 1:
        return False
    cut = next(iter(cuts))
    if (
        isinstance(cut, bool)
        or not isinstance(cut, int)
        or cut < row["entry_sequence"]
        or cut > row["latest_entry_sequence"]
    ):
        return False
    actual = {(document[0], document[1]) for document in documents}
    if any(v not in {"QA", "QAQ"} or not isinstance(t, bool) for v, t in actual):
        return False
    for document in documents:
        expected = document[3]
        if not isinstance(expected, list):
            return False
        if any(
            not isinstance(item, list)
            or len(item) != 2
            or item[0] not in ("QA", "QAQ")
            or not isinstance(item[1], bool)
            for item in expected
        ):
            return False
        expected_keys = {(item[0], item[1]) for item in expected}
        if len(expected_keys) != len(expected) or expected_keys != actual:
            return False
    return row["owner_tail"] <= cut and (
        row["successor_sequence"] is None or row["successor_sequence"] <= cut
    )


class HistoryProjectionRepository:
    def __init__(self, provider: VerifiedPostgresConnectionProviderProtocol) -> None:
        self._provider = provider

    def build_question(
        self, session_id: str, q1_entry_id: str, *, deadline_monotonic: float
    ) -> HistoryBuildSuccess | HistoryBuildFailure:
        # The head and all required canonical rows come from this same RR.
        # The connection is physically closed before any publish lock is taken.
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            session = connection.execute(
                "SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id=%s",
                (session_id,),
            ).fetchone()
            cut = 0 if session is None else int(session["latest_entry_sequence"])
            return build_history_group(
                connection,
                session_id,
                q1_entry_id,
                covered_entry_sequence=cut,
            )

    def publish(
        self, build: HistoryBuildSuccess, *, deadline_monotonic: float
    ) -> str:
        if not isinstance(build, HistoryBuildSuccess):
            raise TypeError("history publication requires a successful canonical build")
        if build.memory_domain_id is None:
            return "parent_missing"
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            isolation_level=IsolationLevel.READ_COMMITTED,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            # All publishers and strong controls take this same exclusive
            # admission before their first parent row lock. A waiting publisher
            # holds no parent KEY SHARE, including for different questions.
            lock_session_admission(connection, session_id=build.session_id)
            session = connection.execute(
                "SELECT memory_domain_id FROM pulsara_v3.sessions WHERE id=%s FOR KEY SHARE",
                (build.session_id,),
            ).fetchone()
            if session is None or session["memory_domain_id"] != build.memory_domain_id:
                return "parent_missing"
            question = connection.execute(
                """SELECT 1 FROM pulsara_v3.transcript_entries
                   WHERE session_id=%s AND id=%s AND entry_kind='USER_MESSAGE'
                     AND conversation_scope_kind='ROOT'""",
                (build.session_id, build.q1_entry_id),
            ).fetchone()
            if question is None:
                return "question_missing"
            stored = connection.execute(
                """SELECT max(covered_entry_sequence) AS cut
                   FROM pulsara_v3.history_search_documents
                   WHERE session_id=%s AND q1_entry_id=%s""",
                (build.session_id, build.q1_entry_id),
            ).fetchone()["cut"]
            if stored is not None and stored > build.covered_entry_sequence:
                return "stale_skipped"
            # Same-cut replacement repairs incomplete positive groups. An old
            # successful zero group is subject to the same non-regression check.
            connection.execute(
                "DELETE FROM pulsara_v3.history_search_documents WHERE session_id=%s AND q1_entry_id=%s",
                (build.session_id, build.q1_entry_id),
            )
            expected = [[variant, tools] for variant, tools in sorted(build.expected_variants)]
            for (variant, tools), document in sorted(build.documents.items()):
                messages = document["messages"]
                connection.execute(
                    """INSERT INTO pulsara_v3.history_search_documents
                       (id,session_id,q1_entry_id,q2_entry_id,variant,include_tools,
                        entry_ids,identity,body,positions,messages,
                        covered_entry_sequence,expected_variants)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        f"{build.session_id}/{build.q1_entry_id}/{variant}/{int(tools)}",
                        build.session_id,
                        build.q1_entry_id,
                        document["q2_entry_id"],
                        variant,
                        tools,
                        [message["entry_id"] for message in messages],
                        Jsonb(document["identity"]),
                        document["body"],
                        Jsonb(document["positions"]),
                        Jsonb(messages),
                        build.covered_entry_sequence,
                        Jsonb(expected),
                    ),
                )
        return "published"

    def maintain_question(
        self, session_id: str, q1_entry_id: str, *, deadline_monotonic: float
    ) -> str:
        build = self.build_question(
            session_id, q1_entry_id, deadline_monotonic=deadline_monotonic
        )
        if isinstance(build, HistoryBuildFailure):
            logging.getLogger(__name__).warning(
                "History build failed for session %s question %s at %s entry %s",
                session_id,
                q1_entry_id,
                build.stage,
                build.entry_id,
                exc_info=(type(build.error), build.error, build.error.__traceback__),
            )
            return "build_failed"
        return self.publish(build, deadline_monotonic=deadline_monotonic)

    def resolve_history_questions(
        self, session_id: str, entry_ids: tuple[str, ...], *, deadline_monotonic: float
    ) -> tuple[HistoryQuestionKey, ...]:
        if not entry_ids:
            return ()
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """WITH changed AS MATERIALIZED (
                     SELECT id,entry_kind,entry_owner_kind,turn_id,
                            imported_history_group_id,entry_sequence
                     FROM pulsara_v3.transcript_entries
                     WHERE session_id=%s AND id=ANY(%s)
                       AND conversation_scope_kind='ROOT'
                   ), affected AS (
                     SELECT q.id FROM changed e
                     JOIN pulsara_v3.transcript_entries q
                       ON q.session_id=%s AND q.entry_kind='USER_MESSAGE'
                      AND q.conversation_scope_kind='ROOT'
                      AND q.entry_owner_kind=e.entry_owner_kind
                      AND ((e.entry_owner_kind='EXECUTED_TURN' AND q.turn_id=e.turn_id)
                        OR (e.entry_owner_kind='IMPORTED_HISTORY'
                            AND q.imported_history_group_id=e.imported_history_group_id))
                     UNION
                     SELECT previous.id FROM changed e
                     CROSS JOIN LATERAL (
                       SELECT q.id FROM pulsara_v3.transcript_entries q
                       WHERE q.session_id=%s AND q.entry_kind='USER_MESSAGE'
                         AND q.conversation_scope_kind='ROOT'
                         AND q.entry_sequence<e.entry_sequence
                       ORDER BY q.entry_sequence DESC LIMIT 1
                     ) previous WHERE e.entry_kind='USER_MESSAGE'
                   ) SELECT id FROM affected ORDER BY id""",
                (session_id, list(entry_ids), session_id, session_id),
            ).fetchall()
        return tuple(HistoryQuestionKey(session_id, row["id"]) for row in rows)

    def scan_history_questions(
        self, after_id: str, page_size: int, *, deadline_monotonic: float
    ) -> HistoryProjectionPage:
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
            raise ValueError("history scan page size must be positive")
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """WITH questions AS MATERIALIZED (
                     SELECT e.id,e.session_id,e.entry_sequence,e.entry_owner_kind,
                            e.turn_id,e.imported_history_group_id,s.latest_entry_sequence
                     FROM pulsara_v3.transcript_entries e
                     JOIN pulsara_v3.sessions s ON s.id=e.session_id
                     WHERE e.id>%s AND e.conversation_scope_kind='ROOT'
                       AND e.entry_kind='USER_MESSAGE'
                     ORDER BY e.id LIMIT %s
                   ) SELECT q.id,q.session_id,q.entry_sequence,q.latest_entry_sequence,
                     CASE WHEN q.entry_owner_kind='EXECUTED_TURN' THEN
                       (SELECT max(e.entry_sequence) FROM pulsara_v3.transcript_entries e
                        WHERE e.session_id=q.session_id AND e.conversation_scope_kind='ROOT'
                          AND e.entry_owner_kind='EXECUTED_TURN' AND e.turn_id=q.turn_id)
                     ELSE
                       (SELECT max(e.entry_sequence) FROM pulsara_v3.transcript_entries e
                        WHERE e.session_id=q.session_id AND e.conversation_scope_kind='ROOT'
                          AND e.entry_owner_kind='IMPORTED_HISTORY'
                          AND e.imported_history_group_id=q.imported_history_group_id)
                     END AS owner_tail,
                     (SELECT e.entry_sequence FROM pulsara_v3.transcript_entries e
                      WHERE e.session_id=q.session_id AND e.conversation_scope_kind='ROOT'
                        AND e.entry_kind='USER_MESSAGE' AND e.entry_sequence>q.entry_sequence
                      ORDER BY e.entry_sequence LIMIT 1) AS successor_sequence,
                     (SELECT jsonb_agg(jsonb_build_array(d.variant,d.include_tools,
                               d.covered_entry_sequence,d.expected_variants))
                      FROM pulsara_v3.history_search_documents d
                      WHERE d.session_id=q.session_id AND d.q1_entry_id=q.id) AS documents
                   FROM questions q ORDER BY q.id""",
                (after_id, page_size),
            ).fetchall()
        questions = tuple(
            HistoryQuestionKey(row["session_id"], row["id"])
            for row in rows
            if not _metadata_complete(row)
        )
        return HistoryProjectionPage(
            questions=questions,
            next_cursor=rows[-1]["id"] if rows else after_id,
            done=len(rows) < page_size,
        )


__all__ = ["HistoryProjectionRepository", "HistoryProjectionPage", "HistoryQuestionKey"]
