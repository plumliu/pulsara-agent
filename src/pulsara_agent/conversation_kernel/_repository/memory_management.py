"""One CRUD owner shared by model tools and user-level memory pages."""

from __future__ import annotations
from dataclasses import dataclass
from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id, require_id
from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryFactKind,
    MemoryRecord,
    memory_title,
    memory_statement,
    memory_timestamp,
    canonical_memory_recorded_at,
)
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementError,
    page_size,
    normalized_search,
    encode_cursor,
    decode_cursor,
)
from pulsara_agent.memory.scope import CTX_GLOBAL
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .locking import lock_canonical_identities
from .deletion import delete_unreferenced_workspaces


def _lock_workspaces(c, domain, contexts):
    identities = tuple(sorted({value for value in contexts if value != CTX_GLOBAL}))
    if identities:
        lock_canonical_identities(
            c, namespace="workspace", memory_domain_id=domain, identities=identities
        )
    return identities


@dataclass(frozen=True, slots=True)
class MemoryMutationResult:
    record: MemoryRecord
    changed: bool


class _MemoryManagementOperations:
    def _management_connection(self, *, deadline_monotonic, execute=False):
        return self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL
            if execute
            else PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.READ_COMMITTED
            if execute
            else IsolationLevel.REPEATABLE_READ,
        )

    @staticmethod
    def _memory_scope(c, domain, context_id, read_binding=None):
        if read_binding is not None and (
            read_binding.memory_domain_id != domain
            or not read_binding.can_read(context_id)
        ):
            raise MemoryManagementError(
                "MEMORY_SCOPE_UNAVAILABLE", 403, "当前会话不能访问该记忆范围。"
            )
        if (
            context_id != CTX_GLOBAL
            and not c.execute(
                "SELECT 1 FROM pulsara_v3.workspaces WHERE memory_domain_id=%s AND id=%s AND workspace_kind='project'",
                (domain, context_id),
            ).fetchone()
        ):
            raise MemoryManagementError(
                "MEMORY_SCOPE_UNAVAILABLE", 404, "所选项目不可用。"
            )
        return context_id

    def _management_context(self, c, domain, selection):
        return self._memory_scope(
            c,
            domain,
            CTX_GLOBAL if selection.view == "global" else selection.workspace_id,
        )

    def create_memory(
        self,
        *,
        memory_domain_id,
        context_id,
        kind,
        title,
        statement="",
        read_binding=None,
        deadline_monotonic,
    ):
        kind = MemoryFactKind(kind)
        title, statement = memory_title(title), memory_statement(kind, statement)
        with self._management_connection(
            deadline_monotonic=deadline_monotonic, execute=True
        ) as c:
            _lock_workspaces(c, memory_domain_id, (context_id,))
            self._memory_scope(c, memory_domain_id, context_id, read_binding)
            row = c.execute(
                "INSERT INTO pulsara_v3.memory_facts (id,memory_domain_id,context_id,fact_kind,title,statement) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    random_id("memory"),
                    memory_domain_id,
                    context_id,
                    kind.value,
                    title,
                    statement,
                ),
            ).fetchone()
            return MemoryRecord.from_row(row)

    def update_memory(
        self,
        *,
        memory_domain_id,
        fact_id,
        expected_updated_at,
        statement=None,
        context_id=None,
        read_binding=None,
        expected_context_id=None,
        deadline_monotonic,
    ):
        require_id(fact_id, "memory")
        expected = memory_timestamp(expected_updated_at)
        if statement is None and context_id is None:
            raise ValueError("修改须提供正文或保存范围。")
        with self._management_connection(
            deadline_monotonic=deadline_monotonic, execute=True
        ) as c:
            row = c.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=%s FOR UPDATE",
                (memory_domain_id, fact_id),
            ).fetchone()
            if row is None:
                raise MemoryManagementError(
                    "MEMORY_NOT_FOUND", 404, "这条记忆已不存在。"
                )
            _lock_workspaces(
                c,
                memory_domain_id,
                (row["context_id"], context_id or row["context_id"]),
            )
            self._memory_scope(c, memory_domain_id, row["context_id"], read_binding)
            if (
                expected_context_id is not None
                and row["context_id"] != expected_context_id
            ):
                raise MemoryManagementError(
                    "MEMORY_CHANGED", 409, "记忆保存范围已变化，请重新查询。"
                )
            if row["updated_at"] != expected:
                raise MemoryManagementError(
                    "MEMORY_CHANGED", 409, "记忆已变化，请重新查询后再决定。"
                )
            context_id = row["context_id"] if context_id is None else context_id
            self._memory_scope(c, memory_domain_id, context_id, read_binding)
            value = (
                row["statement"]
                if statement is None
                else memory_statement(row["fact_kind"], statement)
            )
            changed = (row["statement"], row["context_id"]) != (value, context_id)
            if not changed:
                return MemoryMutationResult(MemoryRecord.from_row(row), False)
            updated = c.execute(
                "UPDATE pulsara_v3.memory_facts SET statement=%s,context_id=%s WHERE memory_domain_id=%s AND id=%s AND updated_at=%s RETURNING *",
                (value, context_id, memory_domain_id, fact_id, expected),
            ).fetchone()
            if updated is None:
                raise MemoryManagementError(
                    "MEMORY_CHANGED", 409, "记忆已变化，请重新查询后再决定。"
                )
            if row["statement"] != updated["statement"]:
                c.execute(
                    "DELETE FROM pulsara_v3.memory_embeddings WHERE memory_domain_id=%s AND fact_id=%s",
                    (memory_domain_id, fact_id),
                )
            if row["context_id"] != context_id and row["context_id"] != CTX_GLOBAL:
                delete_unreferenced_workspaces(
                    c, memory_domain_id, (row["context_id"],)
                )
            return MemoryMutationResult(MemoryRecord.from_row(updated), True)

    def delete_memory(
        self,
        *,
        memory_domain_id,
        fact_id,
        expected_updated_at,
        read_binding=None,
        expected_context_id=None,
        deadline_monotonic,
    ):
        require_id(fact_id, "memory")
        expected = memory_timestamp(expected_updated_at)
        with self._management_connection(
            deadline_monotonic=deadline_monotonic, execute=True
        ) as c:
            row = c.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=%s FOR UPDATE",
                (memory_domain_id, fact_id),
            ).fetchone()
            if row is None:
                raise MemoryManagementError(
                    "MEMORY_NOT_FOUND", 404, "这条记忆已不存在。"
                )
            old_workspaces = _lock_workspaces(c, memory_domain_id, (row["context_id"],))
            self._memory_scope(c, memory_domain_id, row["context_id"], read_binding)
            if (
                expected_context_id is not None
                and row["context_id"] != expected_context_id
            ):
                raise MemoryManagementError(
                    "MEMORY_CHANGED", 409, "记忆保存范围已变化，请重新查询。"
                )
            if row["updated_at"] != expected:
                raise MemoryManagementError(
                    "MEMORY_CHANGED", 409, "记忆已变化，请重新查询后再决定。"
                )
            c.execute(
                "DELETE FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=%s AND updated_at=%s",
                (memory_domain_id, fact_id, expected),
            )
            if old_workspaces:
                delete_unreferenced_workspaces(c, memory_domain_id, old_workspaces)

    def memory_management_projects(
        self, *, memory_domain_id, deadline_monotonic, limit=40, cursor=None
    ):
        page_size(limit)
        filters = {"domain": memory_domain_id, "directory": "projects"}
        key = decode_cursor(cursor, filters, 2)
        with self._management_connection(deadline_monotonic=deadline_monotonic) as c:
            rows = c.execute(
                """SELECT w.id AS workspace_id,w.workspace_label AS label,w.workspace_root AS root,
                greatest(max(f.updated_at),max(s.updated_at)) AS last_activity_at
                FROM pulsara_v3.workspaces w LEFT JOIN pulsara_v3.memory_facts f ON f.memory_domain_id=w.memory_domain_id AND f.project_workspace_id=w.id
                LEFT JOIN pulsara_v3.sessions s ON s.memory_domain_id=w.memory_domain_id AND s.workspace_id=w.id
                WHERE w.memory_domain_id=%s AND w.workspace_kind='project'
                  AND (%s::text IS NULL OR (w.workspace_label,w.id)>(%s,%s))
                GROUP BY w.id,w.workspace_label,w.workspace_root
                ORDER BY w.workspace_label,w.id LIMIT %s""",
                (
                    memory_domain_id,
                    None if key is None else key[0],
                    None if key is None else key[0],
                    None if key is None else key[1],
                    limit + 1,
                ),
            ).fetchall()
        selected = rows[:limit]
        for row in selected:
            row["last_activity_at"] = (
                None
                if row["last_activity_at"] is None
                else canonical_memory_recorded_at(row["last_activity_at"])
            )
        return {
            "items": selected,
            "next_cursor": encode_cursor(
                filters, (selected[-1]["label"], selected[-1]["workspace_id"])
            )
            if len(rows) > limit
            else None,
        }

    def memory_management_catalog(
        self,
        *,
        memory_domain_id,
        selection,
        deadline_monotonic,
        kind=None,
        search=None,
        limit=40,
        cursor=None,
    ):
        page_size(limit)
        if kind is not None:
            MemoryFactKind(kind)
        search = normalized_search(search)
        with self._management_connection(deadline_monotonic=deadline_monotonic) as c:
            context = self._management_context(c, memory_domain_id, selection)
            filters = {
                "domain": memory_domain_id,
                "context": context,
                "kind": kind,
                "search": search,
            }
            key = decode_cursor(cursor, filters, 2)
            rows = c.execute(
                """SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND context_id=%s
                AND (%s::text IS NULL OR fact_kind=%s) AND strpos(lower(search_text),lower(%s))>0
                AND (%s::timestamptz IS NULL OR (updated_at,id)<(%s::timestamptz,%s))
                ORDER BY updated_at DESC,id DESC LIMIT %s""",
                (
                    memory_domain_id,
                    context,
                    kind,
                    kind,
                    search,
                    None if key is None else key[0],
                    None if key is None else key[0],
                    None if key is None else key[1],
                    limit + 1,
                ),
            ).fetchall()
        selected = rows[:limit]
        return {
            "items": [MemoryRecord.from_row(row).value() for row in selected],
            "next_cursor": encode_cursor(
                filters,
                (
                    canonical_memory_recorded_at(selected[-1]["updated_at"]),
                    selected[-1]["id"],
                ),
            )
            if len(rows) > limit
            else None,
        }

    def memory_management_detail(
        self, *, memory_domain_id, selection, fact_id, deadline_monotonic
    ):
        require_id(fact_id, "memory")
        with self._management_connection(deadline_monotonic=deadline_monotonic) as c:
            context = self._management_context(c, memory_domain_id, selection)
            row = c.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=%s AND context_id=%s",
                (memory_domain_id, fact_id, context),
            ).fetchone()
            if row is None:
                raise MemoryManagementError(
                    "MEMORY_NOT_FOUND", 404, "这条记忆已不存在或已移到其他范围。"
                )
            return MemoryRecord.from_row(row).value()
