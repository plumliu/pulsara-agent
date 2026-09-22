"""Workspace and Host-writer authority operations."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal
from psycopg.types.json import Jsonb
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.contracts import (
    HostWriterAcquisitionKind,
    HostWriterGuard,
    WriterLease,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_from_dict,
    model_call_binding_to_dict,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    ConversationKernelConflict,
    StaleHostWriter,
    _utcnow,
)
from .locking import lock_canonical_identities

class _AuthorityOperations:
    def validate_host_writer(
        self,
        guard: HostWriterGuard,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Confirm that an exact Host writer still owns the open session."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._require_writer(connection, guard, lock=False)

    def read_session_model_call_binding(
        self,
        guard: HostWriterGuard,
        *,
        deadline_monotonic: float,
    ) -> ModelCallBinding | None:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = self._require_writer(connection, guard, lock=False)
            return model_call_binding_from_dict(row["model_call_binding"])

    def update_session_model_call_binding(
        self,
        guard: HostWriterGuard,
        *,
        binding: ModelCallBinding,
        deadline_monotonic: float,
    ) -> ModelCallBinding:
        """Replace the next-NEW_TURN choice under the canonical session lock."""

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            connection.execute(
                """
                UPDATE pulsara_v3.sessions
                SET model_call_binding=%s, updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (Jsonb(model_call_binding_to_dict(binding)), guard.session_id),
            )
        return binding

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
        intent: Literal["NEW", "EXISTING"],
        session_id: str,
        workspace_id: str,
        workspace_kind: str = "project",
        workspace_root: str | None = None,
        workspace_label: str | None = None,
        memory_domain_id: str = "u_local",
        writer_owner_id: str,
        lease_seconds: float,
        deadline_monotonic: float,
    ) -> WriterLease:
        if intent not in {"NEW", "EXISTING"}:
            raise ValueError("explicit Host writer acquisition intent is required")
        if lease_seconds <= 0:
            raise ValueError("writer lease must be finite and positive")
        if workspace_kind not in {"project", "transient"}:
            raise ValueError("workspace kind is invalid")
        normalized_workspace_root = workspace_root or workspace_id
        if workspace_kind == "project":
            root_path = Path(normalized_workspace_root)
            canonical_label = root_path.name or root_path.as_posix()
            normalized_workspace_label = workspace_label or canonical_label
        else:
            normalized_workspace_label = workspace_label or workspace_id
        if not normalized_workspace_root or not normalized_workspace_label:
            raise ValueError("workspace metadata is incomplete")
        if workspace_kind == "project":
            if normalized_workspace_label != canonical_label:
                raise ValueError("project workspace label must be derived from its root")
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
                if intent == "EXISTING" and row is None:
                    raise ConversationKernelConflict("session is unavailable")
                if intent == "NEW" and row is not None:
                    raise ConversationKernelConflict("new session identity already exists")
                # For an existing Session this follows the frozen
                # session -> workspace order; for first creation there is no
                # Session row to lock yet. The workspace is immutable and the
                # runtime role intentionally has no UPDATE grant, so all
                # workspace owners share this transaction-local identity lock.
                lock_canonical_identities(
                    connection,
                    namespace="workspace",
                    memory_domain_id=memory_domain_id,
                    identities=(workspace_id,),
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.workspaces (
                        memory_domain_id, id, workspace_kind,
                        workspace_root, workspace_label
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        memory_domain_id,
                        workspace_id,
                        workspace_kind,
                        normalized_workspace_root,
                        normalized_workspace_label,
                    ),
                )
                workspace = connection.execute(
                    """
                    SELECT id, workspace_kind, workspace_root, workspace_label
                    FROM pulsara_v3.workspaces
                    WHERE memory_domain_id=%s AND id=%s
                    """,
                    (memory_domain_id, workspace_id),
                ).fetchone()
                if workspace is None or (
                    str(workspace["workspace_kind"]) != workspace_kind
                    or str(workspace["workspace_root"]) != normalized_workspace_root
                    or str(workspace["workspace_label"]) != normalized_workspace_label
                ):
                    raise ConversationKernelConflict(
                        "workspace canonical metadata conflict"
                    )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO pulsara_v3.sessions (
                            id, workspace_id, memory_domain_id,
                            lifecycle, writer_generation,
                            writer_lease_owner_id, writer_lease_expires_at
                        ) VALUES (%s, %s, %s, 'OPEN', 1, %s, %s)
                        """,
                        (
                            session_id,
                            workspace_id,
                            memory_domain_id,
                            writer_owner_id,
                            expires_at,
                        ),
                    )
                    generation = 1
                    acquisition_kind = HostWriterAcquisitionKind.NEW_SESSION
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
                        acquisition_kind = (
                            HostWriterAcquisitionKind.SAME_OWNER_RENEWAL
                        )
                    else:
                        generation = int(row["writer_generation"]) + 1
                        acquisition_kind = HostWriterAcquisitionKind.HOST_TAKEOVER
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
            acquisition_kind=acquisition_kind,
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
                  AND (%s::text IS NULL OR memory_domain_id = %s::text)
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
        return WriterLease(
            guard=guard,
            expires_at=expires_at,
            acquisition_kind=HostWriterAcquisitionKind.SAME_OWNER_RENEWAL,
        )
