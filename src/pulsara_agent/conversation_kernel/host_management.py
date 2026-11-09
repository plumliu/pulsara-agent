"""User-level memory, session catalog, and retirement operations on the Host core."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Literal

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.query import (
    CanonicalConversationQuery,
    CanonicalProviderCallUsagePage,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.root_status import LATEST_ROOT_SUMMARY_SQL
from pulsara_agent.conversation_kernel.session_deletion import (
    KernelSessionRetirement,
)
from pulsara_agent.llm.model_connections import model_call_binding_from_dict
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput, resolve_workspace

from .host_contracts import (
    KernelSessionSummary,
)
from .host_tasks import _core_database_operation, _join_task_beyond_logical_deadline


class _HostCoreManagementOperations:
    """User-level memory, session catalog, and retirement operations on the Host core.

    Operations share the concrete Host's existing state and admission locks.
    """

    @_core_database_operation
    async def memory_management_projects(
        self, *, memory_domain_id, limit=40, cursor=None
    ):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.memory_management_projects,
            memory_domain_id=memory_domain_id,
            limit=limit,
            cursor=cursor,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def memory_management_catalog(
        self, *, memory_domain_id, selection, **filters
    ):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.memory_management_catalog,
            memory_domain_id=memory_domain_id,
            selection=selection,
            deadline_monotonic=self._canonical_deadline(),
            **filters,
        )

    @_core_database_operation
    async def memory_management_detail(
        self,
        *,
        memory_domain_id,
        selection,
        fact_id,
        limit=40,
        cursor=None,
    ):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.memory_management_detail,
            memory_domain_id=memory_domain_id,
            selection=selection,
            fact_id=fact_id,
            limit=limit,
            cursor=cursor,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def memory_deletion_preview(
        self, *, memory_domain_id, selection, fact_id, additional=()
    ):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.memory_deletion_preview,
            memory_domain_id=memory_domain_id,
            selection=selection,
            fact_id=fact_id,
            additional=additional,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def memory_management_edit_statement(
        self, *, memory_domain_id, selection, fact_id, statement, expected_updated_at
    ):
        repository = await self._ensure_resources()
        # A disconnected HTTP client must not detach an in-flight DB commit.
        task = asyncio.create_task(
            asyncio.to_thread(
                repository.memory_management_edit_statement,
                memory_domain_id=memory_domain_id,
                selection=selection,
                fact_id=fact_id,
                statement=statement,
                expected_updated_at=expected_updated_at,
                deadline_monotonic=self._canonical_deadline(),
            )
        )
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
        result = task.result()
        if result["changed"]:
            async with self._lock:
                for session in self._sessions.values():
                    if session._memory_domain_id == memory_domain_id:
                        session._memory_embedding_maintainer.offer_wake()
        if cancelled is not None:
            raise cancelled
        return result

    async def wake_memory_embeddings(self):
        async with self._lock:
            for session in self._sessions.values():
                session._memory_embedding_maintainer.offer_wake()

    @_core_database_operation
    async def execute_memory_deletion(
        self, *, memory_domain_id, selection, fact_id, additional, expected_records
    ):
        repository = await self._ensure_resources()
        # Join physical execution before the HTTP owner may close its confirmation file.
        task = asyncio.create_task(
            asyncio.to_thread(
                repository.execute_memory_deletion,
                memory_domain_id=memory_domain_id,
                selection=selection,
                fact_id=fact_id,
                additional=additional,
                expected_records=expected_records,
                deadline_monotonic=self._canonical_deadline(),
            )
        )
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
        result = task.result()
        if cancelled is not None:
            raise cancelled
        return result

    async def mark_capability_change(self, workspace_root: Path | None = None) -> int:
        canonical_root = (
            workspace_root.resolve(strict=False) if workspace_root is not None else None
        )
        async with self._lock:
            roots = (
                {canonical_root}
                if canonical_root is not None
                else set(self._workspace_capability_revisions)
            )
            for root in roots:
                self._workspace_capability_revisions[root] = (
                    self._workspace_capability_revisions.get(root, 0) + 1
                )
            sessions = tuple(
                session
                for session in self._sessions.values()
                if canonical_root is None
                or session.workspace.workspace_root.resolve() == canonical_root
            )
        marked = 0
        for session in sessions:
            try:
                await session.request_capability_refresh()
                marked += 1
            except RuntimeError:
                async with self._lock:
                    if self._sessions.get(session.host_session_id) is session:
                        raise
        return marked

    @_core_database_operation
    async def list_resumable_sessions(
        self,
        *,
        workspace_input: HostWorkspaceInput,
        include_archived: bool = False,
        limit: int = 20,
    ) -> list[KernelSessionSummary]:
        if not 1 <= limit <= 100:
            raise ValueError("session list limit is out of bounds")
        repository = await self._ensure_resources()
        workspace = resolve_workspace(workspace_input, intent="READ")
        rows = await asyncio.to_thread(
            _list_resumable_session_rows,
            repository,
            workspace.workspace_key,
            workspace.memory_domain.memory_domain_id,
            include_archived,
            limit,
            self._canonical_deadline(),
        )
        return [_kernel_session_summary(row) for row in rows]

    @_core_database_operation
    async def list_resumable_sessions_across_workspaces(
        self,
        *,
        memory_domain_id: str = "u_local",
        include_archived: bool = False,
    ) -> list[KernelSessionSummary]:
        """Cold-list every resumable Session in one local memory domain."""

        repository = await self._ensure_resources()
        rows = await asyncio.to_thread(
            _list_resumable_session_rows_across_workspaces,
            repository,
            memory_domain_id,
            include_archived,
            self._canonical_deadline(),
        )
        return [_kernel_session_summary(row) for row in rows]

    @_core_database_operation
    async def read_media_annotations(
        self, *, memory_domain_id, path, modified_ns, after
    ):
        from pulsara_agent.conversation_kernel.media_annotations import (
            read_media_annotations,
        )

        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            read_media_annotations,
            repository,
            memory_domain_id=memory_domain_id,
            path=path,
            modified_ns=modified_ns,
            after=after,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def search_sessions(
        self,
        *,
        memory_domain_id: str,
        query: str,
        lifecycle: str = "ALL",
        cursor: str | None = None,
        limit: int = 20,
    ):
        from pulsara_agent.conversation_kernel.session_search import search_session_page

        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            search_session_page,
            repository,
            memory_domain_id=memory_domain_id,
            query=query,
            lifecycle=lifecycle,
            cursor=cursor,
            limit=limit,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def rename_session(
        self, session_id: str, *, memory_domain_id: str, title: str
    ) -> str:
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.rename_session,
            session_id=session_id,
            memory_domain_id=memory_domain_id,
            title=title,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def read_resumable_session(
        self,
        session_id: str,
        *,
        memory_domain_id: str = "u_local",
        include_archived: bool = False,
    ) -> KernelSessionSummary | None:
        """Cold-read one Session together with its exact workspace input."""

        if not session_id:
            raise ValueError("session_id is required")
        repository = await self._ensure_resources()
        row = await asyncio.to_thread(
            _read_resumable_session_row,
            repository,
            session_id,
            memory_domain_id,
            include_archived,
            self._canonical_deadline(),
        )
        return None if row is None else _kernel_session_summary(row)

    @_core_database_operation
    async def read_provider_call_usage_page(
        self,
        *,
        session_id: str,
        conversation_scope_kind: str | None = None,
        after_observed_at: datetime | None = None,
        after_resolved_model_call_id: str | None = None,
        maximum_rows: int = 256,
    ) -> CanonicalProviderCallUsagePage:
        """Cold-read one keyset page of persisted provider usage observations."""

        if not session_id:
            raise ValueError("session_id is required")
        repository = await self._ensure_resources()
        query = CanonicalConversationQuery(
            repository.connection_provider,
            watchdog_policy=self._deadlines.policy,
        )
        return await asyncio.to_thread(
            query.page_provider_call_usage,
            session_id=session_id,
            conversation_scope_kind=conversation_scope_kind,
            after_observed_at=after_observed_at,
            after_resolved_model_call_id=after_resolved_model_call_id,
            maximum_rows=maximum_rows,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def canonical_session_exists(
        self, session_id: str, memory_domain_id: str
    ) -> bool:
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.canonical_session_exists,
            session_id=session_id,
            memory_domain_id=memory_domain_id,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def idle_session_ids(self, memory_domain_id: str):
        repository = await self._ensure_resources()
        async with self._lock:
            local_session_ids = tuple(s.session_id for s in self._sessions.values())
        return await asyncio.to_thread(
            repository.idle_session_ids,
            memory_domain_id=memory_domain_id,
            local_session_ids=local_session_ids,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def read_session_lifecycle(self, session_id: str, memory_domain_id: str):
        repository = await self._ensure_resources()
        return await asyncio.to_thread(
            repository.read_session_lifecycle,
            session_id=session_id,
            memory_domain_id=memory_domain_id,
            deadline_monotonic=self._canonical_deadline(),
        )

    @_core_database_operation
    async def prepare_session_archive(self, operation: KernelSessionRetirement):
        """Freeze existing Host admission before checking canonical idle; do not stop work."""
        from .repository_errors import SessionDeletionBusy, SessionWriterConflict

        if (
            operation.owner is not self
            or self._session_retirements.get(operation.session_id) is not operation
        ):
            raise RuntimeError("session archive lacks admission authority")
        repository = await self._ensure_resources()
        async with self._lock:
            sessions = tuple(
                s
                for s in self._sessions.values()
                if s.session_id == operation.session_id
            )
        gates = []
        try:
            if len(sessions) > 1:
                raise SessionWriterConflict("multiple Host owners have not settled")
            for session in sessions:
                gate = await session.prepare_safe_runtime_reopen()
                gates.append((session, gate))
                if session.archive_has_terminal_work():
                    raise SessionDeletionBusy("session has terminal work")
            await asyncio.to_thread(
                repository.archive_session,
                session_id=operation.session_id,
                memory_domain_id=operation.memory_domain_id,
                closed_writer=sessions[0]._lease.guard if sessions else None,
                check_only=True,
                deadline_monotonic=self._canonical_deadline(),
            )
            for session, gate in gates:
                await session.commit_safe_runtime_reopen(gate)
        except Exception:
            for session, gate in gates:
                await session.abort_safe_runtime_reopen(gate)
            raise

    @_core_database_operation(retain_result_on_cancel=True)
    async def commit_session_archive(self, operation: KernelSessionRetirement) -> str:
        if (
            operation.owner is not self
            or not operation.physical_full
            or operation.quarantined
            or self._session_retirements.get(operation.session_id) is not operation
        ):
            raise RuntimeError("session archive lacks close authority")
        repository = await self._ensure_resources()
        deadline = self._canonical_deadline()
        worker = asyncio.create_task(
            asyncio.to_thread(
                repository.archive_session,
                session_id=operation.session_id,
                memory_domain_id=operation.memory_domain_id,
                closed_writer=operation.closed_writer,
                deadline_monotonic=deadline,
            )
        )
        await _join_task_beyond_logical_deadline(worker, deadline_monotonic=deadline)
        return worker.result()

    @_core_database_operation
    async def unarchive_session(self, session_id: str, memory_domain_id: str):
        repository = await self._ensure_resources()
        deadline = self._canonical_deadline()
        worker = asyncio.create_task(
            asyncio.to_thread(
                repository.unarchive_session,
                session_id=session_id,
                memory_domain_id=memory_domain_id,
                deadline_monotonic=deadline,
            )
        )
        await _join_task_beyond_logical_deadline(worker, deadline_monotonic=deadline)
        return worker.result()

    async def quiesce_session_retirement(
        self, operation: KernelSessionRetirement
    ) -> None:
        async with self._lock:
            if self._session_retirements.get(operation.session_id) is not operation:
                raise RuntimeError("session deletion owner is stale")
            sessions = tuple(
                s
                for s in self._sessions.values()
                if s.session_id == operation.session_id
                or s.root_session_id == operation.session_id
            )
        for session in sorted(sessions, key=lambda s: s._lease.guard.writer_generation):
            if (
                session.workspace.memory_domain.memory_domain_id
                != operation.memory_domain_id
            ):
                raise RuntimeError("session deletion domain mismatch")
            await self.close_session(session.host_session_id)
            if session.session_id == operation.session_id:
                operation.closed_writer = session._lease.guard
        operation.physical_full = True

    @_core_database_operation(retain_result_on_cancel=True)
    async def commit_session_deletion(
        self,
        operation: KernelSessionRetirement,
        *,
        required_lifecycle: Literal["ARCHIVED"] | None = None,
        idle_before: datetime | None = None,
    ) -> str:
        if (
            operation.owner is not self
            or not operation.physical_full
            or operation.quarantined
            or self._session_retirements.get(operation.session_id) is not operation
        ):
            raise RuntimeError("session deletion has no physical close authority")
        repository = await self._ensure_resources()
        deadline = self._canonical_deadline()
        worker = asyncio.create_task(
            asyncio.to_thread(
                repository.delete_session,
                session_id=operation.session_id,
                memory_domain_id=operation.memory_domain_id,
                closed_writer=operation.closed_writer,
                deadline_monotonic=deadline,
                required_lifecycle=required_lifecycle,
                idle_before=idle_before,
            )
        )
        await _join_task_beyond_logical_deadline(worker, deadline_monotonic=deadline)
        result = worker.result()
        return result

    async def finish_session_retirement(
        self,
        operation: KernelSessionRetirement,
        *,
        quarantine: bool,
    ) -> None:
        async with self._lock:
            if self._session_retirements.get(operation.session_id) is not operation:
                raise RuntimeError("session deletion settlement is stale")
            operation.quarantined = quarantine
            if not quarantine:
                self._session_retirements.pop(operation.session_id)
            self._open_attempts.pop(operation.settlement, None)
            if not operation.settlement.done():
                operation.settlement.set_result(None)


def _list_resumable_session_rows(
    repository: ConversationKernelRepository,
    workspace_id: str,
    memory_domain_id: str,
    include_archived: bool,
    limit: int,
    deadline_monotonic: float,
):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
        isolation_level=IsolationLevel.REPEATABLE_READ,
    ) as connection:
        return connection.execute(
            f"""
            SELECT s.id, s.title, s.workspace_id, w.workspace_kind, w.workspace_root,
                   w.workspace_label, s.memory_domain_id, s.lifecycle,
                   s.writer_generation, s.latest_entry_sequence,
                   CASE WHEN s.lifecycle='ARCHIVED' THEN s.updated_at ELSE GREATEST(s.created_at, (
                       SELECT e.accepted_at
                       FROM pulsara_v3.transcript_entries AS e
                       WHERE e.session_id = s.id
                       ORDER BY e.entry_sequence DESC
                       LIMIT 1
                   )) END AS updated_at,
                   s.model_call_binding,
                   {LATEST_ROOT_SUMMARY_SQL} AS latest_root_turn
            FROM pulsara_v3.sessions AS s
            JOIN pulsara_v3.workspaces AS w
              ON w.memory_domain_id=s.memory_domain_id AND w.id=s.workspace_id
            WHERE s.root_session_id IS NULL AND s.workspace_id = %s AND s.memory_domain_id = %s
              AND (%s OR s.lifecycle = 'OPEN')
            -- sessions.updated_at includes writer-lease maintenance.  Order the
            -- user-facing list by canonical conversation activity instead.
            ORDER BY updated_at DESC, s.id LIMIT %s
            """,
            (workspace_id, memory_domain_id, include_archived, limit),
        ).fetchall()


def _list_resumable_session_rows_across_workspaces(
    repository: ConversationKernelRepository,
    memory_domain_id: str,
    include_archived: bool,
    deadline_monotonic: float,
):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
        isolation_level=IsolationLevel.REPEATABLE_READ,
    ) as connection:
        return connection.execute(
            f"""
            SELECT s.id, s.title, s.workspace_id, w.workspace_kind, w.workspace_root,
                   w.workspace_label, s.memory_domain_id, s.lifecycle,
                   s.writer_generation, s.latest_entry_sequence,
                   CASE WHEN s.lifecycle='ARCHIVED' THEN s.updated_at ELSE GREATEST(s.created_at, (
                       SELECT e.accepted_at
                       FROM pulsara_v3.transcript_entries AS e
                       WHERE e.session_id = s.id
                       ORDER BY e.entry_sequence DESC
                       LIMIT 1
                   )) END AS updated_at,
                   s.model_call_binding,
                   (SELECT count(*) FROM pulsara_v3.sessions AS child
                    WHERE child.root_session_id = s.id) AS subagent_count,
                   {LATEST_ROOT_SUMMARY_SQL} AS latest_root_turn
            FROM pulsara_v3.sessions AS s
            JOIN pulsara_v3.workspaces AS w
              ON w.memory_domain_id=s.memory_domain_id AND w.id=s.workspace_id
            WHERE s.root_session_id IS NULL AND s.memory_domain_id = %s AND (%s OR s.lifecycle = 'OPEN')
            -- sessions.updated_at includes writer-lease maintenance.  Order the
            -- user-facing list by canonical conversation activity instead.
            ORDER BY updated_at DESC, s.id
            """,
            (memory_domain_id, include_archived),
        ).fetchall()


def _read_resumable_session_row(
    repository: ConversationKernelRepository,
    session_id: str,
    memory_domain_id: str,
    include_archived: bool,
    deadline_monotonic: float,
):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
        isolation_level=IsolationLevel.REPEATABLE_READ,
    ) as connection:
        return connection.execute(
            f"""
            SELECT s.id, s.title, s.workspace_id, w.workspace_kind, w.workspace_root,
                   w.workspace_label, s.memory_domain_id, s.lifecycle,
                   s.writer_generation, s.latest_entry_sequence,
                   CASE WHEN s.lifecycle='ARCHIVED' THEN s.updated_at ELSE GREATEST(s.created_at, (SELECT e.accepted_at
                       FROM pulsara_v3.transcript_entries AS e WHERE e.session_id = s.id
                       ORDER BY e.entry_sequence DESC LIMIT 1)) END AS updated_at,
                   s.model_call_binding,
                   {LATEST_ROOT_SUMMARY_SQL} AS latest_root_turn
            FROM pulsara_v3.sessions AS s
            JOIN pulsara_v3.workspaces AS w
              ON w.memory_domain_id=s.memory_domain_id AND w.id=s.workspace_id
            WHERE s.id = %s AND s.memory_domain_id = %s
              AND (%s OR s.lifecycle = 'OPEN')
            """,
            (session_id, memory_domain_id, include_archived),
        ).fetchone()


def _kernel_session_summary(row) -> KernelSessionSummary:
    return KernelSessionSummary(
        session_id=str(row["id"]),
        title=row["title"],
        workspace_id=str(row["workspace_id"]),
        workspace_kind=str(row["workspace_kind"]),
        workspace_root=str(row["workspace_root"]),
        workspace_label=str(row["workspace_label"]),
        memory_domain_id=str(row["memory_domain_id"]),
        lifecycle=str(row["lifecycle"]),
        writer_generation=int(row["writer_generation"]),
        latest_entry_sequence=int(row["latest_entry_sequence"]),
        updated_at=row["updated_at"],
        model_call_binding=model_call_binding_from_dict(row["model_call_binding"]),
        latest_root_turn=row["latest_root_turn"],
        subagent_count=row.get("subagent_count"),
    )
