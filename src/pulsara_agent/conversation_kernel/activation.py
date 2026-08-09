"""Fail-closed Stage 2 runtime-role activation boundary."""

from __future__ import annotations

from time import monotonic

from psycopg.rows import dict_row

from pulsara_agent.storage.migrations.manifest import (
    CONVERSATION_KERNEL_RELATIONS,
    CONVERSATION_KERNEL_RUNTIME_PRIVILEGES,
    build_postgres_schema_manifest,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


_RELATION_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
_LEGACY_PRODUCT_RELATIONS = tuple(
    str(item["relation_name"])
    for item in build_postgres_schema_manifest(12).owned_relations
    if str(item["schema_name"]) == "public"
    and str(item["relation_name"]) != "pulsara_schema_migrations"
)


def require_stage2_runtime_privilege_boundary(
    provider: VerifiedPostgresConnectionProviderProtocol,
    *,
    deadline_monotonic: float,
) -> None:
    """Prove the borrowed runtime role can touch only the v3 product plane."""

    if deadline_monotonic <= monotonic():
        raise TimeoutError("Stage 2 privilege verification deadline expired")
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline_monotonic,
    ) as connection:
        if connection.execute(
            "SELECT pg_catalog.has_schema_privilege(current_user, 'pulsara_v3', 'USAGE') AS allowed"
        ).fetchone()["allowed"] is not True:
            raise RuntimeError("Stage 2 runtime role cannot use pulsara_v3")
        for relation in CONVERSATION_KERNEL_RELATIONS:
            expected = frozenset(
                CONVERSATION_KERNEL_RUNTIME_PRIVILEGES[relation]
            )
            for privilege in _RELATION_PRIVILEGES:
                allowed = connection.execute(
                    "SELECT pg_catalog.has_table_privilege(current_user, %s, %s) AS allowed",
                    (f"pulsara_v3.{relation}", privilege),
                ).fetchone()["allowed"]
                if bool(allowed) != (privilege in expected):
                    raise RuntimeError(
                        "Stage 2 runtime relation privilege boundary is not exact"
                    )
        for relation in _LEGACY_PRODUCT_RELATIONS:
            for privilege in _RELATION_PRIVILEGES:
                allowed = connection.execute(
                    "SELECT pg_catalog.has_table_privilege(current_user, %s, %s) AS allowed",
                    (f"public.{relation}", privilege),
                ).fetchone()["allowed"]
                if allowed is True:
                    raise RuntimeError(
                        "Stage 2 runtime role retains a legacy product privilege"
                    )


__all__ = ["require_stage2_runtime_privilege_boundary"]
