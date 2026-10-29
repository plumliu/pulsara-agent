"""Atomic session aggregate deletion; memory and blobs have independent owners."""

from datetime import datetime
from time import monotonic
from typing import Literal

from psycopg import IsolationLevel
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.repository_errors import SessionDeletionBusy
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .locking import lock_canonical_identities, lock_session_admission


class _SessionDeletionOperations:
    def canonical_session_exists(
        self, *, session_id: str, memory_domain_id: str, deadline_monotonic: float
    ) -> bool:
        """Unlike a resumable summary, this includes ARCHIVED sessions."""
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM pulsara_v3.sessions WHERE id=%s AND memory_domain_id=%s",
                    (session_id, memory_domain_id),
                ).fetchone()
                is not None
            )

    def release_host_writer(
        self, guard: HostWriterGuard, *, deadline_monotonic: float
    ) -> None:
        """Called only after physical close; never release a replacement owner."""
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            connection.execute(
                """UPDATE pulsara_v3.sessions
                   SET writer_lease_owner_id=NULL, writer_lease_expires_at=NULL
                   WHERE id=%s AND writer_generation=%s AND writer_lease_owner_id=%s""",
                (guard.session_id, guard.writer_generation, guard.writer_owner_id),
            )

    def delete_session(
        self,
        *,
        session_id: str,
        memory_domain_id: str,
        closed_writer: HostWriterGuard | None,
        deadline_monotonic: float,
        required_lifecycle: Literal["ARCHIVED"] | None,
        idle_before: datetime | None = None,
    ) -> Literal["DELETED", "ABSENT", "NOT_ARCHIVED", "NOT_IDLE"]:
        if closed_writer is not None and closed_writer.session_id != session_id:
            raise ValueError("closed writer belongs to another session")
        if idle_before is not None and idle_before.utcoffset() is None:
            raise ValueError("automatic retirement cutoff must have a timezone")
        while monotonic() < deadline_monotonic:
            try:
                with self._provider.connection(
                    lane=PostgresConnectionLane.HOST_CONTROL,
                    row_factory=dict_row,
                    isolation_level=IsolationLevel.READ_COMMITTED,
                    deadline_monotonic=deadline_monotonic,
                ) as connection:
                    lock_session_admission(connection, session_id=session_id)
                    # Every replan starts here: a prior physical close does not
                    # authorize deleting a writer that took over after rollback.
                    row = connection.execute(
                        """SELECT workspace_id, lifecycle, root_session_id, idle_since,
                                  writer_generation, writer_lease_owner_id,
                                  writer_lease_expires_at > clock_timestamp() AS lease_live
                           FROM pulsara_v3.sessions
                           WHERE id=%s AND memory_domain_id=%s FOR UPDATE""",
                        (session_id, memory_domain_id),
                    ).fetchone()
                    if row is None:
                        return "ABSENT"
                    if idle_before is not None:
                        # Automatic retirement competes with request admission on
                        # this same lock. A scheduler's earlier idle observation
                        # cannot authorize deleting newly accepted work.
                        if (
                            row["root_session_id"] is None
                            or row["idle_since"] is None
                            or row["idle_since"] > idle_before
                        ):
                            return "NOT_IDLE"
                        busy = connection.execute(
                            """SELECT EXISTS(SELECT 1 FROM pulsara_v3.prompt_queue_items
                                 WHERE session_id=%s AND status='PENDING')
                               OR EXISTS(SELECT 1 FROM pulsara_v3.turns
                                 WHERE session_id=%s AND status='RUNNING') AS busy""",
                            (session_id, session_id),
                        ).fetchone()
                        if busy["busy"]:
                            return "NOT_IDLE"
                    # Recheck on every replan under the same row lock used by
                    # unarchive; a UI snapshot never authorizes deleting OPEN.
                    if (
                        required_lifecycle is not None
                        and row["lifecycle"] != required_lifecycle
                    ):
                        return "NOT_ARCHIVED"
                    if closed_writer is not None:
                        if row[
                            "writer_generation"
                        ] != closed_writer.writer_generation or row[
                            "writer_lease_owner_id"
                        ] not in (None, closed_writer.writer_owner_id):
                            raise SessionDeletionBusy(
                                "session writer changed after close"
                            )
                    elif row["writer_lease_owner_id"] is not None and row["lease_live"]:
                        raise SessionDeletionBusy("session has a live writer")
                    children = connection.execute(
                        "SELECT id FROM pulsara_v3.sessions WHERE root_session_id=%s ORDER BY id",
                        (session_id,),
                    ).fetchall()
                    for child in children:
                        lock_session_admission(connection, session_id=str(child["id"]))
                    live_child = connection.execute(
                        """SELECT id FROM pulsara_v3.sessions WHERE root_session_id=%s
                           AND writer_lease_owner_id IS NOT NULL
                           AND writer_lease_expires_at > clock_timestamp()
                           ORDER BY id FOR UPDATE""",
                        (session_id,),
                    ).fetchone()
                    if live_child is not None:
                        raise SessionDeletionBusy("owned Agent has a live writer")
                    lock_canonical_identities(
                        connection,
                        namespace="workspace",
                        memory_domain_id=memory_domain_id,
                        identities=(row["workspace_id"],),
                    )
                    connection.execute(
                        "DELETE FROM pulsara_v3.sessions WHERE id=%s", (session_id,)
                    )
                    connection.execute(
                        """DELETE FROM pulsara_v3.workspaces w
                           WHERE w.memory_domain_id=%s AND w.id=%s
                           AND NOT EXISTS (SELECT 1 FROM pulsara_v3.sessions s
                             WHERE s.memory_domain_id=w.memory_domain_id AND s.workspace_id=w.id)
                           AND NOT EXISTS (SELECT 1 FROM pulsara_v3.memory_facts f
                             WHERE f.memory_domain_id=w.memory_domain_id AND f.project_workspace_id=w.id)""",
                        (memory_domain_id, row["workspace_id"]),
                    )
                    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
                return "DELETED"
            except (DeadlockDetected, SerializationFailure):
                # The existing per-operation database deadline bounds physical
                # work, not history size or a new retry-count quota.
                continue
        raise TimeoutError("session deletion deadline expired")
