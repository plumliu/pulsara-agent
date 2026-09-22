"""Atomic session aggregate deletion; memory and blobs have independent owners."""

from dataclasses import dataclass
from time import monotonic
from typing import Literal

from psycopg import IsolationLevel
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .locking import lock_canonical_identities


class SessionDeletionBusy(RuntimeError):
    """Deletion has no authority over the current canonical writer."""


@dataclass(frozen=True, slots=True)
class _DeletionSources:
    workspace_ids: tuple[str, ...]
    fact_ids: tuple[str, ...]
    relation_ids: tuple[str, ...]


def _sources(
    connection, session_id: str, domain: str, workspace: str
) -> _DeletionSources:
    relations = connection.execute(
        """SELECT r.id, r.source_fact_id, r.target_fact_id
           FROM pulsara_v3.memory_relations r
           JOIN pulsara_v3.tool_results t ON t.id=r.created_by_tool_result_id
           WHERE t.session_id=%s AND r.memory_domain_id=%s ORDER BY r.id""",
        (session_id, domain),
    ).fetchall()
    endpoints = sorted(
        {str(r[k]) for r in relations for k in ("source_fact_id", "target_fact_id")}
    )
    facts = connection.execute(
        """SELECT f.id, f.context_id FROM pulsara_v3.memory_facts f
           WHERE f.memory_domain_id=%s AND (
             f.id=ANY(%s::text[]) OR EXISTS (
               SELECT 1 FROM pulsara_v3.tool_results t
               WHERE t.id=f.created_by_tool_result_id AND t.session_id=%s
             )) ORDER BY f.id""",
        (domain, endpoints, session_id),
    ).fetchall()
    return _DeletionSources(
        tuple(
            sorted(
                {workspace}
                | {
                    str(f["context_id"])
                    for f in facts
                    if f["context_id"] != "ctx:global"
                }
            )
        ),
        tuple(str(f["id"]) for f in facts),
        tuple(str(r["id"]) for r in relations),
    )


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
    ) -> Literal["DELETED", "ABSENT"]:
        if closed_writer is not None and closed_writer.session_id != session_id:
            raise ValueError("closed writer belongs to another session")
        while monotonic() < deadline_monotonic:
            try:
                with self._provider.connection(
                    lane=PostgresConnectionLane.HOST_CONTROL,
                    row_factory=dict_row,
                    isolation_level=IsolationLevel.READ_COMMITTED,
                    deadline_monotonic=deadline_monotonic,
                ) as connection:
                    # Every replan starts here: a prior physical close does not
                    # authorize deleting a writer that took over after rollback.
                    row = connection.execute(
                        """SELECT workspace_id, writer_generation, writer_lease_owner_id,
                                  writer_lease_expires_at > clock_timestamp() AS lease_live
                           FROM pulsara_v3.sessions
                           WHERE id=%s AND memory_domain_id=%s FOR UPDATE""",
                        (session_id, memory_domain_id),
                    ).fetchone()
                    if row is None:
                        return "ABSENT"
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
                    plan = _sources(
                        connection, session_id, memory_domain_id, row["workspace_id"]
                    )
                    lock_canonical_identities(
                        connection,
                        namespace="workspace",
                        memory_domain_id=memory_domain_id,
                        identities=plan.workspace_ids,
                    )
                    connection.execute(
                        "SELECT id FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s "
                        "AND id=ANY(%s::text[]) ORDER BY id FOR UPDATE",
                        (memory_domain_id, list(plan.fact_ids)),
                    ).fetchall()
                    lock_canonical_identities(
                        connection,
                        namespace="memory-relation",
                        memory_domain_id=memory_domain_id,
                        identities=plan.relation_ids,
                    )
                    if (
                        _sources(
                            connection,
                            session_id,
                            memory_domain_id,
                            row["workspace_id"],
                        )
                        != plan
                    ):
                        connection.rollback()
                        continue
                    connection.execute(
                        "DELETE FROM pulsara_v3.sessions WHERE id=%s", (session_id,)
                    )
                    connection.execute(
                        """DELETE FROM pulsara_v3.workspaces w
                           WHERE w.memory_domain_id=%s AND w.id=ANY(%s::text[])
                           AND NOT EXISTS (SELECT 1 FROM pulsara_v3.sessions s
                             WHERE s.memory_domain_id=w.memory_domain_id AND s.workspace_id=w.id)
                           AND NOT EXISTS (SELECT 1 FROM pulsara_v3.memory_facts f
                             WHERE f.memory_domain_id=w.memory_domain_id AND f.project_workspace_id=w.id)""",
                        (memory_domain_id, list(plan.workspace_ids)),
                    )
                    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
                return "DELETED"
            except (DeadlockDetected, SerializationFailure):
                # The existing per-operation database deadline bounds physical
                # work, not history size or a new retry-count quota.
                continue
        raise TimeoutError("session deletion deadline expired")
