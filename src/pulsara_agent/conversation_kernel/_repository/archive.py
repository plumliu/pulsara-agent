"""Reversible, idle-only session lifecycle; no execution recovery or new records."""

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .deletion import SessionDeletionBusy


# Used both for UI eligibility and, authoritatively, under the session writer lock.
IDLE_SESSION_SQL = """
NOT EXISTS (SELECT 1 FROM pulsara_v3.turns t WHERE t.session_id=s.id AND t.status='RUNNING')
AND NOT EXISTS (SELECT 1 FROM pulsara_v3.prompt_queue_items q WHERE q.session_id=s.id AND q.status='PENDING')
AND NOT EXISTS (SELECT 1 FROM pulsara_v3.subagent_tasks t WHERE t.session_id=s.id
                AND t.status IN ('PENDING_START','WAITING_DEPENDENCY','ACTIVE'))
AND NOT EXISTS (SELECT 1 FROM pulsara_v3.plan_workflows p WHERE p.session_id=s.id AND p.status='ACTIVE')
AND NOT EXISTS (SELECT 1 FROM pulsara_v3.plan_interactions p WHERE p.session_id=s.id AND p.status='OPEN')
AND NOT EXISTS (SELECT 1 FROM pulsara_v3.tool_execution_attempts a WHERE a.session_id=s.id
                AND NOT EXISTS (SELECT 1 FROM pulsara_v3.tool_results r
                                WHERE r.session_id=a.session_id AND r.attempt_id=a.id))
"""


class _SessionArchiveOperations:
    def idle_session_ids(
        self,
        *,
        memory_domain_id: str,
        local_session_ids: tuple[str, ...],
        deadline_monotonic: float,
    ) -> frozenset[str]:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=deadline_monotonic
        ) as connection:
            return frozenset(
                r[0]
                for r in connection.execute(
                    f"SELECT s.id FROM pulsara_v3.sessions s WHERE s.memory_domain_id=%s "
                    f"AND s.lifecycle='OPEN' AND {IDLE_SESSION_SQL} "
                    "AND (s.id=ANY(%s) OR s.writer_lease_owner_id IS NULL "
                    "OR s.writer_lease_expires_at <= clock_timestamp())",
                    (memory_domain_id, list(local_session_ids)),
                ).fetchall()
            )

    def read_session_lifecycle(
        self, *, session_id: str, memory_domain_id: str, deadline_monotonic: float
    ) -> str | None:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                "SELECT lifecycle FROM pulsara_v3.sessions WHERE id=%s AND memory_domain_id=%s",
                (session_id, memory_domain_id),
            ).fetchone()
            return None if row is None else row[0]

    def archive_session(
        self,
        *,
        session_id: str,
        memory_domain_id: str,
        closed_writer: HostWriterGuard | None,
        deadline_monotonic: float,
        check_only: bool = False,
    ) -> str:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.READ_COMMITTED,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """SELECT s.lifecycle, s.writer_generation, s.writer_lease_owner_id,
                          s.writer_lease_expires_at > clock_timestamp() AS lease_live
                   FROM pulsara_v3.sessions s WHERE s.id=%s AND s.memory_domain_id=%s FOR UPDATE""",
                (session_id, memory_domain_id),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if closed_writer is not None:
                if (
                    closed_writer.session_id != session_id
                    or row["writer_generation"] != closed_writer.writer_generation
                    or row["writer_lease_owner_id"]
                    not in (None, closed_writer.writer_owner_id)
                ):
                    raise SessionDeletionBusy("session writer changed")
            elif row["writer_lease_owner_id"] is not None and row["lease_live"]:
                raise SessionDeletionBusy("session has a live writer")
            if row["lifecycle"] == "ARCHIVED":
                return "ARCHIVED"
            # Fresh statement after acquiring the writer lock, not a lock-wait snapshot.
            idle = connection.execute(
                f"SELECT {IDLE_SESSION_SQL} AS idle FROM pulsara_v3.sessions s WHERE s.id=%s",
                (session_id,),
            ).fetchone()["idle"]
            if not idle:
                raise SessionDeletionBusy("session has unfinished work")
            if check_only:
                return "OPEN"
            connection.execute(
                """UPDATE pulsara_v3.sessions SET lifecycle='ARCHIVED', writer_lease_owner_id=NULL,
                   writer_lease_expires_at=NULL, updated_at=clock_timestamp() WHERE id=%s""",
                (session_id,),
            )
            return "ARCHIVED"

    def unarchive_session(
        self, *, session_id: str, memory_domain_id: str, deadline_monotonic: float
    ) -> str:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                "SELECT lifecycle FROM pulsara_v3.sessions WHERE id=%s AND memory_domain_id=%s FOR UPDATE",
                (session_id, memory_domain_id),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if row[0] == "ARCHIVED":
                connection.execute(
                    "UPDATE pulsara_v3.sessions SET lifecycle='OPEN', updated_at=clock_timestamp() WHERE id=%s",
                    (session_id,),
                )
            return "OPEN"
