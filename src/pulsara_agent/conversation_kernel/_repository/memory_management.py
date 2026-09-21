"""Canonical management queries and user-confirmed transactional memory deletion."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import zip_longest
from time import monotonic

from psycopg import IsolationLevel, sql
from psycopg.errors import (
    DeadlockDetected,
    SerializationFailure,
    LockNotAvailable,
    QueryCanceled,
    ForeignKeyViolation,
    UniqueViolation,
)
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.memory.contracts import (
    canonical_json_bytes,
    canonical_memory_recorded_at,
)
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementError,
    MemoryManagementFact,
    confirmation_records,
    decode_cursor,
    deletion_graph,
    encode_cursor,
    normalized_search,
    page_size,
    relative_role,
    with_end,
)
from pulsara_agent.memory.scope import CTX_GLOBAL
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


_PROJECTS = """
WITH activity AS (
    SELECT s.workspace_id, s.workspace_label, s.workspace_root, s.id,
           GREATEST((SELECT e.accepted_at FROM pulsara_v3.transcript_entries e
                     WHERE e.session_id=s.id ORDER BY e.entry_sequence DESC LIMIT 1),
                    s.created_at) AS last_activity_at
    FROM pulsara_v3.sessions s
    WHERE s.memory_domain_id=%s AND s.workspace_kind='project'
), projects AS (
    SELECT DISTINCT ON (workspace_id) workspace_id, workspace_label AS label,
           workspace_root AS root, last_activity_at
    FROM activity ORDER BY workspace_id, last_activity_at DESC, id DESC
)
"""

_ACTIVE_CONFLICT = """EXISTS (
    SELECT 1 FROM pulsara_v3.memory_relations r
    JOIN pulsara_v3.memory_facts other
      ON other.memory_domain_id=r.memory_domain_id
     AND other.id=CASE WHEN r.source_fact_id=f.id THEN r.target_fact_id ELSE r.source_fact_id END
    WHERE r.memory_domain_id=f.memory_domain_id AND r.relation_kind='CONTRADICTS'
      AND (r.source_fact_id=f.id OR r.target_fact_id=f.id)
      AND f.lifecycle='ACTIVE' AND other.lifecycle='ACTIVE'
)"""


def _freeze(value):
    if isinstance(value, dict):
        return tuple((key, _freeze(value[key])) for key in sorted(value))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class FrozenMemoryDeletionPlan:
    facts: tuple
    relations: tuple
    restore: tuple
    lock_fact_ids: tuple[str, ...]
    confirmation: tuple[bytes, ...]


def _frozen_rows(rows):
    return tuple(_freeze(row) for row in rows)


def _remaining(connection, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise MemoryManagementError(
            "MEMORY_DELETION_PLANNING_TIMEOUT", 504, "记忆操作超时，尚未删除，请重试"
        )
    connection.execute(
        "SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true)",
        (str(max(1, int(remaining * 1000))), str(max(1, int(remaining * 1000)))),
    )


class _MemoryManagementOperations:
    def _management_connection(self, *, deadline_monotonic, execute=False):
        return self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_MAINTENANCE
            if execute
            else PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.SERIALIZABLE
            if execute
            else IsolationLevel.REPEATABLE_READ,
        )

    @staticmethod
    def _management_context(connection, domain, selection):
        if selection.view == "global":
            return CTX_GLOBAL
        row = connection.execute(
            _PROJECTS + "SELECT workspace_id FROM projects WHERE workspace_id=%s",
            (domain, selection.workspace_id),
        ).fetchone()
        if row is None:
            raise MemoryManagementError(
                "MEMORY_PROJECT_NOT_FOUND", 404, "找不到这个项目"
            )
        return row["workspace_id"]

    def memory_management_projects(
        self, *, memory_domain_id, deadline_monotonic, limit=40, cursor=None
    ):
        page_size(limit)
        filters = {"domain": memory_domain_id, "directory": "projects"}
        key = decode_cursor(cursor, filters, 2)
        with self._management_connection(deadline_monotonic=deadline_monotonic) as c:
            _remaining(c, deadline_monotonic)
            rows = c.execute(
                _PROJECTS
                + """SELECT p.* FROM projects p
                WHERE EXISTS (
                    SELECT 1 FROM pulsara_v3.memory_facts f
                    WHERE f.memory_domain_id=%s AND f.context_id=p.workspace_id
                )
                  AND (%s::timestamptz IS NULL OR (p.last_activity_at, p.workspace_id)<(%s::timestamptz,%s))
                ORDER BY p.last_activity_at DESC, p.workspace_id DESC LIMIT %s""",
                (
                    memory_domain_id,
                    memory_domain_id,
                    None if key is None else key[0],
                    None if key is None else key[0],
                    None if key is None else key[1],
                    limit + 1,
                ),
            ).fetchall()
        selected = rows[:limit]
        next_cursor = (
            encode_cursor(
                filters,
                (
                    selected[-1]["last_activity_at"].isoformat(),
                    selected[-1]["workspace_id"],
                ),
            )
            if len(rows) > limit
            else None
        )
        return {
            "items": [
                {**r, "last_activity_at": r["last_activity_at"].isoformat()}
                for r in selected
            ],
            "next_cursor": next_cursor,
        }

    def memory_management_catalog(
        self,
        *,
        memory_domain_id,
        selection,
        deadline_monotonic,
        lifecycle="active",
        kind=None,
        search=None,
        limit=40,
        cursor=None,
    ):
        page_size(limit)
        from pulsara_agent.conversation_kernel.memory.contracts import MemoryFactKind

        if lifecycle not in {"active", "updated"}:
            raise ValueError("记忆状态筛选无效")
        if kind is not None:
            MemoryFactKind(kind)
        search = normalized_search(search)
        with self._management_connection(deadline_monotonic=deadline_monotonic) as c:
            _remaining(c, deadline_monotonic)
            context = self._management_context(c, memory_domain_id, selection)
            filters = {
                "domain": memory_domain_id,
                "context": context,
                "lifecycle": lifecycle,
                "kind": kind,
                "search": search,
            }
            key = decode_cursor(cursor, filters, 2)
            # strpos is a literal substring: %, _ and backslash have no SQL pattern meaning.
            rows = c.execute(
                f"""SELECT f.*, {_ACTIVE_CONFLICT} AS needs_confirmation
                FROM pulsara_v3.memory_facts f
                WHERE memory_domain_id=%s AND context_id=%s AND lifecycle=%s
                  AND (%s::text IS NULL OR fact_kind=%s)
                  AND strpos(lower(statement), lower(%s))>0
                  AND (%s::timestamptz IS NULL OR (updated_at,id)<(%s::timestamptz,%s))
                ORDER BY updated_at DESC,id DESC LIMIT %s""",
                (
                    memory_domain_id,
                    context,
                    "ACTIVE" if lifecycle == "active" else "SUPERSEDED",
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
            labels = self._management_labels(c, memory_domain_id)
            return {
                "items": [
                    {
                        **asdict(MemoryManagementFact.from_row(row)),
                        "needs_confirmation": row["needs_confirmation"],
                        "context_label": labels.get(context, "项目记忆"),
                    }
                    for row in selected
                ],
                "next_cursor": encode_cursor(
                    filters,
                    (selected[-1]["updated_at"].isoformat(), selected[-1]["id"]),
                )
                if len(rows) > limit
                else None,
            }

    @staticmethod
    def _management_labels(c, domain):
        return {
            CTX_GLOBAL: "全局记忆",
            **{
                r["workspace_id"]: r["label"]
                for r in c.execute(
                    _PROJECTS + "SELECT * FROM projects", (domain,)
                ).fetchall()
            },
        }

    def memory_management_detail(
        self,
        *,
        memory_domain_id,
        selection,
        fact_id,
        deadline_monotonic,
        limit=40,
        cursor=None,
    ):
        page_size(limit)
        with self._management_connection(deadline_monotonic=deadline_monotonic) as c:
            _remaining(c, deadline_monotonic)
            context = self._management_context(c, memory_domain_id, selection)
            filters = {
                "domain": memory_domain_id,
                "context": context,
                "fact_id": fact_id,
            }
            key = decode_cursor(cursor, filters, 3)
            row = c.execute(
                f"""SELECT f.*, {_ACTIVE_CONFLICT} AS needs_confirmation,
                f.source_session_id AS origin_session_id,
                r.tool_call_entry_id AS producer_entry_id,
                e.turn_id, s.lifecycle AS source_session_lifecycle
                FROM pulsara_v3.memory_facts f
                JOIN pulsara_v3.tool_results r
                  ON r.session_id=f.source_session_id AND r.id=f.source_tool_result_id
                JOIN pulsara_v3.transcript_entries e
                  ON e.entry_owner_kind='EXECUTED_TURN'
                 AND e.id=r.tool_call_entry_id AND e.session_id=r.session_id
                JOIN pulsara_v3.sessions s
                  ON s.id=f.source_session_id AND s.memory_domain_id=f.memory_domain_id
                WHERE f.memory_domain_id=%s AND f.context_id=%s AND f.id=%s""",
                (memory_domain_id, context, fact_id),
            ).fetchone()
            if row is None:
                raise MemoryManagementError("MEMORY_NOT_FOUND", 404, "这条记忆已不存在")
            relations = c.execute(
                """SELECT r.*, owner_result.session_id AS owner_source_session_id,
                       owner_result.tool_call_entry_id AS owner_entry_id,
                       owner_entry.turn_id AS owner_turn_id,
                       owner_session.lifecycle AS owner_session_lifecycle,
                       owner_block.tool_name AS owner_write_tool
                FROM pulsara_v3.memory_relations r
                JOIN pulsara_v3.tool_results owner_result
                  ON owner_result.session_id=r.owner_session_id
                 AND owner_result.id=r.owner_tool_result_id
                JOIN pulsara_v3.sessions owner_session
                  ON owner_session.id=owner_result.session_id
                 AND owner_session.memory_domain_id=r.memory_domain_id
                JOIN pulsara_v3.transcript_entries owner_entry
                  ON owner_entry.session_id=owner_result.session_id
                 AND owner_entry.id=owner_result.tool_call_entry_id
                 AND owner_entry.entry_owner_kind='EXECUTED_TURN'
                JOIN pulsara_v3.assistant_message_blocks owner_block
                  ON owner_block.session_id=owner_result.session_id
                 AND owner_block.assistant_entry_id=owner_result.tool_call_entry_id
                 AND owner_block.tool_call_id=owner_result.tool_call_id
                WHERE r.memory_domain_id=%s AND (r.source_fact_id=%s OR r.target_fact_id=%s)
                  AND (%s::text IS NULL OR (r.relation_kind,r.accepted_at,r.id)>(%s,%s::timestamptz,%s))
                ORDER BY r.relation_kind,r.accepted_at,r.id LIMIT %s""",
                (
                    memory_domain_id,
                    fact_id,
                    fact_id,
                    None if key is None else key[0],
                    None if key is None else key[0],
                    None if key is None else key[1],
                    None if key is None else key[2],
                    limit + 1,
                ),
            ).fetchall()
            companions = {
                r["target_fact_id"]
                if r["source_fact_id"] == fact_id
                else r["source_fact_id"]
                for r in relations[:limit]
            }
            facts = {
                r["id"]: r
                for r in c.execute(
                    "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=ANY(%s)",
                    (memory_domain_id, sorted(companions)),
                ).fetchall()
            }
            labels = self._management_labels(c, memory_domain_id)
            chosen = relations[:limit]
            return {
                "fact": {
                    **asdict(MemoryManagementFact.from_row(row)),
                    "needs_confirmation": row["needs_confirmation"],
                    "context_label": labels.get(context, "项目记忆"),
                },
                "formation": "由对话中的 Pulsara 直接保存；请按需核对内容",
                "source": {
                    "session_id": row["origin_session_id"],
                    "turn_id": row["turn_id"],
                    "entry_id": row["producer_entry_id"],
                }
                if row["source_session_lifecycle"] == "OPEN" else None,
                "relations": [
                    self._management_relation(
                        r,
                        row,
                        facts[
                            r["target_fact_id"]
                            if r["source_fact_id"] == fact_id
                            else r["source_fact_id"]
                        ],
                    )
                    for r in chosen
                ],
                "next_cursor": encode_cursor(
                    filters,
                    (
                        chosen[-1]["relation_kind"],
                        chosen[-1]["accepted_at"].isoformat(),
                        chosen[-1]["id"],
                    ),
                )
                if len(relations) > limit
                else None,
            }

    @staticmethod
    def _management_relation(relation, subject, companion):
        projected = {
            "relation_id": relation["id"],
            "subject": asdict(MemoryManagementFact.from_row(subject)),
            "companion": asdict(MemoryManagementFact.from_row(companion)),
            "relative_role": relative_role(
                relation["relation_kind"],
                selected_is_source=relation["source_fact_id"] == subject["id"],
            ).value,
            "recorded_at": canonical_memory_recorded_at(relation["accepted_at"]),
        }
        if "owner_write_tool" in relation:
            projected["owner"] = {
                "write_tool": relation["owner_write_tool"],
                "source": {
                    "session_id": relation["owner_source_session_id"],
                    "turn_id": relation["owner_turn_id"],
                    "entry_id": relation["owner_entry_id"],
                }
                if relation["owner_session_lifecycle"] == "OPEN" else None,
            }
        return projected

    def memory_deletion_preview(
        self, *, memory_domain_id, selection, fact_id, additional=(), deadline_monotonic
    ):
        with self._management_connection(deadline_monotonic=deadline_monotonic) as c:
            _remaining(c, deadline_monotonic)
            plan = self._memory_deletion_plan(
                c, memory_domain_id, selection, fact_id, additional
            )
        return plan.confirmation

    def _memory_deletion_plan(self, c, domain, selection, root, additional):
        context = self._management_context(c, domain, selection)
        if (
            c.execute(
                "SELECT id FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND context_id=%s AND id=%s",
                (domain, context, root),
            ).fetchone()
            is None
        ):
            raise MemoryManagementError("MEMORY_NOT_FOUND", 404, "这条记忆已不存在")
        additional = tuple(sorted(set(additional) - {root}))
        seeds = (root, *additional)
        # Read the connected basis/update neighborhood, and one-hop conflict endpoints.
        # UNION terminates cycles without a history, inventory, or graph-depth cutoff.
        relations = c.execute(
            """WITH RECURSIVE neighborhood(id) AS (
            SELECT id FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=ANY(%s)
            UNION
            SELECT CASE WHEN r.source_fact_id=n.id THEN r.target_fact_id ELSE r.source_fact_id END
            FROM neighborhood n JOIN pulsara_v3.memory_relations r
              ON r.memory_domain_id=%s AND (r.source_fact_id=n.id OR r.target_fact_id=n.id)
            WHERE r.relation_kind IN ('BASED_ON','SUPERSEDES')
        ) SELECT DISTINCT r.* FROM pulsara_v3.memory_relations r
          WHERE r.memory_domain_id=%s AND (r.source_fact_id IN (SELECT id FROM neighborhood)
                                     OR r.target_fact_id IN (SELECT id FROM neighborhood))
          ORDER BY r.id""",
            (domain, list(seeds), domain, domain),
        ).fetchall()
        graph = deletion_graph(seeds, relations)
        ids = set(seeds) | {
            e[k] for e in relations for k in ("source_fact_id", "target_fact_id")
        }
        facts = {
            r["id"]: r
            for r in c.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=ANY(%s) ORDER BY id",
                (domain, sorted(ids)),
            ).fetchall()
        }
        if not set(seeds) <= facts.keys():
            raise MemoryManagementError(
                "MEMORY_NOT_FOUND", 404, "一并删除的记忆已不存在"
            )
        removed = [r for r in relations if r["id"] in graph.relation_ids]
        restores = [
            facts[i] for i in graph.restore_ids if facts[i]["lifecycle"] == "SUPERSEDED"
        ]
        restores.sort(key=lambda r: (r["context_id"], r["accepted_at"], r["id"]))
        deleted = sorted(graph.delete_ids)
        conflicts = []

        def conflict(reason, subject, companion=None, group=None):
            conflicts.append(
                {
                    "type": "RESTORATION_CONFLICT",
                    "reason": reason,
                    "subject": asdict(MemoryManagementFact.from_row(subject)),
                    "companion": None
                    if companion is None
                    else asdict(MemoryManagementFact.from_row(companion)),
                    "group": group or subject["id"],
                }
            )

        for target, ancestor in graph.blocked_ancestry:
            conflict("SURVIVING_SUPERSEDE_ANCESTRY", facts[target], facts[ancestor])
        contexts = sorted({r["context_id"] for r in restores})
        active = c.execute(
            """SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s
            AND context_id=ANY(%s) AND lifecycle='ACTIVE' AND NOT(id=ANY(%s))
            AND fact_semantic_digest=ANY(%s) ORDER BY accepted_at,id""",
            (domain, contexts, deleted, [r["fact_semantic_digest"] for r in restores]),
        ).fetchall()
        active_by_key = {
            (r["context_id"], r["fact_semantic_digest"]): r for r in active
        }
        groups = {}
        for restored in restores:
            key = (restored["context_id"], restored["fact_semantic_digest"])
            groups.setdefault(key, []).append(restored)
            if key in active_by_key:
                conflict("ACTIVE_SEMANTIC_COLLISION", restored, active_by_key[key])
        for group in groups.values():
            if len(group) > 1:
                anchor = min(r["id"] for r in group)
                for r in group:
                    conflict("RESTORATION_SEMANTIC_COLLISION", r, group=anchor)
        effects = []
        final_active = {
            r["id"]
            for r in facts.values()
            if r["lifecycle"] == "ACTIVE" and r["id"] not in graph.delete_ids
        } | {r["id"] for r in restores}
        for relation in relations:
            effect = (
                "REMOVED"
                if relation["id"] in graph.relation_ids
                else (
                    "BECOMES_ACTIVE_CONFLICT"
                    if relation["relation_kind"] == "CONTRADICTS"
                    and relation["source_fact_id"] in final_active
                    and relation["target_fact_id"] in final_active
                    and (
                        relation["source_fact_id"] in graph.restore_ids
                        or relation["target_fact_id"] in graph.restore_ids
                    )
                    else None
                )
            )
            if effect is None:
                continue
            effects.append(
                {
                    "type": "RELATION_EFFECT",
                    **self._management_relation(
                        relation,
                        facts[relation["source_fact_id"]],
                        facts[relation["target_fact_id"]],
                    ),
                    "effect": effect,
                }
            )
        effects.sort(
            key=lambda r: (
                r["relation_id"],
                r["effect"],
                r["subject"]["fact_id"],
                r["companion"]["fact_id"],
            )
        )
        conflicts.sort(
            key=lambda r: (
                r["reason"],
                r["subject"]["context_id"],
                r["group"],
                r["subject"]["fact_id"],
                "" if r["companion"] is None else r["companion"]["fact_id"],
            )
        )
        delete_rows = sorted(
            [facts[i] for i in graph.delete_ids],
            key=lambda r: (r["memory_domain_id"], r["context_id"], r["id"]),
        )
        records = tuple(
            canonical_json_bytes(r)
            for r in with_end(
                confirmation_records(
                    selection=selection,
                    root=root,
                    additional=additional,
                    deletes=map(MemoryManagementFact.from_row, delete_rows),
                    effects=effects,
                    restores=map(MemoryManagementFact.from_row, restores),
                    conflicts=conflicts,
                )
            )
        )
        return FrozenMemoryDeletionPlan(
            _frozen_rows(delete_rows),
            _frozen_rows(removed),
            _frozen_rows(restores),
            tuple(sorted(set(facts) | {r["id"] for r in active})),
            records,
        )

    def execute_memory_deletion(
        self,
        *,
        memory_domain_id,
        selection,
        fact_id,
        additional,
        expected_records,
        deadline_monotonic,
    ):
        # Only concrete memory FK/unique conflicts can mean a concurrently added reference.
        reference_conflicts = {
            "memory_relations_memory_domain_id_source_context_id_source_fkey",
            "memory_relations_memory_domain_id_target_context_id_target_fkey",
            "uq_pulsara_v3_memory_active_semantic",
        }
        while True:
            if monotonic() >= deadline_monotonic:
                raise MemoryManagementError(
                    "MEMORY_DELETION_PLANNING_TIMEOUT",
                    504,
                    "记忆删除超时，尚未删除，请重试",
                )
            try:
                with self._management_connection(
                    deadline_monotonic=deadline_monotonic, execute=True
                ) as c:
                    _remaining(c, deadline_monotonic)
                    plan = self._memory_deletion_plan(
                        c, memory_domain_id, selection, fact_id, additional
                    )
                    c.execute(
                        "SELECT id FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE",
                        (memory_domain_id, list(plan.lock_fact_ids)),
                    ).fetchall()
                    c.execute(
                        "SELECT id FROM pulsara_v3.memory_relations WHERE id=ANY(%s::text[]) ORDER BY id FOR UPDATE",
                        ([dict(r)["id"] for r in plan.relations],),
                    ).fetchall()
                    fresh = self._memory_deletion_plan(
                        c, memory_domain_id, selection, fact_id, additional
                    )
                    if fresh.lock_fact_ids != plan.lock_fact_ids:
                        c.rollback()
                        continue
                    plan = self._memory_deletion_plan(
                        c, memory_domain_id, selection, fact_id, additional
                    )
                    if any(
                        a != b
                        for a, b in zip_longest(plan.confirmation, expected_records())
                    ):
                        raise MemoryManagementError(
                            "MEMORY_DELETION_PLAN_DRIFTED",
                            409,
                            "记忆已发生变化，请重新确认",
                            preview=plan.confirmation,
                        )
                    import json

                    if json.loads(plan.confirmation[0])["disposition"] != "READY":
                        raise MemoryManagementError(
                            "MEMORY_DELETION_NEEDS_RESOLUTION",
                            409,
                            "请选择需要一并删除的旧记忆",
                            preview=plan.confirmation,
                        )
                    _remaining(c, deadline_monotonic)
                    for table, rows in (("memory_relations", plan.relations),):
                        self._memory_exact_delete(c, table, rows)
                    c.execute(
                        "DELETE FROM pulsara_v3.memory_embeddings WHERE memory_domain_id=%s AND fact_id=ANY(%s::text[])",
                        (memory_domain_id, [dict(r)["id"] for r in plan.facts]),
                    )
                    self._memory_exact_delete(c, "memory_facts", plan.facts)
                    operation_at = datetime.now(timezone.utc)
                    for frozen in plan.restore:
                        old = dict(frozen)
                        row = c.execute(
                            "UPDATE pulsara_v3.memory_facts SET lifecycle='ACTIVE',updated_at=%s WHERE id=%s RETURNING *",
                            (operation_at, old["id"]),
                        ).fetchone()
                        if _freeze(row) != _freeze(
                            {**old, "lifecycle": "ACTIVE", "updated_at": operation_at}
                        ):
                            raise RuntimeError(
                                "memory restoration changed unexpected fields"
                            )
                    _remaining(c, deadline_monotonic)
                    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    result = []
                    for raw in plan.confirmation:
                        record = json.loads(raw)
                        if record["type"] == "HEADER":
                            record.pop("disposition")
                            record["result"] = "DELETED"
                        elif record["type"] in {"ADDITIONAL_ROOT", "END"}:
                            continue
                        elif record["type"] == "FACT_RESTORE":
                            record["fact"]["lifecycle"] = "ACTIVE"
                            record["fact"]["updated_at"] = operation_at.isoformat()
                        result.append(record)
                    result = tuple(canonical_json_bytes(r) for r in with_end(result))
                return result
            except (
                DeadlockDetected,
                SerializationFailure,
                LockNotAvailable,
                QueryCanceled,
            ):
                continue
            except (ForeignKeyViolation, UniqueViolation) as exc:
                if exc.diag.constraint_name not in reference_conflicts:
                    raise

    @staticmethod
    def _memory_exact_delete(c, table, rows):
        for frozen in rows:
            old = dict(frozen)
            keys = ("fact_id", "ordinal") if table.endswith("_refs") else ("id",)
            statement = sql.SQL(
                "DELETE FROM pulsara_v3.{} WHERE {} RETURNING *"
            ).format(
                sql.Identifier(table),
                sql.SQL(" AND ").join(
                    sql.SQL("{}=%s").format(sql.Identifier(k)) for k in keys
                ),
            )
            actual = c.execute(statement, tuple(old[k] for k in keys)).fetchall()
            if len(actual) != 1 or _freeze(actual[0]) != frozen:
                raise RuntimeError("memory deletion row differs from its locked plan")
