"""Admin-owned reconciliation of the clean Kernel runtime grants."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json

from psycopg import Connection, sql

from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint
from pulsara_agent.storage.migrations.manifest import (
    CONVERSATION_KERNEL_RUNTIME_PRIVILEGES,
)


@dataclass(frozen=True, slots=True)
class CleanRuntimeGrantPolicy:
    relation_privileges: dict[str, tuple[str, ...]]
    policy_fingerprint: str


def build_postgres_runtime_grant_policy(
    _through_version: int = 0,
) -> CleanRuntimeGrantPolicy:
    payload = json.loads(
        files("pulsara_agent.storage.migrations.resources")
        .joinpath("0000_conversation_kernel_runtime_grants_v1.json")
        .read_text(encoding="utf-8")
    )
    relations = {
        str(name): tuple(str(item) for item in privileges)
        for name, privileges in payload["relation_grants"].items()
    }
    if relations != CONVERSATION_KERNEL_RUNTIME_PRIVILEGES:
        raise RuntimeError("clean runtime grant artifact drifted")
    return CleanRuntimeGrantPolicy(
        relation_privileges=relations,
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
        connection.execute(
            sql.SQL(
                "REVOKE ALL ON FUNCTION "
                "pulsara_v3.enforce_conversation_kernel_invariants() FROM {}"
            ).format(sql.Identifier(runtime_role))
        )
        connection.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION "
                "pulsara_v3.enforce_conversation_kernel_invariants() TO {}"
            ).format(sql.Identifier(runtime_role))
        )
        return tuple(fingerprints)


__all__ = [
    "CleanRuntimeGrantPolicy",
    "PostgresRuntimeGrantExecutor",
    "build_postgres_runtime_grant_policy",
]
