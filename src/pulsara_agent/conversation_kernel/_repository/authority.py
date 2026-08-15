"""Workspace and Host-writer authority operations."""

from __future__ import annotations

from datetime import timedelta
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard, WriterLease
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    ConversationKernelConflict,
    StaleHostWriter,
    _utcnow,
)

class _AuthorityOperations:
    def read_session_workspace_id(
        self,
        guard: HostWriterGuard,
        *,
        deadline_monotonic: float,
    ) -> str:
        """Resolve the exact writer-scoped workspace before candidate freeze."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            return self._workspace_id(connection, guard.session_id)

    def acquire_host_writer(
        self,
        *,
        session_id: str,
        workspace_id: str,
        memory_domain_id: str = "u_local",
        writer_owner_id: str,
        lease_seconds: float,
        deadline_monotonic: float,
    ) -> WriterLease:
        if lease_seconds <= 0:
            raise ValueError("writer lease must be finite and positive")
        expires_at = _utcnow() + timedelta(seconds=lease_seconds)
        self._begin_event_batch()
        try:
            with self._provider.connection(
                lane=PostgresConnectionLane.HOST_CONTROL,
                row_factory=dict_row,
                deadline_monotonic=deadline_monotonic,
            ) as connection:
                row = connection.execute(
                    """
                    SELECT id, workspace_id, memory_domain_id, lifecycle, writer_generation,
                           writer_lease_owner_id, writer_lease_expires_at
                    FROM pulsara_v3.sessions
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO pulsara_v3.sessions (
                            id, workspace_id, memory_domain_id, lifecycle, writer_generation,
                            writer_lease_owner_id, writer_lease_expires_at
                        ) VALUES (%s, %s, %s, 'OPEN', 1, %s, %s)
                        """,
                        (session_id, workspace_id, memory_domain_id, writer_owner_id, expires_at),
                    )
                    generation = 1
                else:
                    if str(row["workspace_id"]) != workspace_id:
                        raise ConversationKernelConflict("session workspace conflict")
                    if str(row["memory_domain_id"]) != memory_domain_id:
                        raise ConversationKernelConflict("session memory domain conflict")
                    if str(row["lifecycle"]) != "OPEN":
                        raise ConversationKernelConflict("session is closed")
                    same_live_owner = (
                        row["writer_lease_owner_id"] == writer_owner_id
                        and row["writer_lease_expires_at"] is not None
                        and row["writer_lease_expires_at"] > _utcnow()
                    )
                    if same_live_owner:
                        generation = int(row["writer_generation"])
                    else:
                        generation = int(row["writer_generation"]) + 1
                    connection.execute(
                        """
                        UPDATE pulsara_v3.sessions
                        SET writer_generation = %s,
                            writer_lease_owner_id = %s,
                            writer_lease_expires_at = %s,
                            updated_at = clock_timestamp()
                        WHERE id = %s
                        """,
                        (generation, writer_owner_id, expires_at, session_id),
                    )
                    if not same_live_owner:
                        self._interrupt_prior_generation(
                            connection,
                            guard=HostWriterGuard(
                                session_id=session_id,
                                writer_generation=generation,
                                writer_owner_id=writer_owner_id,
                            ),
                            workspace_id=workspace_id,
                        )
        except BaseException:
            self._finish_event_batch(committed=False)
            raise
        else:
            self._finish_event_batch(committed=True)
        return WriterLease(
            guard=HostWriterGuard(
                session_id=session_id,
                writer_generation=generation,
                writer_owner_id=writer_owner_id,
            ),
            expires_at=expires_at,
        )

    def renew_host_writer(
        self,
        guard: HostWriterGuard,
        *,
        lease_seconds: float,
        memory_domain_id: str | None = None,
        deadline_monotonic: float,
    ) -> WriterLease:
        if lease_seconds <= 0:
            raise ValueError("writer lease must be finite and positive")
        expires_at = _utcnow() + timedelta(seconds=lease_seconds)
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                UPDATE pulsara_v3.sessions
                SET writer_lease_expires_at = %s, updated_at = clock_timestamp()
                WHERE id = %s AND writer_generation = %s
                  AND writer_lease_owner_id = %s AND lifecycle = 'OPEN'
                  AND (%s IS NULL OR memory_domain_id = %s)
                  AND writer_lease_expires_at > clock_timestamp()
                RETURNING writer_generation
                """,
                (
                    expires_at,
                    guard.session_id,
                    guard.writer_generation,
                    guard.writer_owner_id,
                    memory_domain_id,
                    memory_domain_id,
                ),
            ).fetchone()
            if row is None:
                raise StaleHostWriter("host writer lease is stale")
        return WriterLease(guard=guard, expires_at=expires_at)
