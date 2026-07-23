"""Structured PostgreSQL catalog reads without database-local OID semantics."""

from __future__ import annotations

from hashlib import sha256
from typing import Iterable

from psycopg import Connection
from psycopg.rows import dict_row

from pulsara_agent.storage.migrations.contracts import (
    PostgresDeepObservedCatalogFact,
    PostgresFastObservedCatalogFact,
    postgres_schema_fingerprint,
)
from pulsara_agent.storage.migrations.manifest import (
    POSTGRES_LATEST_SCHEMA_MANIFEST,
)


class PostgresCatalogCanonicalizer:
    """Read logical catalog identities; OIDs are used only as query joins."""

    def read_fast(
        self,
        connection: Connection,
        *,
        relation_names: Iterable[str] | None = None,
    ) -> PostgresFastObservedCatalogFact:
        names = tuple(
            sorted(
                relation_names
                if relation_names is not None
                else (
                    str(item["relation_name"])
                    for item in POSTGRES_LATEST_SCHEMA_MANIFEST.owned_relations
                )
            )
        )
        extensions = self._extensions(connection)
        types = self._types(connection)
        relations = tuple(self._relation_execution_shape(connection, name) for name in names)
        functions = self._function_execution_shapes(connection)
        payload = {
            "schema_version": "postgres_fast_observed_catalog.v1",
            "server_version_num": int(connection.info.server_version),
            "extensions": extensions,
            "types": types,
            "relation_execution_shapes": relations,
            "function_execution_shapes": functions,
        }
        return PostgresFastObservedCatalogFact(
            **payload,
            fast_executable_schema_fingerprint=postgres_schema_fingerprint(
                "pulsara:postgres-fast-executable-schema:v1",
                {
                    "extensions": tuple(
                        {
                            "schema_name": item["schema_name"],
                            "extension_name": item["extension_name"],
                        }
                        for item in extensions
                    ),
                    "types": types,
                    "relation_execution_shapes": relations,
                    "function_execution_shapes": functions,
                },
            ),
        )

    def read_deep(
        self,
        connection: Connection,
        *,
        relation_names: Iterable[str] | None = None,
    ) -> PostgresDeepObservedCatalogFact:
        fast = self.read_fast(connection, relation_names=relation_names)
        relations = tuple(
            {
                **shape,
                "indexes": self._indexes(
                    connection, relation_name=str(shape["relation_name"])
                ),
            }
            for shape in fast.relation_execution_shapes
        )
        functions = self._functions(connection)
        payload = {
            "schema_version": "postgres_deep_observed_catalog.v1",
            "fast_observed_catalog_fingerprint": fast.fast_executable_schema_fingerprint,
            "relations": relations,
            "functions": functions,
        }
        deep_fingerprint = postgres_schema_fingerprint(
            "pulsara:postgres-deep-catalog:v1", payload
        )
        observed_fingerprint = postgres_schema_fingerprint(
            "pulsara:postgres-observed-catalog:v1",
            {
                "fast": fast.fast_executable_schema_fingerprint,
                "deep": deep_fingerprint,
            },
        )
        return PostgresDeepObservedCatalogFact(
            **payload,
            deep_catalog_fingerprint=deep_fingerprint,
            observed_catalog_fingerprint=observed_fingerprint,
        )

    @staticmethod
    def _extensions(connection: Connection) -> tuple[dict[str, object], ...]:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT n.nspname AS schema_name,
                       e.extname AS extension_name,
                       e.extversion AS extension_version
                FROM pg_catalog.pg_extension e
                JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
                WHERE e.extname = 'vector'
                ORDER BY n.nspname, e.extname
                """
            )
            return tuple(_canonical_row(row) for row in cursor.fetchall())

    @staticmethod
    def _types(connection: Connection) -> tuple[dict[str, object], ...]:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT n.nspname AS schema_name, t.typname AS type_name
                FROM pg_catalog.pg_type t
                JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = 'public' AND t.typname = 'vector'
                ORDER BY n.nspname, t.typname
                """
            )
            return tuple(_canonical_row(row) for row in cursor.fetchall())

    def _relation_execution_shape(
        self, connection: Connection, relation_name: str
    ) -> dict[str, object]:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT c.relkind
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = %s
                """,
                (relation_name,),
            )
            relation = cursor.fetchone()
            if relation is None:
                return {
                    "schema_name": "public",
                    "relation_name": relation_name,
                    "relation_kind": "missing",
                    "columns": (),
                    "constraints": (),
                    "required_index_states": (),
                }
            cursor.execute(
                """
                SELECT a.attnum AS ordinal,
                       a.attname AS column_name,
                       tn.nspname AS type_schema,
                       t.typname AS type_name,
                       a.atttypmod AS type_modifier,
                       a.attnotnull AS not_null,
                       CASE WHEN a.attcollation = 0 THEN NULL ELSE cn.nspname END AS collation_schema,
                       CASE WHEN a.attcollation = 0 THEN NULL ELSE co.collname END AS collation_name,
                       a.attgenerated AS generated_kind,
                       a.attidentity AS identity_kind,
                       pg_catalog.pg_get_expr(ad.adbin, ad.adrelid, true) AS default_expression
                FROM pg_catalog.pg_attribute a
                JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
                JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
                LEFT JOIN pg_catalog.pg_attrdef ad
                       ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                LEFT JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation
                LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid = co.collnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = %s
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY a.attnum
                """,
                (relation_name,),
            )
            columns = tuple(_canonical_row(row) for row in cursor.fetchall())
            cursor.execute(
                """
                SELECT con.conname AS constraint_name,
                       con.contype AS constraint_kind,
                       con.condeferrable AS deferrable,
                       con.condeferred AS initially_deferred,
                       con.convalidated AS validated,
                       pg_catalog.pg_get_constraintdef(con.oid, true) AS definition
                FROM pg_catalog.pg_constraint con
                JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = %s
                ORDER BY con.conname
                """,
                (relation_name,),
            )
            constraints = tuple(_canonical_row(row) for row in cursor.fetchall())
            cursor.execute(
                """
                SELECT ic.relname AS index_name,
                       i.indisprimary AS primary,
                       i.indisunique AS unique_index,
                       i.indisvalid AS valid,
                       i.indisready AS ready,
                       i.indislive AS live
                FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class c ON c.oid = i.indrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
                WHERE n.nspname = 'public' AND c.relname = %s
                ORDER BY ic.relname
                """,
                (relation_name,),
            )
            indexes = tuple(_canonical_row(row) for row in cursor.fetchall())
        return {
            "schema_name": "public",
            "relation_name": relation_name,
            "relation_kind": str(relation["relkind"]),
            "columns": columns,
            "constraints": constraints,
            "required_index_states": indexes,
        }

    @staticmethod
    def _indexes(
        connection: Connection, *, relation_name: str
    ) -> tuple[dict[str, object], ...]:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT ic.relname AS index_name,
                       am.amname AS access_method,
                       i.indisprimary AS primary,
                       i.indisunique AS unique_index,
                       i.indisvalid AS valid,
                       i.indisready AS ready,
                       i.indislive AS live,
                       pg_catalog.pg_get_indexdef(i.indexrelid, 0, true) AS definition,
                       pg_catalog.pg_get_expr(i.indpred, i.indrelid, true) AS predicate
                FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class c ON c.oid = i.indrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_catalog.pg_am am ON am.oid = ic.relam
                WHERE n.nspname = 'public' AND c.relname = %s
                ORDER BY ic.relname
                """,
                (relation_name,),
            )
            return tuple(_canonical_row(row) for row in cursor.fetchall())

    @staticmethod
    def _function_rows(connection: Connection) -> tuple[dict[str, object], ...]:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT n.nspname AS schema_name,
                       p.proname AS function_name,
                       pg_catalog.pg_get_function_identity_arguments(p.oid) AS identity_arguments,
                       pg_catalog.pg_get_function_result(p.oid) AS return_type,
                       l.lanname AS language,
                       p.provolatile AS volatility,
                       p.proisstrict AS strict,
                       p.prosecdef AS security_definer,
                       p.proleakproof AS leakproof,
                       p.proparallel AS parallel,
                       COALESCE(p.proconfig, ARRAY[]::text[]) AS function_config,
                       p.prosrc AS source
                FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                JOIN pg_catalog.pg_language l ON l.oid = p.prolang
                WHERE n.nspname = 'public'
                  AND p.proname = 'pulsara_jsonb_text_array'
                ORDER BY n.nspname, p.proname,
                         pg_catalog.pg_get_function_identity_arguments(p.oid)
                """
            )
            return tuple(_canonical_row(row) for row in cursor.fetchall())

    def _function_execution_shapes(
        self, connection: Connection
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {key: value for key, value in row.items() if key != "source"}
            for row in self._function_rows(connection)
        )

    def _functions(self, connection: Connection) -> tuple[dict[str, object], ...]:
        result = []
        for observed in self._function_rows(connection):
            row = dict(observed)
            source = str(row.pop("source"))
            row["source_sha256"] = sha256(source.encode("utf-8")).hexdigest()
            result.append(row)
        return tuple(result)


def _canonical_row(row: object) -> dict[str, object]:
    mapping = dict(row)
    return {
        str(key): tuple(value) if isinstance(value, list) else value
        for key, value in mapping.items()
    }


__all__ = ["PostgresCatalogCanonicalizer"]
