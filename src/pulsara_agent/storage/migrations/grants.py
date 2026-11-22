"""Admin-owned reconciliation of the clean Kernel runtime grants."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json

from psycopg import Connection, sql

from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint
from pulsara_agent.storage.migrations.manifest import (
    CONVERSATION_KERNEL_RUNTIME_COLUMN_PRIVILEGES,
    POSTGRES_RUNTIME_RELATION_PRIVILEGES,
)


@dataclass(frozen=True, slots=True)
class CleanRuntimeGrantPolicy:
    relation_privileges: dict[str, tuple[str, ...]]
    column_privileges: dict[str, dict[str, tuple[str, ...]]]
    function_privileges: dict[str, tuple[str, ...]]
    policy_fingerprint: str


def build_postgres_runtime_grant_policy() -> CleanRuntimeGrantPolicy:
    payload = json.loads(
        files("pulsara_agent.storage.migrations.resources")
        .joinpath("0000_conversation_kernel_runtime_grants_v1.json")
        .read_text(encoding="utf-8")
    )
    relations = {
        str(name): tuple(str(item) for item in privileges)
        for name, privileges in payload["relation_grants"].items()
    }
    if relations != POSTGRES_RUNTIME_RELATION_PRIVILEGES:
        raise RuntimeError("clean runtime grant artifact drifted")
    columns = {
        str(relation): {str(column): tuple(privileges) for column, privileges in grants.items()}
        for relation, grants in payload["column_grants"].items()
    }
    if columns != CONVERSATION_KERNEL_RUNTIME_COLUMN_PRIVILEGES:
        raise RuntimeError("clean runtime column grant artifact drifted")
    functions = {
        str(name): tuple(str(item) for item in privileges)
        for name, privileges in payload["function_grants"].items()
    }
    return CleanRuntimeGrantPolicy(
        relation_privileges=relations,
        function_privileges=functions,
        column_privileges=columns,
        policy_fingerprint=postgres_schema_fingerprint(
            "pulsara:conversation-kernel-runtime-grants:v1",
            _freeze_json(payload),
        ),
    )


def _freeze_json(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _freeze_json(item) for key, item in value.items()}
    return value


class PostgresRuntimeGrantExecutor:
    def reconcile(
        self, connection: Connection, *, runtime_role: str
    ) -> tuple[str, ...]:
        policy = build_postgres_runtime_grant_policy()
        connection.execute(
            sql.SQL("REVOKE ALL ON SCHEMA pulsara_v3 FROM {}").format(
                sql.Identifier(runtime_role)
            )
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA pulsara_v3 TO {}").format(
                sql.Identifier(runtime_role)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON public.pulsara_schema_migrations FROM {}").format(
                sql.Identifier(runtime_role)
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT SELECT ON public.pulsara_schema_migrations TO {}"
            ).format(sql.Identifier(runtime_role))
        )
        connection.execute(
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(
                sql.Identifier(runtime_role)
            )
        )
        fingerprints: list[str] = []
        for relation, required in policy.relation_privileges.items():
            connection.execute(
                sql.SQL("REVOKE ALL ON TABLE pulsara_v3.{} FROM {}").format(
                    sql.Identifier(relation), sql.Identifier(runtime_role)
                )
            )
            connection.execute(
                sql.SQL("GRANT {} ON TABLE pulsara_v3.{} TO {}").format(
                    sql.SQL(", ").join(sql.SQL(item) for item in required),
                    sql.Identifier(relation),
                    sql.Identifier(runtime_role),
                )
            )
            fingerprints.append(
                postgres_schema_fingerprint(
                    "pulsara:clean-runtime-grant:v1",
                    {"relation": relation, "privileges": required},
                )
            )
        for relation, columns in policy.column_privileges.items():
            all_columns = connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='pulsara_v3' AND table_name=%s ORDER BY ordinal_position",
                (relation,),
            ).fetchall()
            column_names = sql.SQL(", ").join(sql.Identifier(str(row[0])) for row in all_columns)
            connection.execute(
                sql.SQL("REVOKE SELECT ({}), INSERT ({}), UPDATE ({}), REFERENCES ({}) ON TABLE pulsara_v3.{} FROM {}").format(
                    column_names, column_names, column_names, column_names,
                    sql.Identifier(relation), sql.Identifier(runtime_role),
                )
            )
            for column, privileges in columns.items():
                connection.execute(
                    sql.SQL("GRANT {} ({}) ON TABLE pulsara_v3.{} TO {}").format(
                        sql.SQL(", ").join(sql.SQL(item) for item in privileges),
                        sql.Identifier(column), sql.Identifier(relation), sql.Identifier(runtime_role),
                    )
                )
        for function_identity, required in policy.function_privileges.items():
            if required != ("EXECUTE",):
                raise RuntimeError("clean runtime function grant is not closed")
            connection.execute(
                sql.SQL("REVOKE ALL ON FUNCTION pulsara_v3.{} FROM {}").format(
                    sql.SQL(function_identity), sql.Identifier(runtime_role)
                )
            )
            connection.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION pulsara_v3.{} TO {}").format(
                    sql.SQL(function_identity), sql.Identifier(runtime_role)
                )
            )
        return tuple(fingerprints)


__all__ = [
    "CleanRuntimeGrantPolicy",
    "PostgresRuntimeGrantExecutor",
    "build_postgres_runtime_grant_policy",
]
