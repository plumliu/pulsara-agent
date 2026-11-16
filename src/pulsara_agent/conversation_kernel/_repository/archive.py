"""Reversible, idle-only session lifecycle; no execution recovery or new records."""

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from pulsara_agent.conversation_kernel.repository_errors import (
    SessionDeletionBusy, SessionWriterConflict,
)
from .locking import lock_session_admission


# Used both for UI eligibility and, authoritatively, under the session writer lock.
# Attempts from terminal turns are history, not proof of physical execution.
# The live Host gates separately retain ownership of work still settling.
IDLE_SESSION_SQL = """
NOT EXISTS (
    SELECT 1 FROM pulsara_v3.sessions owned
    WHERE (owned.id=s.id OR owned.root_session_id=s.id) AND (
        EXISTS (SELECT 1 FROM pulsara_v3.turns t
                WHERE t.session_id=owned.id AND t.status='RUNNING')
        OR EXISTS (SELECT 1 FROM pulsara_v3.prompt_queue_items q
                   WHERE q.session_id=owned.id AND q.status='PENDING')
        OR EXISTS (SELECT 1 FROM pulsara_v3.plan_workflows p
                   WHERE p.session_id=owned.id AND p.status='ACTIVE')
        OR EXISTS (SELECT 1 FROM pulsara_v3.plan_interactions p
                   WHERE p.session_id=owned.id AND p.status='OPEN')
    )
)
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
                    "OR s.writer_lease_expires_at <= clock_timestamp()) "
                    "AND NOT EXISTS (SELECT 1 FROM pulsara_v3.sessions child "
                    "WHERE child.root_session_id=s.id AND child.writer_lease_owner_id IS NOT NULL "
                    "AND child.writer_lease_expires_at > clock_timestamp() "
                    "AND NOT (child.id=ANY(%s)))",
                    (memory_domain_id, list(local_session_ids), list(local_session_ids)),
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
        child_writers: tuple[HostWriterGuard, ...] = (),
    ) -> str:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.READ_COMMITTED,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            lock_session_admission(connection, session_id=session_id)
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
                    raise SessionWriterConflict("session writer changed")
            elif row["writer_lease_owner_id"] is not None and row["lease_live"]:
                raise SessionWriterConflict("session has a live writer")
            if row["lifecycle"] == "ARCHIVED":
                return "ARCHIVED"
            # ROOT admission also serializes new Agent creation. Lock existing
            # children in the same order as delegation/deletion before checking
            # their current work or accepting their exact gated Host owners.
            children = connection.execute(
                "SELECT id FROM pulsara_v3.sessions WHERE root_session_id=%s ORDER BY id",
                (session_id,),
            ).fetchall()
            for child in children:
                lock_session_admission(connection, session_id=child["id"])
            children = connection.execute(
                """SELECT id, writer_generation, writer_lease_owner_id,
                          writer_lease_expires_at > clock_timestamp() AS lease_live
                   FROM pulsara_v3.sessions WHERE root_session_id=%s ORDER BY id FOR UPDATE""",
                (session_id,),
            ).fetchall()
            expected_children = {guard.session_id: guard for guard in child_writers}
            if len(expected_children) != len(child_writers):
                raise SessionWriterConflict("multiple Agent Host owners have not settled")
            for child in children:
                guard = expected_children.pop(child["id"], None)
                if guard is not None:
                    if (
                        child["writer_generation"] != guard.writer_generation
                        or child["writer_lease_owner_id"] not in (None, guard.writer_owner_id)
                    ):
                        raise SessionWriterConflict("owned Agent writer changed")
                elif child["writer_lease_owner_id"] is not None and child["lease_live"]:
                    raise SessionWriterConflict("owned Agent has a live writer")
            if expected_children:
                raise SessionWriterConflict("owned Agent is unavailable")
            # Fresh statement after acquiring the writer lock, not a lock-wait snapshot.
            idle = connection.execute(
                f"SELECT {IDLE_SESSION_SQL} AS idle FROM pulsara_v3.sessions s WHERE s.id=%s",
                (session_id,),
            ).fetchone()["idle"]
            if not idle:
                raise SessionDeletionBusy("session has unfinished work")
            if check_only:
                return "OPEN"
            connection.execute("DELETE FROM pulsara_v3.scheduled_tasks WHERE session_id=%s", (session_id,))
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
            lock_session_admission(connection, session_id=session_id)
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
